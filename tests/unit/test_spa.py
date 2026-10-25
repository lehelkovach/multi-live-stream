"""Static dashboard serving: SPA fallback, assets, API/internal exclusions, traversal."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import fakeredis
import pytest
from fastapi.testclient import TestClient

from core.settings import Settings


@pytest.fixture
def dist(tmp_path: Path) -> Path:
    d = tmp_path / "dist"
    (d / "assets").mkdir(parents=True)
    (d / "index.html").write_text("<!doctype html><div id='root'>spa</div>")
    (d / "assets" / "app-abc.js").write_text("console.log('hi')")
    (d / "favicon.svg").write_text("<svg/>")
    (tmp_path / "outside.txt").write_text("secret")
    return d


@pytest.fixture
def spa_client(
    settings: Settings, dist: Path, fake_redis: fakeredis.aioredis.FakeRedis
) -> Iterator[TestClient]:
    from api.main import create_app

    app = create_app(settings.model_copy(update={"web_dist_dir": str(dist)}))
    with TestClient(app) as c:
        app.state.redis = fake_redis
        yield c


def test_index_and_client_routes(spa_client: TestClient) -> None:
    assert "spa" in spa_client.get("/").text
    assert "spa" in spa_client.get("/destinations/42").text


def test_static_files(spa_client: TestClient) -> None:
    r = spa_client.get("/assets/app-abc.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]
    r = spa_client.get("/favicon.svg")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/svg")


def test_api_and_internal_paths_do_not_fall_back_to_index(spa_client: TestClient) -> None:
    assert spa_client.get("/api/v1/does-not-exist").status_code == 404
    assert spa_client.get("/internal/mediamtx/auth").status_code == 404
    assert spa_client.get("/ws/nope").status_code == 404
    assert spa_client.get("/healthz").status_code == 200


def test_no_path_traversal(spa_client: TestClient) -> None:
    for path in ("/../outside.txt", "/%2e%2e/outside.txt", "/assets/../../outside.txt"):
        r = spa_client.get(path)
        assert "secret" not in r.text, path


def test_without_dist_root_is_404(client: TestClient) -> None:
    assert client.get("/").status_code == 404
