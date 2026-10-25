"""Reconcile loop: desired outputs (DB + ingest state) vs running ffmpeg workers.

Every ``relay_poll_interval`` seconds, or immediately when the API publishes a
change event, the supervisor:

1. asks MediaMTX whether the ingest path is live,
2. loads the global relay switch + destinations from the DB,
3. starts a worker for every enabled destination that has none (if live),
   stops workers whose destination vanished / was disabled / changed, and
4. refreshes status blobs in Redis for everything so the dashboard is current.

A worker failing never influences another: each is its own task + process, and
all cross-worker interaction goes through this loop's bookkeeping only.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import redis.asyncio as aioredis
from sqlalchemy import Engine
from sqlmodel import Session, col, select

from core.app_settings import relay_enabled
from core.crypto import DecryptError, SecretBox
from core.db import init_db, make_engine
from core.logging import get_logger
from core.models import Destination, DestinationStatus
from core.redis import (
    CHANNEL_EVENTS,
    EVENT_DESTINATIONS_CHANGED,
    EVENT_INGEST_PUBLISH,
    EVENT_RELAY_CHANGED,
    make_redis,
)
from core.settings import Settings
from relay.ffmpeg import OutputSpec, build_command
from relay.mediamtx import MediaMTXClient, PathInfo
from relay.state import StatusStore
from relay.worker import DestinationWorker, StatusCallback, WorkerConfig

log = get_logger(__name__)

WAKE_EVENTS = {EVENT_DESTINATIONS_CHANGED, EVENT_RELAY_CHANGED, EVENT_INGEST_PUBLISH}

WorkerFactory = Callable[..., DestinationWorker]


class Supervisor:
    def __init__(
        self,
        settings: Settings,
        *,
        redis: aioredis.Redis | None = None,
        engine: Engine | None = None,
        mediamtx: MediaMTXClient | None = None,
        store: StatusStore | None = None,
        worker_factory: WorkerFactory | None = None,
    ) -> None:
        self.settings = settings
        self._redis = redis
        self._engine = engine
        self._mediamtx = mediamtx
        self._store = store
        self._factory: WorkerFactory = worker_factory or DestinationWorker
        self._box = SecretBox(settings.secret_keys)
        self.workers: dict[int, DestinationWorker] = {}
        self._wake = asyncio.Event()
        self._ingest_prev: tuple[float, int] | None = None
        self._ingest_since: str | None = None
        self._mediamtx_down_logged = False
        self.last_ingest_live = False
        self.worker_config = WorkerConfig(
            backoff_base=settings.relay_backoff_base,
            backoff_max=settings.relay_backoff_max,
            stall_timeout=settings.relay_stall_timeout,
            healthy_after=settings.relay_healthy_after,
            error_after_failures=settings.relay_error_after_failures,
        )

    # ---- wiring --------------------------------------------------------------

    @property
    def redis(self) -> aioredis.Redis:
        if self._redis is None:
            self._redis = make_redis(self.settings.redis_url)
        return self._redis

    @property
    def engine(self) -> Engine:
        if self._engine is None:
            self._engine = make_engine(self.settings.database_url)
            init_db(self._engine)
        return self._engine

    @property
    def mediamtx(self) -> MediaMTXClient:
        if self._mediamtx is None:
            self._mediamtx = MediaMTXClient(self.settings.mediamtx_api_url)
        return self._mediamtx

    @property
    def store(self) -> StatusStore:
        if self._store is None:
            self._store = StatusStore(self.redis, self.engine)
        return self._store

    def wake(self) -> None:
        self._wake.set()

    # ---- main loop -----------------------------------------------------------

    async def run(self) -> None:
        log.info(
            "relay.started",
            poll_interval=self.settings.relay_poll_interval,
            mediamtx=self.settings.mediamtx_api_url,
        )
        listener = asyncio.create_task(self._listen_events(), name="relay-events")
        try:
            while True:
                try:
                    await self.reconcile()
                except Exception:
                    log.exception("relay.reconcile_failed")
                with contextlib.suppress(Exception):
                    await self.store.heartbeat()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._wake.wait(), timeout=self.settings.relay_poll_interval
                    )
                self._wake.clear()
        finally:
            listener.cancel()
            with contextlib.suppress(BaseException):
                await listener
            await self.shutdown()

    async def shutdown(self) -> None:
        log.info("relay.stopping", workers=len(self.workers))
        workers = list(self.workers.items())
        self.workers.clear()
        await asyncio.gather(*(w.stop() for _, w in workers), return_exceptions=True)
        for dest_id, w in workers:
            with contextlib.suppress(Exception):
                await self.store.write_inactive(
                    dest_id, w.name, DestinationStatus.IDLE, detail="relay shutting down"
                )
        if self._mediamtx is not None:
            await self._mediamtx.aclose()
        if self._redis is not None:
            with contextlib.suppress(Exception):
                await self._redis.aclose()
        log.info("relay.stopped")

    # ---- reconcile -----------------------------------------------------------

    async def reconcile(self) -> None:
        ingest = await self._probe_ingest()
        live = ingest is not None and ingest.ready
        relay_on, rows = await asyncio.to_thread(self._load_rows)

        desired: dict[int, tuple[Destination, list[str], str]] = {}
        invalid: dict[int, str] = {}
        if live and relay_on:
            for dest in rows:
                if not dest.enabled or dest.id is None:
                    continue
                try:
                    key = (
                        self._box.decrypt(dest.stream_key_encrypted)
                        if dest.stream_key_encrypted
                        else ""
                    )
                    spec = OutputSpec(dest.output_url(key), dest.profile, dest.ffmpeg_args)
                    cmd = build_command(
                        self.settings.ffmpeg_bin, self.settings.ingest_rtsp_url, spec
                    )
                except (DecryptError, ValueError) as exc:
                    invalid[dest.id] = str(exc)
                    continue
                desired[dest.id] = (dest, cmd, key)

        # stop what should not run (gone, disabled, changed, input offline, relay off)
        for dest_id, worker in list(self.workers.items()):
            want = desired.get(dest_id)
            if want is None or want[1] != worker.cmd:
                reason = "config changed" if want is not None else "stopped"
                log.info("relay.stop_worker", dest_id=dest_id, reason=reason)
                await worker.stop()
                del self.workers[dest_id]

        # start what is missing
        for dest_id, (dest, cmd, key) in desired.items():
            if dest_id in self.workers:
                continue
            on_status: StatusCallback = self.store.write_worker
            worker = self._factory(
                dest_id,
                dest.name,
                cmd,
                config=self.worker_config,
                on_status=on_status,
                secrets=[key, self.settings.ingest_stream_key.get_secret_value()],
            )
            worker.start()
            self.workers[dest_id] = worker
            log.info("relay.start_worker", dest_id=dest_id, dest=dest.name, platform=dest.platform)

        # status for everything without a worker
        known_ids = {d.id for d in rows if d.id is not None}
        for dest in rows:
            if dest.id is None or dest.id in self.workers:
                continue
            if dest.id in invalid:
                await self.store.write_inactive(
                    dest.id, dest.name, DestinationStatus.ERROR, invalid[dest.id]
                )
            elif not dest.enabled:
                await self.store.write_inactive(dest.id, dest.name, DestinationStatus.DISABLED)
            elif not relay_on:
                await self.store.write_inactive(
                    dest.id, dest.name, DestinationStatus.IDLE, "relay stopped"
                )
            elif not live:
                await self.store.write_inactive(
                    dest.id, dest.name, DestinationStatus.IDLE, "no input stream"
                )
            else:
                await self.store.write_inactive(dest.id, dest.name, DestinationStatus.IDLE)
        for stale_id in [i for i in self.workers if i not in known_ids]:
            # row deleted while running: already stopped above, drop its blob
            await self.store.forget(stale_id)

        self.last_ingest_live = live
        await self.store.write_ingest(self._ingest_state(ingest, live))

    def _load_rows(self) -> tuple[bool, list[Destination]]:
        with Session(self.engine) as session:
            enabled = relay_enabled(session)
            rows = list(session.exec(select(Destination).order_by(col(Destination.id))).all())
            session.expunge_all()
            return enabled, rows

    async def _probe_ingest(self) -> PathInfo | None:
        try:
            info = await self.mediamtx.get_path(self.settings.ingest_path)
        except Exception as exc:
            if not self._mediamtx_down_logged:
                log.warning("relay.mediamtx_unreachable", error=str(exc))
                self._mediamtx_down_logged = True
            return None
        if self._mediamtx_down_logged:
            log.info("relay.mediamtx_reachable")
            self._mediamtx_down_logged = False
        return info

    def _ingest_state(self, info: PathInfo | None, live: bool) -> dict[str, Any]:
        now = time.monotonic()
        bitrate = 0.0
        if live and info is not None:
            if self._ingest_prev is not None:
                t0, b0 = self._ingest_prev
                if now > t0 and info.bytes_received >= b0:
                    bitrate = round((info.bytes_received - b0) * 8 / (now - t0) / 1000.0, 1)
            self._ingest_prev = (now, info.bytes_received)
            if self._ingest_since is None:
                self._ingest_since = info.ready_time or datetime.now(UTC).isoformat()
        else:
            self._ingest_prev = None
            self._ingest_since = None
        return {
            "live": live,
            "since": self._ingest_since,
            "bitrate_kbps": bitrate,
            "tracks": info.tracks if (live and info) else [],
            "source": info.source_type if (live and info) else None,
            "updated_at": datetime.now(UTC).isoformat(),
        }

    # ---- events --------------------------------------------------------------

    async def _listen_events(self) -> None:
        while True:
            pubsub = None
            try:
                pubsub = self.redis.pubsub()
                await pubsub.subscribe(CHANNEL_EVENTS)
                async for message in pubsub.listen():
                    if message.get("type") != "message":
                        continue
                    try:
                        event = json.loads(message["data"])
                    except (TypeError, ValueError):
                        continue
                    if isinstance(event, dict) and event.get("type") in WAKE_EVENTS:
                        log.debug("relay.wake", kind=event.get("type"))
                        self.wake()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("relay.events_disconnected", error=str(exc))
                await asyncio.sleep(2.0)
            finally:
                if pubsub is not None:
                    with contextlib.suppress(Exception):
                        await pubsub.aclose()  # type: ignore[no-untyped-call]
