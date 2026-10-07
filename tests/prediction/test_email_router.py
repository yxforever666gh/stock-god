from datetime import timedelta

import httpx
import pytest
from fastapi import FastAPI

from stock_god.prediction.core import stamp
from stock_god.prediction.email import queue_published
from stock_god.prediction.repository import insert
from stock_god.prediction.router import create_router


def seed_published_email(env):
    run = {"run_id": "email-run", "slot": "base43", "trading_date": "2026-09-24",
           "attempt_no": 1, "status": "success", "published": True, "recommendation_count": 2,
           "persisted_at": stamp(env.clock()), "report_markdown": "BASE43 frozen report"}
    with env.db.transaction() as connection:
        insert(connection, "analysis_runs", run | {
            "scheduled_for": stamp(env.clock()), "started_at": stamp(env.clock()),
            "evidence_cutoff_at": stamp(env.clock()), "published": True,
        })
        queue_published(connection, run)
    return {"runId": run["run_id"]}


@pytest.mark.asyncio
async def test_email_queue_is_publication_atomic_and_retries_exactly_four_times(env):
    env.settings.config.update(predictionEmailEnabled=True, predictionEmailSlots=["base43"])
    env.settings.save()
    run = seed_published_email(env)
    deliveries = env.service.repo.rows("email_deliveries")
    assert len(deliveries) == 1 and deliveries[0]["analysis_run_id"] == run["runId"]

    def fail(config, delivery):
        raise RuntimeError("SMTP smtp-secret sender@example.com rejected")

    env.service.email.mailer = fail
    for attempt, advance in enumerate([0, 1, 3, 10], 1):
        env.clock.at += timedelta(minutes=advance)
        await env.service.deliver_emails()
        delivery = env.service.repo.rows("email_deliveries")[0]
        assert delivery["attempt_count"] == attempt and "smtp-secret" not in delivery["last_error"]
        assert delivery["status"] == ("failed" if attempt == 4 else "retry_wait")
    with env.db.connection() as con:
        assert con.execute("SELECT count(*) FROM email_send_logs").fetchone()[0] == 4


@pytest.mark.asyncio
async def test_disabled_email_cancels_pending_and_stale_sending_recovers(env):
    env.settings.config.update(predictionEmailEnabled=True, predictionEmailSlots=["base43"])
    env.settings.save()
    seed_published_email(env)
    env.service.repo.set(
        "email_deliveries",
        {"status": "sending", "updated_at": stamp(env.clock() - timedelta(minutes=3))},
        "1",
    )
    await env.service.deliver_emails()
    assert env.service.repo.rows("email_deliveries")[0]["status"] == "sent"
    env.service.repo.set("email_deliveries", {"status": "retry_wait"}, "1")
    env.settings.config["predictionEmailEnabled"] = False
    env.settings.save()
    await env.service.deliver_emails()
    assert env.service.repo.rows("email_deliveries")[0]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_http_prediction_routes_only_and_invalid_slot_boundary(env):
    seed_published_email(env)
    app = FastAPI()
    app.include_router(create_router(env.service))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert len((await client.get("/api/v1/prediction/slots")).json()) == 25
        assert (await client.get("/api/v1/prediction/recommendations?slot=09:50")).json() == []
        assert (await client.get("/api/v1/prediction/account")).json()["initialCash"] == 30000
        assert (await client.get("/api/v1/prediction/account?slot=oops")).status_code == 400
        assert (await client.get("/api/v1/prediction/analysis-runs/missing")).status_code == 404
        assert (await client.get("/api/v1/research2/slots")).status_code == 404
        assert (
            await client.get("/api/v1/prediction/portfolio/performance?from=2026-09-25&to=2026-09-24")
        ).status_code == 400
