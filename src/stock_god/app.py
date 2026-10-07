"""HTTP composition root. Business rules remain in prediction and market packages."""

import asyncio
import base64
import hashlib
import json
import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from fastapi import Body, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import release_manifest
from .ai.client import AIClient, ProviderError
from .audit import AuditConflict, AuditStore, redact_text
from .config import AppConfig
from .market import MarketServices
from .market import create_router as market_router
from .runtime import Runtime
from .settings import SettingsConflict, SettingsStore
from .storage.db import Database
from .storage.migrations import status
from .web_security import LocalBoundary, loopback

log = logging.getLogger(__name__)


def identity(config: AppConfig) -> dict:
    manifest = release_manifest()
    result = {
        **manifest,
        "commit": "",
        "buildTime": "",
        "artifactSHA256": "",
        "dirty": True,
        "startedAt": datetime.now(UTC).isoformat(),
        "pid": os.getpid(),
        "pythonExecutable": sys.executable,
        "root": str(config.root),
    }
    release = os.environ.get("STOCK_GOD_RELEASE_DIR")
    if release:
        base = Path(release).resolve()
        raw = (base / "build-manifest.json").read_bytes()
        build = json.loads(raw)
        if build.get("appVersion") != manifest["appVersion"] or not build.get("files"):
            raise ValueError("invalid release manifest")
        for name, digest in build["files"].items():
            path = (base / name).resolve()
            if not path.is_relative_to(base) or not path.is_file():
                raise ValueError("release file missing or outside bundle: " + name)
            with path.open("rb") as source:
                actual = hashlib.file_digest(source, "sha256").hexdigest()
            if actual != digest:
                raise ValueError("release file hash mismatch: " + name)
        result.update(
            commit=build["commit"],
            buildTime=build["buildTime"],
            dirty=build["dirty"],
            artifactSHA256=hashlib.sha256(raw).hexdigest(),
            releaseDirectory=str(base),
        )
    return result


class EventHub:
    def __init__(self):
        self.clients = {}

    async def emit(self, event, payload):
        async def send(socket, lock):
            try:
                async with lock, asyncio.timeout(5):
                    await socket.send_json({"event": event, "payload": payload})
            except (OSError, RuntimeError, TimeoutError):
                self.clients.pop(socket, None)

        await asyncio.gather(*(send(socket, lock) for socket, lock in list(self.clients.items())))

    async def close(self):
        for socket in list(self.clients):
            await socket.close(code=1001)
        self.clients.clear()


def create_app(
    config: AppConfig | None = None,
    *,
    market=None,
    ai_factory=AIClient,
    mailer=None,
    clock=None,
    shutdown=None,
) -> FastAPI:
    from .market.meoz_source import MeozAuctionSource
    from .prediction.core import Conflict, NotFound
    from .prediction.router import create_router as prediction_router
    from .prediction.service import PredictionService

    config = config or AppConfig.from_env()
    if not loopback(config.host) or not 1 <= config.port <= 65535:
        raise ValueError("web listener must use a loopback host and valid port")
    database = Database(config.main_db)
    settings = SettingsStore(database)
    market = market or MarketServices(config)
    audit = AuditStore(database)
    prediction = PredictionService(
        database,
        market,
        settings,
        ai_factory,
        audit,
        clock=clock,
        mailer=mailer,
        auction_source=MeozAuctionSource(database, market, clock=clock),
    )
    runtime, hub = Runtime(prediction, market, settings), EventHub()
    build, ready = (
        identity(config),
        {"migrations": False, "database": False, "services": False, "scheduler": False, "ready": False},
    )

    @asynccontextmanager
    async def lifespan(app):
        checked = await asyncio.to_thread(status, config.main_db, config.minute_db)
        if any(value["pending"] for value in checked.values()):
            raise RuntimeError("database upgrade required; run stock-god db migrate before serving")
        ready.update(migrations=True, database=True)
        settings.initialize()
        await asyncio.to_thread(market.initialize_stock_master)
        # Recovery never resumes retired Research 1 or knowledge jobs.
        audit.recover_replays()
        await prediction.recover(resume=config.scheduler_enabled)
        ready["model"] = prediction.models.health()
        ready["services"] = ready["model"]["ready"]
        scheduler = (
            asyncio.create_task(runtime.run(), name="stock-god:scheduler")
            if config.scheduler_enabled
            else None
        )
        ready.update(scheduler=True, ready=True)
        try:
            yield
        finally:
            ready["ready"] = False
            if scheduler:
                scheduler.cancel()
                await asyncio.gather(scheduler, return_exceptions=True)
            await runtime.close()
            await hub.close()
            market.close()
            database.close()

    app = FastAPI(title="Stock God", version="2.0.0", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.add_middleware(LocalBoundary)
    app.state.config, app.state.settings, app.state.market = config, settings, market
    app.state.prediction, app.state.audit, app.state.runtime = prediction, audit, runtime
    app.state.hub, app.state.readiness, app.state.identity = hub, ready, build
    app.include_router(market_router())
    app.include_router(prediction_router(prediction))

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request, error):
        return JSONResponse({"error": str(error.detail)}, error.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        # Never echo a submitted settings/API-key value through a validation error.
        fields = [".".join(str(part) for part in item["loc"]) + ": " + item["msg"] for item in error.errors()]
        return JSONResponse({"error": "; ".join(fields)}, 400)

    @app.exception_handler(ValueError)
    async def value_error(request, error):
        code = (
            404
            if isinstance(error, NotFound)
            else 409
            if isinstance(error, (SettingsConflict, AuditConflict, Conflict))
            else 400
        )
        return JSONResponse({"error": redact_text(str(error))[0]}, code)

    @app.exception_handler(KeyError)
    async def missing_error(request, error):
        return JSONResponse({"error": str(error.args[0])}, 404)

    @app.exception_handler(Exception)
    async def unexpected_error(request, error):
        log.error("request failed: %s", redact_text(str(error))[0])
        return JSONResponse({"error": "internal service error"}, 500)

    @app.get("/livez", operation_id="getLiveness")
    def live():
        return {"ok": True}

    @app.get("/readyz", operation_id="getReadiness")
    def readiness():
        return JSONResponse({**build, "readiness": ready}, 200 if ready["ready"] else 503)

    @app.get("/api/v1/system/info", operation_id="getSystemInfo")
    def info():
        icon = config.frontend_dist / "appicon.png"
        return {
            "version": build["appVersion"],
            "content": build["commit"] or "development build",
            "icon": "data:image/png;base64," + base64.b64encode(icon.read_bytes()).decode()
            if icon.is_file()
            else "",
        }

    @app.post("/api/v1/system/shutdown", operation_id="shutdownSystem")
    async def stop():
        if shutdown:
            asyncio.get_running_loop().call_later(0.3, shutdown)
        return {"ok": True, "message": "shutdown requested"}

    @app.websocket("/api/v1/events/ws")
    async def events(socket: WebSocket):
        await socket.accept()
        hub.clients[socket] = asyncio.Lock()
        try:
            if ready["ready"]:
                await socket.send_json({"event": "loadingMsg", "payload": "done"})
            while True:
                message = await socket.receive()
                if message["type"] == "websocket.disconnect":
                    break
                if len(message.get("bytes") or (message.get("text") or "").encode()) > 1 << 20:
                    await socket.close(code=1009)
                    break
        except WebSocketDisconnect:
            pass
        finally:
            hub.clients.pop(socket, None)

    @app.get("/api/v1/settings", operation_id="getSettings")
    def get_settings():
        return settings.global_values()

    @app.put("/api/v1/settings", operation_id="updateSettings")
    async def set_settings(payload: Annotated[dict, Body()]):
        settings.save_global(payload)
        await hub.emit("updateSettings", settings.global_values())
        return {"ok": True, "message": "设置已保存"}

    @app.get("/api/v1/prediction/settings", operation_id="getPredictionSettings")
    def get_prediction_settings():
        return settings.load().payload()

    @app.put("/api/v1/prediction/settings", operation_id="updatePredictionSettings")
    def set_prediction_settings(payload: Annotated[dict, Body()]):
        if set(payload) != {"revision", "config", "aiConfigs"}:
            raise ValueError("prediction settings require revision, config and aiConfigs")
        before = settings.load()
        after = settings.save(payload["revision"], payload["config"], payload["aiConfigs"])
        prediction.on_settings_changed(before, after)
        return after.payload()

    @app.get("/api/v1/prediction/ai/configs", operation_id="listPredictionAIConfigs")
    def models():
        return [value for value in settings.load().models if not value["disabled"]]

    @app.post("/api/v1/prediction/ai/configs/test", operation_id="testPredictionAIConfig")
    async def test_model(payload: Annotated[dict, Body()]):
        model_id = payload.get("id")
        if set(payload) != {"id"} or not isinstance(model_id, int) or isinstance(model_id, bool):
            raise ValueError("saved model ID is required")
        snapshot = settings.load()
        model = next((value for value in snapshot.models if value["ID"] == model_id), None)
        if not model:
            raise ValueError("model does not belong to prediction")
        started = time.monotonic()
        result = {
            "success": False,
            "message": "测试失败",
            "protocol": model["apiProtocol"],
            "model": model["modelName"],
            "latencyMs": 0,
            "contentPreview": "",
        }
        try:
            value = await ai_factory([{**model, "disabled": False}], force_config_id=model_id).complete(
                "请只回复 OK"
            )
            result.update(
                success=True, message="测试成功", model=value.model, contentPreview=value.content[:120]
            )
        except ProviderError as error:
            result["message"] = redact_text(str(error))[0]
        result["latencyMs"] = int((time.monotonic() - started) * 1000)
        return result

    def require_run(run_id):
        prediction.get_run(run_id)

    @app.get("/api/v1/prediction/analysis-runs/{id}/audit", operation_id="getPredictionAnalysisRunAudit")
    def get_audit(id: str):
        require_run(id)
        return audit.detail(id)

    @app.get(
        "/api/v1/prediction/analysis-runs/{id}/audit/export", operation_id="exportPredictionAnalysisRunAudit"
    )
    def export_audit(id: str):
        require_run(id)
        return Response(
            audit.export(id),
            media_type="application/zip",
            headers={"Content-Disposition": 'attachment; filename="prediction-audit.zip"'},
        )

    @app.post("/api/v1/prediction/replays", operation_id="createPredictionReplay", status_code=202)
    async def create_replay(payload: Annotated[dict, Body()]):
        raise Conflict("旧AI研究报告已归档，BASE43不支持AI重放")

    @app.get("/api/v1/prediction/replays/{id}", operation_id="getPredictionReplay")
    def get_replay(id: str):
        return audit.get_replay(id)

    @app.api_route("/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    def frontend(path: str, request: Request):
        if path.startswith("api/") or path in {"livez", "readyz"}:
            raise HTTPException(404, "route not found")
        base = config.frontend_dist.resolve()
        target = (base / path).resolve()
        if not target.is_relative_to(base):
            raise HTTPException(404, "file not found")
        if not target.is_file():
            if Path(path).suffix or (path and path.split("/")[0] not in {"prediction", "settings", "about"}):
                raise HTTPException(404, "file not found")
            target = base / "index.html"
        if not target.is_file():
            raise HTTPException(503, "frontend build is missing")
        return FileResponse(
            target,
            headers={"Cache-Control": "no-cache" if target.name == "index.html" else "public, max-age=3600"},
        )

    from .contracts import apply_operation_ids, load_spec

    specification = load_spec()
    apply_operation_ids(app, specification)
    app.openapi = lambda: specification
    return app
