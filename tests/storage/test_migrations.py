import json
from pathlib import Path

import pytest

from stock_god.storage.db import Database
from stock_god.storage.migrations import migrate, status, MANIFESTS


@pytest.mark.migration
def test_empty_upgrade_and_idempotence(tmp_path):
    main, minute = tmp_path / "main.db", tmp_path / "minute.db"
    result = migrate(main, minute)
    assert result["main"]["currentVersion"] == 37
    assert result["minute"]["currentVersion"] == 3
    with Database(main, read_only=True).connection() as db:
        assert db.execute("SELECT COUNT(*) FROM research2_accounts").fetchone()[0] == 24
        assert {row[0] for row in db.execute("SELECT cash FROM research2_accounts")} == {30000}
        original = [tuple(row) for row in db.execute("SELECT * FROM research_v160_simulated_accounts")]
        ledger = [tuple(row) for row in db.execute("SELECT * FROM schema_migrations")]
    migrate(main, minute)
    with Database(main, read_only=True).connection() as db:
        assert original == [
            tuple(row) for row in db.execute("SELECT * FROM research_v160_simulated_accounts")
        ]
        assert ledger == [tuple(row) for row in db.execute("SELECT * FROM schema_migrations")]
    assert status(main, minute, verify=True)["main"]["quickCheck"] == "ok"


def test_unknown_source_is_rejected_without_ledger(tmp_path):
    main, minute = tmp_path / "unknown.db", tmp_path / "minute.db"
    with Database(main).transaction() as db:
        db.execute("CREATE TABLE mystery(value TEXT)")
        db.execute("INSERT INTO mystery VALUES('keep')")
    with pytest.raises(ValueError, match="published legacy schema"):
        migrate(main, minute)
    with Database(main, read_only=True).connection() as db:
        assert (
            db.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='schema_migrations'").fetchone()[0] == 0
        )
        assert db.execute("SELECT value FROM mystery").fetchone()[0] == "keep"


def test_checksums_are_frozen():
    assert (
        MANIFESTS["main"][0]["checksum"] == "41df05f8dbf7b1c56fe959ee8893d97938ddfe35425e98110333e47e2ee40ba6"
    )
    assert (
        MANIFESTS["minute"][2]["checksum"]
        == "09a4f300170f52ad46b04e70467945ba9d9b12da545ce0f3faafa735ad4544af"
    )


@pytest.mark.migration
def test_empty_financial_results_match_go_oracle(tmp_path):
    main, minute = tmp_path / "main.db", tmp_path / "minute.db"
    migrate(main, minute)
    golden = json.loads(
        (Path(__file__).parents[1] / "fixtures/migrations/main_empty_golden.json").read_text(encoding="utf8")
    )
    volatile = {
        "id",
        "created_at",
        "updated_at",
        "frozen_at",
        "baseline_at",
        "valued_at",
        "start_after_trading_date",
    }
    with Database(main, read_only=True).connection() as db:
        for table, key in (
            ("research2_accounts", "slot"),
            ("research2_account_capital_events", "event_id"),
            ("research2_account_ledger_snapshots", "snapshot_id"),
            ("research_settings", "center"),
            ("research_v160_simulated_accounts", "id"),
        ):
            expected = {row[key]: row for row in golden[table]}
            actual = {row[key]: dict(row) for row in db.execute("SELECT * FROM " + table)}
            assert expected.keys() == actual.keys(), table
            for identity, want in expected.items():
                got = actual[identity]
                for field, value in want.items():
                    if field in volatile:
                        continue
                    if field == "config_json":
                        assert json.loads(got[field]) == json.loads(value), (table, identity, field)
                    elif field == "effective_at":
                        from stock_god.storage.historical.timestamps import time_key

                        assert time_key(got[field]) == time_key(value)
                    elif isinstance(value, float):
                        assert got[field] == pytest.approx(value, abs=1e-10), (table, identity, field)
                    else:
                        assert got[field] == value, (table, identity, field, got[field], value)
