"""BASE43 atomic ledger and quote boundaries over a disposable current schema."""
from pathlib import Path

import pytest

from stock_god.storage.db import Database
from stock_god.prediction.core import Conflict, local
from stock_god.prediction.repository import Repository, insert, ledger_snapshot, live_value
from stock_god.prediction.views import Views


@pytest.fixture
def account_repo(tmp_path):
    db = Database(tmp_path / "base43.db")
    with db.connection() as connection:
        connection.executescript((Path(__file__).parent / "schema.sql").read_text(encoding="utf-8"))
        columns = {r["name"] for r in connection.execute("PRAGMA table_info(research2_recommendations)")}
        if "allocation_base_cash" not in columns:
            connection.execute("ALTER TABLE research2_recommendations ADD COLUMN allocation_base_cash REAL")
            connection.execute("ALTER TABLE research2_recommendations ADD COLUMN allocation_policy TEXT")
    repo = Repository(db, clock=lambda: local("2026-10-08T09:29:59+08:00"))
    repo.ready()
    return repo


def quote(at="2026-10-08T09:30:00+08:00", price=10):
    return dict(asOf=at, source="fixture", suspended=False, price=price, upperLimit=11, lowerLimit=9)


def publish(repo):
    run, claimed = repo.claim_run(local("2026-10-08T09:29:59+08:00"))
    assert claimed
    return repo.publish_base43(run, [dict(stock_code="sz000002", score=1),
        dict(stock_code="sz000001", score=1), dict(stock_code="sz000003", score=.5)])


def test_ready_capital_idempotent_and_mark_to_market(account_repo):
    repo = account_repo
    repo.ready()
    assert len(repo.rows("account_capital_events")) == 1
    assert len(repo.rows("account_ledger_snapshots")) == 1
    assert Views(repo).account()["netAssetValue"] == 30000
    assert live_value(dict(slot="base43", stock_code="sz000001", quantity=100, current_price=10)) == 1000


def test_frozen_top_two_budget_and_restart(account_repo):
    repo = account_repo
    run = publish(repo)
    items = repo.rows("recommendations", order="selection_rank")
    assert [r["stock_code"] for r in items] == ["sz000001", "sz000002"]
    assert [r["allocation_base_cash"] for r in items] == [15000, 15000]
    at = local("2026-10-08T09:30:00+08:00")
    sell = local("2026-10-09T09:30:00+08:00")
    trade = repo.buy_base43(items[0]["recommendation_id"], quote(), sell, now=at)
    assert trade["quantity"] == 1400
    repo.buy_base43(items[0]["recommendation_id"], quote(), sell, now=at)
    assert len(repo.rows("trades")) == 1
    repo.publish_base43(run, [])
    assert len(repo.rows("recommendations")) == 2


@pytest.mark.parametrize("changed", [dict(asOf="2026-10-08T09:30:01+08:00"),
    dict(asOf="2026-10-08T09:29:59+08:00"), dict(price=11), dict(suspended=True), dict(upperLimit=0)])
def test_buy_rejects_unqualified_quotes(account_repo, changed):
    repo = account_repo
    publish(repo)
    item = repo.rows("recommendations")[0]
    q = quote(); q.update(changed)
    with pytest.raises(Conflict):
        repo.buy_base43(item["recommendation_id"], q, local("2026-10-09T09:30:00+08:00"), now=local("2026-10-08T09:30:00+08:00"))
    assert not repo.rows("trades")


def test_archive_blocks_updates_and_snapshots(account_repo):
    repo = account_repo
    with repo.db.transaction() as connection:
        insert(connection, "accounts", dict(slot="09:50", initial_cash=10000, cash=10000,
            archived_at="2026-10-07T15:00:00+08:00"))
        with pytest.raises(Conflict):
            ledger_snapshot(connection, "09:50", repo.clock(), "forbidden")
    with pytest.raises(Conflict):
        repo.set("accounts", {"cash": 0}, "slot=?", ("09:50",))
    assert repo.row("accounts", "slot=?", ("09:50",))["cash"] == 10000


def test_publication_uses_preopen_cash_despite_later_sell_credit(account_repo):
    repo = account_repo
    run, _ = repo.claim_run(repo.clock())
    repo.set("accounts", {"cash": 50000}, "slot=?", ("base43",))
    repo.clock = lambda: local("2026-10-08T09:30:01+08:00")
    repo.publish_base43(run, [dict(stock_code="sz000001", score=1), dict(stock_code="sz000002", score=2)])
    items = repo.rows("recommendations")
    assert [r["allocation_base_cash"] for r in items] == [15000, 15000]
    assert all(local(r["signal_at"]) == local("2026-10-08T09:29:59+08:00") for r in items)


def test_late_claim_and_late_publication_cannot_create_buy_list(account_repo):
    repo = account_repo
    run, _ = repo.claim_run(repo.clock())
    repo.clock = lambda: local("2026-10-08T09:31:00+08:00")
    with pytest.raises(Conflict):
        repo.publish_base43(run, [dict(stock_code="sz000001", score=1)])
    assert not repo.rows("recommendations")
    repo.clock = lambda: local("2026-10-09T09:30:00+08:00")
    with pytest.raises(Conflict):
        repo.claim_run(repo.clock())


def test_base43_actual_fees_round_each_fractional_cent_half_up():
    from stock_god.prediction.core import base43_trade_cost, trade_cost
    actual = base43_trade_cost("sh600001", 10.15, 100, "sell")
    assert actual["commission"] == 5.0
    assert actual["transfer_fee"] == .01
    assert actual["stamp_duty"] == .51
    assert actual["net_cash_flow"] == 1009.48
    legacy = trade_cost("sh600001", 10.15, 100, "sell")
    assert legacy["transfer_fee"] == pytest.approx(.01015)
    buy = base43_trade_cost("sh600001", 10.15, 100)
    assert buy["stamp_duty"] == 0
    assert buy["net_cash_flow"] == -1020.01


def test_early_publication_rejects_future_evidence_cutoff(account_repo):
    repo = account_repo
    repo.clock = lambda: local("2026-10-08T09:26:00+08:00")
    run, _ = repo.claim_run(repo.clock())
    repo.set("analysis_runs", {"evidence_cutoff_at": "2026-10-08T09:29:55+08:00"},
             "run_id=?", (run["run_id"],))
    with pytest.raises(Conflict):
        repo.publish_base43(run, [dict(stock_code="sz000001", score=1)])
    assert not repo.rows("recommendations")


@pytest.mark.parametrize("at", ["09:29:59", "09:31:00"])
def test_early_recommendation_never_buys_outside_opening_minute(account_repo, at):
    repo = account_repo
    repo.clock = lambda: local("2026-10-08T09:26:00+08:00")
    run, _ = repo.claim_run(repo.clock())
    repo.publish_base43(run, [dict(stock_code="sz000001", score=1)])
    item = repo.rows("recommendations")[0]
    assert local(item["signal_at"]) == local("2026-10-08T09:26:00+08:00")
    assert local(item["target_buy_at"]) == local("2026-10-08T09:30:00+08:00")
    with pytest.raises(Conflict):
        repo.buy_base43(item["recommendation_id"], quote(), local("2026-10-09T09:30:00+08:00"),
                       now=local("2026-10-08T" + at + "+08:00"))
    assert not repo.rows("trades")


def test_revoked_run_and_pending_seats_stay_revoked_after_reenable(account_repo):
    from stock_god.prediction.service import PredictionService

    repo = account_repo
    service = PredictionService(repo.db, None, None, None, None, clock=repo.clock, models=object())
    run, _ = repo.claim_run(repo.clock())
    service.on_settings_changed({"predictionAutoEnabled": True}, {"predictionAutoEnabled": False})
    service.on_settings_changed({"predictionAutoEnabled": False}, {"predictionAutoEnabled": True})
    restarted = Repository(repo.db, clock=repo.clock)
    with pytest.raises(Conflict, match="发布权限"):
        restarted.publish_base43(run, [dict(stock_code="sz000001", score=1)])
    assert not restarted.rows("recommendations")

    # An already published list keeps its historical report, but cannot regain its buy seats.
    later, _ = restarted.claim_run(repo.clock())
    restarted.publish_base43(later, [dict(stock_code="sz000001", score=1)])
    service.on_settings_changed({"predictionAutoEnabled": True}, {"predictionAutoEnabled": False})
    service.on_settings_changed({"predictionAutoEnabled": False}, {"predictionAutoEnabled": True})
    item = restarted.rows("recommendations")[0]
    assert item["status"] == "analysis_only"
    assert restarted.publish_base43(later, [dict(stock_code="sz000002", score=2)])["persisted_at"]
    with pytest.raises(Conflict):
        restarted.buy_base43(item["recommendation_id"], quote(), local("2026-10-09T09:30:00+08:00"),
                             now=local("2026-10-08T09:30:00+08:00"))
    assert not restarted.rows("trades")
