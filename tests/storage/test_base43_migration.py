import pytest

from stock_god.storage import migrations
from stock_god.storage.db import Database


@pytest.mark.migration
def test_v37_preserves_account_rows_and_is_idempotent(tmp_path, monkeypatch):
    main, minute = tmp_path / "main.db", tmp_path / "minute.db"
    with monkeypatch.context() as old:
        old.setitem(migrations.MANIFESTS, "main", migrations.MANIFESTS["main"][:-1])
        old.setitem(migrations.FINAL_VERSION, "main", 36)
        migrations.migrate(main, minute)
        assert migrations.status(main, minute, verify=True)["main"]["currentVersion"] == 36
    with Database(main, read_only=True).connection() as db:
        accounts = [dict(row) for row in db.execute("SELECT * FROM research2_accounts ORDER BY slot")]
        events = [tuple(row) for row in db.execute("SELECT * FROM research2_account_capital_events")]
    migrations.migrate(main, minute)
    with Database(main).transaction() as db:
        upgraded = [dict(row) for row in db.execute("SELECT * FROM research2_accounts ORDER BY slot")]
        timestamps = {row.pop("archived_at") for row in upgraded}
        assert len(timestamps) == 1 and None not in timestamps
        assert upgraded == accounts
        assert events == [tuple(row) for row in db.execute("SELECT * FROM research2_account_capital_events")]
        db.execute(
            "INSERT INTO research2_base43_models VALUES ('2026-10-08','2026-10-07','digest',?, '{}')",
            (b"weights",),
        )
    migrations.migrate(main, minute)
    with Database(main, read_only=True).connection() as db:
        assert db.execute("SELECT model_blob FROM research2_base43_models").fetchone()[0] == b"weights"
        assert len(list(db.execute("SELECT * FROM schema_migrations"))) == 37
    assert migrations.status(main, minute, verify=True)["main"]["currentVersion"] == 37


@pytest.mark.migration
def test_current_shape_checks_model_storage(tmp_path):
    main, minute = tmp_path / "main.db", tmp_path / "minute.db"
    migrations.migrate(main, minute)
    with Database(main).transaction() as db:
        db.execute("DROP TABLE research2_base43_models")
    with pytest.raises(ValueError, match="required table missing: research2_base43_models"):
        migrations.status(main, minute, verify=True)
