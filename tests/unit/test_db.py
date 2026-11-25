"""Unit tests for the ``foundry.db`` package: the engine, the session and the schema helpers.

AAP 0.7.1 splits the ``foundry_db`` promise across ``create_engine``, ``open_session`` and
``rollback_and_close``, and this module pins the properties a regression would break: a per-test
engine that touches no filesystem, a schema built from the models the caller registers, a
transaction whose rows vanish at teardown, and a teardown that never raises and never disposes a
connection it does not own. The package is driven through its public API, so the three calls
``src/foundry/fixtures/db.py`` makes are what these tests exercise.

Where the module on disk differs from the brief (AAP 0.4.3, 0.7.1, 0.4.9): SQLAlchemy 2.0 has no
``Session.closed``, so "the session is closed" is observed as ``in_transaction()`` false plus a
recording wrapper proving ``close()`` runs second of three teardown steps; and two ``create_engine``
calls return engines whose ``URL`` values compare equal, so "a fresh database per test" is proven by
distinct engines holding independent data.

Determinism (AAP 0.14.3): every engine is in memory, nothing is written outside ``tmp_path`` and no
socket is opened. Table names are sorted and rows are counted, never read in insertion order.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

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
"""Table name of :class:`Widget`, so no assertion spells the string twice."""

_GADGET_TABLE = "gadget"
"""Table name of :class:`Gadget`, the second table of the same metadata."""

_CREDENTIAL = "sentinel" + "-password"
"""A password planted in a rejected URL; assembled at run time so no scanner reads it as one."""


class _Base(DeclarativeBase):
    """Declarative base for the two tables below, standing in for a test's own models."""


class Widget(_Base):
    """The model most tests register: an integer primary key and one text column."""

    __tablename__ = _WIDGET_TABLE

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50))


class Gadget(_Base):
    """A second model of the same ``Base``, so one metadata can own two tables."""

    __tablename__ = _GADGET_TABLE

    id: Mapped[int] = mapped_column(primary_key=True)


def _row_count(session: Session) -> int:
    """Return how many :class:`Widget` rows ``session`` can see."""
    return session.execute(select(func.count()).select_from(Widget)).scalar_one()


def _table_names(engine: Engine) -> list[str]:
    """Return the engine's table names, sorted, as SQLAlchemy's inspector reports them."""
    return sorted(inspect(engine).get_table_names())


def _foundry_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Return the ``foundry`` logger's warning messages, oldest first."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.name.startswith("foundry") and record.levelno >= logging.WARNING
    ]


def _record_teardown_calls(
    monkeypatch: pytest.MonkeyPatch, session: Session, engine: Engine
) -> list[str]:
    """Replace the teardown's three methods with recorders and return the call log they fill.

    The documented order of :func:`foundry.db.session.rollback_and_close` is rollback, then close,
    then disposal of the engine, and the three steps leave no other trace once an in-memory
    database is gone -- so the sequence itself is what this records.
    """
    calls: list[str] = []
    original_rollback = Session.rollback
    original_close = Session.close
    original_dispose = Engine.dispose

    def rollback() -> None:
        calls.append("rollback")
        original_rollback(session)

    def close() -> None:
        calls.append("close")
        original_close(session)

    def dispose(*args: object, **kwargs: object) -> None:
        calls.append("dispose")
        original_dispose(engine)

    monkeypatch.setattr(session, "rollback", rollback)
    monkeypatch.setattr(session, "close", close)
    monkeypatch.setattr(engine, "dispose", dispose)
    return calls


@pytest.fixture
def engine() -> Iterator[Engine]:
    """A fresh in-memory engine, disposed at teardown whatever the test did with it."""
    created = create_engine()
    try:
        yield created
    finally:
        dispose_engine(created)


# --- Engine construction (AAP 0.3.1, AAP 0.4.5) -------------------------------


def test_the_barrel_exports_only_the_three_call_lifecycle() -> None:
    """``foundry.db`` publishes the fixture's three calls and nothing else (AAP 0.4.9)."""
    assert db.__all__ == ["create_engine", "open_session", "rollback_and_close"]
    assert db.create_engine is db_engine.create_engine
    assert db.open_session is db_session.open_session
    assert db.rollback_and_close is db_session.rollback_and_close


def test_default_database_url_is_an_in_memory_sqlite_url() -> None:
    """The default URL names sqlite's ``:memory:`` through the synchronous pysqlite driver."""
    url = make_url(DEFAULT_DATABASE_URL)
    assert url.get_backend_name() == "sqlite"
    assert url.get_driver_name() == "pysqlite"
    assert url.database == ":memory:"
    assert SUPPORTED_BACKENDS == ("sqlite",)


def test_create_engine_returns_a_fresh_in_memory_engine_per_call() -> None:
    """Two calls build two distinct engines, each over its own private in-memory database."""
    first = create_engine()
    second = create_engine()
    try:
        assert first is not second
        assert [first.url.get_backend_name(), second.url.get_backend_name()] == ["sqlite", "sqlite"]
        assert [first.url.database, second.url.database] == [":memory:", ":memory:"]
        assert _table_names(first) == []
    finally:
        dispose_engine(first)
        dispose_engine(second)


@pytest.mark.parametrize(
    ("database_url", "reported"),
    [
        ("sqlite+pysqlite:///foundry-test.db", "in-memory"),
        ("postgresql://localhost/app", "sqlite only"),
        ("not a url", "invalid database URL"),
        ("sqlite+aiosqlite:///:memory:", "aiosqlite"),
    ],
)
def test_create_engine_rejects_every_url_it_cannot_serve(database_url: str, reported: str) -> None:
    """Only the synchronous in-memory sqlite URL is accepted; everything else is a FoundryError."""
    with pytest.raises(FoundryError) as raised:
        create_engine(database_url)
    assert reported in str(raised.value)
    assert raised.value.hint is not None


def test_create_engine_reports_a_credential_bearing_url_without_echoing_it() -> None:
    """A rejected URL is reported by backend alone: a URL is a credential carrier (AAP 0.3.2)."""
    with pytest.raises(FoundryError) as raised:
        create_engine(f"postgresql://app-user:{_CREDENTIAL}@db.example.test/app")
    error = raised.value
    assert error.message == "foundry supports sqlite only, got backend 'postgresql'"
    for rendered in (str(error), error.format_message(), repr(error)):
        assert _CREDENTIAL not in rendered
        assert "app-user@" not in rendered


# --- Schema creation from the models a caller registers (AAP 0.3.2) -----------


def test_open_session_creates_the_schema_for_the_registered_models(engine: Engine) -> None:
    """Every table of the registered models' metadata exists before the transaction opens."""
    session = open_session(engine, [Widget])
    try:
        assert _table_names(engine) == [_GADGET_TABLE, _WIDGET_TABLE]
        assert session.in_transaction() is True
    finally:
        rollback_and_close(session)


def test_create_all_and_drop_all_create_and_remove_the_metadata_tables(engine: Engine) -> None:
    """The two helpers round-trip one metadata's schema: created, then dropped, on a live engine."""
    create_all(engine, Widget)
    assert _table_names(engine) == [_GADGET_TABLE, _WIDGET_TABLE]
    drop_all(engine, [Widget, Gadget])
    assert _table_names(engine) == []


def test_metadata_for_deduplicates_models_that_share_one_metadata() -> None:
    """One ``Base`` is one schema: two of its models name one metadata object, not two."""
    assert metadata_for(None) == ()
    single = metadata_for(Widget)
    assert len(single) == 1
    assert single[0] is Widget.__table__.metadata
    assert metadata_for([Widget, Gadget]) == single


def test_normalize_models_accepts_one_model_a_list_and_a_sorted_set() -> None:
    """A single value and a list keep their order; a set is sorted so two processes agree."""
    assert normalize_models(Widget) == (Widget,)
    assert normalize_models([Widget, Gadget]) == (Widget, Gadget)
    assert normalize_models({Widget, Gadget}) == (Gadget, Widget)
    assert normalize_models([Gadget, Widget]) == (Gadget, Widget)


def test_a_plain_table_is_a_model_too(engine: Engine) -> None:
    """An imperatively declared ``Table`` is accepted, and its schema is created like a class's."""
    metadata = MetaData()
    audit = Table("audit", metadata, Column("id", Integer, primary_key=True))

    assert normalize_models(audit) == (audit,)
    assert normalize_models({audit}) == (audit,)
    assert metadata_for(audit) == (metadata,)

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
def test_open_session_rejects_a_value_that_is_not_a_model(
    engine: Engine, models: object, reported: str
) -> None:
    """A non-model is a FoundryError naming its type, raised before any session exists."""
    with pytest.raises(FoundryError) as raised:
        open_session(engine, models)
    assert str(raised.value) == f"not a SQLAlchemy model or table: {reported}"
    assert raised.value.hint is not None
    assert "declarative model" in raised.value.hint
    assert _table_names(engine) == []


# --- The transactional boundary (AAP 0.3.2, AAP 0.7.1) ------------------------


def test_uncommitted_rows_are_visible_to_the_sessions_own_queries(engine: Engine) -> None:
    """A test sees its own writes: the count is zero, then one after ``add`` and ``flush``."""
    session = open_session(engine, [Widget])
    try:
        assert _row_count(session) == 0
        session.add(Widget(name="visible-to-its-own-session"))
        session.flush()
        assert _row_count(session) == 1
    finally:
        rollback_and_close(session)


@pytest.mark.parametrize("commit", [False, True], ids=["flushed-only", "committed-in-transaction"])
def test_teardown_discards_the_rows_the_test_wrote(engine: Engine, commit: bool) -> None:
    """One test sees only its own writes: a row, flushed or committed, is gone after teardown."""
    session = open_session(engine, [Widget])
    session.add(Widget(name="discarded"))
    if commit:
        session.commit()
        assert session.in_transaction() is False
    else:
        session.flush()
        assert session.in_transaction() is True
    rollback_and_close(session)

    follow_up = open_session(engine, [Widget])
    try:
        assert _row_count(follow_up) == 0
    finally:
        rollback_and_close(follow_up)


def test_engines_do_not_share_rows() -> None:
    """Two engines hold two databases: a row written through one is invisible through the other."""
    writer_engine = create_engine()
    reader_engine = create_engine()
    writer = open_session(writer_engine, [Widget])
    reader = open_session(reader_engine, [Widget])
    try:
        writer.add(Widget(name="private-to-the-writer"))
        writer.flush()
        assert _row_count(writer) == 1
        assert _row_count(reader) == 0
    finally:
        rollback_and_close(writer)
        rollback_and_close(reader)


# --- The teardown contract (AAP 0.1.2, AAP 0.7.1, AAP 0.14.3) -----------------


def test_teardown_rolls_back_then_closes_then_disposes(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The documented order: roll back the transaction, close the session, dispose the engine."""
    session = open_session(engine, [Widget])
    calls = _record_teardown_calls(monkeypatch, session, engine)
    rollback_and_close(session)
    assert calls == ["rollback", "close", "dispose"]
    assert session.in_transaction() is False


def test_teardown_is_idempotent(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """A second teardown performs no work: the session's marker stops it before the first step."""
    session = open_session(engine, [Widget])
    calls = _record_teardown_calls(monkeypatch, session, engine)
    rollback_and_close(session)
    rollback_and_close(session)
    assert calls == ["rollback", "close", "dispose"]


def test_teardown_never_raises_for_a_session_without_a_bind(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A session with no bind cannot be read, so the teardown warns once and returns (AAP 0.1.2)."""
    session = Session()
    with caplog.at_level(logging.WARNING, logger="foundry"):
        rollback_and_close(session)
        rollback_and_close(session)
    warnings = _foundry_warnings(caplog)
    assert len(warnings) == 1, warnings
    assert "plumbing_failure" in warnings[0]
    assert "db.session" in warnings[0]
    assert "UnboundExecutionError" in warnings[0]


def test_teardown_leaves_a_connection_binding_open(caplog: pytest.LogCaptureFixture) -> None:
    """A connection the caller owns is closed with the session but never disposed (AAP 0.7.1)."""
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
        warnings = _foundry_warnings(caplog)
        assert len(warnings) == 1, warnings
        assert "without disposing" in warnings[0]
    finally:
        connection.close()
        dispose_engine(engine)


def test_teardown_reports_a_rollback_fault_and_still_closes(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A failing rollback is a ``WARNING``; the remaining steps still run (AAP 0.1.2)."""
    session = open_session(engine, [Widget])
    closed: list[bool] = []
    original_close = Session.close

    def failing_rollback() -> None:
        raise RuntimeError("the driver refused this rollback")

    def recording_close() -> None:
        closed.append(True)
        original_close(session)

    monkeypatch.setattr(session, "rollback", failing_rollback)
    monkeypatch.setattr(session, "close", recording_close)
    with caplog.at_level(logging.WARNING, logger="foundry"):
        rollback_and_close(session)

    assert closed == [True]
    warnings = _foundry_warnings(caplog)
    assert len(warnings) == 1, warnings
    assert "could not roll back the test" in warnings[0]
    assert "RuntimeError" in warnings[0]
    assert "refused this rollback" not in warnings[0]


def test_teardown_reports_a_dispose_fault_without_raising(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A failing disposal is a ``WARNING``, never an exception (AAP 0.14.3)."""

    def failing_dispose(*args: object, **kwargs: object) -> None:
        raise RuntimeError("the pool refused to close")

    session = open_session(engine, [Widget])
    monkeypatch.setattr(engine, "dispose", failing_dispose)
    with caplog.at_level(logging.WARNING, logger="foundry"):
        rollback_and_close(session)

    warnings = _foundry_warnings(caplog)
    assert len(warnings) == 1, warnings
    assert "could not dispose the in-memory database engine" in warnings[0]


def test_dispose_engine_is_idempotent_and_leaves_a_fresh_empty_database(engine: Engine) -> None:
    """Disposal may run twice; the database it destroys is gone, and the engine still connects."""
    session = open_session(engine, [Widget])
    session.add(Widget(name="dropped-with-the-database"))
    session.commit()
    rollback_and_close(session)

    dispose_engine(engine)
    dispose_engine(engine)
    assert _table_names(engine) == []
    with engine.connect() as connection:
        assert connection.execute(text("select 1")).scalar_one() == 1


# --- Nothing is left behind (AAP 0.3.1, AAP 0.9.2) ----------------------------


def test_the_lifecycle_leaves_no_database_file_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole lifecycle is in memory: no file appears, and a rejected URL writes none either."""
    monkeypatch.chdir(tmp_path)
    assert Path.cwd().resolve() == tmp_path.resolve()
    before = sorted(entry.name for entry in tmp_path.iterdir())

    with pytest.raises(FoundryError):
        create_engine("sqlite+pysqlite:///foundry-test.db")

    engine = create_engine()
    session = open_session(engine, [Widget])
    session.add(Widget(name="never-persisted"))
    session.flush()
    rollback_and_close(session)

    assert sorted(entry.name for entry in tmp_path.iterdir()) == before
    assert sorted(path.name for path in tmp_path.rglob("*.db")) == []
    assert sorted(path.name for path in tmp_path.rglob("*.sqlite")) == []
