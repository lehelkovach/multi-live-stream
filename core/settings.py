"""Application settings, loaded from environment variables (prefix ``OMNI_``).

A single ``Settings`` object is shared by every process (API, relay, chat).
Secrets are never logged; ``repr`` of ``SecretStr`` fields is masked.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LogFormat = Literal["json", "console"]
CookieSecure = Literal["auto", "always", "never"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="OMNI_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- secrets -----------------------------------------------------------
    secret_key: SecretStr = Field(
        description="Fernet key(s) for encrypting credentials at rest. "
        "Comma-separated list; the first key encrypts, all keys can decrypt."
    )
    session_secret: SecretStr = Field(description="Key used to sign the session cookie.")
    ingest_stream_key: SecretStr = Field(
        default=SecretStr(""),
        description="Stream key publishers must use (rtmp://host/live/<key>).",
    )

    # --- admin user --------------------------------------------------------
    admin_username: str = "admin"
    admin_password: SecretStr = SecretStr("")
    admin_password_hash: SecretStr = SecretStr("")

    # --- sessions ----------------------------------------------------------
    session_cookie_name: str = "omni_session"
    session_max_age: int = 60 * 60 * 24 * 30
    cookie_secure: CookieSecure = "auto"

    # --- infrastructure ----------------------------------------------------
    database_url: str = "sqlite:////data/omnistream.db"
    redis_url: str = "redis://redis:6379/0"
    mediamtx_api_url: str = "http://mediamtx:9997"
    mediamtx_rtsp_url: str = "rtsp://mediamtx:8554"
    ingest_path_prefix: str = "live"

    # --- relay -------------------------------------------------------------
    ffmpeg_bin: str = "ffmpeg"
    relay_poll_interval: float = 2.0  # seconds between reconcile passes
    relay_backoff_base: float = 1.0  # first retry delay (doubles each failure)
    relay_backoff_max: float = 60.0  # cap for the retry delay
    relay_stall_timeout: float = 20.0  # no output progress for this long => restart
    relay_healthy_after: float = 30.0  # running this long resets the backoff counter
    relay_error_after_failures: int = 5  # consecutive failures before status "error"

    # --- chat --------------------------------------------------------------
    # OAuth apps registered by the owner (see docs/TODO-owner.md). Empty => that
    # platform's "Connect" button is disabled; manual token entry still works.
    twitch_client_id: str = ""
    twitch_client_secret: SecretStr = SecretStr("")
    google_client_id: str = ""
    google_client_secret: SecretStr = SecretStr("")
    kick_client_id: str = ""
    kick_client_secret: SecretStr = SecretStr("")
    facebook_app_id: str = ""
    facebook_app_secret: SecretStr = SecretStr("")
    graph_api_version: str = "v21.0"
    # Public base URL for OAuth redirects and the Kick webhook; default https://<domain>.
    public_url: str = ""
    allow_fake_chat: bool = False  # enable the "fake" chat platform (tests, demos)
    chat_poll_interval: float = 5.0  # seconds between manager reconcile passes
    youtube_daily_quota: int = 10_000  # units/day granted to the Google project
    youtube_chat_quota_share: float = 0.8  # fraction of the daily quota chat polling may use
    youtube_stream_mode: bool = False  # experimental: liveChatMessages.streamList
    # Outgoing chat rate limits per platform: sustained messages/second and burst size.
    # Conservative defaults from the platform docs (see docs/platforms.md); JSON in .env.
    chat_rate_limits: dict[str, float] = {
        "twitch": 1.0, "youtube": 0.34, "kick": 1.0, "facebook": 0.2, "fake": 20.0
    }  # fmt: skip
    chat_rate_burst: dict[str, int] = {
        "twitch": 5,
        "youtube": 1,
        "kick": 3,
        "facebook": 1,
        "fake": 20,
    }
    chat_send_max_wait: float = 10.0  # seconds a send may wait for a rate-limit slot
    chat_send_timeout: float = 20.0  # seconds per platform call

    # --- ops ---------------------------------------------------------------
    metrics_token: SecretStr = SecretStr("")  # bearer token for /metrics (Prometheus)
    backup_dir: str = "/data/backups"
    backup_keep: int = 14
    chat_retention_days: int = 30  # `omnistream prune` deletes older chat history

    # --- misc --------------------------------------------------------------
    log_level: str = "info"
    log_format: LogFormat = "json"
    domain: str = "localhost"
    web_dist_dir: str = "web/dist"

    @field_validator("secret_key")
    @classmethod
    def _secret_key_not_blank(cls, v: SecretStr) -> SecretStr:
        if not v.get_secret_value().strip():
            msg = "OMNI_SECRET_KEY must be set (run `make secret`)"
            raise ValueError(msg)
        return v

    @field_validator("session_secret")
    @classmethod
    def _session_secret_not_blank(cls, v: SecretStr) -> SecretStr:
        if len(v.get_secret_value().strip()) < 16:
            msg = "OMNI_SESSION_SECRET must be at least 16 characters (run `make secret`)"
            raise ValueError(msg)
        return v

    @field_validator("log_level")
    @classmethod
    def _normalise_log_level(cls, v: str) -> str:
        level = v.strip().lower()
        if level not in {"debug", "info", "warning", "error", "critical"}:
            msg = f"invalid OMNI_LOG_LEVEL: {v!r}"
            raise ValueError(msg)
        return level

    @property
    def secret_keys(self) -> list[str]:
        return [k.strip() for k in self.secret_key.get_secret_value().split(",") if k.strip()]

    @property
    def base_url(self) -> str:
        """Public https base URL (no trailing slash) used in OAuth redirect URIs."""
        if self.public_url:
            return self.public_url.rstrip("/")
        return f"https://{self.domain}"

    @property
    def ingest_path(self) -> str:
        """MediaMTX path the publisher must use, e.g. ``live/<stream key>``."""
        return f"{self.ingest_path_prefix}/{self.ingest_stream_key.get_secret_value()}"

    @property
    def ingest_rtsp_url(self) -> str:
        """Where relay workers pull the live stream from (Compose-internal)."""
        return f"{self.mediamtx_rtsp_url.rstrip('/')}/{self.ingest_path}"

    def has_admin_credentials(self) -> bool:
        return bool(
            self.admin_password_hash.get_secret_value() or self.admin_password.get_secret_value()
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton. Tests call ``get_settings.cache_clear()``."""
    return Settings()  # values come from the environment
