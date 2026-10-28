"""FastAPI application factory.

Run with::

    uvicorn api.main:create_app --factory --host 0.0.0.0 --port 8000 --proxy-headers

The built dashboard (``web/dist``) is served from ``/`` when present, so a single
container can serve both API and UI; in Compose, Caddy fronts ``web`` + ``api``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import APIRouter, FastAPI
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from api.auth import LoginRateLimiter, PasswordVerifier, SessionSigner
from api.routers import auth, chat, destinations, health, ingest, metrics, relay, ws
from api.routers.health import READ_ONLY_HINT
from chat.kick_webhook import PublicKeyCache
from core import __version__
from core.crypto import SecretBox
from core.db import init_db, make_engine, sqlite_writable
from core.logging import configure_logging, get_logger
from core.redis import make_redis
from core.settings import Settings, get_settings

log = get_logger(__name__)

# Every router the app serves (the edge config test in tests/unit/test_deploy_config.py
# checks that Caddy proxies each of their top-level paths to the API).
ROUTERS: tuple[APIRouter, ...] = (
    health.router,
    auth.router,
    ingest.router,
    destinations.router,
    relay.router,
    chat.protected,
    chat.router,
    chat.webhooks,
    ws.router,
    metrics.router,
)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_format)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = settings
        app.state.secret_box = SecretBox(settings.secret_keys)
        app.state.verifier = PasswordVerifier.from_settings(settings)
        app.state.signer = SessionSigner(
            settings.session_secret.get_secret_value(), settings.session_max_age
        )
        app.state.login_limiter = LoginRateLimiter()
        app.state.engine = make_engine(settings.database_url)
        init_db(app.state.engine)
        try:
            if sqlite_writable(app.state.engine) is False:
                log.error("db.read_only", database=settings.database_url, hint=READ_ONLY_HINT)
        except Exception as exc:  # a diagnostic must never block startup
            log.warning("db.write_probe_failed", error=str(exc))
        app.state.redis = make_redis(settings.redis_url)
        app.state.http = httpx.AsyncClient(headers={"User-Agent": "OmniStream/0.1 (+api)"})
        app.state.kick_public_key = PublicKeyCache()
        log.info("api.started", version=__version__, domain=settings.domain)
        try:
            yield
        finally:
            await app.state.http.aclose()
            await app.state.redis.aclose()
            app.state.engine.dispose()
            log.info("api.stopped")

    app = FastAPI(
        title="OmniStream API",
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redoc_url=None,
    )
    for router in ROUTERS:
        app.include_router(router)
    _mount_web(app, Path(settings.web_dist_dir))
    return app


def _mount_web(app: FastAPI, dist: Path) -> None:
    """Serve the built SPA (if present) with an index.html fallback for client routes."""
    index = dist / "index.html"
    if not index.is_file():
        log.info("web.dist_missing", path=str(dist))
        return
    assets = dist / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False, response_model=None)
    def spa(full_path: str) -> Response:  # sync on purpose: file IO runs in the threadpool
        if full_path.startswith(("api/", "ws/", "internal/")):
            return JSONResponse({"detail": "not found"}, status_code=404)
        candidate = (dist / full_path).resolve()
        if full_path and candidate.is_file() and dist.resolve() in candidate.parents:
            return FileResponse(candidate)
        return FileResponse(index)
