"""Helpers for the ``app_setting`` key/value table."""

from __future__ import annotations

from sqlmodel import Session

from core.models import SETTING_RELAY_ENABLED, AppSetting, utcnow


def get_setting(session: Session, key: str, default: str = "") -> str:
    row = session.get(AppSetting, key)
    return row.value if row is not None else default


def set_setting(session: Session, key: str, value: str) -> None:
    row = session.get(AppSetting, key)
    if row is None:
        row = AppSetting(key=key, value=value)
    else:
        row.value = value
        row.updated_at = utcnow()
    session.add(row)


def relay_enabled(session: Session) -> bool:
    return get_setting(session, SETTING_RELAY_ENABLED, "true").lower() == "true"


def set_relay_enabled(session: Session, enabled: bool) -> None:
    set_setting(session, SETTING_RELAY_ENABLED, "true" if enabled else "false")
