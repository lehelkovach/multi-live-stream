"""Login / logout / whoami for the single admin user."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response, status

from api.auth import LoginRateLimiter, clear_session_cookie, set_session_cookie
from api.deps import SettingsDep, UserDep
from api.schemas import LoginRequest, OkOut, UserOut
from core.logging import get_logger

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])
log = get_logger(__name__)


def _client_ip(request: Request) -> str:
    # uvicorn is started with --proxy-headers, so request.client reflects X-Forwarded-For
    # when the request came through Caddy.
    return request.client.host if request.client else "unknown"


@router.post("/login", response_model=UserOut)
async def login(
    body: LoginRequest, request: Request, response: Response, settings: SettingsDep
) -> UserOut:
    limiter: LoginRateLimiter = request.app.state.login_limiter
    ip = _client_ip(request)
    if not limiter.allow(ip):
        log.warning("login.rate_limited", ip=ip)
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, detail="too many attempts")
    if not request.app.state.verifier.verify(body.username, body.password):
        log.warning("login.failed", ip=ip, username=body.username)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="invalid credentials")
    limiter.reset(ip)
    token = request.app.state.signer.issue(body.username)
    set_session_cookie(response, request, settings, token)
    log.info("login.ok", ip=ip, username=body.username)
    return UserOut(username=body.username)


@router.post("/logout", response_model=OkOut)
async def logout(request: Request, response: Response, settings: SettingsDep) -> OkOut:
    clear_session_cookie(response, request, settings)
    return OkOut()


@router.get("/me", response_model=UserOut)
async def me(user: UserDep) -> UserOut:
    return UserOut(username=user)
