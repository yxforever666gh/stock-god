from concurrent.futures import ThreadPoolExecutor

import pytest

from stock_god.prediction.core import Conflict, stamp
from stock_god.prediction.repository import Repository, insert


def test_repeated_and_concurrent_account_initialization_never_duplicates_capital(env):
    def initialize(_):
        Repository(env.db, env.clock).ready()
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(initialize, range(6)))
    accounts = env.service.repo.rows("accounts", "slot='base43'")
    capital = env.service.repo.rows("account_capital_events", "slot='base43'")
    assert len(accounts) == len(capital) == 1
    assert accounts[0]["cash"] == capital[0]["amount"] == 30000


def test_archived_buy_and_sell_are_rejected_without_mutating_ledger(env):
    with env.db.transaction() as connection:
        insert(connection, "recommendations", {
            "recommendation_id": "archived-position", "slot": "09:50",
            "stock_code": "sh600001", "status": "active", "buy_at": stamp(env.clock()),
            "signal_at": stamp(env.clock()), "target_buy_at": stamp(env.clock()),
            "quantity": 100, "buy_price": 10,
        })
    before = {table: env.service.repo.rows(table) for table in ("accounts", "recommendations", "trades")}
    with pytest.raises(Conflict, match="归档"):
        env.service.repo.buy("archived-position", env.market.quote("sh600001"), env.clock())
    with pytest.raises(Conflict, match="归档"):
        env.service.repo.sell("archived-position", env.market.quote("sh600001"), env.clock())
    assert {table: env.service.repo.rows(table) for table in before} == before


@pytest.mark.asyncio
async def test_recovery_without_resume_never_calls_retired_ai_or_market(env):
    await env.service.recover(resume=False)
    assert not env.service._tasks
    assert not env.market.network_calls and not env.ai.calls
    assert not env.service.repo.rows("analysis_runs")
