"""Chat account helpers shared by the REST router and the WebSocket snapshot."""

from __future__ import annotations

import json
from typing import Any

import redis.asyncio as aioredis
from sqlmodel import Session, col, select

from api.schemas import ChatAccountOut, ChatAccountRuntime, ChatMessageOut
from chat.base import Credentials
from core.crypto import DecryptError, SecretBox
from core.models import ChatAccount, ChatAccountStatus
from core.models import ChatMessage as ChatMessageRow
from core.redis import chat_status_key


def list_accounts(session: Session) -> list[ChatAccount]:
    return list(session.exec(select(ChatAccount).order_by(col(ChatAccount.id))).all())


def credentials_of(row: ChatAccount, box: SecretBox) -> Credentials:
    if not row.credentials_encrypted:
        return Credentials()
    try:
        return Credentials.from_json(box.decrypt(row.credentials_encrypted))
    except (DecryptError, ValueError):
        return Credentials()


async def load_chat_runtime(redis: aioredis.Redis, ids: list[int]) -> dict[int, dict[str, Any]]:
    if not ids:
        return {}
    raw = await redis.mget([chat_status_key(i) for i in ids])
    out: dict[int, dict[str, Any]] = {}
    for account_id, blob in zip(ids, raw, strict=True):
        if not blob:
            continue
        try:
            parsed = json.loads(blob)
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, dict):
            out[account_id] = parsed
    return out


def account_out(row: ChatAccount, box: SecretBox, runtime: dict[str, Any] | None) -> ChatAccountOut:
    assert row.id is not None
    creds = credentials_of(row, box)
    pages = [
        {"id": str(p.get("id")), "name": str(p.get("name", ""))}
        for p in creds.extra.get("pages") or []
    ]
    rt = ChatAccountRuntime.model_validate(runtime) if runtime else None
    status: str
    if not row.enabled:
        status = ChatAccountStatus.DISABLED
    elif rt is not None:
        status = rt.status
    else:
        status = row.status or ChatAccountStatus.IDLE
    return ChatAccountOut(
        id=row.id,
        platform=row.platform,
        display_name=row.display_name,
        channel_ref=row.channel_ref,
        external_id=row.external_id,
        enabled=row.enabled,
        status=status,
        last_error=(rt.last_error if rt else None) or row.last_error,
        has_credentials=bool(creds.access_token or pages),
        pages=pages,
        runtime=rt,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def accounts_view(
    session: Session, redis: aioredis.Redis, box: SecretBox
) -> list[ChatAccountOut]:
    rows = list_accounts(session)
    runtime = await load_chat_runtime(redis, [r.id for r in rows if r.id is not None])
    return [account_out(r, box, runtime.get(r.id or -1)) for r in rows]


def message_out(row: ChatMessageRow) -> ChatMessageOut:
    return ChatMessageOut(
        id=row.id,
        platform=row.platform,
        account_id=row.chat_account_id,
        author=row.author,
        author_id=row.author_id,
        text=row.text,
        timestamp=row.timestamp,
        is_own=row.is_own,
        kind=row.kind,
        badges=list(row.badges or []),
    )
