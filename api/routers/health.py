"""Health endpoints.

``/healthz``        liveness, no dependencies, unauthenticated (used by Compose/Caddy).
``/api/v1/health``  readiness-style component report, authenticated.
"""

from __future__ import annotations

import asyncio
import time

import httpx
from fastapi import APIRouter, Request
from sqlalchemy import text

from api.deps import RedisDep, SettingsDep, UserDep
from api.schemas import ComponentHealth, HealthOut, HealthzOut
from core import __version__
from core.db import sqlite_writable
from core.redis import KEY_RELAY_HEARTBEAT

router = APIRouter()

_TIMEOUT = 2.0
READ_ONLY_HINT = (
    "database is read-only: the file in the app_data volume is not writable by the app "
    "user (uid 10001); restore backups with `make restore`"
)


@router.get("/healthz", response_model=HealthzOut, tags=["health"])
async def healthz() -> HealthzOut:
    return HealthzOut(version=__version__)


async def _check_db(request: Request) -> ComponentHealth:
    start = time.perf_counter()

    def _ping() -> bool | None:
        engine = request.app.state.engine
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return sqlite_writable(engine)

    try:
        writable = await asyncio.wait_for(asyncio.to_thread(_ping), _TIMEOUT)
    except Exception as exc:
        return ComponentHealth(ok=False, detail=f"{type(exc).__name__}: {exc}"[:200])
    latency = round((time.perf_counter() - start) * 1000, 2)
    if writable is False:
        return ComponentHealth(ok=False, detail=READ_ONLY_HINT, latency_ms=latency)
    return ComponentHealth(ok=True, latency_ms=latency)


async def _check_redis(redis: RedisDep) -> tuple[ComponentHealth, ComponentHealth]:
    start = time.perf_counter()
    try:
        await asyncio.wait_for(redis.ping(), _TIMEOUT)
        hb_raw = await asyncio.wait_for(redis.get(KEY_RELAY_HEARTBEAT), _TIMEOUT)
    except Exception as exc:
        bad = ComponentHealth(ok=False, detail=f"{type(exc).__name__}: {exc}"[:200])
        return bad, ComponentHealth(ok=False, detail="redis unavailable")
    redis_health = ComponentHealth(
        ok=True, latency_ms=round((time.perf_counter() - start) * 1000, 2)
    )
    if hb_raw is None:
        return redis_health, ComponentHealth(ok=False, detail="no heartbeat yet")
    try:
        age = time.time() - float(hb_raw)
    except ValueError:
        return redis_health, ComponentHealth(ok=False, detail="bad heartbeat value")
    ok = age < 15
    return redis_health, ComponentHealth(ok=ok, detail=f"last heartbeat {age:.1f}s ago")


async def _check_mediamtx(settings: SettingsDep) -> ComponentHealth:
    start = time.perf_counter()
    url = settings.mediamtx_api_url.rstrip("/") + "/v3/paths/list"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(url)
        resp.raise_for_status()
    except Exception as exc:
        return ComponentHealth(ok=False, detail=f"{type(exc).__name__}: {exc}"[:200])
    return ComponentHealth(ok=True, latency_ms=round((time.perf_counter() - start) * 1000, 2))


@router.get("/api/v1/health", response_model=HealthOut, tags=["health"])
async def health(
    request: Request, _user: UserDep, settings: SettingsDep, redis: RedisDep
) -> HealthOut:
    db, (redis_health, relay), mediamtx = await asyncio.gather(
        _check_db(request), _check_redis(redis), _check_mediamtx(settings)
    )
    components = {"db": db, "redis": redis_health, "mediamtx": mediamtx, "relay": relay}
    status = "ok" if all(c.ok for c in components.values()) else "degraded"
    return HealthOut(status=status, version=__version__, components=components)
