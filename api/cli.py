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


def _sqlite_path(url: str) -> str | None:
    if not url.startswith("sqlite:///"):
        return None
    raw = url.removeprefix("sqlite:///")
    return None if raw.startswith(":memory:") else raw


def _cmd_backup(args: argparse.Namespace) -> int:
    """Consistent online copy of the SQLite DB (sqlite3 backup API) + rotation."""
    import shutil
    import sqlite3
    import time
    from pathlib import Path

    from core.settings import get_settings

    settings = get_settings()
    src = _sqlite_path(settings.database_url)
    if src is None:
        print("backup: only SQLite is handled here (use pg_dump for Postgres)", file=sys.stderr)
        return 2
    out_dir = Path(args.dir or settings.backup_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    target = out_dir / f"omnistream-{stamp}.db"
    with sqlite3.connect(src) as source, sqlite3.connect(target) as dest:
        source.backup(dest)
    keep = args.keep if args.keep is not None else settings.backup_keep
    backups = sorted(out_dir.glob("omnistream-*.db"))
    for old in backups[:-keep] if keep > 0 else []:
        old.unlink(missing_ok=True)
    size = target.stat().st_size
    print(f"wrote {target} ({size} bytes); keeping {min(len(backups), keep)} backups in {out_dir}")
    print("reminder: back up .env too (OMNI_SECRET_KEY decrypts the stored stream keys/tokens)")
    if args.copy_to:
        shutil.copy2(target, args.copy_to)
    return 0


def _cmd_prune(args: argparse.Namespace) -> int:
    """Delete chat history older than N days (default OMNI_CHAT_RETENTION_DAYS)."""
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import delete
    from sqlmodel import col

    from core.db import make_engine, session_scope
    from core.models import ChatMessage, OutgoingMessage
    from core.settings import get_settings

    settings = get_settings()
    days = args.days if args.days is not None else settings.chat_retention_days
    cutoff = datetime.now(UTC) - timedelta(days=days)
    engine = make_engine(settings.database_url)
    with session_scope(engine) as session:
        msgs = session.execute(delete(ChatMessage).where(col(ChatMessage.timestamp) < cutoff))
        outs = session.execute(
            delete(OutgoingMessage).where(col(OutgoingMessage.created_at) < cutoff)
        )
    n_msgs, n_outs = getattr(msgs, "rowcount", -1), getattr(outs, "rowcount", -1)
    print(f"pruned {n_msgs} chat messages and {n_outs} outgoing messages older than {days} days")
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
    p_backup = sub.add_parser("backup", help="online backup of the SQLite database with rotation")
    p_backup.add_argument("--dir", help="backup directory (default OMNI_BACKUP_DIR)")
    p_backup.add_argument(
        "--keep", type=int, help="how many backups to keep (default OMNI_BACKUP_KEEP)"
    )
    p_backup.add_argument("--copy-to", help="also copy the new backup to this path")
    p_backup.set_defaults(func=_cmd_backup)
    p_prune = sub.add_parser("prune", help="delete chat history older than N days")
    p_prune.add_argument(
        "--days", type=int, help="retention in days (default OMNI_CHAT_RETENTION_DAYS)"
    )
    p_prune.set_defaults(func=_cmd_prune)
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
