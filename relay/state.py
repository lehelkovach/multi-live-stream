"""Publish relay/worker state to Redis (live view) and the DB (persisted status)."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import redis.asyncio as aioredis
from sqlalchemy import Engine

from core.db import session_scope
from core.events import publish_event
from core.logging import get_logger
from core.models import Destination, DestinationStatus
from core.redis import (
    DEST_STATUS_TTL,
    EVENT_DEST_STATS,
    EVENT_DEST_STATUS,
    EVENT_INGEST_STATE,
    KEY_INGEST_STATE,
    KEY_RELAY_HEARTBEAT,
    dest_status_key,
)
from relay.worker import WorkerStatus

log = get_logger(__name__)


class StatusStore:
    def __init__(self, redis: aioredis.Redis, engine: Engine | None) -> None:
        self._redis = redis
        self._engine = engine
        self._last_status: dict[int, str] = {}
        self._last_ingest_live: bool | None = None

    async def heartbeat(self) -> None:
        await self._redis.set(KEY_RELAY_HEARTBEAT, str(time.time()), ex=60)

    async def write_worker(self, status: WorkerStatus, changed: bool) -> None:
        """Callback from a worker: refresh the live blob, broadcast, persist on change."""
        if status.status == DestinationStatus.LIVE:
            status.last_error = None
        blob = status.to_dict()
        await self._redis.set(dest_status_key(status.id), json.dumps(blob), ex=DEST_STATUS_TTL)
        previous = self._last_status.get(status.id)
        self._last_status[status.id] = status.status
        if changed or previous != status.status:
            await publish_event(self._redis, EVENT_DEST_STATUS, **blob)
            await self._persist(status.id, status.status, status.last_error)
        else:
            await publish_event(self._redis, EVENT_DEST_STATS, **blob)

    async def write_inactive(
        self, dest_id: int, name: str, status: str, detail: str | None = None
    ) -> None:
        """Destinations without a worker: idle (no input / relay stopped) or disabled."""
        blob: dict[str, Any] = WorkerStatus(
            id=dest_id, name=name, status=status, detail=detail
        ).to_dict()
        await self._redis.set(dest_status_key(dest_id), json.dumps(blob), ex=DEST_STATUS_TTL)
        if self._last_status.get(dest_id) != status:
            self._last_status[dest_id] = status
            await publish_event(self._redis, EVENT_DEST_STATUS, **blob)
            await self._persist(dest_id, status, None)

    async def forget(self, dest_id: int) -> None:
        self._last_status.pop(dest_id, None)
        await self._redis.delete(dest_status_key(dest_id))

    async def write_ingest(self, state: dict[str, Any]) -> None:
        await self._redis.set(KEY_INGEST_STATE, json.dumps(state), ex=DEST_STATUS_TTL)
        live = bool(state.get("live"))
        if live or self._last_ingest_live != live:
            await publish_event(self._redis, EVENT_INGEST_STATE, **state)
        self._last_ingest_live = live

    async def _persist(self, dest_id: int, status: str, last_error: str | None) -> None:
        if self._engine is None:
            return

        def _write() -> None:
            assert self._engine is not None
            with session_scope(self._engine) as session:
                dest = session.get(Destination, dest_id)
                if dest is None:
                    return
                dest.status = status
                if status == DestinationStatus.LIVE:
                    dest.last_error = None
                elif last_error is not None:
                    dest.last_error = last_error[:2000]
                session.add(dest)

        try:
            await asyncio.to_thread(_write)
        except Exception as exc:
            log.warning("status.persist_failed", dest_id=dest_id, error=str(exc))
