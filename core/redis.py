"""Redis client factory + shared key names.

Redis carries runtime state (destination status, relay heartbeat) and pub/sub
events (status changes, chat messages). It is a cache, not the system of record.
"""

from __future__ import annotations

import redis.asyncio as aioredis

# ---- keys -------------------------------------------------------------------
KEY_RELAY_HEARTBEAT = "omni:relay:heartbeat"  # unix ts, set by the relay loop
KEY_INGEST_STATE = "omni:ingest:state"  # json: {"live": bool, "path": ..., "since": ...}
KEY_DEST_STATUS = "omni:dest:{dest_id}:status"  # json runtime status per destination
CHANNEL_EVENTS = "omni:events"  # pub/sub: status/ingest events for the dashboard
CHANNEL_CHAT = "omni:chat"  # pub/sub: normalized chat messages


def make_redis(url: str) -> aioredis.Redis:
    return aioredis.from_url(
        url,
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=5,
        health_check_interval=30,
    )
