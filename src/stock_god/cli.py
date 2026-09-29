"""Local process and database commands. Release commands live in scripts/."""

import argparse
import asyncio
import hashlib
import json
import logging
import os
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import uvicorn

from . import commands as extra_commands
from . import release_manifest
from .audit import redact_text
from .config import AppConfig
from .storage.backup import backup_database, verify_database
from .storage.migrations import migrate, status


@contextmanager
def process_lock(path: Path):
    """OS releases this lock on process death; a stale PID file cannot own it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if path.stat().st_size == 0:
            handle.write(b" ")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise RuntimeError("another Stock God process owns this runtime") from error
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def serve(config):
    from .app import create_app

    server = None

    def shutdown():
        if server is not None:
            server.should_exit = True

    with process_lock(config.root / "runtime" / "web.lock"):
        app = create_app(config, shutdown=shutdown)
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host=config.host,
                port=config.port,
                workers=1,
                proxy_headers=False,
                server_header=False,
                timeout_keep_alive=120,
                timeout_graceful_shutdown=10,
                ws_max_size=1 << 20,
                ws="websockets-sansio",
                access_log=False,
            )
        )
        server.run()


async def prediction_command(config, operation, dry_run=True):
    from .ai.client import AIClient
    from .audit import AuditStore
    from .market import MarketServices
    from .prediction import PredictionService
    from .settings import SettingsStore
    from .storage.db import Database

    database = Database(config.main_db)
    market = MarketServices(config)
    service = PredictionService(database, market, SettingsStore(database), AIClient, AuditStore(database))
    try:
        if operation == "backfill-performance":
            return await service.backfill_performance()
        return await service.replay_allocations(dry_run=dry_run)
    finally:
        await service.close()
        market.close()
        database.close()


def start_slot_command(config, plan_path, plan_sha256, apply):
    from .audit import AuditStore
    from .market.replay_inputs import OfflineReplayMarket, hydrate_private_minutes, verified_replay_evidence
    from .prediction.replay import AllocationReplay
    from .prediction.repository import Repository
    from .prediction.slot_correction import StartSlotCorrection
    from .settings import SettingsStore
    from .storage.db import Database

    plan, files = verified_replay_evidence(plan_path, plan_sha256)
    if plan["version"] != release_manifest()["appVersion"]:
        raise ValueError("correction plan version differs from the candidate")
    database = Database(config.main_db)
    repository = Repository(database)
    correction = StartSlotCorrection(repository, AuditStore(database))
    try:
        draft = correction.plan()
        if draft["planHash"] != plan["correctionPlanHash"]:
            raise ValueError("production correction plan differs from the verified copies")
        if not apply:
            return {"planHash": draft["planHash"], "dryRun": True}
        hydrate_private_minutes(config.minute_db, files["minuteDbSHA256"])
        corrected = correction.apply(draft, expected_hash=plan["correctionPlanHash"])
        market = OfflineReplayMarket(
            config, SettingsStore(database).load().config,
            files["calendarSHA256"], files["dailyManifestSHA256"],
        )
        try:
            replay = AllocationReplay(repository, market)
            expected = json.loads(files["financialResultSHA256"].read_text(encoding="utf-8"))
            preview = replay.run(True)
            if (preview["planHash"] != plan["replayPlanHash"] or
                preview["missingSellCount"] or
                preview["missingBuyCount"] != expected["missingBuyCount"] or
                any(gap["phase"] != "buy" or not any(
                    word in gap["reason"] for word in ("涨停", "跌停")
                ) for gap in preview["missing"])):
                raise ValueError("financial replay differs or lacks necessary market evidence")
            result = replay.run(False)
            if (result["planHash"] != expected["planHash"] or
                result["accountCash"] != expected["accountCash"] or
                result["buyCount"] != expected["buyCount"] or
                result["sellCount"] != expected["sellCount"]):
                raise ValueError("applied financial result differs from offline copies")
            for table, known in plan["financialFingerprints"].items():
                rows = [
                    {key: value for key, value in row.items() if key not in
                     ("id", "created_at", "updated_at")}
                    for row in repository.rows(table.removeprefix("research2_"))
                ]
                digest = hashlib.sha256(json.dumps(
                    rows, sort_keys=True, ensure_ascii=False, default=str,
                    separators=(",", ":"),
                ).encode()).hexdigest()
                if len(rows) != known["count"] or digest != known["sha256"]:
                    raise ValueError("applied financial table differs: " + table)
            return {"correction": corrected, "replayPlanHash": result["planHash"],
                    "tradeRows": result["buyCount"] + result["sellCount"], "verified": True}
        finally:
            market.close()
    finally:
        database.close()


def main(argv=None):
    parser = argparse.ArgumentParser(prog="stock-god")
    parser.add_argument("--root", type=Path, help="Persistent project root (data/runtime stay inside)")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--db-path", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    extra_commands.add_arguments(commands)
    run = commands.add_parser("serve", help="Run the local Web service and scheduler")
    run.add_argument("--host")
    run.add_argument("--port", type=int)
    run.add_argument("--no-scheduler", action="store_true")
    db = commands.add_parser("db", help="Explicit SQLite maintenance")
    db_commands = db.add_subparsers(dest="operation", required=True)
    extra_commands.add_db_arguments(db_commands)
    db_status = db_commands.add_parser("status")
    db_status.add_argument("--verify", action="store_true")
    db_commands.add_parser("migrate")
    db_commands.add_parser("verify")
    backup = db_commands.add_parser("backup")
    backup.add_argument("--output", type=Path, required=True)
    prediction = commands.add_parser("prediction")
    prediction_commands = prediction.add_subparsers(dest="operation", required=True)
    prediction_commands.add_parser("backfill-performance")
    replay = prediction_commands.add_parser("replay-allocations")
    replay.add_argument(
        "--apply", action="store_true", help="Apply the verified replay; default only plans it"
    )
    correction = prediction_commands.add_parser("correct-start-slots")
    correction.add_argument("--plan-file", type=Path, required=True)
    correction.add_argument("--plan-sha256", required=True)
    correction.add_argument("--apply", action="store_true")
    commands.add_parser("version")
    args = parser.parse_args(argv)
    config = AppConfig.from_env(args.root)
    if args.data_dir:
        directory = (config.root / args.data_dir).resolve()
        config = replace(config, main_db=directory / "stock.db", minute_db=directory / "minute.db")
    if args.db_path:
        config = replace(config, main_db=(config.root / args.db_path).resolve())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        if args.command == "serve":
            serve(
                replace(
                    config,
                    host=args.host or config.host,
                    port=args.port or config.port,
                    scheduler_enabled=config.scheduler_enabled and not args.no_scheduler,
                )
            )
            return 0
        if args.command == "version":
            result = release_manifest()
        elif args.command == "db":
            if args.operation == "migrate":
                result = migrate(config.main_db, config.minute_db)
            elif args.operation == "status":
                result = status(config.main_db, config.minute_db, verify=args.verify)
            elif args.operation == "verify":
                result = {
                    "schema": status(config.main_db, config.minute_db, verify=True),
                    "main": verify_database(config.main_db),
                    "minute": verify_database(config.minute_db),
                }
            elif args.operation == "backup":
                output = args.output.resolve()
                if output.exists() and any(output.iterdir()):
                    raise ValueError("backup output must be an empty directory")
                output.mkdir(parents=True, exist_ok=True)
                result = {
                    "main": backup_database(config.main_db, output / "stock.db"),
                    "minute": backup_database(config.minute_db, output / "minute.db"),
                }
            else:
                result = extra_commands.execute(args, config)
        elif args.command == "prediction":
            if args.operation == "correct-start-slots":
                result = start_slot_command(config, args.plan_file, args.plan_sha256, args.apply)
            else:
                result = asyncio.run(
                    prediction_command(config, args.operation, not getattr(args, "apply", False))
                )
        else:
            result = extra_commands.execute(args, config)
        output = extra_commands.format_result(args, result)
        if output is not None:
            print(output)
        return 0
    except Exception as error:
        logging.error("%s", redact_text(str(error))[0])
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
