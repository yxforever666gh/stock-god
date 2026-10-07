"""Isolated opening-session acceptance; never runs prediction or trades."""

import argparse
import hashlib
import json
import sqlite3
import time
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

import numpy as np

from stock_god.config import AppConfig
from stock_god.market.common import CN, timestamp
from stock_god.market.meoz_source import SIGNATURE, MeozAuctionSource
from stock_god.market.service import MarketServices
from stock_god.prediction.base43 import feature_candidate
from stock_god.settings import DEFAULTS, SettingsStore
from stock_god.storage import Database


def receipt(source, day):
    """Derive certification flags from independently collected raw facts."""
    cutoff = day.replace(hour=9, minute=29, second=59, microsecond=0)
    inspected = source.inspect(day.strftime("%Y%m%d"), cutoff, volume_unit="lot")
    feature_proofs = []
    for candidate in inspected.get("candidates", []):
        try:
            vector = feature_candidate(candidate)
        except (ValueError, TypeError, KeyError, IndexError):
            vector = None
        valid = vector is not None and vector.shape == (43,) and not np.isinf(vector).any()
        feature_proofs.append({"code": candidate.get("code"), "verified": bool(valid),
            "featureCount": 43 if valid else 0,
            "sha256": hashlib.sha256(vector.astype("<f4").tobytes()).hexdigest()
            if vector is not None and valid else None})
    inspected["featureProofs"] = feature_proofs
    # Certification changes readiness, not the independently observed facts.
    facts = {key: value for key, value in inspected.items()
             if key not in {"sourceStatusJson", "factsSha256", "complete"}}
    canonical = json.dumps(facts, ensure_ascii=False, sort_keys=True, allow_nan=False)
    flags = ("coverageVerified", "volumeVerified", "checkpointsVerified", "finalAuctionVerified")
    checks = {flag: inspected.get("checks", {}).get(flag) is True for flag in flags}
    checks["featuresVerified"] = bool(feature_proofs) and all(proof["verified"] for proof in feature_proofs)
    return {"tradingDate": day.date().isoformat(), "sourceSignature": inspected.get("sourceSignature", SIGNATURE),
            "evidenceSha256": hashlib.sha256(canonical.encode()).hexdigest(),
            "volumeUnit": "lot", "passed": all(checks.values()), **checks,
            "inspection": inspected}


def validate_acceptance(directory, key, *, now=None):
    """Read-only recomputation: a boolean receipt alone never grants readiness."""
    now = now or datetime.now(CN)
    directory = Path(directory).resolve()
    recorded = json.loads((directory / "receipt.json").read_text(encoding="utf-8"))
    required = ("passed", "coverageVerified", "volumeVerified", "checkpointsVerified", "finalAuctionVerified", "featuresVerified")
    if (not all(recorded.get(flag) is True for flag in required)
            or recorded.get("sourceSignature") != SIGNATURE or recorded.get("volumeUnit") != "lot"):
        raise ValueError("acceptance receipt did not pass")
    day = datetime.strptime(recorded["tradingDate"], "%Y-%m-%d").replace(tzinfo=CN)
    cutoff = day.replace(hour=9, minute=29, second=59)
    if now < cutoff:
        raise ValueError("acceptance date or cutoff is in the future")
    config = replace(AppConfig.from_env(), main_db=directory / "facts.db",
                     minute_db=directory / "minute.db", market_index_dir=directory / "minute-index",
                     scheduler_enabled=False)
    database = Database(config.main_db, read_only=True)
    market = MarketServices(config, {**DEFAULTS, "meozApiKey": key})
    source = MeozAuctionSource(database, market)
    try:
        cert = source._read("source", "source_certification")
        if (not cert or cert.get("signature") != SIGNATURE
                or cert.get("keyFingerprint") != hashlib.sha256(key.encode()).hexdigest()
                or cert.get("evidence", {}).get("evidenceSha256") != recorded.get("evidenceSha256")):
            raise ValueError("acceptance credential or certification mismatch")
        measured = receipt(source, day)
        if not measured["passed"] or measured["evidenceSha256"] != recorded.get("evidenceSha256"):
            raise ValueError("acceptance facts changed or no longer pass")
        # Check original observation timestamps too, including eligibility documents.
        prepared = source._read(day.strftime("%Y%m%d"), "source_candidates") or {}
        saved = source._read(day.strftime("%Y%m%d"), "source_snapshot") or {}
        for document in prepared.get("documents", []) + saved.get("documents", []):
            observed = timestamp(document["receivedAt"])
            if observed.date() != day.date() or observed > cutoff:
                raise ValueError("acceptance document was received outside the causal day")
        for record in saved.get("records", []):
            quoted, observed = timestamp(record["asOf"]), timestamp(record["availableAt"])
            if (quoted.date() != day.date() or observed.date() != day.date()
                    or quoted > observed or observed > cutoff):
                raise ValueError("acceptance quote has invalid observation timestamps")
        return measured
    finally:
        market.close()
        database.close()


def install_acceptance(config, key, evidence):
    """Explicit installation after deployment migration; never migrates itself."""
    database = Database(config.main_db)
    with database.connection() as connection:
        version = connection.execute("SELECT MAX(id) FROM schema_migrations").fetchone()[0]
        if version != 37:
            raise ValueError("acceptance installation requires main schema 37")
    store = SettingsStore(database)
    snapshot = store.load()
    settings = dict(snapshot.config)
    settings["meozApiKey"] = key
    # Deployment holds a full rollback backup and keeps the service stopped.
    store.save(snapshot.revision, settings, snapshot.models)
    market = MarketServices(config, settings)
    try:
        MeozAuctionSource(database, market).certify("lot", evidence)
    finally:
        market.close()
        database.close()


def capture(source, market, day, *, clock=None, sleep=None):
    """Observe a complete causal session, with injectable time for deterministic tests."""
    clock = clock or (lambda: datetime.now(CN))
    sleep = sleep or time.sleep
    now = clock()
    if now.date() != day.date() or now >= day.replace(hour=9, minute=15):
        raise ValueError("capture must start on the requested day before 09:15 Asia/Shanghai")
    if not market.is_trading_day(day):
        raise ValueError("requested date is not an independently verified trading day")
    previous = day - timedelta(days=1)
    for _ in range(40):
        if market.is_trading_day(previous):
            break
        previous -= timedelta(days=1)
    else:
        raise ValueError("previous trading day could not be verified")
    prepare_at = day.replace(hour=9)
    while clock() < prepare_at:
        sleep(min(30, (prepare_at - clock()).total_seconds()))
    prepared = source.prepare(day.strftime("%Y%m%d"), previous.strftime("%Y%m%d"))
    if not prepared.get("complete") or not prepared.get("candidates"):
        raise ValueError("candidate preparation failed or has no verifiable candidates")
    start = day.replace(hour=9, minute=15)
    if clock() > start:
        raise ValueError("candidate preparation missed the 09:15 coverage start")
    while clock() < start:
        sleep(min(30, (start - clock()).total_seconds()))
    cutoff = day.replace(hour=9, minute=29, second=59)
    next_poll = start
    while clock() <= cutoff:
        source.poll(day.strftime("%Y%m%d"), clock())
        next_poll += timedelta(seconds=3)
        delay = (next_poll - clock()).total_seconds()
        if delay > 0:
            sleep(min(delay, max(.01, (cutoff - clock()).total_seconds() + .01)))
    return receipt(source, day)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date")
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--accept", type=Path, help="verify a captured session without queries")
    parser.add_argument("--install", action="store_true", help="explicitly install a verified acceptance into main schema 37")
    parser.add_argument("--inspect", action="store_true", help="read an existing capture directory without queries")
    args = parser.parse_args(argv)
    if args.install and not args.accept:
        parser.error("--install requires --accept")
    if args.accept and (args.date or args.output or args.inspect):
        parser.error("--accept cannot be combined with capture or inspect options")
    key = args.key_file.read_text(encoding="utf-8-sig").strip()
    if not key:
        parser.error("API key file is empty")
    if args.accept:
        try:
            evidence = validate_acceptance(args.accept, key)
            if args.install:
                install_acceptance(AppConfig.from_env(), key, evidence)
            print("Acceptance verified" + (" and installed" if args.install else " (read-only)"))
            return 0
        except (ValueError, RuntimeError, OSError, KeyError, sqlite3.Error) as exc:
            print("Acceptance rejected: " + type(exc).__name__)
            return 1
    if not args.date or not args.output:
        parser.error("capture/inspect requires --date and --output")
    day = datetime.strptime(args.date, "%Y-%m-%d").replace(tzinfo=CN)
    original = AppConfig.from_env()
    settings = dict(DEFAULTS)
    if original.main_db.is_file():
        settings = SettingsStore(Database(original.main_db, read_only=True)).load().config
    settings["meozApiKey"] = key
    output = args.output.resolve() if args.inspect else args.output.resolve() / (args.date + "-" + uuid4().hex)
    if not args.inspect:
        output.mkdir(parents=True, exist_ok=False)
    config = replace(original, main_db=output / "facts.db", minute_db=output / "minute.db",
                     market_index_dir=output / "minute-index", scheduler_enabled=False)
    database = Database(config.main_db, read_only=args.inspect)
    if not args.inspect:
        with database.transaction() as connection:
            connection.execute("CREATE TABLE research2_base43_daily_tasks (trading_date TEXT, task_type TEXT, "
                "status TEXT, started_at TEXT, completed_at TEXT, payload_json TEXT, error TEXT, "
                "PRIMARY KEY(trading_date,task_type))")
    market = MarketServices(config, settings)
    source = MeozAuctionSource(database, market)
    print("Isolated acceptance directory: " + str(output), flush=True)
    try:
        result = receipt(source, day) if args.inspect else capture(source, market, day)
        if result["passed"] and not args.inspect:
            source.certify("lot", result)
        if not args.inspect:
            with (output / "receipt.json").open("x", encoding="utf-8") as handle:
                json.dump(result, handle, ensure_ascii=False, indent=2, allow_nan=False)
        print(json.dumps({key: value for key, value in result.items() if key != "inspection"}, ensure_ascii=False))
        return 0 if result["passed"] else 1
    except (ValueError, RuntimeError, KeyError, OSError, sqlite3.Error) as exc:
        # Persist a negative outcome without copying potentially sensitive provider text.
        failed = {"passed": False, "tradingDate": args.date, "errorType": type(exc).__name__}
        if not args.inspect:
            with (output / "receipt.json").open("x", encoding="utf-8") as handle:
                json.dump(failed, handle, ensure_ascii=False, indent=2)
        print("Acceptance failed: " + type(exc).__name__, flush=True)
        return 1
    finally:
        market.close()
        database.close()


if __name__ == "__main__":
    raise SystemExit(main())
