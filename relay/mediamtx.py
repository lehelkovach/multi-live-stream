"""Minimal MediaMTX control-API client (``/v3/paths/list``)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx


@dataclass
class PathInfo:
    name: str
    ready: bool
    bytes_received: int = 0
    bytes_sent: int = 0
    tracks: list[str] = field(default_factory=list)
    source_type: str | None = None
    ready_time: str | None = None
    readers: int = 0

    @classmethod
    def from_api(cls, item: dict[str, Any]) -> PathInfo:
        source = item.get("source") or {}
        return cls(
            name=str(item.get("name", "")),
            ready=bool(item.get("ready", False)),
            bytes_received=int(item.get("bytesReceived") or 0),
            bytes_sent=int(item.get("bytesSent") or 0),
            tracks=[str(t) for t in item.get("tracks") or []],
            source_type=source.get("type") if isinstance(source, dict) else None,
            ready_time=item.get("readyTime"),
            readers=len(item.get("readers") or []),
        )


class MediaMTXClient:
    def __init__(self, base_url: str, timeout: float = 2.0) -> None:
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout)

    async def list_paths(self) -> list[PathInfo]:
        resp = await self._client.get("/v3/paths/list", params={"itemsPerPage": 500})
        resp.raise_for_status()
        data = resp.json()
        return [PathInfo.from_api(item) for item in data.get("items", [])]

    async def get_path(self, name: str) -> PathInfo | None:
        for p in await self.list_paths():
            if p.name == name:
                return p
        return None

    async def aclose(self) -> None:
        await self._client.aclose()
