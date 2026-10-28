"""Store dedupe + manager isolation with the fake adapter (and a failing one)."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import fakeredis
import fakeredis.aioredis
import pytest
from sqlalchemy import Engine
from sqlmodel import Session, select

from chat.base import ChatMessage, Credentials
from chat.manager import ChatManager
from chat.store import ChatStore
from core.crypto import SecretBox
from core.db import init_db, make_engine
from core.models import ChatAccount
from core.models import ChatMessage as ChatMessageRow
from core.redis import CHANNEL_CHAT, chat_status_key
from core.settings import Settings


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    eng = make_engine(f"sqlite:///{tmp_path / 'chat.db'}")
    init_db(eng)
    return eng


def add_account(
    engine: Engine,
    box: SecretBox,
    platform: str,
    name: str,
    extra: dict[str, object] | None = None,
    enabled: bool = True,
) -> int:
    with Session(engine) as session:
        row = ChatAccount(
            platform=platform,
            display_name=name,
            enabled=enabled,
            credentials_encrypted=box.encrypt(Credentials(extra=extra or {}).to_json()),
        )
        session.add(row)
        session.commit()
        assert row.id is not None
        return row.id


async def test_store_persists_dedupes_and_publishes(
    engine: Engine, fake_redis: fakeredis.aioredis.FakeRedis, redis_sync: fakeredis.FakeRedis
) -> None:
    sub = redis_sync.pubsub()
    sub.subscribe(CHANNEL_CHAT)
    sub.get_message(timeout=0.1)
    store = ChatStore(fake_redis, engine)
    msg = ChatMessage(
        id="m1",
        platform="fake",
        author="a",
        author_id="1",
        text="hello",
        timestamp=datetime.now(UTC),
        badges=["moderator"],
        account_id=None,
    )
    assert await store.save(msg) is True
    assert await store.save(msg) is False  # duplicate
    event = json.loads(sub.get_message(timeout=0.5)["data"])
    assert event["type"] == "chat.message"
    assert event["id"] == "fake:m1"
    assert event["badges"] == ["moderator"]
    assert sub.get_message(timeout=0.2) is None
    with Session(engine) as session:
        rows = list(session.exec(select(ChatMessageRow)).all())
        assert len(rows) == 1
        assert rows[0].id == "fake:m1"
        assert rows[0].badges == ["moderator"]


async def wait_for(predicate, within: float = 10.0) -> None:  # type: ignore[no-untyped-def]
    deadline = asyncio.get_running_loop().time() + within
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            pytest.fail("condition not met")
        await asyncio.sleep(0.05)


def status_of(redis_sync: fakeredis.FakeRedis, account_id: int) -> dict[str, object]:
    raw = redis_sync.get(chat_status_key(account_id))
    return dict(json.loads(raw)) if raw else {}


async def test_manager_isolates_failures_and_follows_toggles(
    settings: Settings,
    engine: Engine,
    fake_redis: fakeredis.aioredis.FakeRedis,
    redis_sync: fakeredis.FakeRedis,
) -> None:
    box = SecretBox(settings.secret_keys)
    good = add_account(engine, box, "fake", "good", {"interval": 0.05})
    bad = add_account(engine, box, "fake", "bad", {"fail_connect": "boom"})
    off = add_account(engine, box, "fake", "off", enabled=False)
    cfg = settings.model_copy(update={"allow_fake_chat": True, "chat_poll_interval": 0.2})
    manager = ChatManager(cfg, redis=fake_redis, engine=engine)
    task = asyncio.create_task(manager.run())
    try:
        await wait_for(lambda: status_of(redis_sync, good).get("status") == "connected")
        await wait_for(lambda: status_of(redis_sync, bad).get("status") in ("retrying", "error"))
        assert "boom" in str(status_of(redis_sync, bad).get("last_error"))
        assert status_of(redis_sync, off).get("status") == "disabled"

        def count() -> int:
            with Session(engine) as session:
                return len(list(session.exec(select(ChatMessageRow)).all()))

        await wait_for(lambda: count() >= 3)
        assert status_of(redis_sync, good).get("status") == "connected"  # unaffected by 'bad'

        # disable good -> runner stops, status disabled; re-enable -> back
        with Session(engine) as session:
            row = session.get(ChatAccount, good)
            assert row is not None
            row.enabled = False
            session.add(row)
            session.commit()
        await wait_for(lambda: status_of(redis_sync, good).get("status") == "disabled")
        assert good not in manager.runners
        with Session(engine) as session:
            row = session.get(ChatAccount, good)
            assert row is not None
            row.enabled = True
            session.add(row)
            session.commit()
        await wait_for(lambda: status_of(redis_sync, good).get("status") == "connected")

        # delete bad -> runner removed
        with Session(engine) as session:
            row = session.get(ChatAccount, bad)
            assert row is not None
            session.delete(row)
            session.commit()
        await wait_for(lambda: bad not in manager.runners)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert manager.runners == {}
    assert status_of(redis_sync, good).get("status") == "idle"


async def test_fake_adapter_ids_are_unique_across_instances_with_the_same_account_id(
    settings: Settings,
) -> None:
    """SQLite reuses deleted account ids; message ids (the store's dedupe key) must not repeat."""
    import httpx

    from chat.adapters.fake import FakeAdapter
    from chat.base import AccountRef, AdapterContext

    ids: list[str] = []
    for _ in range(2):  # account 1 deleted and re-created
        adapter = FakeAdapter(
            AdapterContext(
                account=AccountRef(
                    id=1, platform="fake", channel_ref="", external_id="", display_name="d"
                ),
                credentials=Credentials(extra={"interval": 0.01, "limit": 3}),
                settings=settings,
                http=httpx.AsyncClient(),
            )
        )
        await adapter.send("hi")  # queued: comes out first
        stream = adapter.messages()
        ids.extend([(await anext(stream)).id for _ in range(4)])  # 1 echo + 3 generated
        await adapter.close()
    assert len(ids) == 8
    assert len(set(ids)) == 8, ids
