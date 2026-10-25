from __future__ import annotations

import pytest

from core.crypto import DecryptError, SecretBox, generate_key


def test_roundtrip() -> None:
    box = SecretBox([generate_key()])
    token = box.encrypt("live_123456_abcdef")
    assert token != "live_123456_abcdef"
    assert box.decrypt(token) == "live_123456_abcdef"


def test_unicode_roundtrip() -> None:
    box = SecretBox([generate_key()])
    assert box.decrypt(box.encrypt("clé 🔑")) == "clé 🔑"


def test_rotation_old_key_still_decrypts_and_rotate_reencrypts() -> None:
    old, new = generate_key(), generate_key()
    token = SecretBox([old]).encrypt("secret")
    box = SecretBox([new, old])
    assert box.decrypt(token) == "secret"
    rotated = box.rotate(token)
    assert rotated != token
    assert SecretBox([new]).decrypt(rotated) == "secret"
    with pytest.raises(DecryptError):
        SecretBox([new]).decrypt(token)


def test_wrong_key_raises() -> None:
    token = SecretBox([generate_key()]).encrypt("x")
    with pytest.raises(DecryptError):
        SecretBox([generate_key()]).decrypt(token)


def test_garbage_raises() -> None:
    with pytest.raises(DecryptError):
        SecretBox([generate_key()]).decrypt("not-a-token")


def test_bad_key_rejected() -> None:
    with pytest.raises(ValueError, match="OMNI_SECRET_KEY"):
        SecretBox(["short"])
    with pytest.raises(ValueError, match="at least one"):
        SecretBox([])
