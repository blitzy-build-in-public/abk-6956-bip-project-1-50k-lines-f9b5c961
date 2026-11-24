"""Engine construction and deterministic disposal for the in-memory sqlite fixture.

``foundry_db`` (AAP 0.4.3) promises a transactional session on "a fresh in-memory sqlite schema
built from the models the test registers", rolled back at teardown. This module is the engine half
of that promise -- the connection that owns the database, the pool that holds it and the disposal
that ends it -- while :mod:`foundry.db.session` owns the transaction half.

**How the fixture drives it.** ``foundry_db`` calls :func:`create_engine` during setup, runs
``create_all`` for the models the test registered, yields a session bound to that engine, and calls
:func:`dispose_engine` during teardown whatever happened in between. Both calls live in one place,
:mod:`foundry.db.session`, so a test never builds or disposes an engine itself and a teardown that
runs after a failed setup still closes the connection setup opened.

Each test gets its own engine, which also means each xdist worker gets its own private database:
there is no shared file, no shared server and nothing to coordinate between processes, so sharded
and serial runs behave identically. Disposal per test is what keeps a long parallel run from
accumulating connections in a worker that never exits.

**Why** ``StaticPool``. An sqlite ``:memory:`` database belongs to the single connection that
created it: no other connection can see it, and closing that connection destroys it. The default
pool for ``sqlite:///:memory:`` (``SingletonThreadPool``) reuses one connection per thread and so
appears to work in a single-threaded suite, but the moment anything asks the engine for a second
connection it receives a *new, empty* database and the DDL from ``create_all`` becomes invisible.
``StaticPool`` holds exactly one connection for the engine's lifetime, which states that sharing
explicitly and is SQLAlchemy's documented answer for an in-memory sqlite database.

**What the session relies on** (AAP 0.4.3, AAP 0.7.1). ``isolation_level`` and ``autocommit`` are
deliberately left unset. pysqlite's default isolation sends DML inside an implicit transaction, so
a test's writes are rollback-able, while the DDL that ``create_all`` emits is in place before the
session's transaction begins and survives its rollback. Together with ``StaticPool`` that is
exactly the "schema and data survive the transaction" behaviour the fixture promises.

The engine therefore hands out one connection and only one. :mod:`foundry.db.session` is what
begins the transaction on it, creates the schema for the models a test passed in and ends the
test's row set at teardown. This module owns none of that: it exports no sessionmaker, no session
factory, no repository base class and no migration step, and a caller that wants a session asks
the session module for one.

``check_same_thread=False`` is deliberately not passed either. The fixture lifecycle is
single-threaded (AAP 0.4.8, "Fixture lifecycle rule"), and a connection used from a second thread
should fail loudly rather than silently appear to work.

**Foreign keys.** pysqlite leaves ``PRAGMA foreign_keys`` off, so a schema built from declarative
models would silently accept an insert that violates a declared foreign key. When ``foreign_keys``
is true, the connect listener below turns enforcement on for every new DBAPI connection.

**sqlite only, in memory only.** Both restrictions are validation rather than documentation. A
second backend would bring server lifecycle, driver installation and network behaviour that
Foundry does not promise (AAP 0.9.2), and a file-backed database would let one test see another
test's rows and would write a database file outside the ignored tree. :data:`SUPPORTED_BACKENDS`
exists as data rather than as an ``if`` inside the check so that the accepted set is one named
value a reader and a test can both point at, and so that widening it would be a deliberate edit to
a documented constant.

**Credentials.** A database URL can carry ``user:password@host``. Every message this module builds
names the backend, the driver or the database path and never the URL, so a rejected URL cannot
leak a credential into the terminal, a CI log, ``results.json`` or a report (AAP 0.3.2 Security).

Importing this module defines names and nothing else (AAP 0.4.8): no I/O, no environment read, no
logger configuration and no module-level mutable state beyond the constants below. SQLAlchemy is
imported inside the functions that use it because ``foundry.plugin`` is imported at every pytest
start and must stay inside the 150 ms startup budget (implicit requirement I5), while the database
fixture is requested by a minority of tests.

Example:
    Build an engine for a declarative schema and close it deterministically::

        engine = create_engine()
        Base.metadata.create_all(engine)
        dispose_engine(engine)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from foundry.core.exceptions import FoundryError

if TYPE_CHECKING:
    from sqlalchemy.engine import URL, Engine

__all__: Final[tuple[str, ...]] = (
    "DEFAULT_DATABASE_URL",
    "SUPPORTED_BACKENDS",
    "create_engine",
    "dispose_engine",
)

#: The URL ``foundry_db`` builds its engine from (AAP 0.4.3). ``:memory:`` names a private
#: database that lives only as long as the connection holding it, and ``pysqlite`` names the
#: synchronous sqlite driver the dependency set pins; an async driver is deliberately absent.
DEFAULT_DATABASE_URL: Final[str] = "sqlite+pysqlite:///:memory:"

#: The backends this module accepts, compared against ``URL.get_backend_name()``. ``create_engine``
#: rejects any other backend by name, so the tuple is the single place that decision is written.
SUPPORTED_BACKENDS: Final[tuple[str, ...]] = ("sqlite",)

#: The only ``URL.database`` value that names an in-memory database. A URL whose database is
#: ``None`` or the empty string also means in-memory to the sqlite driver and is accepted too.
_MEMORY_DATABASE: Final[str] = ":memory:"

#: Statement the connect listener runs on every new DBAPI connection. sqlite leaves foreign-key
#: enforcement off until this is issued, and it takes effect only outside a transaction, which is
#: why the listener runs at connect time rather than before the first insert.
_FOREIGN_KEY_PRAGMA: Final[str] = "PRAGMA foreign_keys=ON"


def _enable_foreign_keys(dbapi_connection: Any, connection_record: Any) -> None:
    """Turn foreign-key enforcement on for one freshly opened DBAPI connection.

    Registered as an SQLAlchemy ``connect`` listener, so it runs once per new connection rather
    than once per statement. The cursor is closed in a ``finally`` because a failing ``PRAGMA``
    must not leave the cursor open on a connection the pool is about to hand to a session.

    It has to run on ``connect`` rather than just before the first insert: pysqlite ignores
    ``PRAGMA foreign_keys`` when the statement is issued inside a transaction, and the session this
    engine serves begins one immediately. Connecting is the last moment the setting takes effect.

    Args:
        dbapi_connection: The raw DBAPI connection the driver just opened. It is untyped at this
            boundary -- SQLAlchemy hands over whatever object the driver returned, which for
            pysqlite is an ``sqlite3.Connection`` -- and this handler is private, so annotating it
            ``Any`` keeps the "no ``Any`` in a public signature" rule (AAP 0.13.1) intact.
        connection_record: The pool's record for that connection. Unused: the PRAGMA belongs to
            the connection itself, but the event signature requires the parameter.
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute(_FOREIGN_KEY_PRAGMA)
    finally:
        cursor.close()


def _reject_unsupported_backend(url: URL) -> None:
    """Raise when ``url`` addresses a backend other than sqlite.

    The message names the backend only. It never includes the URL, because a URL is a credential
    carrier and this check runs on values a user typed by hand.

    Args:
        url: The parsed database URL to check.

    Raises:
        FoundryError: If the URL's backend is not one of :data:`SUPPORTED_BACKENDS`.
    """
    backend = url.get_backend_name()
    if backend in SUPPORTED_BACKENDS:
        return
    raise FoundryError(
        f"foundry supports sqlite only, got backend {backend!r}",
        hint="use the default in-memory sqlite URL",
    )


def _reject_file_backed_database(url: URL) -> None:
    """Raise when ``url`` names a database on disk rather than an in-memory one.

    A file-backed database breaks the isolation the fixture promises -- two tests would share rows
    through one file -- and would leave a database file behind in a tree nothing cleans up. The
    message names the path only, for the reason given in :func:`_reject_unsupported_backend`.

    Args:
        url: The parsed database URL to check.

    Raises:
        FoundryError: If the URL's database is neither absent, empty nor ``:memory:``.
    """
    database = url.database
    if database is None or database in ("", _MEMORY_DATABASE):
        return
    raise FoundryError(
        f"foundry requires an in-memory sqlite database, got {database!r}",
        hint="each test needs its own in-memory database so one test cannot see another's rows",
    )


def create_engine(database_url: str = DEFAULT_DATABASE_URL, *, foreign_keys: bool = True) -> Engine:
    """Build the engine whose single connection owns the test's in-memory sqlite database.

    The name mirrors ``sqlalchemy.create_engine`` because every caller in the tree uses it
    (AAP 0.7.1); SQLAlchemy's own factory is imported inside this function under a private alias
    so the two never collide. The returned engine is not connected yet -- ``StaticPool`` opens its
    one connection on first use, and the connect listener therefore applies to it.

    The engine is created with ``echo`` at its default of ``False``: SQL echo would write
    statements into the captured stdout of whichever test happened to trigger the connection,
    which is non-deterministic noise in a report and is not a knob this contract exposes.

    Accepted URL forms (``:memory:`` is the only database this function will take)::

        sqlite+pysqlite:///:memory:   # the default, and what foundry_db passes
        sqlite:///:memory:            # the same driver named implicitly
        sqlite+pysqlite://            # no database named: the driver's in-memory default

    Anything else -- another backend, a database path, a string the driver cannot parse -- is
    rejected before an engine is built, so a caller never holds a half-configured engine.

    Args:
        database_url: A sqlite URL naming an in-memory database. The default is
            :data:`DEFAULT_DATABASE_URL`; a caller overrides it only to select a different
            synchronous sqlite driver.
        foreign_keys: Whether to enforce declared foreign keys. Defaults to ``True`` so a fixture
            that builds a schema from declarative models behaves like the database it models;
            ``False`` exists for a test that deliberately inserts orphaned rows.

    Returns:
        An engine over one shared connection to a private in-memory database, to be passed to
        :func:`dispose_engine` at teardown.

    Raises:
        FoundryError: If the URL cannot be parsed, names a backend other than sqlite, names a
            database on disk, or names a driver the interpreter cannot load. No message includes
            the URL or its userinfo.
    """
    # Imported here rather than at module scope: pytest imports `foundry.plugin` at every start,
    # and SQLAlchemy costs far more than the whole 150 ms startup budget allows (I5).
    from sqlalchemy import create_engine as _sa_create_engine
    from sqlalchemy import event
    from sqlalchemy.engine import make_url
    from sqlalchemy.exc import ArgumentError
    from sqlalchemy.pool import StaticPool

    try:
        url = make_url(database_url)
    except ArgumentError as exc:
        raise FoundryError(
            "invalid database URL",
            hint="pass an sqlite URL such as the default in-memory one",
        ) from exc

    # The backend is checked first because it is the more fundamental fault: a URL naming both an
    # unsupported backend and a database file should report the backend, not the path.
    _reject_unsupported_backend(url)
    _reject_file_backed_database(url)

    try:
        engine: Engine = _sa_create_engine(url, poolclass=StaticPool)
    except (ArgumentError, ModuleNotFoundError) as exc:
        # A dialect whose driver is absent from the interpreter reaches here (aiosqlite is the
        # realistic case). Letting the driver's own exception cross this boundary would break the
        # rule that only Foundry's contracted types do, so it is re-raised naming the driver --
        # never the URL.
        raise FoundryError(
            f"sqlalchemy driver {url.get_driver_name()!r} is unavailable",
            hint="use the default in-memory sqlite URL, whose driver ships with sqlalchemy",
        ) from exc

    if foreign_keys:
        event.listen(engine, "connect", _enable_foreign_keys)
    return engine


def dispose_engine(engine: Engine) -> None:
    """Close ``engine``'s pool, which for a ``StaticPool`` engine destroys the database.

    Disposal is the second half of the fixture's isolation promise: closing the single connection a
    ``StaticPool`` engine holds destroys the in-memory database with it, so a later test starts
    empty even when an earlier one committed. ``Engine.dispose()`` is idempotent, so a teardown
    that runs after a failed setup may call this safely.

    Nothing else needs releasing. The pool holds one connection and the engine owns no other file
    handle or cursor, so after this call a worker process carries nothing over from the test that
    just finished -- which is what keeps a long parallel run from accumulating connections.

    Args:
        engine: The engine :func:`create_engine` returned.
    """
    engine.dispose()
