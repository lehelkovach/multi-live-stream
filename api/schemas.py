"""Request/response schemas (Pydantic) for the HTTP API."""

from __future__ import annotations

import shlex
from datetime import datetime
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator, model_validator

from core.models import Platform, Profile

# ---- auth ---------------------------------------------------------------------


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=1000)


class UserOut(BaseModel):
    username: str


class OkOut(BaseModel):
    ok: bool = True


# ---- health -------------------------------------------------------------------


class HealthzOut(BaseModel):
    status: Literal["ok"] = "ok"
    version: str


class ComponentHealth(BaseModel):
    ok: bool
    detail: str | None = None
    latency_ms: float | None = None


class HealthOut(BaseModel):
    status: Literal["ok", "degraded"]
    version: str
    components: dict[str, ComponentHealth]


# ---- destinations -------------------------------------------------------------

ALLOWED_OUTPUT_SCHEMES = ("rtmp", "rtmps", "srt", "udp")


def validate_output_url(url: str) -> str:
    url = url.strip()
    parts = urlsplit(url)
    if parts.scheme not in ALLOWED_OUTPUT_SCHEMES:
        allowed = ", ".join(s + "://" for s in ALLOWED_OUTPUT_SCHEMES)
        msg = f"rtmp_url must start with one of: {allowed}"
        raise ValueError(msg)
    if not parts.netloc or any(ch.isspace() for ch in url):
        msg = "rtmp_url must be a valid URL with a host"
        raise ValueError(msg)
    return url


def validate_stream_key(key: str) -> str:
    key = key.strip()
    if any(ch.isspace() for ch in key) or "/" in key:
        msg = "stream_key must not contain whitespace or '/'"
        raise ValueError(msg)
    return key


class DestinationBase(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    platform: Platform = Platform.CUSTOM
    rtmp_url: str = Field(min_length=1, max_length=500)
    enabled: bool = True
    profile: Profile = Profile.COPY
    ffmpeg_args: str | None = Field(default=None, max_length=2000)

    @field_validator("rtmp_url")
    @classmethod
    def _url(cls, v: str) -> str:
        return validate_output_url(v)

    @field_validator("ffmpeg_args")
    @classmethod
    def _args(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        try:
            shlex.split(v)
        except ValueError as exc:
            msg = f"ffmpeg_args is not a valid argument string: {exc}"
            raise ValueError(msg) from exc
        return v.strip()

    @model_validator(mode="after")
    def _custom_needs_args(self) -> DestinationBase:
        if self.profile == Profile.CUSTOM and not self.ffmpeg_args:
            msg = "profile 'custom' requires ffmpeg_args"
            raise ValueError(msg)
        return self


class DestinationCreate(DestinationBase):
    stream_key: str = Field(default="", max_length=500)

    @field_validator("stream_key")
    @classmethod
    def _key(cls, v: str) -> str:
        return validate_stream_key(v)


class DestinationUpdate(BaseModel):
    """Partial update. ``stream_key=None`` leaves the key unchanged, ``""`` clears it."""

    name: str | None = Field(default=None, min_length=1, max_length=100)
    platform: Platform | None = None
    rtmp_url: str | None = Field(default=None, min_length=1, max_length=500)
    enabled: bool | None = None
    profile: Profile | None = None
    ffmpeg_args: str | None = Field(default=None, max_length=2000)
    stream_key: str | None = Field(default=None, max_length=500)

    @field_validator("rtmp_url")
    @classmethod
    def _url(cls, v: str | None) -> str | None:
        return validate_output_url(v) if v is not None else None

    @field_validator("stream_key")
    @classmethod
    def _key(cls, v: str | None) -> str | None:
        return validate_stream_key(v) if v is not None else None

    @field_validator("ffmpeg_args")
    @classmethod
    def _args(cls, v: str | None) -> str | None:
        if v is None:
            return None
        try:
            shlex.split(v)
        except ValueError as exc:
            msg = f"ffmpeg_args is not a valid argument string: {exc}"
            raise ValueError(msg) from exc
        return v.strip()


class DestinationRuntime(BaseModel):
    """Live view written by the relay into Redis (absent when the relay hasn't reported)."""

    status: str
    last_error: str | None = None
    pid: int | None = None
    started_at: datetime | None = None
    uptime_s: float = 0.0
    bitrate_kbps: float = 0.0
    bytes_sent: int = 0
    restarts: int = 0
    consecutive_failures: int = 0
    next_retry_at: datetime | None = None
    detail: str | None = None  # hint for inactive states ("no input stream", "relay stopped")
    updated_at: datetime | None = None


class DestinationOut(DestinationBase):
    id: int
    stream_key_masked: str
    status: str
    last_error: str | None
    runtime: DestinationRuntime | None
    created_at: datetime
    updated_at: datetime


# ---- relay --------------------------------------------------------------------


class IngestState(BaseModel):
    live: bool
    since: datetime | None = None
    bitrate_kbps: float = 0.0
    tracks: list[str] = Field(default_factory=list)
    source: str | None = None
    updated_at: datetime | None = None


class RelayState(BaseModel):
    enabled: bool
    relay_alive: bool
    heartbeat_age_s: float | None
    ingest: IngestState | None
    live_count: int
    enabled_count: int


class IngestInfo(BaseModel):
    rtmp_server: str
    srt_url: str
    stream_key: str
    path: str
