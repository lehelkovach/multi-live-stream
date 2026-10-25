"""Chat accounts, history, OAuth connect flows and the Kick webhook."""

from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy import update
from sqlmodel import col, select

from api.deps import DbDep, RedisDep, SecretBoxDep, SettingsDep, current_user
from api.schemas import (
    ChatAccountCreate,
    ChatAccountOut,
    ChatAccountUpdate,
    ChatMessageOut,
    ChatPlatformInfo,
    OAuthStartOut,
)
from api.services.chat import (
    account_out,
    accounts_view,
    credentials_of,
    load_chat_runtime,
    message_out,
)
from chat import kick_webhook, oauth
from chat.base import AuthError, ConfigError, Credentials
from core.events import publish_event
from core.logging import get_logger
from core.models import ChatAccount, ChatPlatform, utcnow
from core.models import ChatMessage as ChatMessageRow
from core.redis import (
    CHANNEL_WEBHOOKS_KICK,
    EVENT_CHAT_ACCOUNTS_CHANGED,
    KEY_OAUTH_STATE,
    KEY_WEBHOOK_SEEN,
)

log = get_logger(__name__)
router = APIRouter(prefix="/api/v1/chat", tags=["chat"])
protected = APIRouter(prefix="/api/v1/chat", tags=["chat"], dependencies=[Depends(current_user)])
webhooks = APIRouter(prefix="/api/v1/webhooks", tags=["webhooks"])

PLATFORM_LABELS = {
    "twitch": "Twitch",
    "youtube": "YouTube",
    "kick": "Kick",
    "facebook": "Facebook",
    "fake": "Fake (demo)",
}
MANUAL_HINTS = {
    "twitch": (
        "User access token with user:read:chat (+ user:write:chat). "
        "channel_ref = channel login (optional)."
    ),
    "youtube": (
        "OAuth access + refresh token with the youtube.force-ssl scope. "
        "channel_ref = channel id (optional)."
    ),
    "kick": (
        "User access token with chat:write + events:subscribe; "
        "the webhook URL must be set in the Kick app."
    ),
    "facebook": "Page access token. channel_ref = Page id.",
    "fake": "No credentials needed.",
}


def _get_or_404(session: DbDep, account_id: int) -> ChatAccount:
    row = session.get(ChatAccount, account_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="chat account not found")
    return row


def _platform_allowed(settings: SettingsDep, platform: str) -> None:
    if platform == ChatPlatform.FAKE and not settings.allow_fake_chat:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, detail="fake platform is disabled"
        )


# ---- platforms & accounts -------------------------------------------------------


@protected.get("/platforms", response_model=list[ChatPlatformInfo])
async def platforms(settings: SettingsDep) -> list[ChatPlatformInfo]:
    out = []
    for p in ChatPlatform:
        if p == ChatPlatform.FAKE and not settings.allow_fake_chat:
            continue
        out.append(
            ChatPlatformInfo(
                platform=p.value,
                label=PLATFORM_LABELS[p.value],
                oauth_configured=p.value in oauth.PROVIDERS
                and oauth.is_configured(settings, p.value),
                manual_hint=MANUAL_HINTS[p.value],
            )
        )
    return out


@protected.get("/accounts", response_model=list[ChatAccountOut])
async def list_chat_accounts(
    session: DbDep, redis: RedisDep, box: SecretBoxDep
) -> list[ChatAccountOut]:
    return await accounts_view(session, redis, box)


@protected.post("/accounts", response_model=ChatAccountOut, status_code=status.HTTP_201_CREATED)
async def create_chat_account(
    body: ChatAccountCreate,
    session: DbDep,
    redis: RedisDep,
    box: SecretBoxDep,
    settings: SettingsDep,
) -> ChatAccountOut:
    _platform_allowed(settings, body.platform)
    creds = Credentials(
        access_token=body.access_token.strip(),
        refresh_token=body.refresh_token.strip(),
        extra=dict(body.extra),
    )
    row = ChatAccount(
        platform=body.platform,
        display_name=body.display_name.strip() or PLATFORM_LABELS[body.platform],
        channel_ref=body.channel_ref.strip(),
        enabled=body.enabled,
        credentials_encrypted=box.encrypt(creds.to_json()),
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    log.info("chat_account.created", id=row.id, platform=row.platform)
    await publish_event(redis, EVENT_CHAT_ACCOUNTS_CHANGED, id=row.id, op="create")
    return account_out(row, box, None)


@protected.patch("/accounts/{account_id}", response_model=ChatAccountOut)
async def update_chat_account(
    account_id: int, body: ChatAccountUpdate, session: DbDep, redis: RedisDep, box: SecretBoxDep
) -> ChatAccountOut:
    row = _get_or_404(session, account_id)
    changes = body.model_dump(exclude_unset=True)
    creds = credentials_of(row, box)
    creds_changed = False
    for key in ("access_token", "refresh_token"):
        if changes.get(key) is not None:
            setattr(creds, key, str(changes[key]).strip())
            creds_changed = True
    if changes.get("extra") is not None:
        creds.extra.update(dict(changes["extra"]))
        creds_changed = True
    if creds_changed:
        row.credentials_encrypted = box.encrypt(creds.to_json())
    for key in ("display_name", "channel_ref", "enabled"):
        if key in changes and changes[key] is not None:
            setattr(
                row, key, changes[key].strip() if isinstance(changes[key], str) else changes[key]
            )
    row.updated_at = utcnow()
    session.add(row)
    session.commit()
    session.refresh(row)
    await publish_event(redis, EVENT_CHAT_ACCOUNTS_CHANGED, id=row.id, op="update")
    runtime = await load_chat_runtime(redis, [account_id])
    return account_out(row, box, runtime.get(account_id))


@protected.delete("/accounts/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_chat_account(account_id: int, session: DbDep, redis: RedisDep) -> Response:
    row = _get_or_404(session, account_id)
    # keep the history, just detach it from the account
    session.exec(
        update(ChatMessageRow)
        .where(col(ChatMessageRow.chat_account_id) == account_id)
        .values(chat_account_id=None)
    )
    session.delete(row)
    session.commit()
    await publish_event(redis, EVENT_CHAT_ACCOUNTS_CHANGED, id=account_id, op="delete")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---- history ------------------------------------------------------------------


@protected.get("/messages", response_model=list[ChatMessageOut])
async def messages(
    session: DbDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    before: datetime | None = None,
    platform: str | None = None,
    account_id: int | None = None,
) -> list[ChatMessageOut]:
    """Most recent messages (oldest first within the page). ``before`` pages backwards."""
    stmt = select(ChatMessageRow)
    if before is not None:
        stmt = stmt.where(col(ChatMessageRow.timestamp) < before)
    if platform:
        stmt = stmt.where(ChatMessageRow.platform == platform)
    if account_id is not None:
        stmt = stmt.where(ChatMessageRow.chat_account_id == account_id)
    stmt = stmt.order_by(col(ChatMessageRow.timestamp).desc(), col(ChatMessageRow.id).desc()).limit(
        limit
    )
    rows = list(session.exec(stmt).all())
    rows.reverse()
    return [message_out(r) for r in rows]


# ---- OAuth --------------------------------------------------------------------


@protected.get("/oauth/{platform}/start", response_model=OAuthStartOut)
async def oauth_start(platform: str, settings: SettingsDep, redis: RedisDep) -> OAuthStartOut:
    if platform not in oauth.PROVIDERS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="unknown platform")
    try:
        state = oauth.new_state()
        verifier: str | None = None
        challenge: str | None = None
        if oauth.PROVIDERS[platform].pkce:
            verifier, challenge = oauth.new_pkce()
        url = oauth.authorize_url(settings, platform, state, challenge)
    except ConfigError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await redis.set(
        KEY_OAUTH_STATE.format(state=state),
        json.dumps({"platform": platform, "code_verifier": verifier, "created": time.time()}),
        ex=600,
    )
    return OAuthStartOut(url=url)


@router.get("/oauth/{platform}/callback", include_in_schema=False)
async def oauth_callback(
    platform: str,
    request: Request,
    session: DbDep,
    redis: RedisDep,
    box: SecretBoxDep,
    settings: SettingsDep,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
) -> RedirectResponse:
    def fail(message: str) -> RedirectResponse:
        log.warning("oauth.callback_failed", platform=platform, error=message)
        return RedirectResponse(
            f"/?chat_error={httpx.QueryParams({'m': message[:200]})['m']}", status_code=303
        )

    if error:
        return fail(error_description or error)
    if not code or not state:
        return fail("missing code/state")
    raw = await redis.getdel(KEY_OAUTH_STATE.format(state=state))
    if not raw:
        return fail("login expired or invalid state; try again")
    pending = json.loads(raw)
    if pending.get("platform") != platform:
        return fail("state/platform mismatch")
    try:
        async with httpx.AsyncClient() as http:
            creds = await oauth.exchange_code(
                http, settings, platform, code, pending.get("code_verifier")
            )
            identity = await oauth.fetch_identity(http, settings, platform, creds)
    except (AuthError, ConfigError) as exc:
        return fail(str(exc))
    except httpx.HTTPError as exc:
        return fail(f"network error: {exc}")

    existing = session.exec(
        select(ChatAccount).where(
            ChatAccount.platform == platform, ChatAccount.external_id == identity.external_id
        )
    ).first()
    row = existing or ChatAccount(platform=platform)
    row.external_id = identity.external_id
    row.display_name = (
        identity.display_name or row.display_name or PLATFORM_LABELS.get(platform, platform)
    )
    row.channel_ref = identity.channel_ref or row.channel_ref
    row.credentials_encrypted = box.encrypt(creds.to_json())
    row.enabled = True
    row.updated_at = utcnow()
    session.add(row)
    session.commit()
    session.refresh(row)
    log.info(
        "oauth.connected", platform=platform, account_id=row.id, external_id=identity.external_id
    )
    await publish_event(redis, EVENT_CHAT_ACCOUNTS_CHANGED, id=row.id, op="oauth")
    return RedirectResponse(f"/?chat_connected={platform}", status_code=303)


# ---- Kick webhook (public, signature-verified) ---------------------------------------


@webhooks.post("/kick")
async def kick_hook(request: Request, redis: RedisDep) -> dict[str, bool]:
    body = await request.body()
    headers = request.headers
    message_id = headers.get(kick_webhook.HEADER_MESSAGE_ID, "")
    timestamp = headers.get(kick_webhook.HEADER_TIMESTAMP, "")
    signature = headers.get(kick_webhook.HEADER_SIGNATURE, "")
    event_type = headers.get(kick_webhook.HEADER_TYPE, "")
    if not (message_id and timestamp and signature):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="missing Kick signature headers")
    cache: kick_webhook.PublicKeyCache = request.app.state.kick_public_key
    http: httpx.AsyncClient = request.app.state.http
    pem = await cache.get(http)
    ok = bool(pem) and kick_webhook.verify_signature(
        pem or "", message_id, timestamp, body, signature
    )
    if not ok:
        pem = await cache.get(http, force=True)  # key may have rotated
        ok = bool(pem) and kick_webhook.verify_signature(
            pem or "", message_id, timestamp, body, signature
        )
    if not ok:
        log.warning("kick_webhook.bad_signature", message_id=message_id)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="bad signature")
    seen_key = KEY_WEBHOOK_SEEN.format(platform="kick", message_id=message_id)
    if not await redis.set(seen_key, "1", ex=3600, nx=True):
        return {"ok": True, "duplicate": True}
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="invalid json") from exc
    envelope = {
        "type": event_type,
        "version": headers.get(kick_webhook.HEADER_VERSION, ""),
        "message_id": message_id,
        "timestamp": timestamp,
        "body": payload,
    }
    await redis.publish(CHANNEL_WEBHOOKS_KICK, json.dumps(envelope))
    return {"ok": True}
