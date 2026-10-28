"""Phase 1 acceptance: real MediaMTX + ffmpeg + redis + API + `python -m relay`.

* a test pattern published to ingest arrives at two local "fake destinations",
* killing one destination's ffmpeg does not affect the other (and it comes back),
* the wrong stream key is rejected,
* toggling a destination stops/starts only that output,
* the global stop switch and going offline stop everything.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from typing import Any

import httpx
import pytest

from tests.integration.conftest import INGEST_KEY, Stack, publishing, wait_until

pytestmark = pytest.mark.integration


def _status(client: httpx.Client, dest_id: int) -> dict[str, Any]:
    r = client.get(f"/api/v1/destinations/{dest_id}")
    assert r.status_code == 200, r.text
    return dict(r.json())


def test_wrong_stream_key_is_rejected(stack: Stack) -> None:
    proc = stack.publish_testsrc(stack.ingest_url("not-the-key"), seconds=3)
    try:
        _out, err = proc.communicate(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
        pytest.fail("publisher with wrong key was not rejected")
    assert proc.returncode != 0
    assert b"authentication failed" in err or b"Operation not permitted" in err, err
    assert not stack.ingest.ready(f"live/{INGEST_KEY}")
    assert stack.ingest.path("live/not-the-key") is None


def test_relay_fans_out_isolates_failures_and_follows_toggles(stack: Stack) -> None:
    client = stack.client()
    one = client.post(
        "/api/v1/destinations",
        json={
            "name": "fake one",
            "platform": "custom",
            "rtmp_url": stack.dest_url("one"),
            "stream_key": "k1",
        },
    ).json()
    two = client.post(
        "/api/v1/destinations",
        json={
            "name": "fake two",
            "platform": "custom",
            "rtmp_url": stack.dest_url("two"),
            "stream_key": "k2",
        },
    ).json()
    d1, d2 = one["id"], two["id"]

    # relay alive, ingest offline, destinations idle
    wait_until(lambda: client.get("/api/v1/relay").json()["relay_alive"], what="relay heartbeat")
    wait_until(lambda: _status(client, d1)["status"] == "idle", what="idle status")
    assert _status(client, d1)["runtime"]["detail"] == "no input stream"

    with publishing(stack) as publisher:
        wait_until(lambda: stack.ingest.ready(f"live/{INGEST_KEY}"), what="ingest ready")

        # 1. both fake destinations receive the stream
        wait_until(
            lambda: stack.dest.ready("one/k1") and stack.dest.ready("two/k2"),
            timeout=40,
            what="both outputs ready",
        )
        wait_until(
            lambda: (
                _status(client, d1)["status"] == "live" and _status(client, d2)["status"] == "live"
            ),
            what="api live",
        )
        b1, b2 = stack.dest.bytes_received("one/k1"), stack.dest.bytes_received("two/k2")
        time.sleep(2)
        assert stack.dest.bytes_received("one/k1") > b1
        assert stack.dest.bytes_received("two/k2") > b2
        relay_state = client.get("/api/v1/relay").json()
        assert relay_state["ingest"]["live"] is True
        assert relay_state["live_count"] == 2
        assert _status(client, d1)["runtime"]["bitrate_kbps"] > 0

        # 2. kill destination one's ffmpeg: two keeps flowing, one is restarted
        pid = _status(client, d1)["runtime"]["pid"]
        assert isinstance(pid, int)
        assert pid != publisher.pid
        two_pid = _status(client, d2)["runtime"]["pid"]
        os.kill(pid, signal.SIGKILL)
        samples = []
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            samples.append((stack.dest.ready("two/k2"), stack.dest.bytes_received("two/k2")))
            time.sleep(0.5)
        assert all(ready for ready, _ in samples), samples
        assert samples[-1][1] > samples[0][1], "destination two stalled while one was killed"
        wait_until(
            lambda: (_status(client, d1)["runtime"] or {}).get("restarts", 0) >= 1,
            what="one restarted",
        )
        wait_until(
            lambda: (
                _status(client, d1)["status"] == "live"
                and _status(client, d1)["runtime"]["pid"] not in (None, pid)
            ),
            timeout=30,
            what="one live again",
        )
        wait_until(lambda: stack.dest.ready("one/k1"), what="one ready again")
        assert _status(client, d2)["runtime"]["pid"] == two_pid  # untouched
        assert _status(client, d2)["runtime"]["restarts"] == 0
        assert publisher.poll() is None  # ingest never noticed

        # 3. disable one -> only its output stops; re-enable -> back
        client.patch(f"/api/v1/destinations/{d1}", json={"enabled": False})
        wait_until(lambda: not stack.dest.ready("one/k1"), what="one stopped after disable")
        assert stack.dest.ready("two/k2")
        assert _status(client, d1)["status"] == "disabled"
        client.patch(f"/api/v1/destinations/{d1}", json={"enabled": True})
        wait_until(lambda: stack.dest.ready("one/k1"), timeout=30, what="one back after enable")

        # 4. global stop / start
        client.post("/api/v1/relay/stop")
        wait_until(
            lambda: not stack.dest.ready("one/k1") and not stack.dest.ready("two/k2"),
            what="all stopped",
        )
        assert _status(client, d2)["runtime"]["detail"] == "relay stopped"
        client.post("/api/v1/relay/start")
        wait_until(
            lambda: stack.dest.ready("one/k1") and stack.dest.ready("two/k2"),
            timeout=30,
            what="all back",
        )

    # 5. publisher gone -> workers stop, state idle
    wait_until(
        lambda: not stack.dest.ready("one/k1") and not stack.dest.ready("two/k2"),
        what="outputs stop when ingest ends",
    )
    # the worker notices first ("input stream ended"); the supervisor confirms on its next poll
    wait_until(
        lambda: client.get("/api/v1/relay").json()["ingest"]["live"] is False,
        what="ingest offline",
    )
    wait_until(
        lambda: (_status(client, d1)["runtime"] or {}).get("detail") == "no input stream",
        what="idle with 'no input stream'",
    )
    ended = _status(client, d1)
    assert ended["status"] == "idle"
    # a clean end of input is not reported as a failure, and no blind retry hit a 404
    assert ended["last_error"] in (None, "input stream ended"), ended["last_error"]

    for dest_id in (d1, d2):
        assert client.delete(f"/api/v1/destinations/{dest_id}").status_code == 204


def test_relay_survives_bad_destination_without_affecting_good_one(stack: Stack) -> None:
    client = stack.client()
    good = client.post(
        "/api/v1/destinations",
        json={
            "name": "good",
            "platform": "custom",
            "rtmp_url": stack.dest_url("good"),
            "stream_key": "g",
        },
    ).json()["id"]
    bad = client.post(
        "/api/v1/destinations",
        json={
            "name": "bad",
            "platform": "custom",
            "rtmp_url": "rtmp://127.0.0.1:1/nothing",
            "stream_key": "b",
        },
    ).json()["id"]
    with publishing(stack):
        wait_until(lambda: stack.dest.ready("good/g"), timeout=40, what="good output ready")
        wait_until(
            lambda: _status(client, bad)["status"] in ("retrying", "error"),
            timeout=30,
            what="bad output retrying",
        )
        s = _status(client, bad)
        assert s["last_error"]
        assert "ffmpeg exited" in s["last_error"]
        assert s["runtime"]["next_retry_at"]
        time.sleep(2)
        assert stack.dest.ready("good/g")
        assert _status(client, good)["status"] == "live"
    for dest_id in (good, bad):
        client.delete(f"/api/v1/destinations/{dest_id}")
