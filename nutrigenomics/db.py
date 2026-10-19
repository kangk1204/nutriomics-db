from __future__ import annotations

import sqlite3
from functools import lru_cache
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .settings import DATABASE_URL
from .source_replicas import guard as replica_guard
from .source_replicas import physical_read_path


class Base(DeclarativeBase):
    pass


def _prepare_sqlite_path(url: str) -> None:
    prefix = "sqlite:///"
    if url.startswith(prefix) and url != "sqlite:///:memory:":
        path_text = url[len(prefix) :]
        if path_text.startswith("/"):
            path = Path("/" + path_text.lstrip("/"))
        else:
            path = Path(path_text)
        path.parent.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=4)
def get_engine(url: str = DATABASE_URL) -> Engine:
    _prepare_sqlite_path(url)
    options = {"future": True}
    if url.startswith("sqlite"):
        options["connect_args"] = {"check_same_thread": False, "timeout": 30}
    replica = None
    if url.startswith('sqlite:///') and url != 'sqlite:///:memory:':
        original = Path(url[len('sqlite:///'):]).resolve()
        physical = physical_read_path(original).resolve()
        if physical != original:
            replica = physical
            options.pop('connect_args', None)
            options['creator'] = lambda: sqlite3.connect(replica.as_uri()+'?mode=ro&immutable=1',
                uri=True, check_same_thread=False, timeout=30)
    engine = create_engine(url, **options)
    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _set_sqlite_pragmas(connection, _record) -> None:  # type: ignore[no-untyped-def]
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA query_only=ON" if replica else "PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.close()
        if replica:
            @event.listens_for(engine, 'before_cursor_execute')
            def _replica_source_guard(_connection, _cursor, _statement, _parameters, _context, _many):
                replica_guard()
    return engine


@lru_cache(maxsize=4)
def get_session_factory(url: str = DATABASE_URL) -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(url), autoflush=False, expire_on_commit=False)


def create_schema(url: str = DATABASE_URL) -> None:
    from . import models  # noqa: F401

    engine = get_engine(url)
    Base.metadata.create_all(engine)
    for table in Base.metadata.tables.values():
        for index in table.indexes:
            index.create(engine, checkfirst=True)


def session_scope(url: str = DATABASE_URL):
    session = get_session_factory(url)()
    try:
        yield session
    finally:
        session.close()

