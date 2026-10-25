from __future__ import annotations

import pytest
from pydantic import ValidationError

from core.crypto import generate_key
from core.settings import Settings, get_settings


def test_missing_secret_key_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNI_SESSION_SECRET", "x" * 32)
    with pytest.raises(ValidationError):
        Settings()


def test_blank_secret_key_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNI_SECRET_KEY", "   ")
    monkeypatch.setenv("OMNI_SESSION_SECRET", "x" * 32)
    with pytest.raises(ValidationError, match="OMNI_SECRET_KEY"):
        Settings()


def test_short_session_secret_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNI_SECRET_KEY", generate_key())
    monkeypatch.setenv("OMNI_SESSION_SECRET", "short")
    with pytest.raises(ValidationError, match="OMNI_SESSION_SECRET"):
        Settings()


def test_loads_from_env_and_splits_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    k1, k2 = generate_key(), generate_key()
    monkeypatch.setenv("OMNI_SECRET_KEY", f"{k1}, {k2}")
    monkeypatch.setenv("OMNI_SESSION_SECRET", "x" * 32)
    monkeypatch.setenv("OMNI_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("OMNI_ADMIN_PASSWORD", "hunter22")
    s = get_settings()
    assert s.secret_keys == [k1, k2]
    assert s.log_level == "debug"
    assert s.has_admin_credentials()
    assert "hunter22" not in repr(s)
    assert "hunter22" not in str(s.model_dump())


def test_invalid_log_level(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNI_SECRET_KEY", generate_key())
    monkeypatch.setenv("OMNI_SESSION_SECRET", "x" * 32)
    monkeypatch.setenv("OMNI_LOG_LEVEL", "loud")
    with pytest.raises(ValidationError, match="OMNI_LOG_LEVEL"):
        Settings()
