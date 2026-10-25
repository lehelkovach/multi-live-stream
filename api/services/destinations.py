"""Destination persistence helpers shared by the REST router (and later the UI tests)."""

from __future__ import annotations

import json
from datetime import datetime

import redis.asyncio as aioredis
from sqlmodel import Session, col, select

from core.crypto import SecretBox
from core.models import Destination, DestinationStatus, utcnow
from core.redis import dest_status_key


def mask_key(key: str) -> str:
    if not key:
        return ""
    tail = key[-3:] if len(key) > 6 else ""
    return "••••" + tail


def list_destinations(session: Session) -> list[Destination]:
    return list(session.exec(select(Destination).order_by(col(Destination.id))).all())


def set_stream_key(dest: Destination, box: SecretBox, key: str) -> None:
    dest.stream_key_encrypted = box.encrypt(key) if key else ""
    dest.updated_at = utcnow()


def get_stream_key(dest: Destination, box: SecretBox) -> str:
    return box.decrypt(dest.stream_key_encrypted) if dest.stream_key_encrypted else ""


async def load_runtime(redis: aioredis.Redis, ids: list[int]) -> dict[int, dict[str, object]]:
    """Runtime status blobs written by the relay (missing => relay hasn't reported)."""
    if not ids:
        return {}
    raw = await redis.mget([dest_status_key(i) for i in ids])
    out: dict[int, dict[str, object]] = {}
    for dest_id, blob in zip(ids, raw, strict=True):
        if not blob:
            continue
        try:
            parsed = json.loads(blob)
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, dict):
            out[dest_id] = parsed
    return out


def effective_status(dest: Destination, runtime: dict[str, object] | None) -> str:
    if not dest.enabled:
        return DestinationStatus.DISABLED
    if runtime and isinstance(runtime.get("status"), str):
        return str(runtime["status"])
    return dest.status or DestinationStatus.IDLE


def parse_iso(value: object) -> datetime | None:
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None
