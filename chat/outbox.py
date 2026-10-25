"""Deliver outgoing messages to one, several or all connected chat accounts.

The API inserts an ``outgoing_message`` row and publishes ``chat.send``; the chat
manager calls ``Outbox.dispatch(id)``. Every target is delivered in its own task
(isolation again): a slow or failing platform never delays the others. Each
target's result is written to ``per_target_status`` and broadcast as
``chat.outgoing`` so the dashboard can show ticks per platform; the row's summary
``status`` becomes sent / partial / failed when all targets are done.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from chat.base import SendResult
from chat.ratelimit import RateLimiters
from core.db import session_scope
from core.events import publish_event
from core.logging import get_logger
from core.models import DeliveryStatus, OutgoingMessage, OutgoingStatus
from core.redis import EVENT_CHAT_OUTGOING

if TYPE_CHECKING:
    from chat.manager import AccountRunner, ChatManager

log = get_logger(__name__)


def summarize(per_target: dict[str, Any]) -> str:
    statuses = [str(v.get("status")) for v in per_target.values()]
    if not statuses or any(s == DeliveryStatus.PENDING for s in statuses):
        return OutgoingStatus.PENDING
    sent = sum(1 for s in statuses if s == DeliveryStatus.SENT)
    if sent == len(statuses):
        return OutgoingStatus.SENT
    if sent == 0:
        return OutgoingStatus.FAILED
    return OutgoingStatus.PARTIAL


class Outbox:
    def __init__(self, manager: ChatManager) -> None:
        self.manager = manager
        settings = manager.settings
        self.limiters = RateLimiters(settings.chat_rate_limits, settings.chat_rate_burst)
        self._tasks: set[asyncio.Task[None]] = set()
        # per-row lock: per_target_status is a JSON column, so concurrent target
        # updates must not read-modify-write it at the same time (lost updates)
        self._locks: dict[int, asyncio.Lock] = {}

    def _lock(self, outgoing_id: int) -> asyncio.Lock:
        lock = self._locks.get(outgoing_id)
        if lock is None:
            lock = self._locks[outgoing_id] = asyncio.Lock()
        return lock

    # ---- entry points ----------------------------------------------------------

    def schedule(self, outgoing_id: int) -> None:
        task = asyncio.create_task(self.dispatch(outgoing_id), name=f"chat-outbox-{outgoing_id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def dispatch(self, outgoing_id: int) -> None:
        row = await asyncio.to_thread(self._load, outgoing_id)
        if row is None:
            log.warning("outbox.missing_row", id=outgoing_id)
            return
        targets = self._resolve_targets(row)
        if not targets:
            await self._finish(outgoing_id, {})
            return
        await asyncio.gather(
            *(
                self._deliver(outgoing_id, row.text, row.reply_to, account_id, runner)
                for account_id, runner in targets.items()
            ),
            return_exceptions=True,
        )
        await self._finish(outgoing_id)

    async def cancel_all(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(BaseException):
                await task

    # ---- internals -------------------------------------------------------------

    def _resolve_targets(self, row: OutgoingMessage) -> dict[int, AccountRunner | None]:
        """Account id -> runner (None when the account is enabled but not running)."""
        runners = self.manager.runners
        if "all" in row.targets:
            return dict(runners.items())
        out: dict[int, AccountRunner | None] = {}
        for raw in row.targets:
            try:
                account_id = int(raw)
            except ValueError:
                continue
            out[account_id] = runners.get(account_id)
        return out

    async def _deliver(
        self,
        outgoing_id: int,
        text: str,
        reply_to: str | None,
        account_id: int,
        runner: AccountRunner | None,
    ) -> None:
        platform = runner.ref.platform if runner else "?"
        name = runner.ref.display_name if runner else f"account {account_id}"
        adapter = runner.adapter if runner else None
        if adapter is None:
            await self._record(
                outgoing_id,
                account_id,
                platform,
                name,
                DeliveryStatus.OFFLINE,
                error="account not connected",
            )
            return
        bucket = self.limiters.for_account(account_id, platform)
        wait = bucket.peek_wait()
        if wait > self.manager.settings.chat_send_max_wait:
            await self._record(
                outgoing_id,
                account_id,
                platform,
                name,
                DeliveryStatus.RATE_LIMITED,
                error=f"rate limit: next slot in {wait:.0f}s",
            )
            return
        wait = bucket.reserve()
        if wait > 0:
            await asyncio.sleep(wait)
        # only pass a reply id to the platform that owns the message being answered
        reply = (
            reply_to.split(":", 1)[1] if reply_to and reply_to.startswith(f"{platform}:") else None
        )
        try:
            result: SendResult = await asyncio.wait_for(
                adapter.send(text, reply_to=reply), timeout=self.manager.settings.chat_send_timeout
            )
        except TimeoutError:
            result = SendResult(False, error="timed out")
        except Exception as exc:
            result = SendResult(False, error=f"{type(exc).__name__}: {exc}")
        status = DeliveryStatus.SENT if result.ok else DeliveryStatus.FAILED
        if not result.ok and result.error and "not implemented" in result.error:
            status = DeliveryStatus.UNSUPPORTED
        await self._record(
            outgoing_id,
            account_id,
            platform,
            name,
            status,
            error=result.error,
            message_id=result.message_id,
        )

    async def _record(
        self,
        outgoing_id: int,
        account_id: int,
        platform: str,
        name: str,
        status: str,
        *,
        error: str | None = None,
        message_id: str | None = None,
    ) -> None:
        entry = {
            "status": status,
            "platform": platform,
            "name": name,
            "error": (error or None) and str(error)[:500],
            "message_id": message_id,
            "at": time.time(),
        }
        async with self._lock(outgoing_id):
            await asyncio.to_thread(self._write_target, outgoing_id, account_id, entry)
        await publish_event(
            self.manager.redis, EVENT_CHAT_OUTGOING, id=outgoing_id, account_id=account_id, **entry
        )
        if status != DeliveryStatus.SENT:
            log.warning(
                "outbox.delivery_failed",
                id=outgoing_id,
                account_id=account_id,
                platform=platform,
                status=status,
                error=error,
            )

    async def _finish(self, outgoing_id: int, per_target: dict[str, Any] | None = None) -> None:
        async with self._lock(outgoing_id):
            summary = await asyncio.to_thread(self._write_summary, outgoing_id, per_target)
        self._locks.pop(outgoing_id, None)
        await publish_event(
            self.manager.redis,
            EVENT_CHAT_OUTGOING,
            id=outgoing_id,
            account_id=None,
            status=summary,
            done=True,
        )

    def _load(self, outgoing_id: int) -> OutgoingMessage | None:
        with session_scope(self.manager.engine) as session:
            row = session.get(OutgoingMessage, outgoing_id)
            if row is not None:
                session.expunge(row)
            return row

    def _write_target(self, outgoing_id: int, account_id: int, entry: dict[str, Any]) -> None:
        with session_scope(self.manager.engine) as session:
            row = session.get(OutgoingMessage, outgoing_id)
            if row is None:
                return
            per_target = dict(row.per_target_status)
            per_target[str(account_id)] = entry
            row.per_target_status = per_target
            session.add(row)

    def _write_summary(self, outgoing_id: int, per_target: dict[str, Any] | None) -> str:
        with session_scope(self.manager.engine) as session:
            row = session.get(OutgoingMessage, outgoing_id)
            if row is None:
                return OutgoingStatus.FAILED
            if per_target is not None:
                row.per_target_status = per_target
            row.status = (
                summarize(row.per_target_status) if row.per_target_status else OutgoingStatus.FAILED
            )
            row.finished_at = datetime.now(UTC)
            session.add(row)
            return row.status
