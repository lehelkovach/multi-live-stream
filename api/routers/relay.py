"""Global relay switch, ingest status and the OBS connection details."""

from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import APIRouter, Depends

from api.deps import DbDep, RedisDep, SettingsDep, current_user
from api.schemas import IngestInfo, OkOut, RelayState
from api.services.state import relay_view
from core.app_settings import set_relay_enabled
from core.events import publish_event
from core.logging import get_logger
from core.redis import EVENT_RELAY_CHANGED

router = APIRouter(prefix="/api/v1/relay", tags=["relay"], dependencies=[Depends(current_user)])
log = get_logger(__name__)


@router.get("", response_model=RelayState)
async def state(session: DbDep, redis: RedisDep) -> RelayState:
    return await relay_view(session, redis)


@router.post("/start", response_model=OkOut)
async def start(session: DbDep, redis: RedisDep) -> OkOut:
    set_relay_enabled(session, True)
    session.commit()
    log.info("relay.enabled")
    await publish_event(redis, EVENT_RELAY_CHANGED, enabled=True)
    return OkOut()


@router.post("/stop", response_model=OkOut)
async def stop(session: DbDep, redis: RedisDep) -> OkOut:
    set_relay_enabled(session, False)
    session.commit()
    log.info("relay.disabled")
    await publish_event(redis, EVENT_RELAY_CHANGED, enabled=False)
    return OkOut()


@router.get("/ingest", response_model=IngestInfo)
async def ingest_info(settings: SettingsDep) -> IngestInfo:
    """What to type into OBS. Admin-only; served over HTTPS."""
    host = urlsplit(f"//{settings.domain}").hostname or settings.domain
    key = settings.ingest_stream_key.get_secret_value()
    path = settings.ingest_path
    return IngestInfo(
        rtmp_server=f"rtmp://{host}:1935/{settings.ingest_path_prefix}",
        srt_url=f"srt://{host}:8890?streamid=publish:{path}",
        stream_key=key,
        path=path,
    )
