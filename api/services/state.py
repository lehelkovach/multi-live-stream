"""Shared read models for REST + WebSocket: destination list and relay state."""

from __future__ import annotations

import json
import time

import redis.asyncio as aioredis
from sqlmodel import Session

from api.schemas import DestinationOut, DestinationRuntime, IngestState, RelayState
from api.services.destinations import (
    effective_status,
    get_stream_key,
    list_destinations,
    load_runtime,
    mask_key,
)
from core.app_settings import relay_enabled
from core.crypto import SecretBox
from core.models import Destination, DestinationStatus
from core.redis import KEY_INGEST_STATE, KEY_RELAY_HEARTBEAT


def destination_out(
    dest: Destination, box: SecretBox, runtime: dict[str, object] | None
) -> DestinationOut:
    assert dest.id is not None
    rt = DestinationRuntime.model_validate(runtime) if runtime else None
    return DestinationOut(
        id=dest.id,
        name=dest.name,
        platform=dest.platform,  # stored as str, validated on write
        rtmp_url=dest.rtmp_url,
        enabled=dest.enabled,
        profile=dest.profile,
        ffmpeg_args=dest.ffmpeg_args,
        stream_key_masked=mask_key(get_stream_key(dest, box)),
        status=effective_status(dest, runtime),
        last_error=(rt.last_error if rt else None) or dest.last_error,
        runtime=rt,
        created_at=dest.created_at,
        updated_at=dest.updated_at,
    )


async def destinations_view(
    session: Session, redis: aioredis.Redis, box: SecretBox
) -> list[DestinationOut]:
    rows = list_destinations(session)
    runtime = await load_runtime(redis, [r.id for r in rows if r.id is not None])
    return [destination_out(r, box, runtime.get(r.id or -1)) for r in rows]


async def heartbeat_age(redis: aioredis.Redis) -> float | None:
    raw = await redis.get(KEY_RELAY_HEARTBEAT)
    if raw is None:
        return None
    try:
        return max(0.0, time.time() - float(raw))
    except ValueError:
        return None


async def ingest_state(redis: aioredis.Redis) -> IngestState | None:
    raw = await redis.get(KEY_INGEST_STATE)
    if not raw:
        return None
    try:
        return IngestState.model_validate(json.loads(raw))
    except ValueError:
        return None


async def relay_view(session: Session, redis: aioredis.Redis) -> RelayState:
    rows = list_destinations(session)
    runtime = await load_runtime(redis, [r.id for r in rows if r.id is not None])
    statuses = [effective_status(r, runtime.get(r.id or -1)) for r in rows]
    age = await heartbeat_age(redis)
    return RelayState(
        enabled=relay_enabled(session),
        relay_alive=age is not None and age < 15,
        heartbeat_age_s=age,
        ingest=await ingest_state(redis),
        live_count=sum(1 for s in statuses if s == DestinationStatus.LIVE),
        enabled_count=sum(1 for r in rows if r.enabled),
    )
