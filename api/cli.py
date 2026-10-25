"""Small operator CLI: ``omnistream hash-password`` / ``omnistream secret`` / ``check-config``."""

from __future__ import annotations

import argparse
import getpass
import sys

from core.crypto import generate_key


def _cmd_hash_password(args: argparse.Namespace) -> int:
    from api.auth import hash_password

    password = args.password
    if password is None:
        if sys.stdin.isatty():
            password = getpass.getpass("Password: ")
            if getpass.getpass("Repeat: ") != password:
                print("passwords do not match", file=sys.stderr)
                return 2
        else:
            password = sys.stdin.readline().rstrip("\n")
    if len(password) < 8:
        print("password must be at least 8 characters", file=sys.stderr)
        return 2
    print(hash_password(password))
    return 0


def _cmd_secret(_args: argparse.Namespace) -> int:
    print(generate_key())
    return 0


def _cmd_check_config(_args: argparse.Namespace) -> int:
    from api.auth import PasswordVerifier
    from core.crypto import SecretBox
    from core.settings import get_settings

    try:
        settings = get_settings()
        SecretBox(settings.secret_keys)
        PasswordVerifier.from_settings(settings)
    except Exception as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1
    if not settings.ingest_stream_key.get_secret_value():
        print("warning: OMNI_INGEST_STREAM_KEY is empty; ingest will reject every publisher")
    print("config ok")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="omnistream")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_hash = sub.add_parser(
        "hash-password", help="print an argon2 hash for OMNI_ADMIN_PASSWORD_HASH"
    )
    p_hash.add_argument("--password", help="(insecure; prefer the prompt or stdin)")
    p_hash.set_defaults(func=_cmd_hash_password)
    p_secret = sub.add_parser("secret", help="print a random key (Fernet-compatible)")
    p_secret.set_defaults(func=_cmd_secret)
    p_check = sub.add_parser("check-config", help="validate the current environment")
    p_check.set_defaults(func=_cmd_check_config)
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
