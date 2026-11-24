"""The transactional session half of the ``foundry_db`` fixture.

``foundry_db`` (AAP 0.4.3) promises "a transactional session on a fresh in-memory sqlite
schema built from the models the test registers, rolled back at teardown".
:mod:`foundry.db.engine` owns the connection and the pool that holds it; this module owns
the transaction that makes the promise true, and it is the only place a test's database work
is begun and discarded.

**The contract, stated once.** Inside a test that requested ``foundry_db`` its own writes are
visible: an ``add`` followed by ``flush`` is found by the test's own ``select``, and a
``commit`` leaves the rows in place for the rest of that test. At teardown those writes are
discarded -- the session's transaction is rolled back, the session is closed and the engine
is disposed, which for the ``StaticPool`` connection described in :mod:`foundry.db.engine`
also destroys the in-memory database. Nothing a test does is therefore observable by another
test: not its rows, not its schema, not a committed batch. A test that fails half way through
leaves nothing behind either, because teardown runs whatever the test did (AAP 0.4.3,
AAP 0.7.1, and the ``src/foundry/db`` folder record's "double rollback and closure").

**Two functions, and no third.** :func:`open_session` creates the schema for the models the
test registered and opens the session with its outer transaction already begun;
:func:`rollback_and_close` ends that transaction, closes the session and disposes the engine.
``foundry_db`` calls exactly those two, with ``create_engine`` from :mod:`foundry.db.engine`
supplying the engine, and needs nothing else from this package: no session factory, no
repository base class and no migration step.

**Why the transaction is begun explicitly.** A SQLAlchemy session begins a transaction
lazily, on the first statement it runs, so a session whose test has not touched the database
yet has no transaction to roll back and ``rollback()`` there would be a no-op with an
ambiguous meaning. :func:`open_session` calls ``begin()`` so ``in_transaction()`` is true
from the first moment and the rollback target is unambiguous -- including when a test's very
first call is ``rollback()`` itself.

**Teardown never raises** (AAP 0.1.2 and AAP 0.14.3, failure isolation).
:func:`rollback_and_close` is Foundry's own plumbing: a fixture that cannot roll back or
cannot close its session must not turn a passing test into a failing one or change the run's
exit code, so each step is wrapped, a fault is reported at ``WARNING`` on the ``foundry``
logger, and the function returns. The swallowing is deliberately narrow. It covers this
function's own steps only -- a test's exception belongs to pytest and is never caught,
rewritten or reported here -- and no message it builds carries a connection string, because a
database URL can hold ``user:password`` and a log line reaches the terminal, the captured-log
artifact and the report (AAP 0.3.2 Security).

**SQLAlchemy is imported lazily** (implicit requirement I5). ``foundry.plugin`` is imported
at every pytest start and has a 150 ms budget, while only a minority of tests request the
database fixture, so no SQLAlchemy name is bound at module scope: the names this module
annotates with are imported under ``TYPE_CHECKING`` and the names it calls are imported
inside the function that needs them. Importing this module therefore costs two Foundry
imports and the standard library, and ``sqlalchemy`` stays out of ``sys.modules``.

Those two imports are the whole of its outward dependency. ``foundry.db`` sits on the
foundation rung of the layering table (AAP 0.4.8), so this module imports
:mod:`foundry.db.schema` and :mod:`foundry.obs.logging` from Foundry and nothing else, and it
defines no exception type of its own. Nothing happens at import time beyond defining names --
no I/O, no environment read, no logger configuration, no module-level mutable state -- and the
lifecycle it serves is single-threaded and deterministic (AAP 0.4.8, "Fixture lifecycle
rule"): no thread, no background task, no sleep and no socket.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Final

from foundry.db.schema import create_all, normalize_models
from foundry.obs.logging import get_logger

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection, Engine
    from sqlalchemy.orm import Session

    from foundry.db.schema import ModelSpec

__all__: Final[tuple[str, ...]] = ("open_session", "rollback_and_close")
"""The two calls ``foundry_db`` makes: open a transactional session, and end it."""

_LOGGER: Final[logging.Logger] = get_logger(__name__)
"""This module's logger, a child of the ``foundry`` logger (AAP 0.13.1)."""

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

    1. :func:`foundry.db.schema.create_all` creates every table of every model's metadata on
       ``engine`` first, so the schema exists before the session's transaction opens. The DDL
       is therefore outside what the rollback undoes, which is what lets one test's rows
       disappear while the tables stay usable until the engine is disposed.
    2. A session is built over ``engine`` with ``expire_on_commit=False``. A test may
       ``commit()`` and keep asserting on the instances it holds: with the default ``True``
       the commit would expire them, the next attribute read would trigger a lazy reload, and
       that reload could no longer be served once teardown has ended the transaction.
       Keeping the loaded state on the instance makes the test independent of whether its
       teardown has already run.
    3. ``autoflush`` is left at its default of ``True`` on purpose. A test that adds an object
       and then queries for it must find it, and autoflush is what makes pending state
       visible to the session's own ``select`` without the test having to remember a
       ``flush``. Turning it off would make the fixture's "your own writes are visible"
       promise depend on the test's call order.
    4. ``session.begin()`` opens the outer transaction explicitly, for the reason the module
       docstring gives: the rollback target then exists from the first statement rather than
       appearing lazily with it.

    Args:
        engine: The engine that owns the test's private in-memory sqlite database, normally
            the value of :func:`foundry.db.engine.create_engine`.
        models: The models whose schema the test needs -- a declarative model class, a
            :class:`~sqlalchemy.Table`, an iterable of either, or ``None`` for a session over
            a database whose tables the test never asked for.

    Returns:
        A session bound to ``engine`` with its outer transaction already begun, to be passed
        to :func:`rollback_and_close` at teardown.

    Raises:
        FoundryError: If ``models`` holds a value that is neither a declarative model class
            nor a ``Table``. Every other failure comes from SQLAlchemy itself and propagates
            unchanged, because a schema that cannot be created is the caller's configuration
            error rather than Foundry plumbing: the fault is reported to the caller and no
            session is created, so nothing is left to tear down.
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
    _LOGGER.debug("opened a database session over %d model(s)", len(schema_models))
    return session


def rollback_and_close(session: Session) -> None:
    """Discard the test's work, then close the session and dispose its engine.

    Four steps, in this order, each wrapped so that no fault escapes:

    1. The session's bind is read first, while the session still holds it. Closing releases
       the connection the bind was resolved through, so reading it afterwards is not
       dependable, and the bind is what decides whether an engine disposal is still owed.
    2. If the session reports an open transaction, it is rolled back. That is the step that
       makes the fixture's isolation real: every row the test wrote, including a batch it
       ``commit()``-ed inside the transaction, disappears here.
    3. The session is closed, which returns the connection to the pool and expunges whatever
       the test left in the identity map.
    4. When the bind is an :class:`~sqlalchemy.engine.Engine` it is disposed. Both steps are
       needed, not either: closing the session hands the ``StaticPool`` connection back to
       the pool, and only disposing the engine closes that connection -- which for an
       in-memory sqlite database is also what destroys the database.

    A bind that is not an engine is a real arrangement with a different owner. A session bound
    to a bare :class:`~sqlalchemy.engine.Connection`, or one reporting no bind at all, is
    closed here while its connection is left open, because disposing it belongs to whoever
    created it. That case, and a bind of any type this function cannot dispose, is reported
    once at ``WARNING`` naming its type, so an operator learns that a disposal was skipped
    without reading Foundry's source.

    The call is idempotent: a session this function has already torn down is recognised
    through the ``Session.info`` marker :data:`_TEARDOWN_KEY` names, and a second call
    returns without touching anything. Each step would be harmless a second time anyway --
    SQLAlchemy's ``close()`` and ``Engine.dispose()`` are idempotent -- but a teardown that
    performs no work at all is what a fixture wrapped in pytest's finalisation can rely on.

    It returns nothing and it never raises. Every step is wrapped: a fault is logged at
    ``WARNING`` on the ``foundry`` logger with the exception's own text -- never the URL,
    which is where a credential would be -- and the remaining steps still run (AAP 0.1.2 and
    AAP 0.14.3, failure isolation). Nothing the test did is caught, rewritten or reported
    here: a test's own exception is pytest's business.

    Args:
        session: The session :func:`open_session` returned. A session bound to something other
            than an engine, or one that is already closed, is accepted and handled the same
            way.
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
    session-like object without one -- a stand-in a test passes, say -- is answered ``False``
    so that the teardown still runs: the per-step guards make that safe, while refusing to
    clean up because a marker was unreadable would be the worse failure.

    Args:
        session: The session being torn down.

    Returns:
        ``True`` only when the marker this module writes is present and set to ``True``.
    """
    info = getattr(session, "info", None)
    if not isinstance(info, dict):
        return False
    return info.get(_TEARDOWN_KEY) is True


def _mark_teardown_started(session: Session) -> None:
    """Record in the session that its teardown has begun.

    The marker is set before the first real step rather than after the last, so that a fault
    in one step cannot leave the session looking untouched and invite a second, redundant
    teardown.

    Args:
        session: The session being torn down.
    """
    info = getattr(session, "info", None)
    if isinstance(info, dict):
        info[_TEARDOWN_KEY] = True


def _capture_bind(session: Session) -> Engine | Connection | None | _UnknownBind:
    """Return the session's bind, or :data:`_BIND_UNKNOWN` when it cannot be read.

    The bind is captured before the session is closed: ``get_bind()`` resolves through the
    connection the session is using, and a closed session no longer answers for it. A session
    created with no bind at all raises from ``get_bind()``; that fault is reported here, once,
    and the sentinel tells :func:`_release_bind` to skip a disposal instead of guessing at an
    engine it never saw.

    Args:
        session: The session whose bind is wanted.

    Returns:
        The engine or connection the session is bound to, ``None`` when it reports no bind, or
        :data:`_BIND_UNKNOWN` when reading the bind raised.
    """
    try:
        return session.get_bind()
    except Exception as exc:
        _LOGGER.warning("could not read the database session's bind: %s", exc)
        return _BIND_UNKNOWN


def _release_bind(bind: Engine | Connection | None | _UnknownBind) -> None:
    """Dispose ``bind`` when it is an engine, and report every other kind.

    Disposing an engine Foundry did not build is not this function's decision to make: a
    caller that bound its session to a connection owns that connection's lifetime, so the
    session is closed and the connection is left alone. A bind that is not an engine is
    reported once at ``WARNING`` naming its type -- never its value, because an engine's repr
    carries its URL and a URL can carry a password (AAP 0.3.2 Security). The sentinel is the one
    exception: :func:`_capture_bind` has already logged that fault, so this function stays
    quiet rather than reporting one problem twice.

    Args:
        bind: The value :func:`_capture_bind` returned.
    """
    from sqlalchemy import Engine as SQLAlchemyEngine

    if isinstance(bind, SQLAlchemyEngine):
        _dispose_engine(bind)
        return
    if isinstance(bind, _UnknownBind):
        return
    _LOGGER.warning(
        "closed the database session without disposing it: its bind is a %s, not an Engine",
        _bind_type_name(bind),
    )


def _bind_type_name(bind: Connection | None) -> str:
    """Return the type name to report for a bind this module cannot dispose.

    The qualified type name is used rather than the value's repr, because the repr of a bind
    carries the database URL and an sqlite URL may embed ``user:password``.

    Args:
        bind: The session's bind, as :func:`_capture_bind` returned it.

    Returns:
        The bind's type qualified name, such as ``sqlalchemy.engine.base.Connection``.
    """
    return f"{type(bind).__module__}.{type(bind).__qualname__}"


def _in_transaction(session: Session) -> bool:
    """Report whether ``session`` currently holds an open transaction.

    A test that committed inside the outer transaction has already ended it, so the rollback
    is skipped for that test; the close and the disposal still run, which is what keeps the
    committed batch from outliving the test.

    Args:
        session: The session being torn down.

    Returns:
        ``True`` when the session reports an open transaction, ``False`` otherwise and also
        when the state cannot be read.
    """
    try:
        return bool(session.in_transaction())
    except Exception as exc:
        _LOGGER.warning("could not inspect the database session's transaction: %s", exc)
        return False


def _rollback_transaction(session: Session) -> None:
    """Roll back the session's open transaction, reporting a fault instead of raising it.

    Args:
        session: The session holding the transaction to discard.
    """
    try:
        session.rollback()
    except Exception as exc:
        _LOGGER.warning("could not roll back the test's database transaction: %s", exc)


def _close_session(session: Session) -> None:
    """Close the session, reporting a fault instead of raising it.

    Args:
        session: The session to close.
    """
    try:
        session.close()
    except Exception as exc:
        _LOGGER.warning("could not close the database session: %s", exc)


def _dispose_engine(engine: Engine) -> None:
    """Dispose the engine, closing its pooled connection and ending an in-memory database.

    Args:
        engine: The engine the session was bound to.
    """
    try:
        engine.dispose()
    except Exception as exc:
        _LOGGER.warning("could not dispose the in-memory database engine: %s", exc)
