"""Persist normalized chat messages and publish them + account status to Redis."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import redis.asyncio as aioredis
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError

from chat.base import ChatMessage
from core.db import session_scope
from core.events import publish_event
from core.logging import get_logger
from core.models import ChatAccount
from core.models import ChatMessage as ChatMessageRow
from core.redis import (
    CHANNEL_CHAT,
    DEST_STATUS_TTL,
    EVENT_CHAT_ACCOUNT_STATUS,
    EVENT_CHAT_MESSAGE,
    KEY_CHAT_HEARTBEAT,
    chat_status_key,
)

log = get_logger(__name__)

REPUBLISH_EVERY = 15.0  # seconds


class ChatStore:
    def __init__(self, redis: aioredis.Redis, engine: Engine | None) -> None:
        self._redis = redis
        self._engine = engine
        self._last_status: dict[int, tuple[str, str | None]] = {}
        self._last_published: dict[int, float] = {}

    async def heartbeat(self) -> None:
        await self._redis.set(KEY_CHAT_HEARTBEAT, str(time.time()), ex=60)

    async def save(self, message: ChatMessage) -> bool:
        """Insert (idempotent on platform message id) and broadcast. Returns True if new."""
        if self._engine is not None:
            try:
                is_new = await asyncio.to_thread(self._insert, message)
            except Exception as exc:
                log.warning("chat.persist_failed", key=message.key, error=str(exc))
                is_new = True
            if not is_new:
                return False
        payload = {"type": EVENT_CHAT_MESSAGE, **message.to_event()}
        try:
            await self._redis.publish(CHANNEL_CHAT, json.dumps(payload, default=str))
        except Exception as exc:
            log.warning("chat.publish_failed", key=message.key, error=str(exc))
        return True

    def _insert(self, message: ChatMessage) -> bool:
        assert self._engine is not None
        with session_scope(self._engine) as session:
            if session.get(ChatMessageRow, message.key) is not None:
                return False
            session.add(
                ChatMessageRow(
                    id=message.key,
                    platform=message.platform,
                    chat_account_id=message.account_id,
                    author=message.author[:200],
                    author_id=message.author_id[:200],
                    text=message.text,
                    timestamp=message.timestamp,
                    is_own=message.is_own,
                    kind=message.kind,
                    badges=list(message.badges),
                    raw=message.raw,
                )
            )
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                return False
        return True

    async def set_status(
        self,
        account_id: int,
        *,
        platform: str,
        name: str,
        status: str,
        detail: str | None = None,
        error: str | None = None,
    ) -> None:
        blob: dict[str, Any] = {
            "id": account_id,
            "platform": platform,
            "name": name,
            "status": status,
            "detail": detail,
            "last_error": error,
            "updated_at": time.time(),
        }
        await self._redis.set(chat_status_key(account_id), json.dumps(blob), ex=DEST_STATUS_TTL * 2)
        previous = self._last_status.get(account_id)
        current = (status, error)
        now = time.monotonic()
        if previous != current:
            self._last_status[account_id] = current
            self._last_published[account_id] = now
            await publish_event(self._redis, EVENT_CHAT_ACCOUNT_STATUS, **blob)
            await self._persist_status(account_id, status, error)
        elif now - self._last_published.get(account_id, 0.0) > REPUBLISH_EVERY:
            # unchanged, but a dashboard that loaded mid-transition self-heals
            self._last_published[account_id] = now
            await publish_event(self._redis, EVENT_CHAT_ACCOUNT_STATUS, **blob)

    async def forget(self, account_id: int) -> None:
        self._last_status.pop(account_id, None)
        await self._redis.delete(chat_status_key(account_id))

    async def _persist_status(self, account_id: int, status: str, error: str | None) -> None:
        if self._engine is None:
            return

        def _write() -> None:
            assert self._engine is not None
            with session_scope(self._engine) as session:
                row = session.get(ChatAccount, account_id)
                if row is None:
                    return
                row.status = status
                row.last_error = error[:2000] if error else None
                session.add(row)

        try:
            await asyncio.to_thread(_write)
        except Exception as exc:
            log.warning("chat.status_persist_failed", account_id=account_id, error=str(exc))
