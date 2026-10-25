from __future__ import annotations

import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from chat import oauth
from chat.base import AuthError, ConfigError, Credentials
from core.settings import Settings


def test_authorize_urls(chat_settings: Settings) -> None:
    url = oauth.authorize_url(chat_settings, "twitch", "st4te")
    parts = urlsplit(url)
    q = parse_qs(parts.query)
    assert parts.netloc == "id.twitch.tv"
    assert q["client_id"] == ["twitch-client"]
    assert q["redirect_uri"] == ["https://stream.example.com/api/v1/chat/oauth/twitch/callback"]
    assert q["scope"] == ["user:read:chat user:write:chat"]
    assert q["state"] == ["st4te"]

    verifier, challenge = oauth.new_pkce()
    url = oauth.authorize_url(chat_settings, "kick", "s", challenge)
    q = parse_qs(urlsplit(url).query)
    assert q["code_challenge_method"] == ["S256"]
    assert q["code_challenge"] == [challenge]
    assert len(verifier) > 40
    with pytest.raises(ConfigError, match="PKCE"):
        oauth.authorize_url(chat_settings, "kick", "s")

    url = oauth.authorize_url(chat_settings, "youtube", "s")
    q = parse_qs(urlsplit(url).query)
    assert q["access_type"] == ["offline"]
    assert q["prompt"] == ["consent"]
    assert q["scope"] == ["https://www.googleapis.com/auth/youtube.force-ssl"]

    url = oauth.authorize_url(chat_settings, "facebook", "s")
    assert url.startswith("https://www.facebook.com/v21.0/dialog/oauth?")
    assert "pages_read_engagement" in url


def test_unconfigured_platform(settings: Settings) -> None:
    assert not oauth.is_configured(settings, "twitch")
    with pytest.raises(ConfigError, match="OMNI_"):
        oauth.authorize_url(settings, "twitch", "s")


async def test_exchange_and_refresh_twitch(chat_settings: Settings) -> None:
    calls: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL(oauth.TWITCH_TOKEN)
        form = dict(parse_qs(request.content.decode()))
        calls.append({k: v[0] for k, v in form.items()})
        return httpx.Response(
            200,
            json={
                "access_token": "A1",
                "refresh_token": "R1",
                "expires_in": 3600,
                "scope": ["user:read:chat"],
                "token_type": "bearer",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        creds = await oauth.exchange_code(http, chat_settings, "twitch", "the-code")
        assert creds.access_token == "A1"
        assert creds.refresh_token == "R1"
        assert creds.expires_at is not None
        assert creds.expires_at > time.time() + 3000
        assert calls[0]["grant_type"] == "authorization_code"
        assert calls[0]["code"] == "the-code"
        refreshed = await oauth.refresh(http, chat_settings, "twitch", creds)
        assert calls[1]["grant_type"] == "refresh_token"
        assert calls[1]["refresh_token"] == "R1"
        assert refreshed.scopes == ["user:read:chat"]


async def test_refresh_failure_is_auth_error(chat_settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"status": 400, "message": "Invalid refresh token"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(AuthError, match="Invalid refresh token"):
            await oauth.refresh(
                http, chat_settings, "twitch", Credentials(access_token="x", refresh_token="bad")
            )
        with pytest.raises(AuthError, match="no refresh token"):
            await oauth.refresh(http, chat_settings, "youtube", Credentials(access_token="x"))


async def test_ensure_fresh_only_when_expiring(chat_settings: Settings) -> None:
    hits = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal hits
        hits += 1
        return httpx.Response(200, json={"access_token": "new", "expires_in": 3600})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        fresh = Credentials(access_token="ok", refresh_token="r", expires_at=time.time() + 3600)
        same, changed = await oauth.ensure_fresh(http, chat_settings, "twitch", fresh)
        assert not changed
        assert same is fresh
        assert hits == 0
        soon = Credentials(access_token="old", refresh_token="r", expires_at=time.time() + 60)
        new, changed = await oauth.ensure_fresh(http, chat_settings, "twitch", soon)
        assert changed
        assert new.access_token == "new"
        assert new.refresh_token == "r"
        assert hits == 1


async def test_facebook_exchange_collects_page_tokens(chat_settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        q = dict(request.url.params)
        if path.endswith("/oauth/access_token") and q.get("code"):
            return httpx.Response(
                200, json={"access_token": "short", "token_type": "bearer", "expires_in": 5000}
            )
        if path.endswith("/oauth/access_token") and q.get("grant_type") == "fb_exchange_token":
            assert q["fb_exchange_token"] == "short"
            return httpx.Response(
                200, json={"access_token": "long", "token_type": "bearer", "expires_in": 5184000}
            )
        if path.endswith("/me/accounts"):
            assert q["access_token"] == "long"
            return httpx.Response(
                200, json={"data": [{"id": "111", "name": "My Page", "access_token": "pagetok"}]}
            )
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        creds = await oauth.exchange_code(http, chat_settings, "facebook", "code")
        assert creds.access_token == "long"
        assert creds.extra["pages"] == [{"id": "111", "name": "My Page", "access_token": "pagetok"}]
        identity = await oauth.fetch_identity(http, chat_settings, "facebook", creds)
        assert identity == oauth.Identity("page:111", "My Page", "111")


async def test_fetch_identity_twitch_youtube_kick(chat_settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith(oauth.TWITCH_VALIDATE):
            assert request.headers["Authorization"] == "OAuth tok"
            return httpx.Response(
                200,
                json={
                    "client_id": "twitch-client",
                    "login": "streamer25",
                    "user_id": "1971641",
                    "scopes": ["user:read:chat"],
                    "expires_in": 100,
                },
            )
        if url.startswith(oauth.TWITCH_HELIX + "/users"):
            return httpx.Response(
                200,
                json={
                    "data": [{"id": "1971641", "login": "streamer25", "display_name": "Streamer25"}]
                },
            )
        if url.startswith(oauth.YOUTUBE_API + "/channels"):
            return httpx.Response(
                200, json={"items": [{"id": "UCabc", "snippet": {"title": "My Channel"}}]}
            )
        if url.startswith(oauth.KICK_API + "/users"):
            return httpx.Response(
                200, json={"data": [{"user_id": 42, "name": "kicker", "email": "x@y"}]}
            )
        if url.startswith(oauth.KICK_API + "/channels"):
            return httpx.Response(
                200, json={"data": [{"broadcaster_user_id": 42, "slug": "kicker-slug"}]}
            )
        return httpx.Response(500)

    creds = Credentials(access_token="tok")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        assert await oauth.fetch_identity(http, chat_settings, "twitch", creds) == oauth.Identity(
            "1971641", "Streamer25", "streamer25"
        )
        assert await oauth.fetch_identity(http, chat_settings, "youtube", creds) == oauth.Identity(
            "UCabc", "My Channel", "UCabc"
        )
        assert await oauth.fetch_identity(http, chat_settings, "kick", creds) == oauth.Identity(
            "42", "kicker", "kicker-slug"
        )


def test_credentials_roundtrip() -> None:
    c = Credentials(
        access_token="a", refresh_token="r", expires_at=1.5, scopes=["x"], extra={"pages": []}
    )
    assert Credentials.from_json(c.to_json()) == c
    assert Credentials.from_json("") == Credentials()
    assert Credentials.from_json("[]") == Credentials()
