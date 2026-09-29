"""Local immutable Python releases, verified activation and database rollback."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from ctypes import wintypes
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from stock_god.cli import process_lock  # noqa: E402
from stock_god.market.tunnel import WindowsJob, _ExtendedLimits, _kernel  # noqa: E402
from stock_god.storage.backup import backup_database, verify_database  # noqa: E402


def windows():
    kernel = _kernel()
    definitions = {
        "OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
        "GetProcessTimes": ([wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4, wintypes.BOOL),
        "QueryFullProcessImageNameW": (
            [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)],
            wintypes.BOOL,
        ),
        "TerminateProcess": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
        "QueryInformationJobObject": (
            [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)],
            wintypes.BOOL,
        ),
        "CreateToolhelp32Snapshot": ([wintypes.DWORD, wintypes.DWORD], wintypes.HANDLE),
        "OpenThread": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
        "ResumeThread": ([wintypes.HANDLE], wintypes.DWORD),
        "CreateFileW": (
            [
                wintypes.LPCWSTR,
                wintypes.DWORD,
                wintypes.DWORD,
                ctypes.c_void_p,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.HANDLE,
            ],
            wintypes.HANDLE,
        ),
    }
    for name, (arguments, result) in definitions.items():
        function = getattr(kernel, name)
        function.argtypes, function.restype = arguments, result
    return kernel


def handle_identity(kernel, handle, pid):
    created, exited, system, user = (wintypes.FILETIME() for _ in range(4))
    if not kernel.GetProcessTimes(
        handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(system), ctypes.byref(user)
    ):
        error = ctypes.get_last_error()
        if kernel.WaitForSingleObject(handle, 1000) == 0:
            return None
        raise ctypes.WinError(error)
    size = wintypes.DWORD(32768)
    buffer = ctypes.create_unicode_buffer(size.value)
    if not kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
        # Windows can unmap the image before signalling the process handle.
        # A short bounded wait distinguishes that teardown from live access denial.
        error = ctypes.get_last_error()
        if kernel.WaitForSingleObject(handle, 1000) == 0:
            return None
        raise ctypes.WinError(error)
    return {
        "pid": int(pid),
        "executable": str(Path(buffer.value).resolve()),
        "createdAt100ns": str((created.dwHighDateTime << 32) | created.dwLowDateTime),
    }


def process_identity(pid):
    kernel = windows()
    handle = kernel.OpenProcess(0x1000 | 0x100000, False, int(pid))
    if not handle:
        if ctypes.get_last_error() in (87, 1168):
            return None
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if kernel.WaitForSingleObject(handle, 0) == 0:
            return None
        return handle_identity(kernel, handle, pid)
    finally:
        kernel.CloseHandle(handle)


def same_process(left, right):
    return bool(
        left
        and right
        and left["pid"] == right["pid"]
        and left["createdAt100ns"] == right["createdAt100ns"]
        and Path(left["executable"]).resolve() == Path(right["executable"]).resolve()
    )


def terminate_process(identity, timeout=10):
    """Use a verified kernel handle, never terminate an unverified/reused PID."""
    kernel = windows()
    handle = kernel.OpenProcess(0x1000 | 0x100000 | 1, False, int(identity["pid"]))
    if not handle:
        if ctypes.get_last_error() in (87, 1168):
            return
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if kernel.WaitForSingleObject(handle, 0) == 0:
            return
        if not same_process(identity, handle_identity(kernel, handle, identity["pid"])):
            return  # The original process ended; a reused PID is not ours.
        if not kernel.TerminateProcess(handle, 1):
            if kernel.WaitForSingleObject(handle, 0) == 0:
                return
            raise ctypes.WinError(ctypes.get_last_error())
        if kernel.WaitForSingleObject(handle, int(timeout * 1000)) != 0:
            raise RuntimeError("owned process did not exit")
    finally:
        kernel.CloseHandle(handle)


class _ProcessEntry(ctypes.Structure):
    _fields_ = [
        ("size", wintypes.DWORD),
        ("usage", wintypes.DWORD),
        ("pid", wintypes.DWORD),
        ("heap", ctypes.c_size_t),
        ("module", wintypes.DWORD),
        ("threads", wintypes.DWORD),
        ("parent", wintypes.DWORD),
        ("priority", wintypes.LONG),
        ("flags", wintypes.DWORD),
        ("name", wintypes.WCHAR * 260),
    ]


def process_table():
    kernel = windows()
    for name in ("Process32FirstW", "Process32NextW"):
        function = getattr(kernel, name)
        function.argtypes, function.restype = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry)], wintypes.BOOL
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        row = _ProcessEntry(size=ctypes.sizeof(_ProcessEntry))
        result = {}
        more = kernel.Process32FirstW(snapshot, ctypes.byref(row))
        while more:
            result[int(row.pid)] = {"parent": int(row.parent), "name": str(row.name)}
            more = kernel.Process32NextW(snapshot, ctypes.byref(row))
        return result
    finally:
        kernel.CloseHandle(snapshot)


def owned_processes(record):
    found = {}
    for saved in record.get("processes", []):
        current = process_identity(saved["pid"])
        if same_process(saved, current):
            found[current["pid"]] = current
    table = process_table()
    changed = True
    while changed:
        changed = False
        for pid, row in table.items():
            if pid in found or row["parent"] not in found:
                continue
            current = process_identity(pid)
            if current and int(current["createdAt100ns"]) >= int(found[row["parent"]]["createdAt100ns"]):
                found[pid] = current
                changed = True
    return list(found.values())


def listener_pid():
    ip = ctypes.WinDLL("iphlpapi", use_last_error=True)
    function = ip.GetExtendedTcpTable
    function.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.BOOL,
        wintypes.ULONG,
        ctypes.c_int,
        wintypes.ULONG,
    ]
    function.restype = wintypes.DWORD
    size = wintypes.DWORD(0)
    function(None, ctypes.byref(size), False, 2, 3, 0)
    buffer = ctypes.create_string_buffer(size.value)
    code = function(buffer, ctypes.byref(size), False, 2, 3, 0)
    if code:
        raise OSError(code, "cannot inspect TCP listener ownership")
    words = ctypes.cast(buffer, ctypes.POINTER(wintypes.DWORD))
    owners = set()
    for index in range(words[0]):
        base = 1 + index * 6
        address = int(words[base + 1]).to_bytes(4, "little")
        if (
            words[base] == 2
            and socket.ntohs(words[base + 2] & 0xFFFF) == 34115
            and address in (b"\x7f\0\0\1", b"\0" * 4)
        ):
            owners.add(int(words[base + 5]))
    if len(owners) > 1:
        raise RuntimeError("multiple processes own the release listen address")
    return next(iter(owners), None)


def resume_child(child):
    class ThreadEntry(ctypes.Structure):
        _fields_ = [
            ("size", wintypes.DWORD),
            ("usage", wintypes.DWORD),
            ("tid", wintypes.DWORD),
            ("owner", wintypes.DWORD),
            ("base", wintypes.LONG),
            ("delta", wintypes.LONG),
            ("flags", wintypes.DWORD),
        ]

    kernel = windows()
    for name in ("Thread32First", "Thread32Next"):
        function = getattr(kernel, name)
        function.argtypes, function.restype = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)], wintypes.BOOL
    snapshot = kernel.CreateToolhelp32Snapshot(4, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        row = ThreadEntry(size=ctypes.sizeof(ThreadEntry))
        more = kernel.Thread32First(snapshot, ctypes.byref(row))
        while more:
            if row.owner == child.pid:
                handle = kernel.OpenThread(2, False, row.tid)
                if handle:
                    try:
                        previous = kernel.ResumeThread(handle)
                        if previous not in (0, 0xFFFFFFFF):
                            return
                    finally:
                        kernel.CloseHandle(handle)
            more = kernel.Thread32Next(snapshot, ctypes.byref(row))
        raise RuntimeError("cannot resume the owned suspended child")
    finally:
        kernel.CloseHandle(snapshot)


class StartupJob(WindowsJob):
    def identities(self):
        kernel = windows()
        for capacity in (32, 256, 2048):
            buffer = ctypes.create_string_buffer(8 + capacity * ctypes.sizeof(ctypes.c_size_t))
            returned = wintypes.DWORD()
            if kernel.QueryInformationJobObject(self.handle, 3, buffer, len(buffer), ctypes.byref(returned)):
                count = ctypes.cast(buffer, ctypes.POINTER(wintypes.DWORD))[1]
                pids = (ctypes.c_size_t * count).from_buffer(buffer, 8)
                return [identity for pid in pids if (identity := process_identity(pid))]
            if ctypes.get_last_error() != 234:
                raise ctypes.WinError(ctypes.get_last_error())
        raise RuntimeError("release process group exceeds supported size")

    def disarm(self):
        limits = _ExtendedLimits()
        if not self.kernel.SetInformationJobObject(
            self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        self.close()


def spawn_owned(args, root, env, label, pointer, role="web"):
    job = StartupJob()
    child = None
    record_path = root / "runtime" / (role + "-process.json")
    logs = root / "runtime/logs"
    logs.mkdir(parents=True, exist_ok=True)
    try:
        with (logs / (label + ".out.log")).open("wb") as out, (logs / (label + ".err.log")).open("wb") as err:
            child = subprocess.Popen(
                args,
                cwd=root,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                creationflags=subprocess.CREATE_NO_WINDOW | 0x00000004,
            )  # CREATE_SUSPENDED
        job.assign(child)
        identity = process_identity(child.pid)
        if identity is None:
            raise RuntimeError("new process disappeared before startup")
        record = {"pointer": pointer, "launcherPID": child.pid, "processes": [identity], "phase": "starting"}
        write(record_path, record)
        resume_child(child)
        return child, job, record
    except BaseException:
        job.close()
        if child is not None:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=10)
        raise


@contextmanager
def database_guard(root, *, allow_missing=False):
    """Deny new writable opens while backing up or replacing both databases."""
    handles = []
    kernel = windows()
    try:
        data = inside(root / "data", root)
        for name in ("stock.db", "minute.db"):
            path = inside(data / name, data)
            if not path.exists() and allow_missing:
                continue
            # Read + share-read/share-delete permits our rename but excludes writers.
            handle = kernel.CreateFileW(str(path), 0x80000000, 1 | 4, None, 3, 0x80, None)
            if handle == ctypes.c_void_p(-1).value:
                raise RuntimeError("database is missing or still has an open writer: " + name)
            handles.append(handle)
        yield
    finally:
        for handle in handles:
            kernel.CloseHandle(handle)


def timestamp():
    return datetime.now(UTC).isoformat()


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as output:
        output.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


def digest(path):
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def inside(path, root):
    path, root = Path(path).resolve(), Path(root).resolve()
    if path == root or not path.is_relative_to(root):
        raise ValueError(f"path is outside intended directory: {path}")
    return path


def run(args, *, cwd=ROOT, env=None, capture=False):
    result = subprocess.run(
        [str(arg) for arg in args],
        cwd=cwd,
        env=env,
        check=True,
        stdout=subprocess.PIPE if capture else None,
        encoding="utf-8",
        errors="replace",
    )
    return result.stdout.strip() if capture else None


def clean_commit(root):
    if run(["git", "status", "--porcelain"], cwd=root, capture=True):
        raise ValueError("release requires a clean checkout")
    return run(["git", "rev-parse", "HEAD"], cwd=root, capture=True)


def tree_files(directory, excluded=()):
    directory = Path(directory).resolve()
    return {
        path.relative_to(directory).as_posix(): digest(path)
        for path in sorted(directory.rglob("*"))
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.suffix != ".pyc"
        and path.relative_to(directory).as_posix() not in excluded
        and inside(path, directory)
    }


def bundle_files(directory):
    return tree_files(directory, ("build-manifest.json",))


def verify_bundle(directory, *, root=ROOT):
    release_root = inside(root / "runtime/releases", root)
    directory = inside(directory, release_root)
    manifest_path = directory / "build-manifest.json"
    manifest = read(manifest_path)
    if manifest.get("dirty") is not False or not manifest.get("files"):
        raise ValueError("candidate is dirty or has no file manifest")
    if directory.relative_to(release_root).parts != (manifest["appVersion"], manifest["commit"]):
        raise ValueError("candidate path differs from its immutable version/commit")
    actual_files = bundle_files(directory)
    for name, expected in manifest["files"].items():
        inside(directory / name, directory)
        if actual_files.get(name) != expected:
            raise ValueError("candidate file hash mismatch: " + name)
    if manifest["files"] != actual_files:
        raise ValueError("candidate contains unrecorded files")
    required = {".venv/Scripts/python.exe", ".venv/pyvenv.cfg", "src/stock_god/release_manifest.json"}
    if not required <= set(actual_files):
        raise ValueError("candidate lacks a sealed Python environment or release manifest")
    toolchain = inside(root / "runtime/toolchain", root)
    interpreter = inside(manifest["interpreterExecutable"], toolchain)
    if digest(interpreter) != manifest["interpreterSHA256"]:
        raise ValueError("release interpreter hash mismatch")
    runtime = inside(manifest["interpreterDirectory"], toolchain)
    if interpreter.parent != runtime or not manifest.get("interpreterFiles"):
        raise ValueError("interpreter directory is not sealed")
    if tree_files(runtime) != manifest["interpreterFiles"]:
        raise ValueError("release interpreter/standard library fingerprint mismatch")
    config = dict(
        line.split("=", 1)
        for line in (directory / ".venv/pyvenv.cfg").read_text(encoding="utf-8").splitlines()
        if "=" in line
    )
    config = {key.strip(): value.strip() for key, value in config.items()}
    if (
        Path(config.get("home", "")).resolve() != runtime
        or config.get("include-system-site-packages", "").lower() != "false"
    ):
        raise ValueError("venv is not bound exclusively to the sealed interpreter")
    packaged = read(directory / "src/stock_god/release_manifest.json")
    if any(
        packaged[key] != manifest[key] for key in ("appVersion", "mainSchemaVersion", "minuteSchemaVersion")
    ):
        raise ValueError("packaged release version/schema differs from artifact manifest")
    return {
        **{
            name: manifest[name]
            for name in ("appVersion", "mainSchemaVersion", "minuteSchemaVersion", "commit")
        },
        "kind": "python",
        "releaseDirectory": str(directory),
        "pythonExecutable": str(directory / ".venv/Scripts/python.exe"),
        "interpreterExecutable": str(interpreter),
        "artifactSHA256": digest(manifest_path),
    }


def release_env(pointer, root=ROOT, *, scheduler=True):
    env = os.environ.copy()
    # Ignore inherited source-run overrides when starting a deployed release.
    for key in list(env):
        if key.startswith(("GO_STOCK_", "STOCK_GOD_", "PYTHON")):
            env.pop(key)
    if pointer.get("kind") == "python":
        directory = Path(pointer["releaseDirectory"])
        env.update(
            STOCK_GOD_ROOT=str(root),
            STOCK_GOD_RELEASE_DIR=str(directory),
            STOCK_GOD_FRONTEND_DIST=str(directory / "frontend/dist"),
            STOCK_GOD_SCHEDULER="1" if scheduler else "0",
            PYTHONPATH=str(directory / "src"),
            PYTHONUTF8="1",
            PYTHONDONTWRITEBYTECODE="1",
            PYTHONNOUSERSITE="1",
            # -B prevents cache writes, but still reads existing pyc. A regular
            # sealed file cannot contain cache paths, so execute the sealed sources.
            PYTHONPYCACHEPREFIX=str(directory / "build-manifest.json"),
        )
    else:
        env.update(
            GO_STOCK_WEB_ADDR="127.0.0.1:34115",
            GO_STOCK_DB_PATH=str(root / "data/stock.db") + "?_pragma=journal_mode(WAL)",
            GO_STOCK_MINUTE_DB_PATH=str(root / "data/minute.db") + "?_pragma=journal_mode(WAL)",
            ZONEINFO=pointer["zoneInfo"],
        )
    return env


def build(root, uv):
    commit = clean_commit(root)
    version = read(root / "src/stock_god/release_manifest.json")
    destination = root / "runtime/releases" / version["appVersion"] / commit
    if destination.exists():
        if (destination / "build-manifest.json").is_file():
            return verify_bundle(destination, root=root)
        failed = Path("H:/Download/stock-god-build-cache/failed") / (
            version["appVersion"] + "-" + commit + "-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        )
        inside(destination, root / "runtime/releases")
        inside(failed, Path("H:/Download/stock-god-build-cache/failed"))
        failed.parent.mkdir(parents=True, exist_ok=True)
        os.replace(destination, failed)
    cache = Path("H:/Download/stock-god-build-cache")
    env = os.environ.copy()
    env.update(
        UV_CACHE_DIR=str(cache / "uv"),
        UV_PYTHON_INSTALL_DIR=str(root / "runtime/toolchain/python"),
        HTTP_PROXY="http://127.0.0.1:7890",
        HTTPS_PROXY="http://127.0.0.1:7890",
        npm_config_cache=str(cache / "npm"),
        npm_config_proxy="http://127.0.0.1:7890",
        npm_config_https_proxy="http://127.0.0.1:7890",
    )
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        env[name] = "http://127.0.0.1:7890"
    env["NO_PROXY"] = env["no_proxy"] = "127.0.0.1,localhost,::1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    python_version = (root / ".python-version").read_text(encoding="utf-8").strip()
    run([uv, "python", "install", python_version], env=env, cwd=root)
    interpreter = Path(
        run(
            [uv, "python", "find", "--managed-python", "--system", python_version],
            env=env,
            cwd=root,
            capture=True,
        )
    )
    inside(interpreter, root / "runtime/toolchain")
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
    if not npm:
        raise RuntimeError("npm is unavailable")
    run([npm, "ci", "--no-audit", "--no-fund"], cwd=root / "frontend", env=env)
    run([npm, "run", "build"], cwd=root / "frontend", env=env)
    destination.mkdir(parents=True)
    # A bundle is assembled at its final path because Windows venvs are not relocatable.
    for name in ("src/stock_god", "api", "frontend/dist"):
        shutil.copytree(
            root / name, destination / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
        )
    for name in ("pyproject.toml", "uv.lock", ".python-version", "LICENSE", "NOTICE"):
        if (root / name).is_file():
            shutil.copy2(root / name, destination / name)
    env["UV_PROJECT_ENVIRONMENT"] = str(destination / ".venv")
    run(
        [
            uv,
            "sync",
            "--frozen",
            "--no-dev",
            "--no-install-project",
            "--python",
            interpreter,
            "--project",
            destination,
        ],
        cwd=destination,
        env=env,
    )
    if clean_commit(root) != commit:
        raise RuntimeError("checkout changed during release build; candidate remains unsealed")
    manifest = {
        **version,
        "commit": commit,
        "buildTime": timestamp(),
        "dirty": False,
        "pythonVersion": python_version,
        "interpreterExecutable": str(interpreter),
        "interpreterSHA256": digest(interpreter),
        "interpreterDirectory": str(interpreter.parent),
        "interpreterFiles": tree_files(interpreter.parent),
        "files": bundle_files(destination),
    }
    write(destination / "build-manifest.json", manifest)
    return verify_bundle(destination, root=root)


def verify_pointer(pointer, root=ROOT):
    if pointer.get("kind") == "python":
        actual = verify_bundle(pointer["releaseDirectory"], root=root)
        for key in (
            "appVersion",
            "commit",
            "artifactSHA256",
            "pythonExecutable",
            "interpreterExecutable",
            "mainSchemaVersion",
            "minuteSchemaVersion",
        ):
            if actual[key] != pointer[key]:
                raise ValueError("release pointer identity mismatch: " + key)
    elif pointer.get("kind") in (None, "go"):
        # Old Go binaries are accepted only as recorded rollback artifacts.
        release_root = inside(root / "runtime/releases", root)
        directory = inside(Path(pointer["binary"]).parent, release_root)
        if directory.relative_to(release_root).parts != (pointer["appVersion"], pointer["commit"]):
            raise ValueError("rollback artifact path differs from its immutable version/commit")
        for name, hash_name in (("binary", "artifactSHA256"), ("zoneInfo", "zoneInfoSHA256")):
            path = inside(pointer[name], directory)
            if path.parent != directory:
                raise ValueError("rollback artifacts must share the immutable release directory")
            if digest(path) != pointer[hash_name]:
                raise ValueError("rollback artifact hash mismatch: " + name)
    else:
        raise ValueError("unknown release pointer kind")
    return pointer


def request(path="/readyz", method="GET"):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(
            urllib.request.Request("http://127.0.0.1:34115" + path, method=method), timeout=3
        ) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 503:
            return json.load(error)
        raise


def listening():
    return listener_pid() is not None


def assert_identity(pointer, state, *, require_ready=True, root=None):
    for key in ("appVersion", "commit", "artifactSHA256", "mainSchemaVersion", "minuteSchemaVersion"):
        if state.get(key) != pointer[key]:
            raise RuntimeError("running service does not match release pointer: " + key)
    if state.get("dirty") is not False:
        raise RuntimeError("running release is dirty or its identity is incomplete")
    if require_ready and not state.get("readiness", {}).get("ready"):
        raise RuntimeError("running release is not ready")
    if pointer.get("kind") == "python":
        if Path(state.get("pythonExecutable", "")).resolve() != Path(pointer["pythonExecutable"]).resolve():
            raise RuntimeError("running Python executable mismatch")
        if Path(state.get("releaseDirectory", "")).resolve() != Path(pointer["releaseDirectory"]).resolve():
            raise RuntimeError("running release directory mismatch")
        if root is not None and Path(state.get("root", "")).resolve() != Path(root).resolve():
            raise RuntimeError("running persistent root mismatch")


START_TIMEOUT = 60
STOP_TIMEOUT = 30


def pointer_key(pointer):
    return tuple(
        pointer.get(key)
        for key in ("commit", "artifactSHA256", "appVersion", "mainSchemaVersion", "minuteSchemaVersion")
    ) + (str(Path(pointer.get("releaseDirectory") or pointer.get("binary", "")).resolve()),)


def process_record(root, role="web"):
    path = root / "runtime" / (role + "-process.json")
    return read(path) if path.exists() else None


def assert_native_identity(pointer, state, owners, root):
    pid = listener_pid()
    actual = process_identity(pid) if pid else None
    if actual is None:
        raise RuntimeError("release listener has no verifiable process")
    allowed = (
        {Path(pointer["pythonExecutable"]).resolve(), Path(pointer["interpreterExecutable"]).resolve()}
        if pointer.get("kind") == "python"
        else {Path(pointer["binary"]).resolve()}
    )
    if Path(actual["executable"]).resolve() not in allowed:
        raise RuntimeError("listener executable differs from the exact release")
    if pointer.get("kind") == "python":
        if state.get("pid") != pid or not any(same_process(actual, owned) for owned in owners):
            raise RuntimeError("Python listener PID/creation time is not owned by this release launch")
    elif owners and not any(same_process(actual, owned) for owned in owners):
        raise RuntimeError("Go listener PID/creation time differs from its recorded launch")
    assert_identity(pointer, state, require_ready=False, root=root)
    return actual


def stop(pointer, root=ROOT):
    record = process_record(root)
    matching = record and pointer_key(record["pointer"]) == pointer_key(pointer)
    owners = owned_processes(record) if matching else []
    pid = listener_pid()
    if pid:
        try:
            state = request()
        except (OSError, ValueError):
            if not any(owner["pid"] == pid for owner in owners):
                raise RuntimeError("unresponsive listener is not an owned release process") from None
        else:
            actual = assert_native_identity(pointer, state, owners, root)
            if not owners:
                # A formal old Go release predates Python process receipts. Its
                # exact artifact path/hash and native creation time authorize adoption.
                record = {
                    "pointer": pointer,
                    "launcherPID": actual["pid"],
                    "processes": [actual],
                    "phase": "stopping",
                }
                write(root / "runtime/web-process.json", record)
                owners = owned_processes(record)
            try:
                request("/api/v1/system/shutdown", "POST")
            except (OSError, ValueError):
                pass  # Native ownership still permits bounded failure cleanup.
    if not owners:
        if pointer.get("kind") != "python":
            expected = Path(pointer["binary"]).resolve()
            for candidate_pid, row in process_table().items():
                if row["name"].lower() == expected.name.lower():
                    actual = process_identity(candidate_pid)
                    if actual and Path(actual["executable"]).resolve() == expected:
                        raise RuntimeError(
                            "unrecorded Go release is still starting; do not replace its databases"
                        )
        return
    record["processes"] = owners
    record["phase"] = "stopping"
    write(root / "runtime/web-process.json", record)
    deadline = time.monotonic() + STOP_TIMEOUT
    while time.monotonic() < deadline:
        active = owned_processes(record)
        if not active:
            break
        time.sleep(0.1)
    for identity in reversed(owned_processes(record)):
        terminate_process(identity)
    if owned_processes(record):
        raise RuntimeError("owned release processes remain alive; database files remain untouched")
    if listener_pid() is not None:
        raise RuntimeError("another listener appeared; database files remain untouched")
    record["phase"] = "stopped"
    write(root / "runtime/web-process.json", record)


def cleanup_spawn(child, job, record, root, role="web"):
    members = list(record["processes"])
    try:
        if job.handle:
            members += job.identities()
        record["processes"] = list({item["pid"]: item for item in members}.values())
    finally:
        job.close()
    for identity in reversed(owned_processes(record)):
        terminate_process(identity)
    child.wait(timeout=10)
    if owned_processes(record):
        raise RuntimeError("failed launch still owns live processes")
    record["phase"] = "stopped"
    write(root / "runtime" / (role + "-process.json"), record)


def assert_writers_stopped(root):
    if listener_pid() is not None:
        raise RuntimeError("Web listener is still running")
    for role in ("web", "maintenance"):
        record = process_record(root, role)
        if record and owned_processes(record):
            raise RuntimeError("owned " + role + " process still has access to databases")


def start(pointer, root=ROOT, *, scheduler=True):
    verify_pointer(pointer, root)
    previous = process_record(root)
    if listening():
        state = request()
        owners = (
            owned_processes(previous)
            if previous and pointer_key(previous["pointer"]) == pointer_key(pointer)
            else []
        )
        assert_native_identity(pointer, state, owners, root)
        assert_identity(pointer, state, root=root)
        if pointer.get("kind") == "python" and previous.get("scheduler") != scheduler:
            raise RuntimeError("running scheduler mode differs; stop before restarting")
        return state
    if previous and owned_processes(previous):
        raise RuntimeError("an owned release is alive without a listener; stop it before starting another")
    log_dir = root / "runtime/logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    label = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    args = (
        [pointer["pythonExecutable"], "-B", "-P", "-m", "stock_god", "serve"]
        if pointer.get("kind") == "python"
        else [pointer["binary"]]
    )
    child, job, record = spawn_owned(
        args, root, release_env(pointer, root, scheduler=scheduler), label, pointer
    )
    record["scheduler"] = scheduler
    try:
        deadline = time.monotonic() + START_TIMEOUT
        while time.monotonic() < deadline:
            if child.poll() is not None:
                raise RuntimeError("release process exited; inspect runtime/logs/" + label + ".err.log")
            try:
                state = request()
            except (OSError, ValueError):
                time.sleep(0.1)
                continue
            members = job.identities()
            assert_native_identity(pointer, state, members, root)
            if not state.get("readiness", {}).get("ready"):
                time.sleep(0.1)
                continue
            record.update(processes=members, ready=state, phase="running")
            write(root / "runtime/web-process.json", record)
            job.disarm()
            return state
        raise RuntimeError("release did not become ready; inspect runtime/logs/" + label + ".err.log")
    except BaseException:
        cleanup_spawn(child, job, record, root)
        raise


def ensure_running(pointer, root=ROOT):
    """Keep a healthy owned release, otherwise restart only processes we can identify."""
    was_listening = listening()
    if was_listening:
        try:
            state = request()
        except (OSError, ValueError):
            pass  # stop() must still prove ownership before touching this listener.
        else:
            record = process_record(root)
            owners = (
                owned_processes(record)
                if record and pointer_key(record["pointer"]) == pointer_key(pointer)
                else []
            )
            assert_native_identity(pointer, state, owners, root)
            assert_identity(pointer, state, require_ready=False, root=root)
            if state.get("readiness", {}).get("ready"):
                return {"action": "healthy", "ready": state}
    stop(pointer, root)
    stop_maintenance(root)
    return {"action": "restarted" if was_listening else "started", "ready": start(pointer, root)}


def validate_proof(path, pointer):
    path = Path(path).resolve()
    proof = read(path)
    for key in ("commit", "artifactSHA256"):
        if proof.get(key) != pointer[key]:
            raise ValueError("acceptance belongs to a different candidate: " + key)
    allowed = {"local-release-gate", "offline-cold", "offline-restart", "live-prediction"}
    # The complete language/data migration has explicit additional acceptance.
    # Routine later releases must not silently require external AI calls.
    required = allowed if pointer["appVersion"] == "6.0.0" else {"local-release-gate"}
    stages = proof.get("stages", {})
    if not required <= set(stages) <= allowed or any(
        stage.get("passed") is not True for stage in stages.values()
    ):
        raise ValueError("candidate acceptance is incomplete")
    for name, stage in stages.items():
        if not stage.get("evidencePath") or not stage.get("evidenceSHA256"):
            raise ValueError("acceptance must identify its validation evidence: " + name)
        evidence = inside(path.parent / stage["evidencePath"], path.parent)
        if not evidence.is_file() or digest(evidence) != stage["evidenceSHA256"]:
            raise ValueError("acceptance evidence hash mismatch: " + name)
    return proof


def database_command(pointer, root, command):
    label = datetime.now().strftime("%Y%m%d-%H%M%S-%f") + "-" + command
    args = [pointer["pythonExecutable"], "-B", "-P", "-m", "stock_god", "db", command]
    child, job, record = spawn_owned(
        args, root, release_env(pointer, root, scheduler=False), label, pointer, "maintenance"
    )
    try:
        code = child.wait(timeout=1200)
        if code:
            raise RuntimeError("database " + command + " failed; inspect runtime/logs/" + label + ".err.log")
        return (root / "runtime/logs" / (label + ".out.log")).read_text(encoding="utf-8-sig").strip()
    finally:
        cleanup_spawn(child, job, record, root, "maintenance")


def backup_version(path):
    connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        exists = connection.execute("SELECT 1 FROM sqlite_master WHERE name='schema_migrations'").fetchone()
        return (
            connection.execute("SELECT COALESCE(MAX(id),0) FROM schema_migrations").fetchone()[0]
            if exists
            else 0
        )
    finally:
        connection.close()


def receipt_write(receipt):
    write(Path(receipt["directory"]) / "receipt.json", receipt)


def clear_pending(root, receipt):
    pending = root / "runtime/deployments/pending.json"
    if pending.exists():
        value = read(pending)
        if Path(value["receipt"]).resolve() == (Path(receipt["directory"]) / "receipt.json").resolve():
            pending.unlink()


def stop_maintenance(root):
    record = process_record(root, "maintenance")
    if record:
        for identity in reversed(owned_processes(record)):
            terminate_process(identity)
        if owned_processes(record):
            raise RuntimeError("maintenance process is still running")
        record["phase"] = "stopped"
        write(root / "runtime/maintenance-process.json", record)


def resume_before(receipt, root):
    """The databases are already old: restart only the verified old artifact."""
    before = verify_pointer(receipt["before"], root)
    current = read(root / "runtime/current.json")
    if pointer_key(current) not in (pointer_key(before), pointer_key(receipt["candidate"])):
        raise RuntimeError("recovery receipt does not match the current deployment")
    stop(before, root)
    assert_writers_stopped(root)
    write(root / "runtime/current.json", before)
    state = start(before, root)
    receipt.update(status="rolled_back", rollbackReady=state, recoveredAt=timestamp())
    receipt_write(receipt)
    clear_pending(root, receipt)
    return receipt


def restore(receipt, root):
    before = verify_pointer(receipt["before"], root)
    directory = inside(receipt["directory"], root / "runtime/deployments")
    if receipt.get("status") == "rolled_back":
        if pointer_key(read(root / "runtime/current.json")) != pointer_key(before):
            raise RuntimeError("this rollback receipt no longer owns the current release")
        return receipt
    if receipt.get("status") == "restored":
        return resume_before(receipt, root)
    current = read(root / "runtime/current.json")
    if pointer_key(current) not in (pointer_key(before), pointer_key(receipt["candidate"])):
        raise RuntimeError("rollback receipt does not match the current deployment")
    restore_dir = directory / ("restore-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
    restore_dir.mkdir()
    for name in ("stock.db", "minute.db"):
        source = inside(directory / "backup" / name, directory)
        expected = receipt["backups"][name]["sha256"]
        if digest(source) != expected:
            raise ValueError("rollback backup changed: " + name)
        staged = restore_dir / name
        shutil.copy2(source, staged)
        if digest(staged) != expected:
            raise ValueError("staged rollback backup hash mismatch: " + name)
        verify_database(staged)
        expected_version = before["mainSchemaVersion" if name == "stock.db" else "minuteSchemaVersion"]
        if backup_version(staged) != expected_version:
            raise ValueError("rollback backup schema differs from the recorded old release: " + name)
    stop(receipt["candidate"], root)
    stop_maintenance(root)
    assert_writers_stopped(root)
    # Preserve failed-upgrade files before putting verified backup copies in place.
    resuming = receipt.get("status") == "restoring"
    receipt.update(status="restoring", restoreDirectory=str(restore_dir))
    receipt_write(receipt)
    with process_lock(root / "runtime/web.lock"), database_guard(root, allow_missing=resuming):
        assert_writers_stopped(root)
        for name in ("stock.db", "minute.db"):
            destination = inside(root / "data" / name, root / "data")
            for suffix in ("", "-wal", "-shm"):
                current_file = inside(str(destination) + suffix, root / "data")
                if current_file.exists():
                    os.replace(current_file, restore_dir / ("failed-" + name + suffix))
            os.replace(restore_dir / name, destination)
    receipt["status"] = "restored"
    receipt_write(receipt)
    write(root / "runtime/current.json", before)
    state = start(before, root)
    receipt.update(status="rolled_back", rolledBackAt=timestamp(), rollbackReady=state)
    receipt_write(receipt)
    clear_pending(root, receipt)
    return receipt


def recover_pending(root):
    path = root / "runtime/deployments/pending.json"
    if not path.exists():
        return None
    pointer = read(path)
    receipt_path = inside(pointer["receipt"], root / "runtime/deployments")
    receipt = read(receipt_path)
    if (Path(receipt["directory"]) / "receipt.json").resolve() != receipt_path:
        raise ValueError("pending receipt directory conflict")
    status = receipt["status"]
    if status in ("deployed", "rolled_back", "aborted"):
        clear_pending(root, receipt)
        return receipt
    # A process can become ready just before its controller dies. Complete the
    # already-active cutover instead of discarding legitimate subsequent data.
    if status in ("activating", "restored"):
        expected = receipt["candidate"] if status == "activating" else receipt["before"]
        verify_pointer(expected, root)
        current = read(root / "runtime/current.json")
        if pointer_key(current) == pointer_key(expected) and listening():
            try:
                state = request()
            except (OSError, ValueError):
                state = None
            if state is not None:
                record = process_record(root)
                owners = (
                    owned_processes(record)
                    if record and pointer_key(record["pointer"]) == pointer_key(expected)
                    else []
                )
                assert_native_identity(expected, state, owners, root)
                if state.get("readiness", {}).get("ready"):
                    receipt.update(
                        status="deployed" if status == "activating" else "rolled_back",
                        recoveredAt=timestamp(),
                        ready=state,
                    )
                    receipt_write(receipt)
                    clear_pending(root, receipt)
                    return receipt
    stop_maintenance(root)
    if status == "restored":
        return resume_before(receipt, root)
    if len(receipt.get("backups", {})) == 2:
        return restore(receipt, root)
    if status not in ("prepared", "stopped"):
        raise RuntimeError("incomplete backups cannot authorize database rollback")
    # Migration is never started until both backup receipts are durably saved.
    return resume_before(receipt, root)


def deploy(directory, proof_path, root):
    candidate = verify_bundle(directory, root=root)
    proof = validate_proof(proof_path, candidate)
    recovered = recover_pending(root)
    if (
        recovered
        and recovered["status"] == "deployed"
        and pointer_key(recovered["candidate"]) == pointer_key(candidate)
    ):
        return recovered
    before = verify_pointer(read(root / "runtime/current.json"), root)
    if pointer_key(before) == pointer_key(candidate):
        return {"status": "deployed", "ready": start(before, root), "reused": True}
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    deployment = root / "runtime/deployments" / stamp
    deployment.mkdir(parents=True)
    receipt = {
        "directory": str(deployment),
        "before": before,
        "candidate": candidate,
        "acceptance": proof,
        "status": "prepared",
        "createdAt": timestamp(),
        "backups": {},
        "formatVersion": 2,
        "acceptanceSHA256": digest(proof_path),
    }
    receipt_path = deployment / "receipt.json"
    write(receipt_path, receipt)
    write(root / "runtime/deployments/pending.json", {"receipt": str(receipt_path)})
    try:
        stop(before, root)
        receipt["status"] = "stopped"
        receipt_write(receipt)
        with process_lock(root / "runtime/web.lock"):
            assert_writers_stopped(root)
            with database_guard(root):
                for name in ("stock.db", "minute.db"):
                    receipt["backups"][name] = backup_database(
                        root / "data" / name, deployment / "backup" / name
                    )
                    expected_version = before[
                        "mainSchemaVersion" if name == "stock.db" else "minuteSchemaVersion"
                    ]
                    if backup_version(deployment / "backup" / name) != expected_version:
                        raise ValueError("source database schema differs from running release: " + name)
                    receipt_write(receipt)
            receipt["status"] = "backed_up"
            receipt_write(receipt)
            receipt["status"] = "migrating"
            receipt_write(receipt)
            receipt["migration"] = json.loads(database_command(candidate, root, "migrate"))
            receipt["verification"] = json.loads(database_command(candidate, root, "verify"))
            assert_writers_stopped(root)
        receipt["status"] = "migrated"
        receipt_write(receipt)
        start(candidate, root, scheduler=False)
        stop(candidate, root)
        receipt["status"] = "activating"
        receipt_write(receipt)
        # Persist activation intent before enabling production scheduling.
        write(root / "runtime/current.json", candidate)
        receipt["ready"] = start(candidate, root, scheduler=True)
        candidate["deployedAt"] = timestamp()
        write(root / "runtime/current.json", candidate)
        receipt.update(status="deployed", completedAt=timestamp())
        receipt_write(receipt)
        clear_pending(root, receipt)
        return receipt
    except BaseException as error:
        receipt["failure"] = str(error)
        receipt_write(receipt)
        if receipt["status"] == "prepared":
            receipt["status"] = "aborted"
            receipt_write(receipt)
            clear_pending(root, receipt)
            raise
        if len(receipt["backups"]) == 2:
            restore(receipt, root)
        else:
            # No migration was started, so neither source database was changed.
            start(before, root)
            receipt.update(status="rolled_back", rolledBackAt=timestamp())
            receipt_write(receipt)
            clear_pending(root, receipt)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    commands = parser.add_subparsers(dest="command", required=True)
    builder = commands.add_parser("build")
    builder.add_argument("--uv", type=Path, required=True)
    inspector = commands.add_parser("inspect")
    inspector.add_argument("--candidate", type=Path, required=True)
    deployer = commands.add_parser("deploy")
    deployer.add_argument("--candidate", type=Path, required=True)
    deployer.add_argument("--proof", type=Path, required=True)
    rollback = commands.add_parser("rollback")
    rollback.add_argument("--receipt", type=Path, required=True)
    commands.add_parser("recover")
    for name in ("start", "stop", "restart", "status", "ensure"):
        commands.add_parser(name)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    with process_lock(root / "runtime/deployments/release.lock"):
        if args.command == "build":
            result = build(root, args.uv.resolve())
        elif args.command == "inspect":
            result = verify_bundle(args.candidate, root=root)
        elif args.command == "deploy":
            result = deploy(args.candidate, args.proof, root)
        elif args.command == "rollback":
            path = inside(args.receipt, root / "runtime/deployments")
            result = restore(read(path), root)
        elif args.command == "recover":
            result = recover_pending(root) or {"status": "no_pending_deployment"}
        else:
            if args.command in {"start", "restart", "ensure"}:
                recover_pending(root)
            pointer = verify_pointer(read(root / "runtime/current.json"), root)
            if args.command in {"stop", "restart"}:
                if args.command == "stop" and (root / "runtime/deployments/pending.json").exists():
                    record = process_record(root)
                    if record and owned_processes(record):
                        pointer = record["pointer"]
                stop(pointer, root)
                stop_maintenance(root)
            if args.command == "ensure":
                result = ensure_running(pointer, root)
            elif args.command in {"start", "restart"}:
                result = start(pointer, root)
            elif args.command == "status" and listening():
                result = request()
                record = process_record(root)
                owners = (
                    owned_processes(record)
                    if record and pointer_key(record["pointer"]) == pointer_key(pointer)
                    else []
                )
                assert_native_identity(pointer, result, owners, root)
            else:
                result = {"running": False}
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
