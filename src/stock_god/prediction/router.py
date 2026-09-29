"""HTTP transport for the prediction domain; settings and audit have separate routers."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, HTTPException, Query

from .core import DEFAULT_SLOT, Conflict, NotFound, PredictionError


def create_router(service):
    router = APIRouter(prefix="/api/v1/prediction", tags=["prediction"])

    def call(operation, *args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except NotFound as error:
            raise HTTPException(404, str(error)) from error
        except Conflict as error:
            raise HTTPException(409, str(error)) from error
        except PredictionError as error:
            raise HTTPException(400, str(error)) from error

    async def async_call(operation, *args, **kwargs):
        try:
            return await operation(*args, **kwargs)
        except NotFound as error:
            raise HTTPException(404, str(error)) from error
        except Conflict as error:
            raise HTTPException(409, str(error)) from error
        except PredictionError as error:
            raise HTTPException(400, str(error)) from error

    @router.get("/slots", operation_id="listPredictionSlots")
    def slots():
        return call(service.slots)

    @router.get("/analysis-runs", operation_id="listPredictionAnalysisRuns")
    def runs(limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0), slot: str = ""):
        return call(service.list_runs, limit, offset, slot)

    @router.get("/analysis-runs/browse", operation_id="browsePredictionAnalysisRuns")
    def browse_runs(
        page: int = Query(1, ge=1),
        day: str = "recent5",
        all_reports: bool = Query(True, alias="allReports"),
    ):
        return call(service.browse_runs, page, day, all_reports)

    @router.get("/analysis-runs/{id}", operation_id="getPredictionAnalysisRun")
    def run(id: str):
        return call(service.get_run, id)

    @router.post("/analysis-runs/{id}/rerun", operation_id="rerunPredictionAnalysisRun")
    async def rerun(id: str):
        return await async_call(service.rerun, id)

    @router.get("/recommendations", operation_id="listPredictionRecommendations")
    async def recommendations(
        limit: int = Query(200, ge=1, le=500),
        offset: int = Query(0, ge=0),
        slot: str = DEFAULT_SLOT,
        slots: str = "",
        from_date: str = Query("", alias="from"),
        to_date: str = Query("", alias="to"),
        bought_only: bool = Query(False, alias="boughtOnly"),
    ):
        selected = slots.split(",") if slots.strip() else None
        if bought_only or selected:
            await service.refresh_quotes()
        return call(
            service.list_recommendations,
            limit,
            offset,
            slot,
            slots=selected,
            from_date=from_date,
            to_date=to_date,
            bought_only=bought_only,
        )

    @router.get("/recommendations/{id}", operation_id="getPredictionRecommendation")
    async def recommendation(id: str):
        return call(service.get_recommendation, id)

    @router.get("/recommendations/{id}/chart", operation_id="getPredictionRecommendationChart")
    async def chart(id: str):
        return await async_call(service.chart, id, False)

    @router.post("/recommendations/{id}/chart/refresh", operation_id="refreshPredictionRecommendationChart")
    async def refresh_chart(id: str):
        return await async_call(service.chart, id, True)

    @router.get("/account", operation_id="getPredictionAccount")
    async def account(slot: str = DEFAULT_SLOT):
        await service.refresh_quotes(service.repo.rows("recommendations", "slot=?", (slot,)))
        return call(service.account, slot)

    @router.get("/account/performance", operation_id="getPredictionPerformance")
    async def performance(slot: str = DEFAULT_SLOT):
        await service.refresh_quotes(service.repo.rows("recommendations", "slot=?", (slot,)))
        return call(service.performance, slot)

    @router.get("/portfolio/performance", operation_id="getPredictionPortfolioPerformance")
    def portfolio(
        slots: str = "", from_date: str = Query("", alias="from"), to_date: str = Query("", alias="to")
    ):
        return call(
            service.portfolio_performance, slots.split(",") if slots.strip() else None, from_date, to_date
        )

    @router.post("/email/test", operation_id="testPredictionEmail")
    async def email_test(config: Annotated[dict | None, Body()] = None):
        return await async_call(service.email.send_test, config)

    return router
