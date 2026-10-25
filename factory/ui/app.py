"""FastAPI app: JSON API + Server-Sent Events + the static frontend (no build step).

Security for a loopback-only tool: the server binds 127.0.0.1 (enforced by config
validation). Requests must carry a loopback Host header, which defeats DNS
rebinding. State-changing requests must be JSON, and an Origin header (if sent)
must be loopback too, so a malicious web page cannot drive the factory with a
cross-site form post.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, Query, Request
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    PlainTextResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from factory import __version__
from factory.config.loader import LiveConfig
from factory.ui.service import BadRequest, FactoryService, NotFound

STATIC_DIR = Path(__file__).with_name("static")
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_UNSAFE = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class StartRun(BaseModel):
    request: str = Field(min_length=1, max_length=20_000)
    pipeline: str = "default"
    repo: str | None = None
    model: str | None = None


class Decision(BaseModel):
    decision: str
    comment: str | None = Field(default=None, max_length=20_000)
    approval_id: int | None = None


class ModelChoice(BaseModel):
    alias: str | None = None  # None/"" clears the override


def _hostname(value: str) -> str:
    if value.startswith("["):  # [::1]:8765
        return value[1 : value.find("]")]
    return value.rsplit(":", 1)[0] if value.count(":") == 1 else value


def create_app(
    live: LiveConfig,
    *,
    service: FactoryService | None = None,
    allowed_hosts: frozenset[str] = LOOPBACK_HOSTS,
    resume_interrupted: bool = False,
    watch: bool = True,
) -> FastAPI:
    svc = service or FactoryService(live)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if watch:
            svc.watcher.start()
        if resume_interrupted:
            svc.runs.resume_interrupted()
        try:
            yield
        finally:
            svc.watcher.stop()
            svc.orch.resolver.close()

    app = FastAPI(
        title="Software Factory", version=__version__, docs_url="/api/docs", lifespan=lifespan
    )
    app.state.service = svc

    @app.middleware("http")
    async def loopback_only(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        host = _hostname(request.headers.get("host", ""))
        if host not in allowed_hosts:
            return PlainTextResponse("forbidden host", status_code=403)
        if request.method in _UNSAFE:
            origin = request.headers.get("origin")
            if origin and _hostname(urlsplit(origin).netloc) not in allowed_hosts:
                return PlainTextResponse("forbidden origin", status_code=403)
            if not request.headers.get("content-type", "").startswith("application/json"):
                return PlainTextResponse("JSON body required", status_code=415)
        return await call_next(request)

    @app.exception_handler(NotFound)
    async def not_found(_: Request, exc: NotFound) -> JSONResponse:
        return JSONResponse({"error": str(exc)}, status_code=404)

    @app.exception_handler(BadRequest)
    async def bad_request(_: Request, exc: BadRequest) -> JSONResponse:
        return JSONResponse({"error": str(exc)}, status_code=400)

    # ------------------------------------------------------------------ frontend

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})

    # -------------------------------------------------------------------- status

    @app.get("/api/status")
    def status() -> dict[str, Any]:
        return svc.status()

    @app.get("/api/models")
    def models() -> dict[str, Any]:
        return svc.models()

    @app.post("/api/models/default")
    def set_default_model(choice: ModelChoice) -> dict[str, Any]:
        return svc.set_default_model(choice.alias or None)

    @app.post("/api/models/agents/{agent}")
    def set_agent_model(agent: str, choice: ModelChoice) -> dict[str, Any]:
        return svc.set_agent_model(agent, choice.alias or None)

    @app.get("/api/pipelines")
    def pipelines() -> list[dict[str, Any]]:
        return svc.pipelines()

    # ---------------------------------------------------------------------- runs

    @app.get("/api/runs")
    def runs(limit: int = Query(100, ge=1, le=1000)) -> list[dict[str, Any]]:
        return svc.list_runs(limit)

    @app.post("/api/runs", status_code=201)
    def start_run(body: StartRun) -> dict[str, Any]:
        return svc.create_run(body.request, body.pipeline, body.repo, body.model)

    @app.get("/api/runs/{run_id}")
    def run_detail(run_id: str) -> dict[str, Any]:
        return svc.run_detail(run_id)

    @app.get("/api/runs/{run_id}/events")
    def run_events(run_id: str, limit: int = Query(1000, ge=1, le=20000)) -> list[dict[str, Any]]:
        return svc.events(run_id, limit)

    @app.get("/api/runs/{run_id}/artifacts/{name:path}")
    def artifact(run_id: str, name: str) -> PlainTextResponse:
        return PlainTextResponse(svc.artifact(run_id, name))

    @app.get("/api/runs/{run_id}/changes")
    def changes(run_id: str) -> dict[str, Any]:
        return svc.changes(run_id)

    @app.get("/api/runs/{run_id}/diff")
    def diff(run_id: str, ref: str = "integration") -> PlainTextResponse:
        return PlainTextResponse(svc.diff(run_id, ref))

    @app.post("/api/runs/{run_id}/decision")
    def decide(run_id: str, body: Decision) -> dict[str, Any]:
        return svc.decide(run_id, body.decision, body.comment, body.approval_id)

    @app.post("/api/runs/{run_id}/resume")
    def resume(run_id: str) -> dict[str, Any]:
        return svc.resume(run_id)

    @app.post("/api/runs/{run_id}/model")
    def run_model(run_id: str, choice: ModelChoice) -> dict[str, Any]:
        return svc.set_run_model(run_id, choice.alias or None)

    # ------------------------------------------------------------------- stream

    @app.get("/api/stream")
    async def stream(
        run: str | None = None, limit: int | None = Query(None, ge=1)
    ) -> StreamingResponse:
        return StreamingResponse(
            svc.broadcaster.stream(run, limit),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    return app
