"""Fake platform for tests, demos and the Playwright harness.

Emits a message every ``extra.interval`` seconds (default 2) from a rotating cast,
and ``send()`` echoes the text back as an own message so the UI round-trip can be
exercised without any platform account. Only creatable when ``OMNI_ALLOW_FAKE_CHAT``
is true.
"""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from chat.base import AdapterContext, ChatMessage, SendResult

CAST = [
    ("viewer_one", "v1", "hello from the fake platform 👋", []),
    ("mod_mia", "v2", "testing the unified chat", ["moderator"]),
    ("sub_sam", "v3", "nice stream!", ["subscriber"]),
]


class FakeAdapter:
    platform = "fake"

    def __init__(self, ctx: AdapterContext) -> None:
        self.ctx = ctx
        self.interval = float(ctx.credentials.extra.get("interval") or 2.0)
        self.limit = int(ctx.credentials.extra.get("limit") or 0)  # 0 = unlimited
        self._queue: asyncio.Queue[ChatMessage] = asyncio.Queue()
        self._counter = itertools.count(1)
        self._stop = False

    async def connect(self) -> None:
        if self.ctx.credentials.extra.get("fail_connect"):
            msg = str(self.ctx.credentials.extra["fail_connect"])
            raise RuntimeError(msg)

    async def messages(self) -> AsyncIterator[ChatMessage]:
        cast = itertools.cycle(CAST)
        emitted = 0
        while not self._stop and (self.limit == 0 or emitted < self.limit):
            try:
                own = await asyncio.wait_for(self._queue.get(), timeout=self.interval)
                yield own
                continue
            except TimeoutError:
                pass
            author, author_id, text, badges = next(cast)
            n = next(self._counter)
            emitted += 1
            yield ChatMessage(
                id=f"{self.ctx.account.id}-{n}",
                platform="fake",
                author=author,
                author_id=author_id,
                text=f"{text} #{n}",
                timestamp=datetime.now(UTC),
                badges=list(badges),
                account_id=self.ctx.account.id,
            )
        while not self._stop:  # limit reached: stay connected, only echo own messages
            yield await self._queue.get()

    async def send(self, text: str) -> SendResult:
        n = next(self._counter)
        await self._queue.put(
            ChatMessage(
                id=f"{self.ctx.account.id}-own-{n}",
                platform="fake",
                author=self.ctx.account.display_name or "me",
                author_id="me",
                text=text,
                timestamp=datetime.now(UTC),
                is_own=True,
                account_id=self.ctx.account.id,
            )
        )
        return SendResult(True, message_id=f"{self.ctx.account.id}-own-{n}")

    async def close(self) -> None:
        self._stop = True
