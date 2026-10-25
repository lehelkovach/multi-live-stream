"""Database engine + session helpers (SQLite by default, Postgres-ready).

SQLite is opened in WAL mode with a busy timeout so the API and relay processes
can share one file safely at our (low) write rate.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, event
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from core import models  # noqa: F401  # ensure tables are registered on SQLModel.metadata


def _is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def _sqlite_path(url: str) -> Path | None:
    # sqlite:////abs/path.db  |  sqlite:///rel/path.db  |  sqlite:///:memory:
    raw = url.split("sqlite:///", 1)[1] if "sqlite:///" in url else ""
    if not raw or raw.startswith(":memory:") or raw.startswith("file:"):
        return None
    return Path(raw)


def make_engine(url: str, *, echo: bool = False) -> Engine:
    kwargs: dict[str, Any] = {"echo": echo, "pool_pre_ping": True}
    if _is_sqlite(url):
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 10}
        path = _sqlite_path(url)
        if path is None:
            # in-memory DB must be shared across threads/connections
            kwargs["poolclass"] = StaticPool
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, **kwargs)
    if _is_sqlite(url):

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragmas(dbapi_conn: Any, _record: Any) -> None:
            cur = dbapi_conn.cursor()
            # busy_timeout first: switching to WAL needs an exclusive lock, and the
            # api + relay processes open a fresh database at the same time.
            cur.execute("PRAGMA busy_timeout=5000")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

    return engine


def init_db(engine: Engine, *, attempts: int = 5) -> None:
    """Create any missing tables. (Alembic migrations are a Phase 6 item.)

    The api and relay processes start together and share one SQLite file, so two
    ``create_all`` calls can race on a fresh volume ("table already exists"). A
    retry makes that harmless: the second pass only creates what is still missing.
    """
    for attempt in range(1, attempts + 1):
        try:
            SQLModel.metadata.create_all(engine)
            return
        except OperationalError as exc:
            if "already exists" not in str(exc).lower() or attempt == attempts:
                raise
            time.sleep(0.1 * attempt)


@contextmanager
def session_scope(engine: Engine) -> Iterator[Session]:
    """Transactional scope: commit on success, rollback on error."""
    session = Session(engine)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
