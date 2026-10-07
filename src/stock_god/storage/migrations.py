"""Pure Python upgrade runner with the immutable published Go migration ledger."""

import json
import re
import sqlite3
from hashlib import sha256
from pathlib import Path

from .archive import ARCHIVE_DDL, create_archive_schema, seal_legacy_archive, verify_archive
from .current import BASE43_ALTERS, BASE43_DDL, apply_base43_schema, extend_base43_shape
from .db import Database
from .db import quote_identifier as qi
from .historical.common import now
from .historical.data import apply_data

DATA_DIR = Path(__file__).parent / "historical"
MANIFESTS = {
    kind: json.loads((DATA_DIR / f"{kind}_manifest.json").read_text(encoding="utf8"))
    for kind in ("main", "minute")
}
STAGES = {
    kind: json.loads((DATA_DIR / f"{kind}_schema.json").read_text(encoding="utf8"))
    for kind in ("main", "minute")
}
PUBLISHED_VERSIONS = json.loads((DATA_DIR / "published_versions.json").read_text(encoding="utf8"))
UNVERSIONED_PROFILES = json.loads((DATA_DIR / "unversioned_profiles.json").read_text(encoding="utf8"))
FINAL_VERSION = {"main": 37, "minute": 3}
LEDGER_SQL = """CREATE TABLE IF NOT EXISTS schema_migrations (
id INTEGER PRIMARY KEY, name TEXT NOT NULL, checksum TEXT NOT NULL,
applied_at DATETIME NOT NULL, app_version TEXT NOT NULL)"""
V36 = dict(
    id=36,
    name="stock_god_preserve_legacy_source_rows",
    description="Seal original pre-upgrade rows and DDL in the working main SQLite database before historical rewrites; retain retired research and knowledge tables without runtime entry points.",
    definition="\n".join(ARCHIVE_DDL),
)
V36["checksum"] = sha256(
    f"000036\n{V36['name']}\n{V36['description']}\n{V36['definition']}".encode()
).hexdigest()
MANIFESTS["main"].append(V36)
V37 = dict(
    id=37,
    name="stock_god_base43_account_and_models",
    description="Archive scheduled accounts without changing their financial records and persist BASE43 samples, model weights and daily task receipts.",
    definition="\n".join(BASE43_ALTERS)
    + "\nUPDATE research2_accounts SET archived_at=? WHERE slot GLOB '[0-2][0-9]:[0-5][0-9]' AND archived_at IS NULL;\n"
    + "\n".join(BASE43_DDL),
)
V37["checksum"] = sha256(
    f"000037\n{V37['name']}\n{V37['description']}\n{V37['definition']}".encode()
).hexdigest()
MANIFESTS["main"].append(V37)


def _exists(db, name, kind="table"):
    return (
        db.execute("SELECT 1 FROM sqlite_master WHERE name=? AND type=?", (name, kind)).fetchone() is not None
    )


def _columns(db, name):
    return {row["name"]: dict(row) for row in db.execute("PRAGMA table_xinfo(" + qi(name) + ")")}


def _expected(kind, version):
    objects, columns = {}, {}
    for stage in STAGES[kind][:version]:
        for name in stage["removed"]:
            objects.pop(name, None)
            columns.pop(name, None)
        for obj in stage["objects"]:
            objects[obj["name"]] = obj
        columns.update(stage["columns"])
    if kind == "main" and version >= 37:
        extend_base43_shape(objects, columns)
    return objects, columns


def _read_records(db, kind):
    if not _exists(db, "schema_migrations"):
        return []
    records = [dict(row) for row in db.execute("SELECT * FROM schema_migrations ORDER BY id")]
    manifest = {m["id"]: m for m in MANIFESTS[kind]}
    for position, record in enumerate(records, 1):
        migration = manifest.get(record["id"])
        if not migration:
            raise ValueError(f"unknown applied {kind} migration id {record['id']}")
        if record["id"] != position:
            raise ValueError(f"{kind} migration ledger has a gap before {record['id']}")
        if record["name"] != migration["name"] or record["checksum"].lower() != migration["checksum"].lower():
            raise ValueError(f"{kind} migration {record['id']} name/checksum conflict")
    return records


def _normalize(sql):
    # GORM and explicit historical migrations wrote the same ordinary indexes
    # with different identifier quoting. Keep expression/partial indexes and
    # triggers strict; their string literals must never be case-folded.
    identifier = r'(?:[A-Za-z_][A-Za-z_0-9]*|"[A-Za-z_][A-Za-z_0-9]*"|`[A-Za-z_][A-Za-z_0-9]*`|\[[A-Za-z_][A-Za-z_0-9]*\])'
    match = re.fullmatch(
        rf"\s*CREATE\s+(UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?({identifier})\s+ON\s+({identifier})\s*\((.+)\)\s*;?\s*",
        sql,
        re.IGNORECASE,
    )
    if match:
        columns = []
        for raw in match[4].split(","):
            column = re.fullmatch(rf"\s*({identifier})(?:\s+(ASC|DESC))?\s*", raw, re.IGNORECASE)
            if not column:
                break
            columns.append((column[1].strip('"`[]').lower(), (column[2] or "ASC").upper()))
        else:
            return repr(
                (
                    "index",
                    bool(match[1]),
                    match[2].strip('"`[]').lower(),
                    match[3].strip('"`[]').lower(),
                    columns,
                )
            )
    return " ".join(sql.strip().rstrip(";").replace(" IF NOT EXISTS", "").split())


def _create_sql(sql):
    return (
        re.sub(
            r"^(CREATE (?:UNIQUE |VIRTUAL )?(?:TABLE|INDEX|TRIGGER)) ",
            r"\1 IF NOT EXISTS ",
            sql,
            count=1,
            flags=re.I,
        )
        if "IF NOT EXISTS" not in sql.upper().split("(", 1)[0]
        else sql
    )


def _ensure_table(db, obj, columns):
    name = obj["name"]
    if not _exists(db, name):
        db.execute(_create_sql(obj["sql"]))
        return
    present = _columns(db, name)
    for column in columns:
        if column["name"] in present or column["hidden"]:
            continue
        if column["pk"]:
            raise ValueError(f"missing historical primary-key column {name}.{column['name']}")
        statement = "ALTER TABLE " + qi(name) + " ADD COLUMN " + qi(column["name"]) + " " + column["type"]
        if column["notnull"]:
            if (
                column["dflt_value"] is None
                and db.execute("SELECT EXISTS(SELECT 1 FROM " + qi(name) + ")").fetchone()[0]
            ):
                raise ValueError(
                    f"historical NOT NULL column requires explicit backfill: {name}.{column['name']}"
                )
            statement += " NOT NULL"
        if column["dflt_value"] is not None:
            statement += " DEFAULT " + str(column["dflt_value"])
        db.execute(statement)


def _index_columns_available(db, obj):
    # Compile with SQLite itself so expression indexes retain their exact
    # semantics. EXPLAIN does not execute the schema mutation.
    try:
        db.execute("EXPLAIN " + _create_sql(obj["sql"])).fetchall()
        return True
    except sqlite3.OperationalError as error:
        if "no such column" in str(error):
            return False
        raise


def _schema(db, kind, version):
    stage = STAGES[kind][version - 1]
    objects, expected_columns = _expected(kind, version)
    # AutoMigrate also updates existing tables. Empty-oracle DDL deltas alone
    # cannot describe those updates when a current Go model created fields early.
    auto_tables = (
        {
            3: [
                "settings",
                "research_v160_analysis_runs",
                "research_v160_recommendations",
                "research_v160_lifecycle_messages",
                "research_v160_decision_events",
                "research_v160_simulated_accounts",
                "research_v160_simulated_trades",
                "research_v160_positions",
            ],
            7: [
                "research_v160_recommendations",
                "research_v160_decision_events",
                "research_v160_lifecycle_observations",
            ],
            10: [
                "research_v170_account_cash_flows",
                "research_v170_funding_plans",
                "research_v170_account_snapshots",
                "research_v160_recommendations",
            ],
            13: [
                "settings",
                "research2_analysis_runs",
                "research2_recommendations",
                "research2_trades",
                "research2_accounts",
                "research2_account_snapshots",
            ],
            14: ["settings", "research2_email_deliveries"],
            21: [
                "settings",
                "research_v160_analysis_runs",
                "research_v160_recommendations",
                "research_v270_analysis_triggers",
                "research_v270_buy_opportunities",
            ],
            26: ["research2_execution_chains"],
            28: [
                "research2_accounts",
                "research2_analysis_runs",
                "research2_execution_chains",
                "research2_recommendations",
                "research2_trades",
                "research2_account_snapshots",
            ],
            31: ["research2_account_capital_events", "research2_account_ledger_snapshots"],
            32: ["settings"],
            33: ["research2_recommendations", "research2_account_daily_valuations"],
            34: ["research2_execution_chains", "research2_recommendations", "research2_allocation_replays"],
            35: ["stock_master_refresh_metadata"],
        }.get(version, [])
        if kind == "main"
        else []
    )
    for name in stage["removed"]:
        existing = db.execute("SELECT type FROM sqlite_master WHERE name=?", (name,)).fetchone()
        if existing:
            db.execute("DROP " + existing[0].upper() + " " + qi(name))
    if kind == "main" and version == 3:
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND (name LIKE 'guard_strategy_%' OR name LIKE 'guard_legacy_%' OR name LIKE 'immutable_strategy_%' OR name LIKE 'immutable_corporate_action_%')"
        ).fetchall():
            db.execute("DROP TRIGGER " + qi(row[0]))
    if kind == "main" and version == 24:
        for index in db.execute("PRAGMA index_list('research2_analysis_runs')").fetchall():
            names = [row["name"] for row in db.execute("PRAGMA index_info(" + qi(index["name"]) + ")")]
            if index["unique"] and names == ["trading_date"]:
                if index["name"].startswith("sqlite_autoindex_"):
                    raise ValueError("inline trading_date uniqueness cannot be silently rebuilt")
                db.execute("DROP INDEX " + qi(index["name"]))
    if kind == "main" and version == 28:
        for name in ("idx_research2_runs_date_attempt", "idx_research2_execution_chains_trading_date"):
            db.execute("DROP INDEX IF EXISTS " + qi(name))
    for table in auto_tables:
        _ensure_table(db, objects[table], expected_columns[table])
    for obj in sorted(
        stage["objects"], key=lambda x: {"table": 0, "index": 1, "trigger": 2}.get(x["type"], 3)
    ):
        if obj["type"] == "table":
            _ensure_table(db, obj, stage["columns"][obj["name"]])
        elif obj["type"] in ("index", "trigger"):
            if obj["type"] == "index" and not _index_columns_available(db, obj):
                continue
            found = db.execute("SELECT sql FROM sqlite_master WHERE name=?", (obj["name"],)).fetchone()
            if found and _normalize(found[0]) != _normalize(obj["sql"]):
                db.execute("DROP " + obj["type"].upper() + " " + qi(obj["name"]))
            db.execute(_create_sql(obj["sql"]))
    # Some frozen migrations add individual fields already present in the Go
    # current-model empty oracle; explicitly cover real old-version databases.
    ensure = (
        {
            4: {"ai_config": ["disabled"]},
            5: {"research_v160_analysis_runs": ["model_attempt_log_json"]},
            11: {"settings": ["minute_provider_order", "ai_review_start_time", "ai_review_interval_minutes"]},
            15: {
                "settings": ["experimental_evidence_enabled"],
                "research_v160_analysis_runs": [
                    "strategy_version",
                    "evidence_profile_version",
                    "evidence_set_id",
                ],
                "research2_analysis_runs": [
                    "strategy_version",
                    "evidence_profile_version",
                    "evidence_set_id",
                ],
            },
            22: {"research2_recommendations": ["current_price", "current_price_at"]},
            23: {
                "research2_analysis_runs": ["evidence_window_start_at", "evidence_coverage_pct", "degraded"],
                "research2_trades": ["price_source", "execution_mode"],
            },
            24: {"research2_analysis_runs": ["attempt_no"]},
            25: {
                "research_v160_analysis_runs": ["data_profile_version"],
                "research_v270_buy_opportunities": [
                    "requested_action",
                    "decision_quote_status",
                    "reanalysis_at",
                    "superseded_by_run_id",
                    "data_profile_version",
                ],
                "research_v160_lifecycle_observations": ["data_profile_version"],
                "research_v160_decision_events": ["decision_policy_version"],
            },
            26: {
                "research2_analysis_runs": [
                    "chain_id",
                    "parent_run_id",
                    "trigger_source",
                    "requested_slots",
                    "primary_count",
                    "standby_count",
                ],
                "research2_recommendations": [
                    "selection_role",
                    "selection_rank",
                    "replaces_recommendation_id",
                    "promotion_reason",
                    "execution_failure_code",
                    "execution_quote_price",
                    "execution_quote_at",
                    "execution_limit_price",
                    "execution_limit_distance_pct",
                ],
            },
            29: {"research_v160_simulated_accounts": ["frozen", "frozen_at", "frozen_reason"]},
            30: {"research2_execution_chains": ["allocation_base_cash"]},
            33: {
                "research2_recommendations": [
                    "buy_day_limit_outcome",
                    "buy_day_limit_reason",
                    "buy_day_limit_finalized_at",
                ]
            },
            34: {"research2_execution_chains": ["allocation_policy"]},
        }.get(version, {})
        if kind == "main"
        else {}
    )
    if ensure:
        objects, columns = _expected(kind, version)
        for table, names in ensure.items():
            selected = [c for c in columns[table] if c["name"] in names]
            _ensure_table(db, objects[table], selected)
    indexed_tables = set(auto_tables) | set(ensure)
    for obj in objects.values():
        if (
            obj["type"] == "index"
            and obj["tbl_name"] in indexed_tables
            and not _exists(db, obj["name"], "index")
            and _index_columns_available(db, obj)
        ):
            db.execute(_create_sql(obj["sql"]))
    if kind == "main" and version == 22 and "rank" in _columns(db, "research2_recommendations"):
        db.execute("ALTER TABLE research2_recommendations DROP COLUMN rank")
    if kind == "main" and version == 24:
        if "scheduled_slot" not in _columns(db, "research2_analysis_runs"):
            db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_research2_runs_date_attempt ON research2_analysis_runs(trading_date,attempt_no)"
            )
    if kind == "main" and version == 28:
        db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_research2_runs_date_attempt ON research2_analysis_runs(trading_date,scheduled_slot,attempt_no)"
        )
        db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_research2_execution_chains_trading_date ON research2_execution_chains(trading_date,slot)"
        )


def _affected(db, version):
    names = {
        row[0]
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'legacy_archive_%'"
        )
    }
    result = set()
    if version < 9:
        result.update(name for name in STAGES["main"][8]["removed"] if name in names)
    if version < 29:
        result.update(
            name
            for name in names
            if name.startswith(("research_v", "research_audit_")) or name == "research_replays"
        )
    if version < 35:
        result.update(name for name in names if name.startswith("research2_"))
    if version < 32:
        result.update(names & {"settings", "research_settings", "ai_config"})
    return result


def _validate_unversioned(db, kind):
    names = {
        r[0]
        for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'legacy_archive_%'"
        )
    } - {"schema_migrations"}
    if not names:
        return
    if kind == "main":
        actual = {name: set(_columns(db, name)) for name in names}
        matching = [
            profile
            for profile in UNVERSIONED_PROFILES
            if set(profile["tables"]) <= names
            and all(set(columns) == actual.get(table) for table, columns in profile["tables"].items())
        ]
        if not matching:
            raise ValueError("unversioned database does not match a published legacy schema")
    base, columns = _expected(kind, 2)
    known = set(base)
    if (
        not names <= known
        or (kind == "main" and "settings" not in names)
        or (kind == "minute" and names != {"minute_bar"})
    ):
        raise ValueError("unversioned database does not match a published legacy schema")
    for name in names:
        expected = {col["name"] for col in columns[name]}
        actual = set(_columns(db, name))
        if not actual <= expected:
            raise ValueError("unknown unversioned columns in " + name)
        if "id" in expected and "id" not in actual:
            raise ValueError("missing legacy primary key: " + name)


def _migrate_one(path, kind):
    database = Database(path)
    with database.transaction() as db:
        records = _read_records(db, kind)
        current = len(records)
        if current >= (35 if kind == "main" else 3):
            # Validate the released source before recording the 6.0 transition.
            # A conflicting latest schema must not receive a successful ledger row.
            _status_one(path, kind, True, allow_pending=True)
        if not current:
            _validate_unversioned(db, kind)
        if kind == "main" and current < 35:
            affected = _affected(db, current)
            if affected:
                seal_legacy_archive(db, current, affected)
        db.execute(LEDGER_SQL)
    for migration in MANIFESTS[kind][current:]:
        version = migration["id"]
        with database.transaction() as db:
            # Recheck immediately under the writer lock before each transition.
            live = _read_records(db, kind)
            if len(live) >= version:
                continue
            if len(live) != version - 1:
                raise ValueError("migration ledger changed concurrently")
            if version == 36:
                create_archive_schema(db)
                verify_archive(db)
            elif kind == "main" and version == 37:
                apply_base43_schema(db, now())
            else:
                introduced = (
                    kind == "main"
                    and version == 21
                    and (
                        not _exists(db, "research_v270_analysis_triggers")
                        or not _exists(db, "research_v270_buy_opportunities")
                        or "ai_capital_deployment_enabled" not in _columns(db, "settings")
                    )
                )
                _schema(db, kind, version)
                if kind == "main":
                    apply_data(db, version, introduced_capital=introduced)
            db.execute(
                "INSERT INTO schema_migrations(id,name,checksum,applied_at,app_version) VALUES(?,?,?,?,?)",
                (
                    version,
                    migration["name"],
                    migration["checksum"],
                    now(),
                    "7.0.0" if kind == "main" and version == 37 else "6.0.0",
                ),
            )
    return _status_one(path, kind, True)


def migrate(main_path: str | Path, minute_path: str | Path) -> dict:
    if Path(main_path).resolve() == Path(minute_path).resolve():
        raise ValueError("main and minute paths must differ")
    return {"main": _migrate_one(main_path, "main"), "minute": _migrate_one(minute_path, "minute")}


def _status_one(path, kind, verify, *, allow_pending=False):
    if not Path(path).is_file():
        raise FileNotFoundError(path)
    with Database(path, read_only=True).connection() as db:
        records = _read_records(db, kind)
        result: dict[str, object] = dict(
            database=kind,
            currentVersion=len(records),
            expectedVersion=FINAL_VERSION[kind],
            pending=[m["id"] for m in MANIFESTS[kind][len(records) :]],
            records=records,
        )
        if verify:
            checks = [r[0] for r in db.execute("PRAGMA quick_check")]
            if checks != ["ok"]:
                raise ValueError("SQLite quick_check: " + "; ".join(checks))
            result["quickCheck"] = "ok"
            if result["pending"] and not allow_pending:
                raise ValueError("database has unapplied migrations")
            objects, columns = _expected(kind, len(records))
            for name, obj in objects.items():
                if obj["type"] == "table":
                    if not _exists(db, name):
                        raise ValueError("required table missing: " + name)
                    actual = _columns(db, name)
                    missing = {c["name"] for c in columns[name]} - set(actual)
                    if missing:
                        raise ValueError(f"required columns missing: {name}: {sorted(missing)}")
                elif not _exists(db, name, obj["type"]):
                    raise ValueError("required schema object missing: " + name)
                elif obj["type"] in ("index", "trigger"):
                    actual_sql = db.execute("SELECT sql FROM sqlite_master WHERE name=?", (name,)).fetchone()[
                        0
                    ]
                    if _normalize(actual_sql) != _normalize(obj["sql"]):
                        raise ValueError("schema object definition conflict: " + name)
            if kind == "main":
                if len(records) >= 36 and (
                    not _exists(db, "legacy_archive_sets") or not _exists(db, "legacy_archive_tables")
                ):
                    raise ValueError("schema 36 archive metadata is missing")
                verify_archive(db)
                from .historical.capital import verify_capital

                verify_capital(db)
        return result


def status(main_path: str | Path, minute_path: str | Path, *, verify: bool = False) -> dict:
    return {
        kind: _status_one(path, kind, verify) for kind, path in (("main", main_path), ("minute", minute_path))
    }
