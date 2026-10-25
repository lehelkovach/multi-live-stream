"""OAuth 2 helpers for the chat platforms (authorization-code flow + refresh).

Verified against the platform docs on 2026-10-25 (see docs/platforms.md):
* Twitch   https://id.twitch.tv/oauth2/{authorize,token,validate}
* Google   https://accounts.google.com/o/oauth2/v2/auth, https://oauth2.googleapis.com/token
* Kick     https://id.kick.com/oauth/{authorize,token}  (OAuth 2.1, PKCE S256 required)
* Facebook https://www.facebook.com/<v>/dialog/oauth, graph.facebook.com/<v>/oauth/access_token
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import httpx

from chat.base import AuthError, ConfigError, Credentials
from core.logging import get_logger
from core.settings import Settings

log = get_logger(__name__)

TWITCH_AUTHORIZE = "https://id.twitch.tv/oauth2/authorize"
TWITCH_TOKEN = "https://id.twitch.tv/oauth2/token"
TWITCH_VALIDATE = "https://id.twitch.tv/oauth2/validate"
TWITCH_HELIX = "https://api.twitch.tv/helix"
GOOGLE_AUTHORIZE = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
YOUTUBE_API = "https://www.googleapis.com/youtube/v3"
KICK_AUTHORIZE = "https://id.kick.com/oauth/authorize"
KICK_TOKEN = "https://id.kick.com/oauth/token"
KICK_API = "https://api.kick.com/public/v1"
FACEBOOK_GRAPH = "https://graph.facebook.com"


@dataclass(frozen=True)
class Provider:
    name: str
    authorize_url: str
    token_url: str
    scopes: list[str]
    pkce: bool = False
    extra_authorize: dict[str, str] = field(default_factory=dict)


PROVIDERS: dict[str, Provider] = {
    "twitch": Provider(
        "twitch", TWITCH_AUTHORIZE, TWITCH_TOKEN, ["user:read:chat", "user:write:chat"],
        extra_authorize={"force_verify": "true"},
    ),
    "youtube": Provider(
        "youtube", GOOGLE_AUTHORIZE, GOOGLE_TOKEN,
        ["https://www.googleapis.com/auth/youtube.force-ssl"],
        extra_authorize={
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
        },
    ),
    "kick": Provider(
        "kick", KICK_AUTHORIZE, KICK_TOKEN,
        ["user:read", "channel:read", "chat:write", "events:subscribe"], pkce=True,
    ),
    "facebook": Provider(
        "facebook", "", "",  # version-dependent, filled in by facebook_urls()
        ["pages_show_list", "pages_read_engagement", "pages_read_user_content",
         "pages_manage_engagement"],
    ),
}  # fmt: skip


def facebook_urls(settings: Settings) -> tuple[str, str]:
    v = settings.graph_api_version
    return f"https://www.facebook.com/{v}/dialog/oauth", f"{FACEBOOK_GRAPH}/{v}/oauth/access_token"


def client_credentials(settings: Settings, platform: str) -> tuple[str, str]:
    pairs = {
        "twitch": (settings.twitch_client_id, settings.twitch_client_secret.get_secret_value()),
        "youtube": (settings.google_client_id, settings.google_client_secret.get_secret_value()),
        "kick": (settings.kick_client_id, settings.kick_client_secret.get_secret_value()),
        "facebook": (settings.facebook_app_id, settings.facebook_app_secret.get_secret_value()),
    }
    if platform not in pairs:
        msg = f"unknown OAuth platform {platform!r}"
        raise ConfigError(msg)
    client_id, secret = pairs[platform]
    if not client_id or not secret:
        msg = f"{platform}: OAuth app not configured (set OMNI_*_CLIENT_ID/SECRET in .env)"
        raise ConfigError(msg)
    return client_id, secret


def is_configured(settings: Settings, platform: str) -> bool:
    try:
        client_credentials(settings, platform)
    except ConfigError:
        return False
    return True


def redirect_uri(settings: Settings, platform: str) -> str:
    return f"{settings.base_url}/api/v1/chat/oauth/{platform}/callback"


def new_state() -> str:
    return secrets.token_urlsafe(32)


def new_pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    return verifier, challenge.decode()


def authorize_url(
    settings: Settings, platform: str, state: str, code_challenge: str | None = None
) -> str:
    provider = PROVIDERS[platform]
    client_id, _ = client_credentials(settings, platform)
    auth_url = provider.authorize_url or facebook_urls(settings)[0]
    params: dict[str, str] = {
        "client_id": client_id,
        "redirect_uri": redirect_uri(settings, platform),
        "response_type": "code",
        "scope": " ".join(provider.scopes) if platform != "facebook" else ",".join(provider.scopes),
        "state": state,
        **provider.extra_authorize,
    }
    if provider.pkce:
        if not code_challenge:
            msg = f"{platform} requires PKCE"
            raise ConfigError(msg)
        params["code_challenge"] = code_challenge
        params["code_challenge_method"] = "S256"
    return f"{auth_url}?{urlencode(params)}"


def _credentials_from_token_response(
    data: dict[str, Any], previous: Credentials | None = None
) -> Credentials:
    expires_in = data.get("expires_in")
    expires_at = time.time() + float(expires_in) if expires_in else None
    scopes_raw = data.get("scope") or data.get("scopes") or []
    scopes = scopes_raw.split() if isinstance(scopes_raw, str) else [str(s) for s in scopes_raw]
    return Credentials(
        access_token=str(data.get("access_token", "")),
        refresh_token=str(
            data.get("refresh_token") or (previous.refresh_token if previous else "")
        ),
        expires_at=expires_at,
        token_type=str(data.get("token_type", "bearer")),
        scopes=scopes or (previous.scopes if previous else []),
        extra=dict(previous.extra) if previous else {},
    )


async def _token_request(
    http: httpx.AsyncClient, url: str, data: dict[str, str], *, platform: str
) -> dict[str, Any]:
    try:
        resp = await http.post(url, data=data, headers={"Accept": "application/json"}, timeout=15)
    except httpx.HTTPError as exc:
        msg = f"{platform}: token endpoint unreachable: {exc}"
        raise AuthError(msg) from exc
    if resp.status_code >= 400:
        detail = resp.text[:300]
        log.warning("oauth.token_error", platform=platform, status=resp.status_code, body=detail)
        msg = f"{platform}: token request failed ({resp.status_code}): {detail}"
        raise AuthError(msg)
    body = resp.json()
    if not isinstance(body, dict) or not body.get("access_token"):
        msg = f"{platform}: token response without access_token"
        raise AuthError(msg)
    return body


async def exchange_code(
    http: httpx.AsyncClient,
    settings: Settings,
    platform: str,
    code: str,
    code_verifier: str | None = None,
) -> Credentials:
    client_id, client_secret = client_credentials(settings, platform)
    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri(settings, platform),
    }
    if platform == "facebook":
        _auth, token_url = facebook_urls(settings)
        resp = await http.get(
            token_url, params={k: v for k, v in data.items() if k != "grant_type"}, timeout=15
        )
        if resp.status_code >= 400:
            msg = f"facebook: code exchange failed ({resp.status_code}): {resp.text[:300]}"
            raise AuthError(msg)
        creds = _credentials_from_token_response(resp.json())
        return await facebook_long_lived(http, settings, creds)
    if code_verifier:
        data["code_verifier"] = code_verifier
    body = await _token_request(http, PROVIDERS[platform].token_url, data, platform=platform)
    return _credentials_from_token_response(body)


async def refresh(
    http: httpx.AsyncClient, settings: Settings, platform: str, creds: Credentials
) -> Credentials:
    if platform == "facebook":
        return creds  # long-lived page tokens do not refresh; they are re-issued by reconnecting
    if not creds.refresh_token:
        msg = f"{platform}: no refresh token stored; reconnect the account"
        raise AuthError(msg)
    client_id, client_secret = client_credentials(settings, platform)
    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "refresh_token",
        "refresh_token": creds.refresh_token,
    }
    body = await _token_request(http, PROVIDERS[platform].token_url, data, platform=platform)
    new = _credentials_from_token_response(body, previous=creds)
    log.info("oauth.refreshed", platform=platform, expires_in=body.get("expires_in"))
    return new


async def ensure_fresh(
    http: httpx.AsyncClient,
    settings: Settings,
    platform: str,
    creds: Credentials,
    *,
    within: float = 300,
) -> tuple[Credentials, bool]:
    """Refresh when the access token expires within ``within`` seconds. Returns (creds, changed)."""
    if creds.expires_within(within) and creds.refresh_token:
        return await refresh(http, settings, platform, creds), True
    return creds, False


async def facebook_long_lived(
    http: httpx.AsyncClient, settings: Settings, creds: Credentials
) -> Credentials:
    """Short-lived user token -> long-lived user token -> page tokens (``extra['pages']``)."""
    app_id, app_secret = client_credentials(settings, "facebook")
    v = settings.graph_api_version
    resp = await http.get(
        f"{FACEBOOK_GRAPH}/{v}/oauth/access_token",
        params={
            "grant_type": "fb_exchange_token",
            "client_id": app_id,
            "client_secret": app_secret,
            "fb_exchange_token": creds.access_token,
        },
        timeout=15,
    )
    if resp.status_code >= 400:
        msg = f"facebook: long-lived token exchange failed ({resp.status_code}): {resp.text[:300]}"
        raise AuthError(msg)
    long_lived = _credentials_from_token_response(resp.json(), previous=creds)
    pages_resp = await http.get(
        f"{FACEBOOK_GRAPH}/{v}/me/accounts",
        params={"access_token": long_lived.access_token, "fields": "id,name,access_token"},
        timeout=15,
    )
    if pages_resp.status_code >= 400:
        msg = f"facebook: listing pages failed ({pages_resp.status_code}): {pages_resp.text[:300]}"
        raise AuthError(msg)
    pages = [
        {
            "id": str(p.get("id")),
            "name": str(p.get("name", "")),
            "access_token": str(p.get("access_token", "")),
        }
        for p in pages_resp.json().get("data", [])
        if p.get("id") and p.get("access_token")
    ]
    long_lived.extra["pages"] = pages
    return long_lived


@dataclass(slots=True)
class Identity:
    external_id: str
    display_name: str
    channel_ref: str


async def fetch_identity(
    http: httpx.AsyncClient, settings: Settings, platform: str, creds: Credentials
) -> Identity:
    """Who did we just connect? Used to label/dedupe accounts after OAuth."""
    if platform == "twitch":
        resp = await http.get(
            TWITCH_VALIDATE, headers={"Authorization": f"OAuth {creds.access_token}"}, timeout=15
        )
        if resp.status_code != 200:
            msg = "twitch: token validation failed"
            raise AuthError(msg)
        data = resp.json()
        user_id, login = str(data.get("user_id", "")), str(data.get("login", ""))
        display = login
        client_id, _ = client_credentials(settings, "twitch")
        users = await http.get(
            f"{TWITCH_HELIX}/users",
            headers={"Authorization": f"Bearer {creds.access_token}", "Client-Id": client_id},
            timeout=15,
        )
        if users.status_code == 200 and users.json().get("data"):
            display = str(users.json()["data"][0].get("display_name") or login)
        return Identity(user_id, display, login)
    if platform == "youtube":
        resp = await http.get(
            f"{YOUTUBE_API}/channels",
            params={"part": "snippet", "mine": "true"},
            headers={"Authorization": f"Bearer {creds.access_token}"},
            timeout=15,
        )
        if resp.status_code != 200 or not resp.json().get("items"):
            msg = f"youtube: cannot read own channel ({resp.status_code})"
            raise AuthError(msg)
        item = resp.json()["items"][0]
        return Identity(
            str(item["id"]), str(item.get("snippet", {}).get("title", "")), str(item["id"])
        )
    if platform == "kick":
        headers = {"Authorization": f"Bearer {creds.access_token}"}
        users = await http.get(f"{KICK_API}/users", headers=headers, timeout=15)
        if users.status_code != 200 or not users.json().get("data"):
            msg = f"kick: cannot read current user ({users.status_code})"
            raise AuthError(msg)
        user = users.json()["data"][0]
        user_id, name = str(user.get("user_id", "")), str(user.get("name", ""))
        slug = ""
        channels = await http.get(f"{KICK_API}/channels", headers=headers, timeout=15)
        if channels.status_code == 200 and channels.json().get("data"):
            slug = str(channels.json()["data"][0].get("slug", ""))
        return Identity(user_id, name or slug, slug)
    if platform == "facebook":
        pages = creds.extra.get("pages") or []
        if len(pages) == 1:
            page = pages[0]
            return Identity(f"page:{page['id']}", str(page["name"]), str(page["id"]))
        v = settings.graph_api_version
        me = await http.get(
            f"{FACEBOOK_GRAPH}/{v}/me", params={"access_token": creds.access_token}, timeout=15
        )
        if me.status_code != 200:
            msg = "facebook: cannot read profile"
            raise AuthError(msg)
        data = me.json()
        return Identity(f"user:{data.get('id', '')}", str(data.get("name", "")), "")
    msg = f"no identity lookup for {platform!r}"
    raise ConfigError(msg)
