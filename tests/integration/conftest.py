"""Process fixtures for integration tests: redis-server, two MediaMTX instances, the API
(uvicorn in a thread), and ``python -m relay`` as a subprocess.

Everything binds to random localhost ports. Skipped when a binary is missing:
``mediamtx`` (or $MEDIAMTX_BIN), ``ffmpeg``, ``redis-server``.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn

from api.auth import hash_password
from core.crypto import generate_key
from core.settings import Settings

MEDIAMTX_BIN = os.environ.get("MEDIAMTX_BIN") or shutil.which("mediamtx")
FFMPEG_BIN = shutil.which("ffmpeg")
REDIS_BIN = shutil.which("redis-server")

ADMIN_USER = "admin"
ADMIN_PASSWORD = "integration-test-password"
INGEST_KEY = "itest-stream-key-" + "0" * 16

pytestmark = pytest.mark.integration


def _require_binaries() -> None:
    missing = [
        n
        for n, b in (
            ("mediamtx", MEDIAMTX_BIN),
            ("ffmpeg", FFMPEG_BIN),
            ("redis-server", REDIS_BIN),
        )
        if not b
    ]
    if missing:
        pytest.skip(f"integration binaries missing: {', '.join(missing)}")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def wait_port(port: int, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket() as s:
            s.settimeout(0.2)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.05)
    msg = f"port {port} did not open"
    raise TimeoutError(msg)


def wait_until(
    predicate: Callable[[], object],
    timeout: float = 30.0,
    step: float = 0.25,
    what: str = "condition",
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(step)
    pytest.fail(f"timed out waiting for {what}")


@dataclass
class Proc:
    popen: subprocess.Popen[bytes]
    log: Path

    def stop(self) -> None:
        if self.popen.poll() is None:
            self.popen.terminate()
            try:
                self.popen.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.popen.kill()
                self.popen.wait(timeout=5)

    def tail(self, n: int = 40) -> str:
        try:
            return "\n".join(self.log.read_text(errors="replace").splitlines()[-n:])
        except OSError:
            return ""


def spawn(cmd: list[str], log: Path, env: dict[str, str] | None = None) -> Proc:
    f = log.open("wb")
    popen = subprocess.Popen(
        cmd, stdout=f, stderr=subprocess.STDOUT, env=env, start_new_session=True
    )
    return Proc(popen, log)


@pytest.fixture(scope="module")
def itest_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("itest")


@pytest.fixture(scope="module")
def redis_server(itest_dir: Path) -> Iterator[str]:
    _require_binaries()
    assert REDIS_BIN
    port = free_port()
    proc = spawn(
        [REDIS_BIN, "--port", str(port), "--bind", "127.0.0.1", "--save", "", "--appendonly", "no",
         "--dir", str(itest_dir)],
        itest_dir / "redis.log",
    )  # fmt: skip
    try:
        wait_port(port)
        yield f"redis://127.0.0.1:{port}/0"
    finally:
        proc.stop()


@dataclass
class MediaMTX:
    api_url: str
    rtmp_port: int
    rtsp_port: int
    proc: Proc

    def paths(self) -> dict[str, dict[str, Any]]:
        resp = httpx.get(f"{self.api_url}/v3/paths/list", params={"itemsPerPage": 100}, timeout=2)
        resp.raise_for_status()
        return {item["name"]: item for item in resp.json().get("items", [])}

    def path(self, name: str) -> dict[str, Any] | None:
        return self.paths().get(name)

    def ready(self, name: str) -> bool:
        p = self.path(name)
        return bool(p and p.get("ready"))

    def bytes_received(self, name: str) -> int:
        p = self.path(name)
        return int(p.get("bytesReceived", 0)) if p else 0


def start_mediamtx(itest_dir: Path, name: str, config: str) -> MediaMTX:
    assert MEDIAMTX_BIN
    api_port, rtmp_port, rtsp_port = free_port(), free_port(), free_port()
    cfg = config.format(api_port=api_port, rtmp_port=rtmp_port, rtsp_port=rtsp_port)
    cfg_path = itest_dir / f"mediamtx-{name}.yml"
    cfg_path.write_text(cfg)
    proc = spawn([MEDIAMTX_BIN, str(cfg_path)], itest_dir / f"mediamtx-{name}.log")
    wait_port(api_port)
    return MediaMTX(f"http://127.0.0.1:{api_port}", rtmp_port, rtsp_port, proc)


INGEST_CONFIG = """
logLevel: info
logDestinations: [stdout]
authMethod: http
authHTTPAddress: http://127.0.0.1:{{api_port}}/internal/mediamtx/auth
authHTTPExclude:
  - action: api
  - action: metrics
  - action: pprof
  - action: playback
api: yes
apiAddress: 127.0.0.1:{api_port}
metrics: no
pprof: no
playback: no
rtsp: yes
rtspAddress: 127.0.0.1:{rtsp_port}
rtspTransports: [tcp]
rtmp: yes
rtmpAddress: 127.0.0.1:{rtmp_port}
srt: no
hls: no
webrtc: no
moq: no
pathDefaults:
  source: publisher
  overridePublisher: yes
paths:
  all_others:
"""

# "Fake platforms": a second MediaMTX that accepts any publisher.
DEST_CONFIG = """
logLevel: info
logDestinations: [stdout]
authMethod: internal
authInternalUsers:
  - user: any
    pass:
    permissions:
      - action: publish
      - action: read
      - action: api
api: yes
apiAddress: 127.0.0.1:{api_port}
metrics: no
pprof: no
playback: no
rtsp: yes
rtspAddress: 127.0.0.1:{rtsp_port}
rtspTransports: [tcp]
rtmp: yes
rtmpAddress: 127.0.0.1:{rtmp_port}
srt: no
hls: no
webrtc: no
moq: no
paths:
  all_others:
"""


@dataclass
class Stack:
    settings: Settings
    api_url: str
    ingest: MediaMTX
    dest: MediaMTX
    relay: Proc
    env: dict[str, str]
    itest_dir: Path

    def client(self) -> httpx.Client:
        c = httpx.Client(base_url=self.api_url, timeout=5)
        r = c.post("/api/v1/auth/login", json={"username": ADMIN_USER, "password": ADMIN_PASSWORD})
        assert r.status_code == 200, r.text
        return c

    def ingest_url(self, key: str | None = None) -> str:
        return f"rtmp://127.0.0.1:{self.ingest.rtmp_port}/live/{key or INGEST_KEY}"

    def dest_url(self, app: str) -> str:
        return f"rtmp://127.0.0.1:{self.dest.rtmp_port}/{app}"

    def publish_testsrc(self, url: str, seconds: float | None = None) -> subprocess.Popen[bytes]:
        assert FFMPEG_BIN
        cmd = [
            FFMPEG_BIN, "-hide_banner", "-nostdin", "-loglevel", "error", "-re",
            "-f", "lavfi", "-i", "testsrc=size=320x240:rate=15",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
            "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", "-g", "30",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-ar", "44100",
        ]  # fmt: skip
        if seconds is not None:
            cmd += ["-t", str(seconds)]
        cmd += ["-f", "flv", url]
        return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


@pytest.fixture(scope="module")
def stack(itest_dir: Path, redis_server: str) -> Iterator[Stack]:
    _require_binaries()
    api_port = free_port()
    # ingest MediaMTX needs the API port for the auth hook
    ingest = start_mediamtx(
        itest_dir, "ingest", INGEST_CONFIG.replace("{{api_port}}", str(api_port))
    )
    dest = start_mediamtx(itest_dir, "dest", DEST_CONFIG)

    db_url = f"sqlite:///{itest_dir / 'omni.db'}"
    env_values = {
        "OMNI_SECRET_KEY": generate_key(),
        "OMNI_SESSION_SECRET": "integration-session-secret-0123456789",
        "OMNI_ADMIN_USERNAME": ADMIN_USER,
        "OMNI_ADMIN_PASSWORD_HASH": hash_password(ADMIN_PASSWORD),
        "OMNI_INGEST_STREAM_KEY": INGEST_KEY,
        "OMNI_DATABASE_URL": db_url,
        "OMNI_REDIS_URL": redis_server,
        "OMNI_MEDIAMTX_API_URL": ingest.api_url,
        "OMNI_MEDIAMTX_RTSP_URL": f"rtsp://127.0.0.1:{ingest.rtsp_port}",
        "OMNI_LOG_FORMAT": "console",
        "OMNI_LOG_LEVEL": "info",
        "OMNI_COOKIE_SECURE": "never",
        "OMNI_RELAY_POLL_INTERVAL": "0.5",
        "OMNI_RELAY_BACKOFF_BASE": "0.5",
        "OMNI_RELAY_BACKOFF_MAX": "2",
        "OMNI_RELAY_STALL_TIMEOUT": "8",
        "OMNI_WEB_DIST_DIR": str(itest_dir / "no-dist"),
    }
    settings = Settings(**{k.removeprefix("OMNI_").lower(): v for k, v in env_values.items()})  # type: ignore[arg-type]

    from api.main import create_app

    app = create_app(settings)
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=api_port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True, name="itest-api")
    thread.start()
    wait_port(api_port)

    env = {**os.environ, **env_values, "PYTHONPATH": str(Path(__file__).resolve().parents[2])}
    relay = spawn([sys.executable, "-m", "relay"], itest_dir / "relay.log", env=env)
    try:
        yield Stack(settings, f"http://127.0.0.1:{api_port}", ingest, dest, relay, env, itest_dir)
    finally:
        relay.stop()
        server.should_exit = True
        thread.join(timeout=10)
        ingest.proc.stop()
        dest.proc.stop()
        for name in ("relay", "mediamtx-ingest", "mediamtx-dest"):
            log = itest_dir / f"{name}.log"
            if log.exists():
                print(
                    f"\n----- {name}.log (tail) -----\n"
                    + "\n".join(log.read_text(errors="replace").splitlines()[-25:])
                )


@contextlib.contextmanager
def publishing(stack: Stack, key: str | None = None) -> Iterator[subprocess.Popen[bytes]]:
    proc = stack.publish_testsrc(stack.ingest_url(key))
    try:
        yield proc
    finally:
        if proc.poll() is None:
            proc.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=5)
