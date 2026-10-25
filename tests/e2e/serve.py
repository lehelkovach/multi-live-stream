#!/usr/bin/env python3
"""Stack for the Playwright smoke test (web/e2e), no Docker and no ffmpeg needed.

Starts: redis-server (random port) + the API serving web/dist + the *real* relay
supervisor wired to a fake MediaMTX and fake workers (so "live" needs no video).

Test hooks (unauthenticated, only exist in this harness):
    POST /__test/ingest/on     the fake ingest goes live -> workers start -> status live
    POST /__test/ingest/off    ingest offline -> workers stop -> status idle

Usage: python tests/e2e/serve.py --port 8099 [--dist web/dist]
Admin credentials: admin / e2e-password-123
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

import uvicorn
from fastapi import APIRouter, FastAPI

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from api.auth import hash_password  # noqa: E402
from api.main import create_app  # noqa: E402
from core.crypto import generate_key  # noqa: E402
from core.models import DestinationStatus  # noqa: E402
from core.settings import Settings  # noqa: E402
from relay.mediamtx import PathInfo  # noqa: E402
from relay.supervisor import Supervisor  # noqa: E402
from relay.worker import WorkerConfig, WorkerStatus  # noqa: E402

ADMIN_USER = "admin"
ADMIN_PASSWORD = "e2e-password-123"
INGEST_KEY = "e2e-ingest-key-0123456789abcdef"


class FakeMediaMTX:
    def __init__(self) -> None:
        self.live = False
        self.bytes = 0

    async def get_path(self, name: str) -> PathInfo | None:
        if not self.live:
            return None
        self.bytes += 750_000  # ~6 Mbps at a 1 s poll
        return PathInfo(
            name=name,
            ready=True,
            bytes_received=self.bytes,
            tracks=["H264", "MPEG-4 Audio"],
            source_type="rtmpConn",
        )

    async def aclose(self) -> None:
        return None


class FakeWorker:
    """Reports starting -> live and climbing byte counters; no process involved."""

    def __init__(
        self,
        dest_id: int,
        name: str,
        cmd: list[str],
        *,
        config: WorkerConfig,
        on_status: Callable[[WorkerStatus, bool], Awaitable[None]],
        secrets: list[str],
    ) -> None:
        self.dest_id, self.name, self.cmd, self.config = dest_id, name, list(cmd), config
        self._on_status, self._secrets = on_status, secrets
        self.status = WorkerStatus(id=dest_id, name=name)
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _run(self) -> None:
        self.status.pid = 4000 + self.dest_id
        self.status.started_at = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
        self.status.status = DestinationStatus.STARTING
        await self._on_status(self.status, True)
        await asyncio.sleep(0.5)
        self.status.status = DestinationStatus.LIVE
        start = time.monotonic()
        changed = True
        while True:
            self.status.uptime_s = round(time.monotonic() - start, 1)
            self.status.bytes_sent += 312_500
            self.status.bitrate_kbps = 2500.0
            await self._on_status(self.status, changed)
            changed = False
            await asyncio.sleep(1.0)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def start_redis(workdir: Path) -> tuple[subprocess.Popen[bytes], str]:
    binary = shutil.which("redis-server")
    if not binary:
        print("redis-server not found on PATH", file=sys.stderr)
        raise SystemExit(2)
    port = free_port()
    proc = subprocess.Popen(
        [
            binary,
            "--port",
            str(port),
            "--bind",
            "127.0.0.1",
            "--save",
            "",
            "--appendonly",
            "no",
            "--dir",
            str(workdir),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return proc, f"redis://127.0.0.1:{port}/0"
        time.sleep(0.05)
    proc.kill()
    print("redis-server did not start", file=sys.stderr)
    raise SystemExit(2)


async def main_async(port: int, dist: Path) -> int:
    workdir = Path(tempfile.mkdtemp(prefix="omni-e2e-"))
    redis_proc, redis_url = start_redis(workdir)
    settings = Settings(
        secret_key=generate_key(),
        session_secret="e2e-session-secret-not-for-production-0123",
        ingest_stream_key=INGEST_KEY,
        admin_username=ADMIN_USER,
        admin_password_hash=hash_password(ADMIN_PASSWORD),
        database_url=f"sqlite:///{workdir / 'e2e.db'}",
        redis_url=redis_url,
        mediamtx_api_url="http://127.0.0.1:1",
        log_format="console",
        log_level="warning",
        cookie_secure="never",
        web_dist_dir=str(dist),
        relay_poll_interval=0.5,
    )
    fake = FakeMediaMTX()
    supervisor = Supervisor(settings, mediamtx=fake, worker_factory=FakeWorker)  # type: ignore[arg-type]

    app: FastAPI = create_app(settings)
    hooks = APIRouter(prefix="/__test")

    @hooks.post("/ingest/on")
    async def ingest_on() -> dict[str, bool]:
        fake.live = True
        supervisor.wake()
        return {"live": True}

    @hooks.post("/ingest/off")
    async def ingest_off() -> dict[str, bool]:
        fake.live = False
        fake.bytes = 0
        supervisor.wake()
        return {"live": False}

    # routers must be registered before the SPA catch-all handles the path
    app.router.routes.insert(0, hooks.routes[0])
    app.router.routes.insert(1, hooks.routes[1])

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    sup_task = asyncio.create_task(supervisor.run())
    print(f"e2e stack on http://127.0.0.1:{port}  (redis {redis_url}, dist {dist})", flush=True)
    try:
        await server.serve()
    finally:
        sup_task.cancel()
        with contextlib.suppress(BaseException):
            await sup_task
        redis_proc.terminate()
        with contextlib.suppress(Exception):
            redis_proc.wait(timeout=5)
        shutil.rmtree(workdir, ignore_errors=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--dist", default=str(ROOT / "web" / "dist"))
    args = parser.parse_args()
    dist = Path(args.dist)
    if not (dist / "index.html").is_file():
        print(f"{dist}/index.html missing: run `make web-build` first", file=sys.stderr)
        return 2
    return asyncio.run(main_async(args.port, dist))


if __name__ == "__main__":
    raise SystemExit(main())
