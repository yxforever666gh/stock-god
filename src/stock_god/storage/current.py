"""Current SQLite transitions, separate from the immutable historical ledger."""

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
