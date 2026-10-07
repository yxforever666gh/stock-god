"""Current SQLite transitions, separate from the immutable historical ledger."""

import math

BASE43_DDL = (
    "CREATE TABLE research2_base43_samples (sample_id TEXT PRIMARY KEY, trade_date TEXT NOT NULL, code TEXT NOT NULL, feature_json TEXT NOT NULL, roi REAL, mature_date TEXT, label_status TEXT NOT NULL DEFAULT 'pending', evidence_json TEXT NOT NULL DEFAULT '{}')",
    "CREATE INDEX idx_research2_base43_samples_maturity ON research2_base43_samples(label_status,mature_date)",
    "CREATE TABLE research2_base43_models (prediction_date TEXT PRIMARY KEY, trained_at TEXT NOT NULL, sample_digest TEXT NOT NULL, model_blob BLOB NOT NULL, metadata_json TEXT NOT NULL)",
    "CREATE TABLE research2_base43_daily_tasks (trading_date TEXT NOT NULL, task_type TEXT NOT NULL, status TEXT NOT NULL, started_at TEXT NOT NULL, completed_at TEXT, payload_json TEXT NOT NULL DEFAULT '{}', error TEXT, PRIMARY KEY(trading_date,task_type))",
)
BASE43_ALTERS = (
    "ALTER TABLE research2_accounts ADD COLUMN archived_at datetime",
    "ALTER TABLE research2_recommendations ADD COLUMN allocation_base_cash REAL",
    "ALTER TABLE research2_recommendations ADD COLUMN allocation_policy TEXT NOT NULL DEFAULT 'legacy_recorded'",
)
ARCHIVED_COLUMN = {
    "cid": 0,
    "name": "archived_at",
    "type": "datetime",
    "notnull": 0,
    "dflt_value": None,
    "pk": 0,
    "hidden": 0,
}


def apply_base43_schema(db, archived_at):
    for statement in BASE43_ALTERS:
        db.execute(statement)
    db.execute(
        "UPDATE research2_accounts SET archived_at=? WHERE slot GLOB '[0-2][0-9]:[0-5][0-9]' AND archived_at IS NULL",
        (archived_at,),
    )
    for statement in BASE43_DDL:
        db.execute(statement)


def extend_base43_shape(objects, columns):
    """Derive the current shape with SQLite rather than modifying frozen oracles."""
    import sqlite3

    columns["research2_accounts"] = [*columns["research2_accounts"], ARCHIVED_COLUMN]
    columns["research2_recommendations"] = [
        *columns["research2_recommendations"],
        dict(ARCHIVED_COLUMN, name="allocation_base_cash", type="REAL"),
        dict(
            ARCHIVED_COLUMN, name="allocation_policy", type="TEXT", notnull=1, dflt_value="'legacy_recorded'"
        ),
    ]
    with sqlite3.connect(":memory:") as oracle:
        oracle.row_factory = sqlite3.Row
        for statement in BASE43_DDL:
            oracle.execute(statement)
        for row in oracle.execute("SELECT name,type,tbl_name,sql FROM sqlite_master WHERE sql IS NOT NULL"):
            obj = dict(row)
            objects[obj["name"]] = obj
            if obj["type"] == "table":
                columns[obj["name"]] = [
                    dict(c) for c in oracle.execute("PRAGMA table_xinfo(" + obj["name"] + ")")
                ]


class _LegacyCapitalReader:
    """Scope the frozen oracle's account enumeration without changing its checks."""

    def __init__(self, db):
        self.db = db

    def execute(self, statement, params=()):
        if statement == 'SELECT * FROM "research2_accounts" ORDER BY slot':
            statement = 'SELECT * FROM "research2_accounts" WHERE slot<>? ORDER BY slot'
            params = ("base43",)
        return self.db.execute(statement, params)


def verify_base43_capital(db):
    """Verify archived capital unchanged and the independent current account."""
    from .historical.capital import verify_capital

    verify_capital(_LegacyCapitalReader(db))
    if db.execute(
        "SELECT 1 FROM research2_accounts WHERE slot<>'base43' AND COALESCE(archived_at,'')=''"
    ).fetchone():
        raise ValueError("BASE43 legacy accounts must remain archived")
    account = db.execute("SELECT * FROM research2_accounts WHERE slot='base43'").fetchone()
    events = list(db.execute("SELECT * FROM research2_account_capital_events WHERE slot='base43'"))
    trades = list(db.execute("SELECT net_cash_flow FROM research2_trades WHERE slot='base43'"))
    snapshots = db.execute(
        "SELECT COUNT(*) FROM research2_account_ledger_snapshots "
        "WHERE slot='base43' AND valuation_basis='capital_ledger_v1'"
    ).fetchone()[0]
    if account is None:
        if events or trades or snapshots:
            raise ValueError("BASE43 capital records have no account")
        return  # A schema-only migration precedes runtime account initialization.
    if account["archived_at"]:
        raise ValueError("BASE43 current account must remain active")
    if (len(events) != 1 or events[0]["event_id"] != "base43-initial-external"
            or events[0]["event_type"] != "initial_external" or events[0]["external"] != 1
            or events[0]["source"] != "base43_initial_capital"):
        raise ValueError("BASE43 requires its own initial capital event")
    amounts = [account["initial_cash"], account["seed_cash"], account["cash"], events[0]["amount"],
               *(row[0] for row in trades)]
    if any(not isinstance(value, (int, float)) or not math.isfinite(value) for value in amounts):
        raise ValueError("BASE43 capital/trade amounts must be finite")
    if (abs(account["initial_cash"] - 30000) > .01 or abs(account["seed_cash"] - 30000) > .01
            or abs(events[0]["amount"] - 30000) > .01 or account["cash"] < -1e-7
            or abs(account["cash"] - events[0]["amount"] - sum(row[0] for row in trades)) > .01):
        raise ValueError("BASE43 capital/trade balance mismatch")
    if db.execute(
        "SELECT 1 FROM research2_trades t LEFT JOIN research2_recommendations r "
        "ON r.recommendation_id=t.recommendation_id WHERE (t.slot='base43' OR r.slot='base43') "
        "AND (r.recommendation_id IS NULL OR t.slot<>r.slot) LIMIT 1"
    ).fetchone():
        raise ValueError("BASE43 trade ownership crosses account boundaries")
    if not snapshots:
        raise ValueError("BASE43 capital ledger snapshot missing")
