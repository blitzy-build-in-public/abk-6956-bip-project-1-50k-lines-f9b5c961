"""Unit tests for ``foundry.db``: the engine, the session and the schema helpers."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, func, inspect, select, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from foundry import db
from foundry.core.exceptions import FoundryError
from foundry.db import create_engine, open_session, rollback_and_close
from foundry.db import engine as db_engine
from foundry.db import session as db_session
from foundry.db.engine import DEFAULT_DATABASE_URL, SUPPORTED_BACKENDS, dispose_engine
from foundry.db.schema import create_all, drop_all, metadata_for, normalize_models

_WIDGET_TABLE = "widget"
_GADGET_TABLE = "gadget"
_TABLES = [_GADGET_TABLE, _WIDGET_TABLE]
_SQLITE_ONLY = "foundry supports sqlite only, got backend 'postgresql'"
_CREDENTIAL = "sentinel" + "-password"  # assembled at run time so no scanner reads it as one
_FAULT = "the resource refused to move"
_BIND_FAILURES = ("plumbing_failure", "db.session", "UnboundExecutionError")
_Log = pytest.LogCaptureFixture


class _Base(DeclarativeBase): ...


class Widget(_Base):
    __tablename__ = _WIDGET_TABLE

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50))


class Gadget(_Base):
    __tablename__ = _GADGET_TABLE

    id: Mapped[int] = mapped_column(primary_key=True)


def _row_count(session: Session) -> int:
    return session.execute(select(func.count()).select_from(Widget)).scalar_one()


def _table_names(engine: Engine) -> list[str]:
    return sorted(inspect(engine).get_table_names())


def _only_warning(caplog: _Log) -> str:
    records = [record for record in caplog.records if record.name.startswith("foundry")]
    warnings = [record.getMessage() for record in records if record.levelno >= logging.WARNING]
    assert len(warnings) == 1, warnings
    return warnings[0]


def _record_calls(monkeypatch: pytest.MonkeyPatch, session: Session, engine: Engine) -> list[str]:
    calls: list[str] = []
    for owner, name in ((session, "rollback"), (session, "close"), (engine, "dispose")):
        original: Any = getattr(owner, name)

        def record(_name: str = name, _call: Any = original) -> None:
            calls.append(_name)
            _call()

        monkeypatch.setattr(owner, name, record)
    return calls


@pytest.fixture
def engine() -> Iterator[Engine]:
    created = create_engine()
    try:
        yield created
    finally:
        dispose_engine(created)


def test_the_barrel_publishes_three_calls_and_an_in_memory_default() -> None:
    assert db.__all__ == ["create_engine", "open_session", "rollback_and_close"]
    assert db.create_engine is db_engine.create_engine
    assert db.open_session is db_session.open_session
    assert db.rollback_and_close is db_session.rollback_and_close
    url = make_url(DEFAULT_DATABASE_URL)
    assert url.get_backend_name() == "sqlite" and url.database == ":memory:"
    assert url.get_driver_name() == "pysqlite" and SUPPORTED_BACKENDS == ("sqlite",)


def test_two_engines_hold_independent_in_memory_databases() -> None:
    first, second = create_engine(), create_engine()
    writer, reader = open_session(first, [Widget]), open_session(second, [Widget])
    try:
        assert first is not second and _table_names(first) == _TABLES
        assert [first.url.database, second.url.database] == [":memory:", ":memory:"]
        writer.add(Widget(name="private-to-the-writer"))
        writer.flush()
        assert (_row_count(writer), _row_count(reader)) == (1, 0)
    finally:
        rollback_and_close(writer)
        rollback_and_close(reader)
        dispose_engine(first)
        dispose_engine(second)


@pytest.mark.parametrize(
    ("database_url", "reported"),
    [
        ("sqlite+pysqlite:///foundry-test.db", "in-memory"),
        ("postgresql://localhost/app", _SQLITE_ONLY),
        ("not a url", "invalid database URL"),
        ("sqlite+aiosqlite:///:memory:", "aiosqlite"),
        (f"postgresql://app-user:{_CREDENTIAL}@db.example.test/app", _SQLITE_ONLY),
    ],
)
def test_create_engine_rejects_urls_it_cannot_serve(database_url: str, reported: str) -> None:
    with pytest.raises(FoundryError) as raised:
        create_engine(database_url)
    assert reported in str(raised.value) and raised.value.hint is not None
    rendered = (str(raised.value), raised.value.format_message(), repr(raised.value))
    assert all(_CREDENTIAL not in one and "app-user@" not in one for one in rendered)


def test_the_schema_helpers_accept_models_tables_and_sets(engine: Engine) -> None:
    single = metadata_for(Widget)
    assert metadata_for(None) == () and len(single) == 1
    assert single[0] is Widget.__table__.metadata and metadata_for([Widget, Gadget]) == single
    assert normalize_models(Widget) == (Widget,)
    assert normalize_models([Widget, Gadget]) == (Widget, Gadget)
    assert normalize_models({Widget, Gadget}) == (Gadget, Widget)
    audit = Table("audit", MetaData(), Column("id", Integer, primary_key=True))
    assert normalize_models(audit) == (audit,) and normalize_models({audit}) == (audit,)
    assert metadata_for(audit) == (audit.metadata,)
    create_all(engine, [Widget, Gadget])
    assert _table_names(engine) == _TABLES
    drop_all(engine, [Widget, Gadget])
    assert _table_names(engine) == []
    create_all(engine, audit)
    assert _table_names(engine) == ["audit"]


@pytest.mark.parametrize(
    ("models", "reported"),
    [
        (Widget(name="an-instance-not-its-class"), "Widget"),
        (42, "int"),
        ("abc", "str"),
        ([Widget, 3.5], "float"),
    ],
)
def test_open_session_refuses_a_non_model(engine: Engine, models: object, reported: str) -> None:
    with pytest.raises(FoundryError) as raised:
        open_session(engine, models)
    assert str(raised.value) == f"not a SQLAlchemy model or table: {reported}"
    assert "declarative model" in (raised.value.hint or "") and _table_names(engine) == []


@pytest.mark.parametrize("commit", [False, True], ids=["flushed", "committed"])
def test_writes_are_visible_then_discarded_on_teardown(engine: Engine, commit: bool) -> None:
    session = open_session(engine, [Widget])
    assert session.in_transaction() is True
    assert _table_names(engine) == _TABLES and _row_count(session) == 0
    session.add(Widget(name="discarded"))
    (session.commit if commit else session.flush)()
    assert session.in_transaction() is (not commit) and _row_count(session) == 1
    rollback_and_close(session)
    follow_up = open_session(engine, [Widget])
    assert _row_count(follow_up) == 0
    rollback_and_close(follow_up)


def test_teardown_runs_in_order_once(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    session = open_session(engine, [Widget])
    calls = _record_calls(monkeypatch, session, engine)
    rollback_and_close(session)
    rollback_and_close(session)
    assert calls == ["rollback", "close", "dispose"] and session.in_transaction() is False


@pytest.mark.parametrize(
    ("failing", "reported"),
    [
        ("rollback", "could not roll back the test"),
        ("dispose", "could not dispose the in-memory database engine"),
    ],
)
def test_a_teardown_fault_is_reported_and_the_session_still_closes(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, caplog: _Log, failing: str, reported: str
) -> None:
    session = open_session(engine, [Widget])
    closed: list[bool] = []

    def refuse(*args: object, **kwargs: object) -> None:
        raise RuntimeError(_FAULT)

    def recording_close() -> None:
        closed.append(True)
        Session.close(session)

    monkeypatch.setattr(session if failing == "rollback" else engine, failing, refuse)
    monkeypatch.setattr(session, "close", recording_close)
    with caplog.at_level(logging.WARNING, logger="foundry"):
        rollback_and_close(session)
    warning = _only_warning(caplog)
    assert closed == [True] and "RuntimeError" in warning and _FAULT not in warning
    assert reported in warning


def test_teardown_never_raises_for_a_session_without_a_bind(caplog: _Log) -> None:
    session = Session()
    with caplog.at_level(logging.WARNING, logger="foundry"):
        rollback_and_close(session)
        rollback_and_close(session)
    warning = _only_warning(caplog)
    assert all(part in warning for part in _BIND_FAILURES)


def test_teardown_leaves_a_connection_binding_open(caplog: _Log) -> None:
    engine = create_engine()
    connection = engine.connect()
    try:
        session = open_session(connection, [Widget])
        session.add(Widget(name="rolled-back-on-the-caller's-connection"))
        session.flush()
        with caplog.at_level(logging.WARNING, logger="foundry"):
            rollback_and_close(session)
        assert connection.closed is False
        assert connection.execute(select(func.count()).select_from(Widget)).scalar_one() == 0
        assert "without disposing" in _only_warning(caplog)
    finally:
        connection.close()
        dispose_engine(engine)


def test_lifecycle_leaves_no_db_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    engine = create_engine()
    session = open_session(engine, [Widget])
    session.add(Widget(name="never-persisted"))
    session.commit()
    rollback_and_close(session)
    dispose_engine(engine)
    dispose_engine(engine)
    assert _table_names(engine) == []
    with engine.connect() as connection:
        assert connection.execute(text("select 1")).scalar_one() == 1
    assert sorted(entry.name for entry in tmp_path.iterdir()) == []
    assert not list(tmp_path.rglob("*.db")) and not list(tmp_path.rglob("*.sqlite"))
