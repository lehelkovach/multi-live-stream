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
DEST_STATUS_TTL = 30  # seconds; the relay refreshes every poll, so a dead relay fades out
CHANNEL_EVENTS = "omni:events"  # pub/sub: status/ingest/config events for relay + dashboard
CHANNEL_CHAT = "omni:chat"  # pub/sub: normalized chat messages

# Event types published on CHANNEL_EVENTS (json objects with a "type" field)
EVENT_DESTINATIONS_CHANGED = "destinations.changed"  # api -> relay: re-read the DB now
EVENT_RELAY_CHANGED = "relay.changed"  # api -> relay: global start/stop flipped
EVENT_INGEST_PUBLISH = "ingest.publish"  # api -> relay: a publisher was just accepted
EVENT_INGEST_STATE = "ingest.state"  # relay -> dashboard: live/offline + bitrate
EVENT_DEST_STATUS = "destination.status"  # relay -> dashboard: status changed
EVENT_DEST_STATS = "destination.stats"  # relay -> dashboard: periodic bitrate/uptime


KEY_CHAT_HEARTBEAT = "omni:chat:heartbeat"
KEY_CHAT_ACCOUNT_STATUS = "omni:chat:{account_id}:status"
KEY_OAUTH_STATE = "omni:oauth:state:{state}"  # pending OAuth flow (TTL 10 min)
KEY_YOUTUBE_QUOTA = "omni:youtube:quota:{day}"  # units spent today (UTC day)
KEY_WEBHOOK_SEEN = "omni:webhook:seen:{platform}:{message_id}"  # dedupe (TTL)
CHANNEL_WEBHOOKS_KICK = "omni:webhooks:kick"  # api -> kick adapter: verified webhook payloads

EVENT_CHAT_ACCOUNTS_CHANGED = "chat.accounts.changed"  # api -> chat manager
EVENT_CHAT_ACCOUNT_STATUS = "chat.account.status"  # chat manager -> dashboard
EVENT_CHAT_MESSAGE = "chat.message"  # chat manager -> dashboard (on CHANNEL_CHAT)
EVENT_CHAT_SEND = "chat.send"  # api -> chat manager: deliver outgoing_message <id>
EVENT_CHAT_OUTGOING = "chat.outgoing"  # chat manager -> dashboard: per-target delivery status


def dest_status_key(dest_id: int) -> str:
    return KEY_DEST_STATUS.format(dest_id=dest_id)


def chat_status_key(account_id: int) -> str:
    return KEY_CHAT_ACCOUNT_STATUS.format(account_id=account_id)


def make_redis(url: str) -> aioredis.Redis:
    return aioredis.from_url(
        url,
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=5,
        health_check_interval=30,
    )
