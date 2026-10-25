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

    def has_admin_credentials(self) -> bool:
        return bool(
            self.admin_password_hash.get_secret_value() or self.admin_password.get_secret_value()
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton. Tests call ``get_settings.cache_clear()``."""
    return Settings()  # values come from the environment
