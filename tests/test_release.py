import importlib.util
import json
import os
import py_compile
import subprocess
import sys
import time
from pathlib import Path

import pytest

from stock_god.storage.db import Database

spec = importlib.util.spec_from_file_location(
    "release_operations", Path(__file__).parents[1] / "scripts/release.py"
)
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


def candidate(root):
    directory = root / "runtime/releases/6.0.0/fixture-commit"
    directory.mkdir(parents=True)
    python = directory / ".venv/Scripts/python.exe"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"fixture python launcher")
    interpreter = root / "runtime/toolchain/python/python.exe"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_bytes(b"fixture interpreter")
    (interpreter.parent / "python313.dll").write_bytes(b"fixture runtime library")
    (directory / ".venv/pyvenv.cfg").write_text(
        "home = " + str(interpreter.parent) + "\ninclude-system-site-packages = false\n", encoding="utf-8"
    )
    (directory / "app.py").write_text("fixture", encoding="utf-8")
    manifest = {
        "appVersion": "6.0.0",
        "mainSchemaVersion": 36,
        "minuteSchemaVersion": 3,
        "commit": "fixture-commit",
        "dirty": False,
        "interpreterExecutable": str(interpreter),
        "interpreterSHA256": release.digest(interpreter),
        "interpreterDirectory": str(interpreter.parent),
        "interpreterFiles": release.tree_files(interpreter.parent),
    }
    release.write(
        directory / "src/stock_god/release_manifest.json",
        {key: manifest[key] for key in ("appVersion", "mainSchemaVersion", "minuteSchemaVersion")},
    )
    manifest["files"] = release.bundle_files(directory)
    release.write(directory / "build-manifest.json", manifest)
    return directory, release.verify_bundle(directory, root=root)


def proof(path, pointer):
    stages = {}
    for name in ("local-release-gate", "offline-cold", "offline-restart", "live-prediction"):
        evidence = path.parent / (name + ".txt")
        evidence.write_text("passed " + pointer["commit"] + " " + pointer["artifactSHA256"], encoding="utf-8")
        stages[name] = {
            "passed": True,
            "evidencePath": evidence.name,
            "evidenceSHA256": release.digest(evidence),
        }
    value = {
        "commit": pointer["commit"],
        "artifactSHA256": pointer["artifactSHA256"],
        "stages": stages,
    }
    release.write(path, value)
    return value


def legacy(root):
    directory = root / "runtime/releases/5.2.5/old-commit"
    directory.mkdir(parents=True)
    (directory / "go-stock-web.exe").write_bytes(b"old rollback fixture")
    (directory / "zoneinfo.zip").write_bytes(b"old time zones")
    pointer = {
        "appVersion": "5.2.5",
        "mainSchemaVersion": 35,
        "minuteSchemaVersion": 3,
        "commit": "old-commit",
        "binary": str(directory / "go-stock-web.exe"),
        "artifactSHA256": release.digest(directory / "go-stock-web.exe"),
        "zoneInfo": str(directory / "zoneinfo.zip"),
        "zoneInfoSHA256": release.digest(directory / "zoneinfo.zip"),
    }
    release.write(root / "runtime/current.json", pointer)
    return pointer


def test_candidate_seals_environment_and_rejects_extra_files(tmp_path):
    directory, pointer = candidate(tmp_path)
    assert release.verify_pointer(pointer, tmp_path) == pointer
    (directory / "extra.py").write_text("unexpected", encoding="utf-8")
    with pytest.raises(ValueError, match="unrecorded"):
        release.verify_bundle(directory, root=tmp_path)
    (directory / "extra.py").unlink()
    (directory / "app.py").write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="hash mismatch"):
        release.verify_bundle(directory, root=tmp_path)
    with pytest.raises(ValueError, match="outside"):
        release.inside(tmp_path / "../outside", tmp_path)


def test_acceptance_is_bound_to_final_candidate_and_all_four_stages(tmp_path):
    _, pointer = candidate(tmp_path)
    path = tmp_path / "proof.json"
    value = proof(path, pointer)
    assert release.validate_proof(path, pointer) == value
    value["commit"] = "old"
    release.write(path, value)
    with pytest.raises(ValueError, match="different candidate"):
        release.validate_proof(path, pointer)
    value = proof(path, pointer)
    value["stages"]["offline-restart"]["passed"] = False
    release.write(path, value)
    with pytest.raises(ValueError, match="incomplete"):
        release.validate_proof(path, pointer)


def test_later_releases_do_not_require_unrequested_live_provider_calls(tmp_path):
    _, pointer = candidate(tmp_path)
    path = tmp_path / "proof.json"
    value = proof(path, pointer)
    value["stages"] = {"local-release-gate": value["stages"]["local-release-gate"]}
    release.write(path, value)
    with pytest.raises(ValueError, match="incomplete"):
        release.validate_proof(path, pointer)
    later = {**pointer, "appVersion": "6.0.1"}
    assert release.validate_proof(path, later) == value


def test_failed_upgrade_restores_both_databases_and_old_pointer(tmp_path, monkeypatch):
    directory, pointer = candidate(tmp_path)
    before = legacy(tmp_path)
    proof_path = tmp_path / "proof.json"
    proof(proof_path, pointer)
    for name in ("stock.db", "minute.db"):
        with Database(tmp_path / "data" / name).transaction() as db:
            db.execute("CREATE TABLE records(value TEXT)")
            db.execute("INSERT INTO records VALUES('preserve original')")
            db.execute("CREATE TABLE schema_migrations(id INTEGER PRIMARY KEY)")
            db.execute("INSERT INTO schema_migrations VALUES(?)", (35 if name == "stock.db" else 3,))
    stops, starts = [], []
    monkeypatch.setattr(release, "stop", lambda item, *args: stops.append(item["commit"]))
    monkeypatch.setattr(
        release, "start", lambda item, root, **kw: starts.append(item["commit"]) or {"ok": True}
    )
    monkeypatch.setattr(release, "listening", lambda: False)
    monkeypatch.setattr(release, "listener_pid", lambda: None)

    def broken_migration(*args):
        with Database(tmp_path / "data/stock.db").transaction() as db:
            db.execute("UPDATE records SET value='partial upgrade'")
        raise RuntimeError("fixture migration failure")

    monkeypatch.setattr(release, "database_command", broken_migration)
    with pytest.raises(RuntimeError, match="fixture migration failure"):
        release.deploy(directory, proof_path, tmp_path)
    assert stops == [before["commit"], pointer["commit"]] and starts == [before["commit"]]
    assert release.read(tmp_path / "runtime/current.json") == before
    for name in ("stock.db", "minute.db"):
        with Database(tmp_path / "data" / name, read_only=True).connection() as db:
            assert db.execute("SELECT value FROM records").fetchone()[0] == "preserve original"
    receipt = release.read(next((tmp_path / "runtime/deployments").glob("*/receipt.json")))
    assert receipt["status"] == "rolled_back"
    assert len(receipt["backups"]) == 2
    assert list(Path(receipt["directory"]).glob("restore-*/failed-stock.db"))


def test_running_identity_rejects_stale_python_and_wrong_commit(tmp_path):
    _, pointer = candidate(tmp_path)
    state = {**pointer, "dirty": False, "readiness": {"ready": True}}
    release.assert_identity(pointer, state)
    state["pythonExecutable"] = str(tmp_path / "other-python.exe")
    with pytest.raises(RuntimeError, match="Python executable mismatch"):
        release.assert_identity(pointer, state)
    state.update(pointer)
    state["commit"] = "stale"
    with pytest.raises(RuntimeError, match="commit"):
        release.assert_identity(pointer, state)


def test_runtime_dll_is_sealed_but_regenerated_cache_does_not_change_identity(tmp_path):
    directory, pointer = candidate(tmp_path)
    dll = Path(pointer["interpreterExecutable"]).parent / "python313.dll"
    dll.write_bytes(b"changed runtime")
    with pytest.raises(ValueError, match="standard library fingerprint"):
        release.verify_bundle(directory, root=tmp_path)
    dll.write_bytes(b"fixture runtime library")
    cache = directory / "src/stock_god/__pycache__/app.cpython-313.pyc"
    cache.parent.mkdir()
    cache.write_bytes(b"unexpected executable bytecode")
    runtime_cache = dll.parent / "__pycache__/fixture.cpython-313.pyc"
    runtime_cache.parent.mkdir()
    runtime_cache.write_bytes(b"regenerated runtime cache")
    assert release.verify_bundle(directory, root=tmp_path) == pointer


def test_release_environment_ignores_regenerated_stale_python_cache(tmp_path):
    directory, pointer = candidate(tmp_path)
    package = directory / "src/cache_fixture.py"
    package.write_text("VALUE = 'old'\n", encoding="utf-8")
    source_time = package.stat().st_mtime_ns
    py_compile.compile(str(package), doraise=True)
    package.write_text("VALUE = 'new'\n", encoding="utf-8")
    os.utime(package, ns=(source_time, source_time))
    env = release.release_env(pointer, tmp_path)
    result = subprocess.run(
        [sys.executable, "-B", "-P", "-c", "import cache_fixture; print(cache_fixture.VALUE)"],
        env=env,
        check=True,
        capture_output=True,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert result.stdout.strip() == "new"


def test_proof_checks_actual_evidence_and_rejects_path_escape(tmp_path):
    _, pointer = candidate(tmp_path)
    path = tmp_path / "proof.json"
    value = proof(path, pointer)
    (tmp_path / "offline-cold.txt").write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="evidence hash"):
        release.validate_proof(path, pointer)
    value = proof(path, pointer)
    value["stages"]["offline-cold"]["evidencePath"] = "../outside.txt"
    release.write(path, value)
    with pytest.raises(ValueError, match="outside"):
        release.validate_proof(path, pointer)


class ProcessWorld:
    def __init__(self, root, pointer, monkeypatch, *, ready=False, bound=True):
        self.root, self.pointer, self.ready, self.bound = root, pointer, ready, bound
        self.started = False
        self.live = {100: False, 101: False}
        self.birth = {100: "10000", 101: "10100"}
        self.terminated = []
        self.job = None
        monkeypatch.setattr(release, "listener_pid", self.listener)
        monkeypatch.setattr(release, "process_identity", self.identity)
        monkeypatch.setattr(
            release,
            "process_table",
            lambda: {100: {"parent": 0, "name": "python.exe"}, 101: {"parent": 100, "name": "python.exe"}},
        )
        monkeypatch.setattr(release, "terminate_process", self.terminate)
        monkeypatch.setattr(release, "request", self.request)
        monkeypatch.setattr(release, "spawn_owned", self.spawn)
        monkeypatch.setattr(release, "START_TIMEOUT", 0.02)
        monkeypatch.setattr(release, "STOP_TIMEOUT", 0.02)

    def listener(self):
        return 101 if self.started and self.bound and self.live[101] else None

    def identity(self, pid):
        if not self.live.get(pid):
            return None
        return {
            "pid": pid,
            "createdAt100ns": self.birth[pid],
            "executable": self.pointer["pythonExecutable"]
            if pid == 100
            else self.pointer["interpreterExecutable"],
        }

    def terminate(self, identity):
        if release.same_process(identity, self.identity(identity["pid"])):
            self.terminated.append(identity["pid"])
            self.live[identity["pid"]] = False

    def state(self):
        return {
            **self.pointer,
            "pid": 101,
            "dirty": False,
            "root": str(self.root),
            "readiness": {"ready": self.ready},
        }

    def request(self, path="/readyz", method="GET"):
        if not self.listener():
            raise OSError("not listening")
        if path.endswith("shutdown"):
            self.bound = False  # Listener closes before the database writer exits.
            return {"ok": True}
        return self.state()

    def spawn(self, args, root, env, label, pointer, role="web"):
        world = self
        self.started = True
        self.live = {100: True, 101: True}

        class Child:
            pid = 100

            def poll(self):
                return None if world.live[100] else 0

            def wait(self, timeout):
                assert not any(world.live.values())
                return 0

        class Job:
            handle = 1

            def identities(self):
                return [world.identity(pid) for pid in world.live if world.live[pid]]

            def close(self):
                world.live = {100: False, 101: False}
                self.handle = None

            def disarm(self):
                self.handle = None

        self.job = Job()
        record = {
            "pointer": pointer,
            "launcherPID": 100,
            "processes": self.job.identities(),
            "phase": "starting",
        }
        release.write(root / "runtime" / (role + "-process.json"), record)
        return Child(), self.job, record


@pytest.mark.parametrize("bound", [False, True])
def test_failed_start_kills_the_owned_launcher_and_worker_even_without_ready(tmp_path, monkeypatch, bound):
    _, pointer = candidate(tmp_path)
    world = ProcessWorld(tmp_path, pointer, monkeypatch, ready=False, bound=bound)
    with pytest.raises(RuntimeError, match="did not become ready"):
        release.start(pointer, tmp_path)
    assert not any(world.live.values())
    assert release.read(tmp_path / "runtime/web-process.json")["phase"] == "stopped"


def test_stop_accepts_503_identity_and_waits_for_non_listening_database_writer(tmp_path, monkeypatch):
    _, pointer = candidate(tmp_path)
    world = ProcessWorld(tmp_path, pointer, monkeypatch, ready=True)
    release.start(pointer, tmp_path)
    record = release.read(tmp_path / "runtime/web-process.json")
    assert [item["pid"] for item in record["processes"]] == [100, 101]
    world.ready = False
    release.stop(pointer, tmp_path)
    assert set(world.terminated) == {100, 101}
    assert not any(world.live.values())


def test_ensure_keeps_a_healthy_release_and_starts_a_stopped_one(tmp_path, monkeypatch):
    _, pointer = candidate(tmp_path)
    world = ProcessWorld(tmp_path, pointer, monkeypatch, ready=True)
    started = release.ensure_running(pointer, tmp_path)
    assert started["action"] == "started" and started["ready"]["pid"] == 101
    assert release.ensure_running(pointer, tmp_path)["action"] == "healthy"
    assert not world.terminated and all(world.live.values())


def test_ensure_restarts_an_owned_release_that_is_not_ready(tmp_path, monkeypatch):
    _, pointer = candidate(tmp_path)
    world = ProcessWorld(tmp_path, pointer, monkeypatch, ready=True)
    release.start(pointer, tmp_path)
    world.ready = False
    original_spawn = world.spawn

    def recovered_spawn(*args, **kwargs):
        world.ready = True
        world.bound = True
        return original_spawn(*args, **kwargs)

    monkeypatch.setattr(release, "spawn_owned", recovered_spawn)
    result = release.ensure_running(pointer, tmp_path)
    assert result["ready"]["readiness"]["ready"]
    assert set(world.terminated) == {100, 101}


def test_ensure_never_kills_an_unknown_listener(tmp_path, monkeypatch):
    _, pointer = candidate(tmp_path)
    world = ProcessWorld(tmp_path, pointer, monkeypatch, ready=True)
    monkeypatch.setattr(release, "listener_pid", lambda: 999)
    with pytest.raises(RuntimeError, match="not an owned release process"):
        release.ensure_running(pointer, tmp_path)
    assert not world.terminated and not world.started


def test_reused_pid_is_not_an_owned_python_listener(tmp_path, monkeypatch):
    _, pointer = candidate(tmp_path)
    world = ProcessWorld(tmp_path, pointer, monkeypatch, ready=True)
    release.start(pointer, tmp_path)
    world.birth = {100: "20000", 101: "20100"}
    with pytest.raises(RuntimeError, match="creation time"):
        release.stop(pointer, tmp_path)
    assert not world.terminated and all(world.live.values())


@pytest.mark.skipif(os.name != "nt", reason="Windows process ownership fixture")
def test_real_suspended_venv_launch_is_owned_before_it_can_run(tmp_path):
    marker = tmp_path / "child-pid.txt"
    pointer = {"commit": "fixture", "artifactSHA256": "fixture", "kind": "python"}
    code = (
        "import os,time,pathlib; pathlib.Path("
        + repr(str(marker))
        + ").write_text(str(os.getpid())); time.sleep(20)"
    )
    child, job, record = release.spawn_owned(
        [sys.executable, "-B", "-c", code], tmp_path, os.environ.copy(), "owned-fixture", pointer
    )
    try:
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        pid = int(marker.read_text())
        identities = job.identities()
        assert any(item["pid"] == pid for item in identities)
        assert any(item["pid"] == child.pid for item in identities)
        assert all(item["createdAt100ns"].isdigit() for item in identities)
    finally:
        release.cleanup_spawn(child, job, record, tmp_path)
    assert child.poll() is not None


@pytest.mark.skipif(os.name != "nt", reason="Windows process ownership fixture")
@pytest.mark.parametrize("exited", [False, True])
def test_identity_query_handles_exit_race_without_ignoring_live_access_denial(exited):
    import ctypes

    class Kernel:
        def GetProcessTimes(self, *args):
            return True

        def QueryFullProcessImageNameW(self, *args):
            ctypes.set_last_error(5)
            return False

        def WaitForSingleObject(self, *args):
            return 0 if exited else 258

    if exited:
        assert release.handle_identity(Kernel(), 1, 123) is None
    else:
        with pytest.raises(PermissionError):
            release.handle_identity(Kernel(), 1, 123)


def fixture_databases(root):
    for name, version in (("stock.db", 35), ("minute.db", 3)):
        with Database(root / "data" / name).transaction() as db:
            db.execute("CREATE TABLE records(value TEXT)")
            db.execute("INSERT INTO records VALUES('original')")
            db.execute("CREATE TABLE schema_migrations(id INTEGER PRIMARY KEY)")
            db.execute("INSERT INTO schema_migrations VALUES(?)", (version,))


def fixture_migrate(pointer, root, command):
    if command == "migrate":
        for name in ("stock.db", "minute.db"):
            with Database(root / "data" / name).transaction() as db:
                db.execute("UPDATE records SET value='upgraded'")
                if name == "stock.db":
                    db.execute("INSERT INTO schema_migrations VALUES(36)")
    return json.dumps({command: "ok"})


def database_values(root):
    values = []
    for name in ("stock.db", "minute.db"):
        with Database(root / "data" / name, read_only=True).connection() as db:
            values.append(db.execute("SELECT value FROM records").fetchone()[0])
    return values


def deployment_fixture(root, monkeypatch):
    directory, pointer = candidate(root)
    before = legacy(root)
    fixture_databases(root)
    path = root / "proof.json"
    proof(path, pointer)
    events = []
    monkeypatch.setattr(release, "listener_pid", lambda: None)
    monkeypatch.setattr(release, "stop", lambda item, *args: events.append(("stop", item["commit"])))
    monkeypatch.setattr(
        release,
        "start",
        lambda item, root, **kw: (
            events.append(("start", item["commit"], kw.get("scheduler", True))) or {"ok": True}
        ),
    )
    monkeypatch.setattr(release, "database_command", fixture_migrate)
    return directory, pointer, before, path, events


def pending_receipt(root, receipt):
    release.receipt_write(receipt)
    release.write(
        root / "runtime/deployments/pending.json",
        {"receipt": str(Path(receipt["directory"]) / "receipt.json")},
    )


def test_successful_deploy_migrates_once_and_starts_cold_before_hot(tmp_path, monkeypatch):
    directory, pointer, before, path, events = deployment_fixture(tmp_path, monkeypatch)
    receipt = release.deploy(directory, path, tmp_path)
    assert receipt["status"] == "deployed"
    assert release.read(tmp_path / "runtime/current.json")["commit"] == pointer["commit"]
    assert database_values(tmp_path) == ["upgraded", "upgraded"]
    assert events == [
        ("stop", before["commit"]),
        ("start", pointer["commit"], False),
        ("stop", pointer["commit"]),
        ("start", pointer["commit"], True),
    ]
    assert not (tmp_path / "runtime/deployments/pending.json").exists()
    assert len(receipt["backups"]) == 2


@pytest.mark.parametrize("failure_during_hot", [False, True])
@pytest.mark.parametrize("bound", [False, True])
def test_unready_candidate_cannot_block_dual_database_rollback(
    tmp_path, monkeypatch, failure_during_hot, bound
):
    directory, pointer = candidate(tmp_path)
    before = legacy(tmp_path)
    fixture_databases(tmp_path)
    path = tmp_path / "proof.json"
    proof(path, pointer)
    world = ProcessWorld(tmp_path, pointer, monkeypatch)
    original_start, original_stop = release.start, release.stop
    old_restarted = []

    def start(item, root, *, scheduler=True):
        if item["commit"] == before["commit"]:
            assert not any(world.live.values())
            assert database_values(root) == ["original", "original"]
            old_restarted.append(item["binary"])
            return {"ok": True}
        world.ready = failure_during_hot and not scheduler
        world.bound = bound or world.ready
        return original_start(item, root, scheduler=scheduler)

    monkeypatch.setattr(release, "start", start)
    monkeypatch.setattr(
        release,
        "stop",
        lambda item, root: None if item["commit"] == before["commit"] else original_stop(item, root),
    )
    monkeypatch.setattr(release, "database_command", fixture_migrate)
    with pytest.raises(RuntimeError, match="did not become ready"):
        release.deploy(directory, path, tmp_path)
    assert old_restarted == [before["binary"]]
    assert release.read(tmp_path / "runtime/current.json") == before
    assert not (tmp_path / "runtime/deployments/pending.json").exists()


@pytest.mark.parametrize("name", ["stock.db", "minute.db"])
def test_database_guard_rejects_either_open_writer_and_releases_all_handles(tmp_path, name):
    fixture_databases(tmp_path)
    with Database(tmp_path / "data" / name).connection():
        with pytest.raises(RuntimeError, match="open writer"):
            with release.database_guard(tmp_path):
                pytest.fail("a database writer was missed")
    with release.database_guard(tmp_path):
        # A new writable open is also prohibited until BOTH backup reads finish.
        for database_name in ("stock.db", "minute.db"):
            with pytest.raises(OSError):
                with (tmp_path / "data" / database_name).open("r+b"):
                    pass
    with Database(tmp_path / "data/stock.db").transaction() as db:
        db.execute("UPDATE records SET value='guard released'")
    assert database_values(tmp_path) == ["guard released", "original"]


@pytest.mark.parametrize("name", ["stock.db", "minute.db"])
def test_restore_checks_both_backup_hashes_before_stopping_or_replacing(tmp_path, monkeypatch, name):
    directory, _, _, path, events = deployment_fixture(tmp_path, monkeypatch)
    receipt = release.deploy(directory, path, tmp_path)
    events.clear()
    (Path(receipt["directory"]) / "backup" / name).write_bytes(b"tampered")
    with pytest.raises(ValueError, match="backup changed"):
        release.restore(receipt, tmp_path)
    assert events == []
    assert database_values(tmp_path) == ["upgraded", "upgraded"]


def test_crash_between_database_replacements_resumes_verified_restore(tmp_path, monkeypatch):
    directory, _, before, path, _ = deployment_fixture(tmp_path, monkeypatch)
    receipt = release.deploy(directory, path, tmp_path)
    # Simulate power loss after preserving the first failed database, before its replacement.
    os.replace(tmp_path / "data/stock.db", Path(receipt["directory"]) / "first-failed-stock.db")
    receipt["status"] = "restoring"
    pending_receipt(tmp_path, receipt)
    recovered = release.recover_pending(tmp_path)
    assert recovered["status"] == "rolled_back"
    assert release.read(tmp_path / "runtime/current.json") == before
    assert database_values(tmp_path) == ["original", "original"]


def test_crash_after_restore_does_not_restore_over_new_old_version_data(tmp_path, monkeypatch):
    directory, _, before, path, events = deployment_fixture(tmp_path, monkeypatch)
    receipt = release.deploy(directory, path, tmp_path)
    release.restore(receipt, tmp_path)
    with Database(tmp_path / "data/stock.db").transaction() as db:
        db.execute("UPDATE records SET value='new data after restore'")
    receipt["status"] = "restored"
    pending_receipt(tmp_path, receipt)
    events.clear()
    recovered = release.recover_pending(tmp_path)
    assert recovered["status"] == "rolled_back"
    assert events == [("stop", before["commit"]), ("start", before["commit"], True)]
    assert database_values(tmp_path) == ["new data after restore", "original"]


def test_crash_after_healthy_activation_finalizes_without_stopping_service(tmp_path, monkeypatch):
    _, pointer = candidate(tmp_path)
    before = legacy(tmp_path)
    world = ProcessWorld(tmp_path, pointer, monkeypatch, ready=True)
    release.start(pointer, tmp_path)
    release.write(tmp_path / "runtime/current.json", pointer)
    receipt = {
        "directory": str(tmp_path / "runtime/deployments/fixture"),
        "before": before,
        "candidate": pointer,
        "status": "activating",
        "backups": {},
    }
    pending_receipt(tmp_path, receipt)
    assert release.recover_pending(tmp_path)["status"] == "deployed"
    assert all(world.live.values()) and not world.terminated
    assert not (tmp_path / "runtime/deployments/pending.json").exists()


def test_crash_before_backups_restarts_only_old_release(tmp_path, monkeypatch):
    _, pointer, before, _, events = deployment_fixture(tmp_path, monkeypatch)
    receipt = {
        "directory": str(tmp_path / "runtime/deployments/fixture"),
        "before": before,
        "candidate": pointer,
        "status": "prepared",
        "backups": {},
    }
    pending_receipt(tmp_path, receipt)
    assert release.recover_pending(tmp_path)["status"] == "rolled_back"
    assert events == [("stop", before["commit"]), ("start", before["commit"], True)]
    assert database_values(tmp_path) == ["original", "original"]


def test_old_go_rollback_requires_exact_immutable_artifacts(tmp_path):
    pointer = legacy(tmp_path)
    assert release.verify_pointer(pointer, tmp_path) == pointer
    path = Path(pointer["binary"])
    path.write_bytes(b"different binary")
    with pytest.raises(ValueError, match="hash mismatch"):
        release.verify_pointer(pointer, tmp_path)
    pointer["artifactSHA256"] = release.digest(path)
    pointer["commit"] = "different-commit"
    with pytest.raises(ValueError, match="immutable"):
        release.verify_pointer(pointer, tmp_path)


def test_failed_native_enumeration_still_closes_kill_on_close_job(tmp_path, monkeypatch):
    _, pointer = candidate(tmp_path)
    world = ProcessWorld(tmp_path, pointer, monkeypatch)
    child, job, record = world.spawn([], tmp_path, {}, "fixture", pointer)
    monkeypatch.setattr(job, "identities", lambda: (_ for _ in ()).throw(OSError("fixture kernel failure")))
    with pytest.raises(OSError, match="kernel failure"):
        release.cleanup_spawn(child, job, record, tmp_path)
    assert not any(world.live.values())
