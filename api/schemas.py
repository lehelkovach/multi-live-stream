"""Request/response schemas (Pydantic) for the HTTP API."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=1000)


class UserOut(BaseModel):
    username: str


class OkOut(BaseModel):
    ok: bool = True


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
