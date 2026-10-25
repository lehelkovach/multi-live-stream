"""Chat manager: one supervised runner per enabled chat account.

Each runner builds its adapter, iterates ``messages()`` and hands every message to
the ``ChatStore``. Failures are contained to the runner: ``NotLiveError`` waits for
the next broadcast (faster while our own ingest is live), ``AuthError`` /
``ConfigError`` wait five minutes (the owner has to act), anything else backs off
exponentially. Config changes (enable/disable/edit) are picked up on the next
reconcile pass or immediately via the ``chat.accounts.changed`` Redis event.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
import time
from datetime import datetime
from typing import Any

import httpx
import redis.asyncio as aioredis
from sqlalchemy import Engine
from sqlmodel import Session, col, select

from chat.base import (
    AccountRef,
    AdapterContext,
    AdapterError,
    AuthError,
    ChatAdapter,
    ConfigError,
    Credentials,
    NotLiveError,
)
from chat.registry import make_adapter
from chat.store import ChatStore
from core.crypto import DecryptError, SecretBox
from core.db import init_db, make_engine, session_scope
from core.logging import get_logger
from core.models import ChatAccount, ChatAccountStatus, utcnow
from core.redis import (
    CHANNEL_EVENTS,
    EVENT_CHAT_ACCOUNTS_CHANGED,
    EVENT_INGEST_STATE,
    KEY_INGEST_STATE,
    make_redis,
)
from core.settings import Settings

log = get_logger(__name__)

AUTH_RETRY = 300.0
NOT_LIVE_RETRY_WHEN_INGEST_LIVE = 60.0
NOT_LIVE_RETRY_WHEN_IDLE = 300.0
BACKOFF_BASE = 2.0
BACKOFF_MAX = 120.0
ERROR_AFTER = 5


class AccountRunner:
    def __init__(self, manager: ChatManager, row: ChatAccount) -> None:
        assert row.id is not None
        self.manager = manager
        self.account_id: int = row.id
        self.version = row.updated_at
        self.ref = AccountRef(
            id=row.id,
            platform=row.platform,
            channel_ref=row.channel_ref,
            external_id=row.external_id,
            display_name=row.display_name,
        )
        self.credentials_raw = row.credentials_encrypted
        self.adapter: ChatAdapter | None = None
        self.failures = 0
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._current: tuple[str, str | None, str | None] | None = None
        self._log = log.bind(account_id=row.id, platform=row.platform, name=row.display_name)

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name=f"chat-account-{self.account_id}")

    def wake(self) -> None:
        self._wake.set()

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self.adapter is not None:
            with contextlib.suppress(Exception):
                await self.adapter.close()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(BaseException):
                await self._task
            self._task = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def _status(
        self, status: str, detail: str | None = None, error: str | None = None
    ) -> None:
        await self.manager.store.set_status(
            self.account_id,
            platform=self.ref.platform,
            name=self.ref.display_name,
            status=status,
            detail=detail,
            error=error,
        )

    async def _run(self) -> None:
        while not self._stop.is_set():
            delay = BACKOFF_BASE
            adapter: ChatAdapter | None = None
            try:
                ctx = self.manager.build_context(self)
                adapter = make_adapter(self.ref.platform, ctx)
                self.adapter = adapter
                await self._status(ChatAccountStatus.CONNECTING)
                await adapter.connect()
                self.failures = 0
                await self._status(ChatAccountStatus.CONNECTED)
                async for message in adapter.messages():
                    message.account_id = self.account_id
                    await self.manager.store.save(message)
                    if self._stop.is_set():
                        break
                if self._stop.is_set():
                    break
                delay = 1.0  # stream ended cleanly: reconnect promptly
                await self._status(ChatAccountStatus.RETRYING, detail="stream ended, reconnecting")
            except asyncio.CancelledError:
                raise
            except NotLiveError as exc:
                delay = await self.manager.not_live_delay()
                await self._status(ChatAccountStatus.WAITING, detail=str(exc))
            except AuthError as exc:
                delay = AUTH_RETRY
                self._log.warning("chat.auth_error", error=str(exc))
                await self._status(ChatAccountStatus.AUTH_ERROR, error=str(exc))
            except ConfigError as exc:
                delay = AUTH_RETRY
                self._log.warning("chat.config_error", error=str(exc))
                await self._status(ChatAccountStatus.ERROR, error=str(exc))
            except (AdapterError, Exception) as exc:
                self.failures += 1
                delay = min(BACKOFF_BASE * 2 ** (self.failures - 1), BACKOFF_MAX) * random.uniform(
                    1.0, 1.2
                )
                status = (
                    ChatAccountStatus.ERROR
                    if self.failures >= ERROR_AFTER
                    else ChatAccountStatus.RETRYING
                )
                self._log.warning(
                    "chat.adapter_failed",
                    error=f"{type(exc).__name__}: {exc}",
                    failures=self.failures,
                    retry_in=round(delay, 1),
                )
                await self._status(status, error=f"{type(exc).__name__}: {exc}"[:500])
            finally:
                if adapter is not None:
                    with contextlib.suppress(Exception):
                        await adapter.close()
                self.adapter = None
            if self._stop.is_set():
                break
            self._wake.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=delay)


class ChatManager:
    def __init__(
        self,
        settings: Settings,
        *,
        redis: aioredis.Redis | None = None,
        engine: Engine | None = None,
        store: ChatStore | None = None,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self._redis = redis
        self._engine = engine
        self._store = store
        self._http = http
        self._box = SecretBox(settings.secret_keys)
        self.runners: dict[int, AccountRunner] = {}
        self._wake = asyncio.Event()

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
    def store(self) -> ChatStore:
        if self._store is None:
            self._store = ChatStore(self.redis, self.engine)
        return self._store

    @property
    def http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(headers={"User-Agent": "OmniStream/0.1 (+chat)"})
        return self._http

    def wake(self) -> None:
        self._wake.set()

    def build_context(self, runner: AccountRunner) -> AdapterContext:
        try:
            creds = (
                Credentials.from_json(self._box.decrypt(runner.credentials_raw))
                if runner.credentials_raw
                else Credentials()
            )
        except (DecryptError, ValueError) as exc:
            msg = f"stored credentials cannot be decrypted ({exc}); reconnect the account"
            raise ConfigError(msg) from exc

        async def save_credentials(new: Credentials) -> None:
            runner.credentials_raw = self._box.encrypt(new.to_json())
            await asyncio.to_thread(
                self._write_credentials, runner.account_id, runner.credentials_raw
            )

        async def save_identity(external_id: str, display_name: str, channel_ref: str) -> None:
            runner.ref.external_id = external_id
            runner.ref.display_name = display_name
            runner.ref.channel_ref = channel_ref
            await asyncio.to_thread(
                self._write_identity, runner.account_id, external_id, display_name, channel_ref
            )

        return AdapterContext(
            account=runner.ref,
            credentials=creds,
            settings=self.settings,
            http=self.http,
            redis=self.redis,
            save_credentials=save_credentials,
            save_identity=save_identity,
        )

    def _write_credentials(self, account_id: int, encrypted: str) -> None:
        with session_scope(self.engine) as session:
            row = session.get(ChatAccount, account_id)
            if row is not None:
                row.credentials_encrypted = encrypted  # deliberately no updated_at bump
                session.add(row)

    def _write_identity(
        self, account_id: int, external_id: str, display_name: str, channel_ref: str
    ) -> None:
        with session_scope(self.engine) as session:
            row = session.get(ChatAccount, account_id)
            if row is not None:
                row.external_id = external_id
                row.display_name = display_name or row.display_name
                row.channel_ref = channel_ref or row.channel_ref
                session.add(row)

    async def not_live_delay(self) -> float:
        try:
            raw = await self.redis.get(KEY_INGEST_STATE)
            live = bool(json.loads(raw).get("live")) if raw else False
        except Exception:
            live = False
        return NOT_LIVE_RETRY_WHEN_INGEST_LIVE if live else NOT_LIVE_RETRY_WHEN_IDLE

    # ---- main loop -----------------------------------------------------------

    async def run(self) -> None:
        log.info("chat.started", poll_interval=self.settings.chat_poll_interval)
        listener = asyncio.create_task(self._listen_events(), name="chat-events")
        try:
            while True:
                try:
                    await self.reconcile()
                except Exception:
                    log.exception("chat.reconcile_failed")
                with contextlib.suppress(Exception):
                    await self.store.heartbeat()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._wake.wait(), timeout=self.settings.chat_poll_interval
                    )
                self._wake.clear()
        finally:
            listener.cancel()
            with contextlib.suppress(BaseException):
                await listener
            await self.shutdown()

    async def shutdown(self) -> None:
        runners = list(self.runners.values())
        self.runners.clear()
        await asyncio.gather(*(r.stop() for r in runners), return_exceptions=True)
        for r in runners:
            with contextlib.suppress(Exception):
                await self.store.set_status(
                    r.account_id,
                    platform=r.ref.platform,
                    name=r.ref.display_name,
                    status=ChatAccountStatus.IDLE,
                    detail="chat service stopped",
                )
        if self._http is not None:
            await self._http.aclose()
        if self._redis is not None:
            with contextlib.suppress(Exception):
                await self._redis.aclose()
        log.info("chat.stopped")

    def _load_rows(self) -> list[ChatAccount]:
        with Session(self.engine) as session:
            rows = list(session.exec(select(ChatAccount).order_by(col(ChatAccount.id))).all())
            session.expunge_all()
            return rows

    async def reconcile(self) -> None:
        rows = await asyncio.to_thread(self._load_rows)
        wanted: dict[int, ChatAccount] = {r.id: r for r in rows if r.enabled and r.id is not None}
        for account_id, runner in list(self.runners.items()):
            row = wanted.get(account_id)
            if row is None or row.updated_at != runner.version or not runner.running:
                reason = (
                    "removed"
                    if row is None
                    else ("changed" if row.updated_at != runner.version else "crashed")
                )
                log.info("chat.stop_runner", account_id=account_id, reason=reason)
                await runner.stop()
                del self.runners[account_id]
        for account_id, row in wanted.items():
            if account_id not in self.runners:
                runner = AccountRunner(self, row)
                runner.start()
                self.runners[account_id] = runner
                log.info(
                    "chat.start_runner",
                    account_id=account_id,
                    platform=row.platform,
                    name=row.display_name,
                )
        known = {r.id for r in rows}
        for row in rows:
            if row.id is not None and not row.enabled:
                await self.store.set_status(
                    row.id,
                    platform=row.platform,
                    name=row.display_name,
                    status=ChatAccountStatus.DISABLED,
                )
        for stale in [i for i in list(self.runners) if i not in known]:
            await self.store.forget(stale)

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
                        event: Any = json.loads(message["data"])
                    except (TypeError, ValueError):
                        continue
                    if not isinstance(event, dict):
                        continue
                    kind = event.get("type")
                    if kind == EVENT_CHAT_ACCOUNTS_CHANGED:
                        self.wake()
                    elif kind == EVENT_INGEST_STATE and event.get("live"):
                        for runner in self.runners.values():
                            runner.wake()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("chat.events_disconnected", error=str(exc))
                await asyncio.sleep(2.0)
            finally:
                if pubsub is not None:
                    with contextlib.suppress(Exception):
                        await pubsub.aclose()  # type: ignore[no-untyped-call]


def touch(row: ChatAccount) -> None:
    """Bump ``updated_at`` so the manager restarts that account's runner."""
    row.updated_at = utcnow()


__all__ = ["AccountRunner", "ChatManager", "touch"]
_ = (time, datetime)  # re-exported names kept for type checkers
