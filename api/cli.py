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
        # a self-contained file: no -wal/-shm needed to read it, even from a read-only mount
        dest.execute("PRAGMA journal_mode=DELETE")
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


REQUIRED_TABLES = {"destination", "chat_account", "chat_message"}


def _cmd_restore(args: argparse.Namespace) -> int:
    """Replace the live SQLite database with a backup.

    Run it as the app user with the services stopped (``make restore`` does both):
    the backup is validated, the current database is copied aside first, the new
    file is written next to the target and renamed into place (so the result is
    owned by the app user even if the old file was not), and stale WAL files are
    removed so SQLite cannot replay them over the restored data.
    """
    import os
    import sqlite3
    import time
    from pathlib import Path

    from core.settings import get_settings

    settings = get_settings()
    target_raw = _sqlite_path(settings.database_url)
    if target_raw is None:
        print("restore: only SQLite is handled here (use pg_restore for Postgres)", file=sys.stderr)
        return 2
    source = Path(args.file)
    if not source.is_file():
        print(f"restore: {source} does not exist", file=sys.stderr)
        return 2
    wal = Path(f"{source}-wal")
    if wal.exists() and wal.stat().st_size > 0:
        print(
            f"restore: {wal} exists, so {source} may be missing changes; "
            "use a file made by `omnistream backup`",
            file=sys.stderr,
        )
        return 2
    # immutable=1: the backup is never written to, and may sit on a read-only mount
    source_uri = f"file:{source}?mode=ro&immutable=1"
    try:
        with sqlite3.connect(source_uri, uri=True) as con:
            integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
            tables = {
                r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
    except sqlite3.DatabaseError as exc:
        print(f"restore: {source} is not a SQLite database ({exc})", file=sys.stderr)
        return 2
    if integrity != "ok":
        print(f"restore: integrity check failed: {integrity}", file=sys.stderr)
        return 2
    missing = REQUIRED_TABLES - tables
    if missing:
        print(
            f"restore: not an OmniStream backup (missing tables: {sorted(missing)})",
            file=sys.stderr,
        )
        return 2
    target = Path(target_raw)
    if not args.yes:
        answer = input(f"Replace {target} with {source}? Services must be stopped. [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("aborted")
            return 1
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        aside_dir = Path(settings.backup_dir)
        aside_dir.mkdir(parents=True, exist_ok=True)
        aside = (
            aside_dir / f"prerestore-omnistream-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}.db"
        )
        with (
            sqlite3.connect(f"file:{target}?mode=ro", uri=True) as cur_db,
            sqlite3.connect(aside) as out,
        ):
            cur_db.backup(out)
        print(f"current database saved to {aside}")
    tmp = target.with_name(target.name + ".restore-tmp")
    tmp.unlink(missing_ok=True)
    with sqlite3.connect(source_uri, uri=True) as src, sqlite3.connect(tmp) as dst:
        src.backup(dst)
    for suffix in ("-wal", "-shm"):
        Path(f"{target}{suffix}").unlink(missing_ok=True)
    os.replace(tmp, target)
    print(f"restored {source} -> {target}")
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
    p_restore = sub.add_parser(
        "restore", help="replace the database with a backup (stop services first)"
    )
    p_restore.add_argument("file", help="backup file created by `omnistream backup`")
    p_restore.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    p_restore.set_defaults(func=_cmd_restore)
    p_prune = sub.add_parser("prune", help="delete chat history older than N days")
    p_prune.add_argument(
        "--days", type=int, help="retention in days (default OMNI_CHAT_RETENTION_DAYS)"
    )
    p_prune.set_defaults(func=_cmd_prune)
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
