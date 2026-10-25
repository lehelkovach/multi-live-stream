"""Supervisor reconcile logic with a fake MediaMTX, fake workers, fakeredis and a temp DB."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import fakeredis
import fakeredis.aioredis
import pytest
from sqlalchemy import Engine
from sqlmodel import Session

from core.app_settings import set_relay_enabled
from core.crypto import SecretBox
from core.db import init_db, make_engine
from core.models import Destination, DestinationStatus
from core.redis import CHANNEL_EVENTS, KEY_INGEST_STATE, dest_status_key
from core.settings import Settings
from relay.mediamtx import PathInfo
from relay.state import StatusStore
from relay.supervisor import Supervisor
from relay.worker import WorkerConfig, WorkerStatus
from tests.conftest import INGEST_KEY


class FakeMediaMTX:
    def __init__(self) -> None:
        self.live = False
        self.bytes = 0
        self.fail = False

    async def get_path(self, name: str) -> PathInfo | None:
        if self.fail:
            msg = "connection refused"
            raise ConnectionError(msg)
        if not self.live:
            return None
        self.bytes += 250_000
        return PathInfo(
            name=name,
            ready=True,
            bytes_received=self.bytes,
            tracks=["H264"],
            source_type="rtmpConn",
        )

    async def aclose(self) -> None:
        return None


class FakeWorker:
    instances: list[FakeWorker] = []

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
        self.on_status, self.secrets = on_status, secrets
        self.started = False
        self.stopped = False
        self.status = WorkerStatus(id=dest_id, name=name)
        FakeWorker.instances.append(self)

    def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def report_live(self) -> None:
        self.status.status = DestinationStatus.LIVE
        self.status.bitrate_kbps = 2500.0
        await self.on_status(self.status, True)


@pytest.fixture(autouse=True)
def _reset_fakes() -> None:
    FakeWorker.instances.clear()


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    eng = make_engine(f"sqlite:///{tmp_path / 'relay.db'}")
    init_db(eng)
    return eng


@pytest.fixture
def box(settings: Settings) -> SecretBox:
    return SecretBox(settings.secret_keys)


@pytest.fixture
def sup(
    settings: Settings, engine: Engine, fake_redis: fakeredis.aioredis.FakeRedis
) -> tuple[Supervisor, FakeMediaMTX]:
    mtx = FakeMediaMTX()
    store = StatusStore(fake_redis, engine)
    s = Supervisor(
        settings,
        redis=fake_redis,
        engine=engine,
        mediamtx=mtx,  # type: ignore[arg-type]
        store=store,
        worker_factory=FakeWorker,  # type: ignore[arg-type]
    )
    return s, mtx


def add_dest(engine: Engine, box: SecretBox, name: str, key: str = "k", **kw: Any) -> int:
    with Session(engine) as session:
        d = Destination(name=name, rtmp_url="rtmp://dest.example/app", **kw)
        d.stream_key_encrypted = box.encrypt(key) if key else ""
        session.add(d)
        session.commit()
        assert d.id is not None
        return d.id


def read_status(redis_sync: fakeredis.FakeRedis, dest_id: int) -> dict[str, Any]:
    raw = redis_sync.get(dest_status_key(dest_id))
    assert raw is not None
    return dict(json.loads(raw))


async def test_nothing_runs_while_ingest_is_offline(
    sup: tuple[Supervisor, FakeMediaMTX],
    engine: Engine,
    box: SecretBox,
    redis_sync: fakeredis.FakeRedis,
) -> None:
    s, _ = sup
    d1 = add_dest(engine, box, "yt")
    await s.reconcile()
    assert s.workers == {}
    st = read_status(redis_sync, d1)
    assert st["status"] == "idle"
    assert st["detail"] == "no input stream"
    ingest = json.loads(redis_sync.get(KEY_INGEST_STATE) or "{}")
    assert ingest["live"] is False


async def test_workers_start_when_live_and_stop_when_offline(
    sup: tuple[Supervisor, FakeMediaMTX],
    engine: Engine,
    box: SecretBox,
    redis_sync: fakeredis.FakeRedis,
) -> None:
    s, mtx = sup
    d1 = add_dest(engine, box, "yt", key="ytkey")
    d2 = add_dest(engine, box, "twitch", key="twkey")
    add_dest(engine, box, "off", enabled=False)
    mtx.live = True
    await s.reconcile()
    assert set(s.workers) == {d1, d2}
    w1 = s.workers[d1]
    assert isinstance(w1, FakeWorker)
    assert w1.started
    assert w1.cmd[-1] == "rtmp://dest.example/app/ytkey"
    assert "ytkey" in w1.secrets
    assert INGEST_KEY in w1.secrets
    assert w1.cmd[w1.cmd.index("-i") + 1].endswith(f"/live/{INGEST_KEY}")
    assert read_status(redis_sync, 3)["status"] == "disabled"

    await s.reconcile()  # idempotent: nothing restarted
    assert len(FakeWorker.instances) == 2

    assert isinstance(w1, FakeWorker)
    await w1.report_live()
    assert read_status(redis_sync, d1)["status"] == "live"
    with Session(engine) as session:
        row = session.get(Destination, d1)
        assert row is not None
        assert row.status == "live"

    ingest = json.loads(redis_sync.get(KEY_INGEST_STATE) or "{}")
    assert ingest["live"] is True
    assert ingest["tracks"] == ["H264"]

    mtx.live = False
    await s.reconcile()
    assert s.workers == {}
    assert all(w.stopped for w in FakeWorker.instances)
    assert read_status(redis_sync, d1)["status"] == "idle"


async def test_toggle_delete_and_config_change(
    sup: tuple[Supervisor, FakeMediaMTX],
    engine: Engine,
    box: SecretBox,
    redis_sync: fakeredis.FakeRedis,
) -> None:
    s, mtx = sup
    mtx.live = True
    d1 = add_dest(engine, box, "yt", key="k1")
    d2 = add_dest(engine, box, "fb", key="k2")
    await s.reconcile()
    first = s.workers[d1]

    # disable d2 -> only d2 stops
    with Session(engine) as session:
        row = session.get(Destination, d2)
        assert row is not None
        row.enabled = False
        session.add(row)
        session.commit()
    await s.reconcile()
    assert set(s.workers) == {d1}
    assert s.workers[d1] is first  # untouched
    assert read_status(redis_sync, d2)["status"] == "disabled"

    # change d1's key -> d1 restarted with a new command
    with Session(engine) as session:
        row = session.get(Destination, d1)
        assert row is not None
        row.stream_key_encrypted = box.encrypt("rotated")
        session.add(row)
        session.commit()
    await s.reconcile()
    assert s.workers[d1] is not first
    assert first.stopped  # type: ignore[attr-defined]
    assert s.workers[d1].cmd[-1].endswith("/rotated")

    # delete d1 -> stopped and its status blob dropped
    with Session(engine) as session:
        row = session.get(Destination, d1)
        assert row is not None
        session.delete(row)
        session.commit()
    await s.reconcile()
    assert s.workers == {}
    assert redis_sync.get(dest_status_key(d1)) is None


async def test_global_stop_switch(
    sup: tuple[Supervisor, FakeMediaMTX],
    engine: Engine,
    box: SecretBox,
    redis_sync: fakeredis.FakeRedis,
) -> None:
    s, mtx = sup
    mtx.live = True
    d1 = add_dest(engine, box, "yt")
    await s.reconcile()
    assert d1 in s.workers
    with Session(engine) as session:
        set_relay_enabled(session, False)
        session.commit()
    await s.reconcile()
    assert s.workers == {}
    st = read_status(redis_sync, d1)
    assert st["status"] == "idle"
    assert st["detail"] == "relay stopped"


async def test_invalid_destination_is_reported_not_fatal(
    sup: tuple[Supervisor, FakeMediaMTX],
    engine: Engine,
    box: SecretBox,
    redis_sync: fakeredis.FakeRedis,
) -> None:
    s, mtx = sup
    mtx.live = True
    bad = add_dest(engine, box, "bad", profile="custom")  # custom without args
    good = add_dest(engine, box, "good")
    with Session(engine) as session:  # undecryptable key (e.g. rotated secret)
        row = session.get(Destination, bad)
        assert row is not None
        row.stream_key_encrypted = "gAAAAgarbage"
        session.add(row)
        session.commit()
    await s.reconcile()
    assert set(s.workers) == {good}
    st = read_status(redis_sync, bad)
    assert st["status"] == "error"
    assert st["detail"]


async def test_mediamtx_unreachable_means_offline(
    sup: tuple[Supervisor, FakeMediaMTX], engine: Engine, box: SecretBox
) -> None:
    s, mtx = sup
    mtx.live = True
    d1 = add_dest(engine, box, "yt")
    await s.reconcile()
    assert d1 in s.workers
    mtx.fail = True
    await s.reconcile()
    assert s.workers == {}
    mtx.fail = False
    await s.reconcile()
    assert d1 in s.workers


async def test_events_wake_the_loop(
    sup: tuple[Supervisor, FakeMediaMTX], fake_redis: fakeredis.aioredis.FakeRedis
) -> None:
    import asyncio

    s, _ = sup
    task = asyncio.create_task(s._listen_events())
    await asyncio.sleep(0.2)
    await fake_redis.publish(CHANNEL_EVENTS, json.dumps({"type": "destination.stats", "id": 1}))
    await asyncio.sleep(0.1)
    assert not s._wake.is_set()  # relay's own events do not wake it
    await fake_redis.publish(CHANNEL_EVENTS, json.dumps({"type": "destinations.changed", "id": 1}))
    for _ in range(50):
        if s._wake.is_set():
            break
        await asyncio.sleep(0.02)
    assert s._wake.is_set()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_shutdown_stops_everything(
    sup: tuple[Supervisor, FakeMediaMTX],
    engine: Engine,
    box: SecretBox,
    redis_sync: fakeredis.FakeRedis,
) -> None:
    s, mtx = sup
    mtx.live = True
    d1 = add_dest(engine, box, "yt")
    await s.reconcile()
    await s.shutdown()
    assert s.workers == {}
    assert all(w.stopped for w in FakeWorker.instances)
    assert read_status(redis_sync, d1)["status"] == "idle"
