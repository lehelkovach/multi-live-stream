"""Publish JSON events on the shared Redis channel (best effort, never raises)."""

from __future__ import annotations

import json
from typing import Any

import redis.asyncio as aioredis

from core.logging import get_logger
from core.redis import CHANNEL_EVENTS

log = get_logger(__name__)


async def publish_event(redis: aioredis.Redis, event_type: str, **payload: Any) -> None:
    message = json.dumps({"type": event_type, **payload}, default=str)
    try:
        await redis.publish(CHANNEL_EVENTS, message)
    except Exception as exc:
        log.warning("event.publish_failed", type=event_type, error=str(exc))
