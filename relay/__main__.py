"""``python -m relay``: run the supervisor. ``--healthcheck`` exits 0 if it is alive."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
import sys
import time

from core.logging import configure_logging, get_logger
from core.redis import KEY_RELAY_HEARTBEAT, make_redis
from core.settings import Settings, get_settings
from relay.supervisor import Supervisor

log = get_logger(__name__)


async def healthcheck(settings: Settings, max_age: float = 15.0) -> int:
    redis = make_redis(settings.redis_url)
    try:
        raw = await redis.get(KEY_RELAY_HEARTBEAT)
    finally:
        await redis.aclose()
    if raw is None:
        return 1
    try:
        return 0 if time.time() - float(raw) < max_age else 1
    except ValueError:
        return 1


async def serve(settings: Settings) -> int:
    supervisor = Supervisor(settings)
    task = asyncio.create_task(supervisor.run(), name="relay-supervisor")
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, task.cancel)
    with contextlib.suppress(asyncio.CancelledError):
        await task
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m relay")
    parser.add_argument("--healthcheck", action="store_true", help="exit 0 if the relay is alive")
    args = parser.parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    if args.healthcheck:
        return asyncio.run(healthcheck(settings))
    return asyncio.run(serve(settings))


if __name__ == "__main__":
    sys.exit(main())
