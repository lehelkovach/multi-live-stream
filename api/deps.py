"""FastAPI dependencies: settings, DB session, Redis, authenticated user."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

import redis.asyncio as aioredis
from fastapi import Depends, HTTPException, Request, status
from sqlmodel import Session

from api.auth import PasswordVerifier, SessionSigner
from core.crypto import SecretBox
from core.settings import Settings


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings  # type: ignore[no-any-return]


def get_db(request: Request) -> Iterator[Session]:
    with Session(request.app.state.engine) as session:
        yield session


def get_redis(request: Request) -> aioredis.Redis:
    return request.app.state.redis  # type: ignore[no-any-return]


def get_secret_box(request: Request) -> SecretBox:
    return request.app.state.secret_box  # type: ignore[no-any-return]


def get_signer(request: Request) -> SessionSigner:
    return request.app.state.signer  # type: ignore[no-any-return]


def get_verifier(request: Request) -> PasswordVerifier:
    return request.app.state.verifier  # type: ignore[no-any-return]


def current_user(request: Request) -> str:
    """Username from the session cookie, or 401."""
    settings: Settings = request.app.state.settings
    signer: SessionSigner = request.app.state.signer
    user = signer.read(request.cookies.get(settings.session_cookie_name))
    if user is None or user != settings.admin_username:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="not authenticated")
    return user


SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
DbDep = Annotated[Session, Depends(get_db)]
RedisDep = Annotated[aioredis.Redis, Depends(get_redis)]
SecretBoxDep = Annotated[SecretBox, Depends(get_secret_box)]
UserDep = Annotated[str, Depends(current_user)]
