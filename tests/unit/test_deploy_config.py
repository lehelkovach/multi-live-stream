"""Static checks that the deploy configs agree with the code."""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path

from fastapi.routing import APIRoute, APIWebSocketRoute

from api.main import ROUTERS

ROOT = Path(__file__).resolve().parents[2]
CADDYFILE = ROOT / "deploy" / "Caddyfile"


def backend_patterns() -> list[str]:
    match = re.search(r"^\s*@backend\s+path\s+(.+)$", CADDYFILE.read_text(), re.MULTILINE)
    assert match, "deploy/Caddyfile must define a @backend path matcher"
    return match.group(1).split()


def all_paths() -> set[str]:
    return {
        route.path
        for router in ROUTERS
        for route in router.routes
        if isinstance(route, APIRoute | APIWebSocketRoute)
    }


def top_level_api_paths() -> set[str]:
    """Routes the API serves outside /api/* (and outside the internal hooks)."""
    return {p for p in all_paths() if not p.startswith(("/api/", "/internal/"))}


def test_every_top_level_api_route_is_proxied_by_the_edge() -> None:
    patterns = backend_patterns()
    assert "/api/*" in patterns
    missing = [
        p
        for p in sorted(top_level_api_paths())
        if not any(fnmatch.fnmatch(p, pat) for pat in patterns)
    ]
    assert missing == [], f"add these to @backend in deploy/Caddyfile: {missing}"
    assert {"/healthz", "/metrics", "/ws/events"} <= top_level_api_paths()


def test_internal_hooks_are_blocked_at_the_edge() -> None:
    text = CADDYFILE.read_text()
    assert re.search(r"handle /internal/\*\s*\{\s*respond 404", text), "edge must 404 /internal/*"
    assert text.index("handle /internal/*") < text.index("handle @backend"), "block must come first"


def test_mediamtx_auth_hook_points_at_the_api_route() -> None:
    config = (ROOT / "ingest" / "mediamtx.yml").read_text()
    match = re.search(r"^authHTTPAddress:\s*(\S+)", config, re.MULTILINE)
    assert match
    path = "/" + match.group(1).split("/", 3)[3]
    assert path in all_paths()
