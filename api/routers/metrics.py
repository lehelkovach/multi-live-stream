"""Prometheus metrics + a small JSON stats endpoint (Phase 6).

``GET /metrics`` is readable with the dashboard session or with
``Authorization: Bearer <OMNI_METRICS_TOKEN>`` for an external Prometheus. Values
are computed per scrape from Redis (relay/chat runtime) and the DB (counts), so no
background work is needed and a dead service shows up as a stale heartbeat.
"""

from __future__ import annotations

import hmac
import time
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Gauge, Info, generate_latest
from sqlalchemy import func
from sqlmodel import Session, col, select

from api.deps import DbDep, RedisDep, SettingsDep, current_user
from api.services.chat import list_accounts, load_chat_runtime
from api.services.destinations import effective_status, list_destinations, load_runtime
from api.services.state import heartbeat_age, ingest_state
from core import __version__
from core.logging import get_logger
from core.models import ChatMessage, Destination, OutgoingMessage
from core.redis import KEY_CHAT_HEARTBEAT, KEY_YOUTUBE_QUOTA

log = get_logger(__name__)
router = APIRouter(tags=["metrics"])


def metrics_auth(request: Request, settings: SettingsDep) -> None:
    """Session cookie (dashboard) or bearer token (Prometheus)."""
    token = settings.metrics_token.get_secret_value()
    header = request.headers.get("authorization", "")
    if token and header.lower().startswith("bearer "):
        if hmac.compare_digest(header[7:].strip().encode(), token.encode()):
            return
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="bad metrics token")
    current_user(request)


async def _chat_heartbeat_age(redis: RedisDep) -> float | None:
    raw = await redis.get(KEY_CHAT_HEARTBEAT)
    if raw is None:
        return None
    try:
        return max(0.0, time.time() - float(raw))
    except ValueError:
        return None


def _messages_today(session: Session) -> dict[str, int]:
    start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    rows = session.exec(
        select(ChatMessage.platform, func.count())
        .where(col(ChatMessage.timestamp) >= start)
        .group_by(ChatMessage.platform)
    ).all()
    return {str(platform): int(count) for platform, count in rows}


def _outgoing_today(session: Session) -> dict[str, int]:
    start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    rows = session.exec(
        select(OutgoingMessage.status, func.count())
        .where(col(OutgoingMessage.created_at) >= start)
        .group_by(OutgoingMessage.status)
    ).all()
    return {str(s): int(c) for s, c in rows}


async def collect(session: Session, redis: RedisDep) -> dict[str, Any]:
    """Everything both endpoints need, gathered once."""
    destinations: list[Destination] = list_destinations(session)
    dest_runtime = await load_runtime(redis, [d.id for d in destinations if d.id is not None])
    accounts = list_accounts(session)
    acc_runtime = await load_chat_runtime(redis, [a.id for a in accounts if a.id is not None])
    ingest = await ingest_state(redis)
    quota_raw = await redis.get(KEY_YOUTUBE_QUOTA.format(day=datetime.now(UTC).strftime("%Y%m%d")))
    return {
        "destinations": [
            {
                "id": d.id,
                "name": d.name,
                "platform": d.platform,
                "enabled": d.enabled,
                "status": effective_status(d, dest_runtime.get(d.id or -1)),
                "runtime": dest_runtime.get(d.id or -1) or {},
            }
            for d in destinations
        ],
        "accounts": [
            {
                "id": a.id,
                "name": a.display_name,
                "platform": a.platform,
                "enabled": a.enabled,
                "status": (acc_runtime.get(a.id or -1) or {}).get(
                    "status", "disabled" if not a.enabled else a.status
                ),
            }
            for a in accounts
        ],
        "ingest": ingest.model_dump() if ingest else {"live": False, "bitrate_kbps": 0.0},
        "relay_heartbeat_age_s": await heartbeat_age(redis),
        "chat_heartbeat_age_s": await _chat_heartbeat_age(redis),
        "messages_today": _messages_today(session),
        "outgoing_today": _outgoing_today(session),
        "youtube_quota_units_today": int(quota_raw) if quota_raw else 0,
    }


@router.get("/api/v1/stats", dependencies=[Depends(current_user)])
async def stats(session: DbDep, redis: RedisDep) -> dict[str, Any]:
    return await collect(session, redis)


@router.get("/metrics", dependencies=[Depends(metrics_auth)], include_in_schema=False)
async def metrics(session: DbDep, redis: RedisDep, settings: SettingsDep) -> Response:
    data = await collect(session, redis)
    reg = CollectorRegistry()
    Info("omnistream", "OmniStream build", registry=reg).info(
        {"version": __version__, "domain": settings.domain}
    )
    Gauge("omnistream_ingest_live", "1 while a publisher is connected", registry=reg).set(
        1 if data["ingest"].get("live") else 0
    )
    Gauge("omnistream_ingest_bitrate_kbps", "Ingest bitrate", registry=reg).set(
        float(data["ingest"].get("bitrate_kbps") or 0)
    )
    hb = Gauge(
        "omnistream_service_heartbeat_age_seconds",
        "Seconds since a service last reported",
        ["service"],
        registry=reg,
    )
    for name, age in (
        ("relay", data["relay_heartbeat_age_s"]),
        ("chat", data["chat_heartbeat_age_s"]),
    ):
        hb.labels(service=name).set(age if age is not None else float("inf"))
    d_status = Gauge(
        "omnistream_destination_status",
        "1 for the destination's current status",
        ["destination", "platform", "status"],
        registry=reg,
    )
    d_bitrate = Gauge(
        "omnistream_destination_bitrate_kbps",
        "Output bitrate",
        ["destination", "platform"],
        registry=reg,
    )
    d_uptime = Gauge(
        "omnistream_destination_uptime_seconds",
        "Seconds the current ffmpeg has been running",
        ["destination", "platform"],
        registry=reg,
    )
    d_restarts = Gauge(
        "omnistream_destination_restarts",
        "ffmpeg restarts since the relay started",
        ["destination", "platform"],
        registry=reg,
    )
    for d in data["destinations"]:
        rt = d["runtime"]
        d_status.labels(destination=d["name"], platform=d["platform"], status=d["status"]).set(1)
        d_bitrate.labels(destination=d["name"], platform=d["platform"]).set(
            float(rt.get("bitrate_kbps") or 0)
        )
        d_uptime.labels(destination=d["name"], platform=d["platform"]).set(
            float(rt.get("uptime_s") or 0)
        )
        d_restarts.labels(destination=d["name"], platform=d["platform"]).set(
            float(rt.get("restarts") or 0)
        )
    a_status = Gauge(
        "omnistream_chat_account_status",
        "1 for the chat account's current status",
        ["account", "platform", "status"],
        registry=reg,
    )
    for a in data["accounts"]:
        a_status.labels(account=a["name"], platform=a["platform"], status=a["status"]).set(1)
    msgs = Gauge(
        "omnistream_chat_messages_today",
        "Chat messages stored since 00:00 UTC",
        ["platform"],
        registry=reg,
    )
    for platform, count in data["messages_today"].items():
        msgs.labels(platform=platform).set(count)
    out = Gauge(
        "omnistream_chat_outgoing_today",
        "Outgoing messages since 00:00 UTC by summary status",
        ["status"],
        registry=reg,
    )
    for st, count in data["outgoing_today"].items():
        out.labels(status=st).set(count)
    Gauge(
        "omnistream_youtube_quota_units_today",
        "YouTube Data API units spent today (UTC)",
        registry=reg,
    ).set(data["youtube_quota_units_today"])
    return Response(content=generate_latest(reg), media_type=CONTENT_TYPE_LATEST)
