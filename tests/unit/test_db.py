from __future__ import annotations

import threading
from pathlib import Path

import pytest
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, SQLModel

from core import db as core_db
from core.models import Destination


def test_init_db_retries_when_another_process_created_tables_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = core_db.make_engine(f"sqlite:///{tmp_path / 'race.db'}")
    real = SQLModel.metadata.create_all
    calls = {"n": 0}

    def flaky(bind, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OperationalError(
                "CREATE TABLE destination", {}, Exception("table destination already exists")
            )
        return real(bind, **kw)

    monkeypatch.setattr(SQLModel.metadata, "create_all", flaky)
    core_db.init_db(engine)
    assert calls["n"] == 2
    with Session(engine) as session:
        session.add(Destination(name="x", rtmp_url="rtmp://a/b"))
        session.commit()


def test_init_db_reraises_other_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = core_db.make_engine(f"sqlite:///{tmp_path / 'other.db'}")

    def boom(bind, **kw):
        raise OperationalError("x", {}, Exception("disk I/O error"))

    monkeypatch.setattr(SQLModel.metadata, "create_all", boom)
    with pytest.raises(OperationalError, match="disk I/O"):
        core_db.init_db(engine)


def test_concurrent_init_db_on_fresh_file(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'fresh.db'}"
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            core_db.init_db(core_db.make_engine(url))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
