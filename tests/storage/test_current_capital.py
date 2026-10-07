"""Current verification keeps archived and BASE43 capital in separate accounts."""

import pytest

from stock_god.prediction.core import local
from stock_god.prediction.repository import Repository
from stock_god.storage import migrations
from stock_god.storage.db import Database


@pytest.fixture
def initialized_capital(app_config):
    repo = Repository(Database(app_config.main_db), clock=lambda: local("2026-10-08T09:26:00+08:00"))
    repo.ready()
    return app_config, repo


def financial_rows(database):
    with Database(database, read_only=True).connection() as db:
        return {
            table: [tuple(row) for row in db.execute("SELECT * FROM " + table + " ORDER BY id")]
            for table in ("research2_accounts", "research2_account_capital_events", "research2_trades",
                          "research2_account_ledger_snapshots")
        }


def buy_once(repo):
    run, created = repo.claim_run(repo.clock())
    assert created
    repo.publish_base43(run, [dict(stock_code="sz000001", score=1)])
    item = repo.rows("recommendations")[0]
    repo.buy_base43(item["recommendation_id"],
                   dict(asOf="2026-10-08T09:30:00+08:00", source="fixture", suspended=False,
                        price=10, upperLimit=11, lowerLimit=9),
                   local("2026-10-09T09:30:00+08:00"), now=local("2026-10-08T09:30:00+08:00"))


@pytest.mark.parametrize("with_trade", [False, True])
def test_initialized_v37_verifies_without_rewriting_either_capital(initialized_capital, with_trade):
    config, repo = initialized_capital
    if with_trade:
        buy_once(repo)
    before = financial_rows(config.main_db)
    assert len(before["research2_accounts"]) == 25
    result = migrations.status(config.main_db, config.minute_db, verify=True)
    assert result["main"]["currentVersion"] == 37
    assert result["main"]["quickCheck"] == "ok"
    assert before == financial_rows(config.main_db)


@pytest.mark.parametrize("statement,reason", [
    ("UPDATE research2_accounts SET cash=cash+1 WHERE slot='09:30'", "capital/trade balance mismatch"),
    ("DELETE FROM research2_account_capital_events WHERE slot='09:30' AND event_type='initial_external'",
     "missing/conflicting required capital event"),
    ("DELETE FROM research2_accounts WHERE slot='09:30'", "exactly 24 valid slot accounts"),
    ("UPDATE research2_accounts SET archived_at=NULL WHERE slot='09:30'", "must remain archived"),
])
def test_initialized_v37_preserves_legacy_oracle(initialized_capital, statement, reason):
    config, repo = initialized_capital
    with repo.db.transaction() as db:
        db.execute(statement)
    with pytest.raises(ValueError, match=reason):
        migrations.status(config.main_db, config.minute_db, verify=True)


@pytest.mark.parametrize("statement,reason", [
    ("UPDATE research2_accounts SET cash=cash+1 WHERE slot='base43'", "capital/trade balance mismatch"),
    ("UPDATE research2_accounts SET initial_cash=10000 WHERE slot='base43'", "capital/trade balance mismatch"),
    ("UPDATE research2_account_capital_events SET source='user_initial_capital' WHERE slot='base43'",
     "requires its own initial capital event"),
    ("UPDATE research2_accounts SET archived_at='2026-10-08' WHERE slot='base43'", "must remain active"),
    ("DELETE FROM research2_accounts WHERE slot='base43'", "capital records have no account"),
    ("DELETE FROM research2_account_ledger_snapshots WHERE slot='base43'", "ledger snapshot missing"),
])
def test_initialized_v37_rejects_current_capital_corruption(initialized_capital, statement, reason):
    config, repo = initialized_capital
    with repo.db.transaction() as db:
        db.execute(statement)
    with pytest.raises(ValueError, match=reason):
        migrations.status(config.main_db, config.minute_db, verify=True)


def test_current_trade_cannot_reference_archived_or_missing_recommendation(initialized_capital):
    config, repo = initialized_capital
    buy_once(repo)
    with repo.db.transaction() as db:
        db.execute("UPDATE research2_trades SET recommendation_id='missing' WHERE slot='base43'")
    with pytest.raises(ValueError, match="trade ownership crosses account boundaries"):
        migrations.status(config.main_db, config.minute_db, verify=True)
