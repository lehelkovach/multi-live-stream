"""MediaMTX HTTP auth hook.

MediaMTX POSTs ``{"ip","user","password","token","action","path","protocol","id",
"query","userAgent"}`` for every publish/read; a 2xx allows it, anything else denies.
Caddy returns 404 for ``/internal/*`` so this is only reachable inside the Compose
network.

Rules:
* ``publish`` - only on the configured ingest path ``live/<stream key>``.
* ``read``    - only from private / loopback addresses (the relay workers).
* anything else is denied (api/metrics/pprof are excluded in mediamtx.yml anyway).
"""

from __future__ import annotations

import hmac
import ipaddress

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict

from api.auth import LoginRateLimiter
from api.deps import RedisDep, SettingsDep
from core.events import publish_event
from core.logging import get_logger
from core.redis import EVENT_INGEST_PUBLISH

router = APIRouter(prefix="/internal/mediamtx", tags=["internal"])
log = get_logger(__name__)

_publish_limiter = LoginRateLimiter(limit=30, window=60.0)


class MediaMTXAuthRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    ip: str = ""
    user: str = ""
    password: str = ""
    action: str = ""
    path: str = ""
    protocol: str = ""
    id: str = ""
    query: str = ""


# Networks allowed to *read* the ingest stream: loopback, RFC1918 (Compose networks),
# link-local, CGNAT/Tailscale (100.64/10), IPv6 ULA + link-local. Deliberately explicit
# instead of ``is_private`` (which also matches documentation ranges etc.).
_INTERNAL_NETWORKS = [
    ipaddress.ip_network(n)
    for n in (
        "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
        "169.254.0.0/16", "100.64.0.0/10", "::1/128", "fc00::/7", "fe80::/10",
    )
]  # fmt: skip


def _is_internal(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in _INTERNAL_NETWORKS)


def _deny(reason: str, body: MediaMTXAuthRequest) -> HTTPException:
    log.warning(
        "ingest.denied",
        reason=reason,
        action=body.action,
        path=body.path,
        ip=body.ip,
        protocol=body.protocol,
    )
    return HTTPException(status.HTTP_401_UNAUTHORIZED, detail=reason)


@router.post("/auth", status_code=status.HTTP_200_OK)
async def mediamtx_auth(
    body: MediaMTXAuthRequest, request: Request, settings: SettingsDep, redis: RedisDep
) -> dict[str, bool]:
    if body.action == "publish":
        if not _publish_limiter.allow(body.ip or "unknown"):
            raise _deny("rate limited", body)
        key = settings.ingest_stream_key.get_secret_value()
        if not key:
            raise _deny("no ingest key configured", body)
        expected = settings.ingest_path
        if not hmac.compare_digest(body.path.encode(), expected.encode()):
            raise _deny("bad stream key", body)
        _publish_limiter.reset(body.ip or "unknown")
        log.info("ingest.publish_accepted", ip=body.ip, protocol=body.protocol, id=body.id)
        await publish_event(
            redis, EVENT_INGEST_PUBLISH, path=body.path, protocol=body.protocol, ip=body.ip
        )
        return {"ok": True}

    if body.action == "read":
        if not _is_internal(body.ip):
            raise _deny("read from non-internal address", body)
        return {"ok": True}

    raise _deny(f"unsupported action {body.action!r}", body)
