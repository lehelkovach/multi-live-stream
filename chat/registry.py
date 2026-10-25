"""platform name -> adapter class."""

from __future__ import annotations

from chat.adapters.facebook import FacebookAdapter
from chat.adapters.fake import FakeAdapter
from chat.adapters.kick import KickAdapter
from chat.adapters.twitch import TwitchAdapter
from chat.adapters.youtube import YouTubeAdapter
from chat.base import AdapterContext, ChatAdapter, ConfigError

ADAPTERS: dict[str, type] = {
    "twitch": TwitchAdapter,
    "youtube": YouTubeAdapter,
    "kick": KickAdapter,
    "facebook": FacebookAdapter,
    "fake": FakeAdapter,
}


def make_adapter(platform: str, ctx: AdapterContext) -> ChatAdapter:
    cls = ADAPTERS.get(platform)
    if cls is None:
        msg = f"no chat adapter for platform {platform!r}"
        raise ConfigError(msg)
    adapter: ChatAdapter = cls(ctx)
    return adapter
