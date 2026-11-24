"""The transactional session half of the ``foundry_db`` fixture.

``foundry_db`` (AAP 0.4.3) promises "a transactional session on a fresh in-memory sqlite schema
built from the models the test registers, rolled back at teardown".
:mod:`foundry.db.engine` owns the connection and the pool that holds it; this module owns the
transaction that makes the promise true, and it is the only place a test's database work is begun
and discarded.

**The contract, stated once.** Inside a test that requested ``foundry_db`` its own writes are
visible: an ``add`` followed by ``flush`` is found by the test's own ``select``, and a ``commit``
leaves the rows in place for the rest of that test. At teardown those writes are discarded -- the
transaction is rolled back, the session closed and the engine disposed, which for the
``StaticPool`` connection of :mod:`foundry.db.engine` also destroys the in-memory database. Nothing
a test does is observable by another test: not its rows, not its schema, not a committed batch, and
a test that fails half way through leaves nothing behind either (AAP 0.4.3, AAP 0.7.1).

**Two functions, and no third.** :func:`open_session` creates the schema for the models the test
registered and opens the session with its outer transaction already begun;
:func:`rollback_and_close` ends that transaction, closes the session and disposes the engine. The
transaction is begun explicitly because a session begins one lazily: a session whose test has not
touched the database yet would otherwise have no unambiguous rollback target, including when the
test's first call is ``rollback()`` itself.

**Teardown never raises** (AAP 0.1.2 and AAP 0.14.3, failure isolation). A fixture that cannot roll
back or close must not turn a passing test into a failing one or change the exit code, so each step
is wrapped, a fault is reported at ``WARNING`` through
:func:`foundry.obs.events.emit_plumbing_failure`, and the function returns. The swallowing covers
this function's own steps only -- a test's exception belongs to pytest. No message carries a
connection string, because a database URL can hold ``user:password`` and a log line reaches the
terminal, the captured-log artifact and the report (AAP 0.3.2 Security), and the exception
contributes its class name alone: a driver's message routinely embeds ``[SQL: ...]`` with the
``[parameters: (...)]`` row values a test wrote.

**SQLAlchemy is imported lazily** (implicit requirement I5). ``foundry.plugin`` is imported at every
pytest start inside a 150 ms budget while only a minority of tests request the database fixture, so
no SQLAlchemy name is bound at module scope: annotated names come from ``TYPE_CHECKING`` and called
names from inside the function that needs them. The outward dependency is :mod:`foundry.db.schema`
for the schema and :mod:`foundry.obs.events` for every record emitted -- the single emission point
AAP 0.13.1 declares, so this module acquires no logger of its own and inherits the emitter's levels,
scrubbing and class-name-only rule; AAP 0.4.8 permits exactly ``core`` and ``obs`` here, and no
exception type is defined. Importing it defines names only, and the lifecycle it serves is
single-threaded and deterministic (AAP 0.4.8, "Fixture lifecycle rule"): no thread, no background
task, no sleep and no socket.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from foundry.db.schema import create_all, normalize_models
from foundry.obs.events import LEVEL_DEBUG, LifecycleEvent, emit_plumbing_failure, log_event

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection, Engine
    from sqlalchemy.orm import Session

    from foundry.db.schema import ModelSpec

__all__: Final[tuple[str, ...]] = ("open_session", "rollback_and_close")
"""The two calls ``foundry_db`` makes: open a transactional session, and end it."""

_COMPONENT: Final[str] = "db.session"
"""Component name this module's plumbing-failure events carry, so a fault names its source."""

_TEARDOWN_KEY: Final[str] = "foundry.db.session.torn_down"
"""Key written into ``Session.info`` so a repeated teardown performs no work.

``Session.info`` is SQLAlchemy's public per-session dictionary, so the marker travels with the
session rather than living in a module-level set that nothing would clear between tests.
:func:`rollback_and_close` sets it as its first action and returns immediately on a later
call that finds it.
"""


class _UnknownBind:
    """Marker type for "the session's bind could not be read at teardown".

    It carries no data and has exactly one instance, :data:`_BIND_UNKNOWN`. It exists so that
    :func:`_capture_bind` can say "unknown" in its return annotation without falling back to
    ``object`` or to ``None``, which a session also legitimately reports.
    """


_BIND_UNKNOWN: Final[_UnknownBind] = _UnknownBind()
"""The single :class:`_UnknownBind` instance, which callers recognise by its type."""


# --- Public API --------------------------------------------------------------


def open_session(engine: Engine, models: ModelSpec = None) -> Session:
    """Create the schema for ``models`` and return an open, transactional session.

    The order of the steps is the contract:

    1. :func:`foundry.db.schema.create_all` creates every table of every model's metadata first, so
       the schema exists before the session's transaction opens and the DDL is outside what the
       rollback undoes: one test's rows disappear while the tables stay usable until disposal.
    2. The session is built with ``expire_on_commit=False``, so a test may ``commit()`` and keep
       asserting on the instances it holds: with the default the commit would expire them, and the
       reload the next attribute read triggers could not be served after teardown.
    3. ``autoflush`` keeps its default of ``True`` on purpose: a test that adds an object and then
       queries for it must find it, without having to remember a ``flush``.
    4. ``session.begin()`` opens the outer transaction explicitly, as the module docstring says.

    Raises:
        FoundryError: If ``models`` holds a value that is neither a declarative model class nor a
            ``Table``. Every other failure comes from SQLAlchemy itself and propagates unchanged: a
            schema that cannot be created is the caller's configuration error rather than Foundry
            plumbing, and no session was created, so nothing is left to tear down.
    """
    schema_models = normalize_models(models)
    create_all(engine, schema_models)

    # Imported here rather than at module scope: pytest imports `foundry.plugin` at every
    # start and SQLAlchemy costs more than the whole 150 ms budget allows (I5).
    from sqlalchemy.orm import Session as SQLAlchemySession

    session = SQLAlchemySession(bind=engine, expire_on_commit=False)
    try:
        session.begin()
    except BaseException:
        # A session that never reached its transaction must not outlive this call: closing it
        # here keeps the failure from leaving the pool holding a connection nothing owns.
        _close_session(session)
        raise
    log_event(
        LifecycleEvent(
            name="db_session",
            level=LEVEL_DEBUG,
            fields=(("models", str(len(schema_models))),),
        )
    )
    return session


def rollback_and_close(session: Session) -> None:
    """Discard the test's work, then close the session and dispose its engine.

    Four steps, in this order, each wrapped so that no fault escapes:

    1. The session's bind is read first, while the session still holds it: closing releases the
       connection the bind was resolved through, and the bind decides whether a disposal is owed.
    2. An open transaction is rolled back, which is the step that makes the isolation real: every
       row the test wrote, a batch it ``commit()``-ed included, disappears here.
    3. The session is closed, which returns the connection to the pool and expunges whatever the
       test left in the identity map.
    4. The bind is disposed when it is an :class:`~sqlalchemy.engine.Engine`. Both steps are
       needed: closing hands the ``StaticPool`` connection back to the pool, and only disposing the
       engine closes it -- which for an in-memory sqlite database is also what destroys it.

    A bind that is not an engine is a real arrangement with a different owner: a bare
    :class:`~sqlalchemy.engine.Connection`, or no bind at all, is closed here while its connection
    stays open, because disposing it belongs to whoever created it, and that case is reported once
    at ``WARNING`` naming the bind's type.

    The call is idempotent and it never raises: a session already torn down is recognised through
    the ``Session.info`` marker :data:`_TEARDOWN_KEY` names and returns untouched -- each step is
    harmless a second time, but a teardown that does no work is what a fixture wrapped in pytest's
    finalisation can rely on -- and each fault is reported at ``WARNING`` through
    :func:`foundry.obs.events.emit_plumbing_failure` with the exception's class name alone while the
    remaining steps still run (AAP 0.1.2, AAP 0.14.3). A test's own exception is never caught.
    """
    if _teardown_already_started(session):
        return
    _mark_teardown_started(session)

    bind = _capture_bind(session)

    if _in_transaction(session):
        _rollback_transaction(session)

    _close_session(session)

    _release_bind(bind)


# --- Private helpers ---------------------------------------------------------


def _teardown_already_started(session: Session) -> bool:
    """Report whether a previous call already tore this session down.

    The marker lives in ``Session.info``, which every real session exposes as a plain dict. A
    session-like object without one is answered ``False`` so that the teardown still runs: the
    per-step guards make that safe, while refusing to clean up because a marker was unreadable
    would be the worse failure.
    """
    info = getattr(session, "info", None)
    if not isinstance(info, dict):
        return False
    return info.get(_TEARDOWN_KEY) is True


def _mark_teardown_started(session: Session) -> None:
    """Record in the session that its teardown has begun.

    Set before the first real step rather than after the last, so a fault in one step cannot leave
    the session looking untouched and invite a second, redundant teardown.
    """
    info = getattr(session, "info", None)
    if isinstance(info, dict):
        info[_TEARDOWN_KEY] = True


def _capture_bind(session: Session) -> Engine | Connection | None | _UnknownBind:
    """Return the session's bind, or :data:`_BIND_UNKNOWN` when it cannot be read.

    Captured before the session is closed, because ``get_bind()`` resolves through the connection
    the session is using and a closed session no longer answers for it. A session created with no
    bind at all raises from ``get_bind()``; that fault is reported here, once, and the sentinel
    tells :func:`_release_bind` to skip a disposal instead of guessing at an engine it never saw.
    """
    try:
        return session.get_bind()
    except Exception as exc:
        emit_plumbing_failure(_COMPONENT, "could not read the database session's bind", exc=exc)
        return _BIND_UNKNOWN


def _release_bind(bind: Engine | Connection | None | _UnknownBind) -> None:
    """Dispose ``bind`` when it is an engine, and report every other kind.

    Disposing an engine Foundry did not build is not this function's decision: a caller that bound
    its session to a connection owns that connection's lifetime, so the session is closed and the
    connection left alone. A bind that is not an engine is reported once at ``WARNING`` naming its
    type -- never its value, because an engine's repr carries its URL and a URL can carry a password
    (AAP 0.3.2 Security). The sentinel is the one exception: :func:`_capture_bind` has already
    reported that fault, so this function stays quiet rather than reporting one problem twice.
    """
    from sqlalchemy import Engine as SQLAlchemyEngine

    if isinstance(bind, SQLAlchemyEngine):
        _dispose_engine(bind)
        return
    if isinstance(bind, _UnknownBind):
        return
    emit_plumbing_failure(
        _COMPONENT,
        "closed the database session without disposing it: its bind is a "
        f"{_bind_type_name(bind)}, not an Engine",
    )


def _bind_type_name(bind: Connection | None) -> str:
    """Return the type name to report for a bind this module cannot dispose.

    The qualified type name rather than the value's repr: the repr of a bind carries the database
    URL, and an sqlite URL may embed ``user:password``.
    """
    return f"{type(bind).__module__}.{type(bind).__qualname__}"


def _in_transaction(session: Session) -> bool:
    """Report whether ``session`` currently holds an open transaction.

    A test that committed inside the outer transaction has already ended it, so the rollback is
    skipped for that test; the close and the disposal still run, which is what keeps the committed
    batch from outliving the test. An unreadable state answers ``False``.
    """
    try:
        return bool(session.in_transaction())
    except Exception as exc:
        emit_plumbing_failure(
            _COMPONENT, "could not inspect the database session's transaction", exc=exc
        )
        return False


def _rollback_transaction(session: Session) -> None:
    """Roll back the session's open transaction, reporting a fault instead of raising it."""
    try:
        session.rollback()
    except Exception as exc:
        emit_plumbing_failure(
            _COMPONENT, "could not roll back the test's database transaction", exc=exc
        )


def _close_session(session: Session) -> None:
    """Close the session, reporting a fault instead of raising it."""
    try:
        session.close()
    except Exception as exc:
        emit_plumbing_failure(_COMPONENT, "could not close the database session", exc=exc)


def _dispose_engine(engine: Engine) -> None:
    """Dispose the engine, closing its pooled connection and ending an in-memory database."""
    try:
        engine.dispose()
    except Exception as exc:
        emit_plumbing_failure(
            _COMPONENT, "could not dispose the in-memory database engine", exc=exc
        )
