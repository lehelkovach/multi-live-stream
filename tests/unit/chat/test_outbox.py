"""Fan-out to fake adapters, partial failure reporting and rate limiting."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import fakeredis
import fakeredis.aioredis
import pytest
from sqlalchemy import Engine
from sqlmodel import Session

from chat.base import Credentials
from chat.manager import ChatManager
from chat.outbox import summarize
from chat.ratelimit import RateLimiters, TokenBucket
from core.crypto import SecretBox
from core.db import init_db, make_engine
from core.models import ChatAccount, OutgoingMessage
from core.redis import CHANNEL_EVENTS, chat_status_key
from core.settings import Settings


def test_token_bucket_reserve_and_wait() -> None:
    now = [100.0]
    bucket = TokenBucket(rate=1.0, burst=2, clock=lambda: now[0])
    assert bucket.reserve() == 0.0
    assert bucket.reserve() == 0.0
    assert bucket.reserve() == pytest.approx(1.0)  # third caller waits one second
    assert bucket.reserve() == pytest.approx(2.0)  # queued behind the previous one
    now[0] += 2.0  # the 4th reservation owns this second; a newcomer still waits
    assert bucket.peek_wait() == pytest.approx(1.0)
    now[0] += 1.0
    assert bucket.peek_wait() == pytest.approx(0.0)
    assert bucket.reserve() == 0.0
    assert bucket.reserve() == pytest.approx(1.0)


def test_rate_limiters_are_per_account() -> None:
    now = [0.0]
    limiters = RateLimiters({"twitch": 0.5}, {"twitch": 1}, clock=lambda: now[0])
    a = limiters.for_account(1, "twitch")
    b = limiters.for_account(2, "twitch")
    assert a is limiters.for_account(1, "twitch")
    assert a is not b
    assert a.reserve() == 0.0
    assert a.reserve() == pytest.approx(2.0)
    assert b.reserve() == 0.0


def test_summarize() -> None:
    assert summarize({}) == "pending"
    assert summarize({"1": {"status": "pending"}}) == "pending"
    assert summarize({"1": {"status": "sent"}, "2": {"status": "sent"}}) == "sent"
    assert summarize({"1": {"status": "sent"}, "2": {"status": "failed"}}) == "partial"
    assert summarize({"1": {"status": "rate_limited"}, "2": {"status": "offline"}}) == "failed"


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    eng = make_engine(f"sqlite:///{tmp_path / 'outbox.db'}")
    init_db(eng)
    return eng


def add_account(
    engine: Engine,
    box: SecretBox,
    name: str,
    extra: dict[str, object] | None = None,
    enabled: bool = True,
) -> int:
    with Session(engine) as session:
        row = ChatAccount(
            platform="fake",
            display_name=name,
            enabled=enabled,
            credentials_encrypted=box.encrypt(Credentials(extra=extra or {}).to_json()),
        )
        session.add(row)
        session.commit()
        assert row.id is not None
        return row.id


def queue(engine: Engine, text: str, targets: list[str], reply_to: str | None = None) -> int:
    with Session(engine) as session:
        row = OutgoingMessage(text=text, targets=targets, per_target_status={}, reply_to=reply_to)
        session.add(row)
        session.commit()
        assert row.id is not None
        return row.id


def load(engine: Engine, outgoing_id: int) -> OutgoingMessage:
    with Session(engine) as session:
        row = session.get(OutgoingMessage, outgoing_id)
        assert row is not None
        session.expunge(row)
        return row


async def wait_for(predicate, within: float = 10.0) -> None:  # type: ignore[no-untyped-def]
    deadline = asyncio.get_running_loop().time() + within
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            pytest.fail("condition not met")
        await asyncio.sleep(0.05)


async def test_fan_out_partial_failure_offline_and_events(
    settings: Settings,
    engine: Engine,
    fake_redis: fakeredis.aioredis.FakeRedis,
    redis_sync: fakeredis.FakeRedis,
) -> None:
    box = SecretBox(settings.secret_keys)
    good = add_account(engine, box, "good", {"interval": 60, "limit": 0})
    bad = add_account(engine, box, "bad", {"interval": 60, "fail_send": True})
    off = add_account(engine, box, "off", enabled=False)
    cfg = settings.model_copy(update={"allow_fake_chat": True, "chat_poll_interval": 0.2})
    manager = ChatManager(cfg, redis=fake_redis, engine=engine)
    sub = redis_sync.pubsub()
    sub.subscribe(CHANNEL_EVENTS)
    sub.get_message(timeout=0.1)
    task = asyncio.create_task(manager.run())
    try:
        await wait_for(
            lambda: (
                json.loads(redis_sync.get(chat_status_key(good)) or "{}").get("status")
                == "connected"
            )
        )
        await wait_for(
            lambda: (
                json.loads(redis_sync.get(chat_status_key(bad)) or "{}").get("status")
                == "connected"
            )
        )

        # broadcast to all: good sent, bad failed -> partial
        all_id = queue(engine, "hello everyone", ["all"])
        await fake_redis.publish(CHANNEL_EVENTS, json.dumps({"type": "chat.send", "id": all_id}))
        await wait_for(lambda: load(engine, all_id).status != "pending")
        row = load(engine, all_id)
        assert row.status == "partial"
        assert row.per_target_status[str(good)]["status"] == "sent"
        assert row.per_target_status[str(bad)]["status"] == "failed"
        assert "rejected" in row.per_target_status[str(bad)]["error"]
        assert str(off) not in row.per_target_status  # disabled accounts are not targets

        # explicit targets incl. an account that is not running -> offline
        some_id = queue(engine, "just you", [str(good), str(off)])
        await fake_redis.publish(CHANNEL_EVENTS, json.dumps({"type": "chat.send", "id": some_id}))
        await wait_for(lambda: load(engine, some_id).status != "pending")
        row = load(engine, some_id)
        assert row.per_target_status[str(good)]["status"] == "sent"
        assert row.per_target_status[str(off)]["status"] == "offline"
        assert row.status == "partial"

        # the fake adapter echoes own messages -> they show up in history as is_own
        from sqlmodel import select

        from core.models import ChatMessage as ChatMessageRow

        def own_count() -> int:
            with Session(engine) as session:
                return len([m for m in session.exec(select(ChatMessageRow)).all() if m.is_own])

        await wait_for(lambda: own_count() >= 2)

        events = []
        while (msg := sub.get_message(timeout=0.2)) is not None:
            if msg["type"] == "message":
                data = json.loads(msg["data"])
                if data["type"] == "chat.outgoing":
                    events.append(data)
        per_target = [e for e in events if e.get("account_id") is not None]
        done = [e for e in events if e.get("done")]
        assert {e["status"] for e in per_target} >= {"sent", "failed", "offline"}
        assert [e["status"] for e in done] == ["partial", "partial"]
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_rate_limit_marks_targets(
    settings: Settings,
    engine: Engine,
    fake_redis: fakeredis.aioredis.FakeRedis,
    redis_sync: fakeredis.FakeRedis,
) -> None:
    box = SecretBox(settings.secret_keys)
    acc = add_account(engine, box, "limited", {"interval": 60})
    cfg = settings.model_copy(
        update={
            "allow_fake_chat": True,
            "chat_poll_interval": 0.2,
            "chat_rate_limits": {"fake": 0.1},  # one message per 10 s
            "chat_rate_burst": {"fake": 1},
            "chat_send_max_wait": 1.0,
        }
    )
    manager = ChatManager(cfg, redis=fake_redis, engine=engine)
    task = asyncio.create_task(manager.run())
    try:
        await wait_for(
            lambda: (
                json.loads(redis_sync.get(chat_status_key(acc)) or "{}").get("status")
                == "connected"
            )
        )
        first = queue(engine, "one", ["all"])
        second = queue(engine, "two", ["all"])
        await manager.outbox.dispatch(first)
        await manager.outbox.dispatch(second)
        assert load(engine, first).status == "sent"
        row = load(engine, second)
        assert row.status == "failed"
        assert row.per_target_status[str(acc)]["status"] == "rate_limited"
        assert "next slot" in row.per_target_status[str(acc)]["error"]
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_reply_id_only_goes_to_owning_platform(
    settings: Settings, engine: Engine, fake_redis: fakeredis.aioredis.FakeRedis
) -> None:
    """reply_to 'twitch:abc' must not be passed to a fake-platform adapter."""
    from chat.base import SendResult

    box = SecretBox(settings.secret_keys)
    acc = add_account(engine, box, "fk", {"interval": 60})
    cfg = settings.model_copy(update={"allow_fake_chat": True, "chat_poll_interval": 0.2})
    manager = ChatManager(cfg, redis=fake_redis, engine=engine)
    task = asyncio.create_task(manager.run())
    seen: list[str | None] = []
    try:
        await wait_for(lambda: acc in manager.runners and manager.runners[acc].adapter is not None)
        adapter = manager.runners[acc].adapter
        assert adapter is not None

        async def spy(text: str, *, reply_to: str | None = None) -> SendResult:
            seen.append(reply_to)
            return SendResult(True, message_id="x")

        adapter.send = spy  # type: ignore[method-assign]
        await manager.outbox.dispatch(queue(engine, "r1", ["all"], reply_to="twitch:abc"))
        await manager.outbox.dispatch(queue(engine, "r2", ["all"], reply_to="fake:7-1"))
        assert seen == [None, "7-1"]
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
