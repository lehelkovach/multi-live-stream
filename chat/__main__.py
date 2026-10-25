"""``python -m chat``: run the chat manager. ``--healthcheck`` exits 0 if alive."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
import sys
import time

from chat.manager import ChatManager
from core.logging import configure_logging
from core.redis import KEY_CHAT_HEARTBEAT, make_redis
from core.settings import Settings, get_settings


async def healthcheck(settings: Settings, max_age: float = 30.0) -> int:
    redis = make_redis(settings.redis_url)
    try:
        raw = await redis.get(KEY_CHAT_HEARTBEAT)
    finally:
        await redis.aclose()
    if raw is None:
        return 1
    try:
        return 0 if time.time() - float(raw) < max_age else 1
    except ValueError:
        return 1


async def serve(settings: Settings) -> int:
    manager = ChatManager(settings)
    task = asyncio.create_task(manager.run(), name="chat-manager")
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, task.cancel)
    with contextlib.suppress(asyncio.CancelledError):
        await task
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m chat")
    parser.add_argument("--healthcheck", action="store_true")
    args = parser.parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    if args.healthcheck:
        return asyncio.run(healthcheck(settings))
    return asyncio.run(serve(settings))


if __name__ == "__main__":
    sys.exit(main())
