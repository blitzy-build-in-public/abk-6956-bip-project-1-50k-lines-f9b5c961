"""Engine construction and deterministic disposal for the in-memory sqlite fixture.

Engine construction and deterministic disposal for the in-memory sqlite fixture. ``foundry_db`` (AAP
0.4.3) promises a transactional session on "a fresh in-memory sqlite schema built from the models
the test registers", rolled back at teardown. This module is the engine half -- the connection that
owns the database, the pool holding it and the disposal that ends it -- while
:mod:`foundry.db.session` owns the transaction half. ``foundry_db`` calls :func:`create_engine`
during setup and :func:`dispose_engine` during teardown whatever happened in between, both calls
living in the session module, so a teardown after a failed setup still closes the connection setup
opened. Each test gets its own engine, so each xdist worker gets a private database: no shared file,
no shared server, nothing to coordinate, and a long parallel run accumulates no connections.
``StaticPool`` is required. An sqlite ``:memory:`` database belongs to the single connection that
created it, and closing that connection destroys it; the default ``SingletonThreadPool`` reuses one
connection per thread, so a second request receives a new, empty database and the DDL from
``create_all`` becomes invisible. ``isolation_level`` and ``autocommit`` stay unset: pysqlite's
default isolation sends DML inside an implicit transaction, so a test's writes are rollback-able,
while ``create_all``'s DDL is in place before the transaction begins and survives its rollback (AAP
0.4.3, AAP 0.7.1). ``check_same_thread=False`` is not passed either, the lifecycle being
single-threaded (AAP 0.4.8), so a connection used from a second thread fails loudly. pysqlite leaves
``PRAGMA foreign_keys`` off, so the connect listener below turns enforcement on for every new DBAPI
connection rather than letting a declarative schema accept a violating insert. Only sqlite in memory
is accepted, and both restrictions are validation: a second backend would bring server lifecycle,
driver installation and network behaviour Foundry does not promise (AAP 0.9.2), and a file-backed
database would let one test see another's rows. :data:`SUPPORTED_BACKENDS` is data rather than an
``if``, so the accepted set is one named value a reader and a test can both point at. A database URL
can carry ``user:password@host``, so every message names the backend, the driver or the database
path and never the URL (AAP 0.3.2). Importing defines names only (AAP 0.4.8), and SQLAlchemy is
imported inside the functions that use it: ``foundry.plugin`` loads at every pytest start inside the
150 ms budget (implicit requirement I5).
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

    Registered as an SQLAlchemy ``connect`` listener, so it runs once per new connection, and on
    ``connect`` rather than before the first insert because pysqlite ignores ``PRAGMA foreign_keys``
    inside a transaction, which the session this engine serves begins immediately. The cursor is
    closed in a ``finally`` so a failing PRAGMA cannot leave it open on a pooled connection.

    Args:
        dbapi_connection: The raw DBAPI connection the driver just opened, untyped at this boundary
            because SQLAlchemy hands over whatever object the driver returned.
        connection_record: The pool's record for that connection; unused, but the event signature
            requires it.
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute(_FOREIGN_KEY_PRAGMA)
    finally:
        cursor.close()


def _reject_unsupported_backend(url: URL) -> None:
    """Raise when ``url`` addresses a backend other than sqlite.

    The message names the backend only, never the URL, which is a credential carrier and comes here
    as a value a user typed by hand.

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
    (AAP 0.7.1); SQLAlchemy's own factory is imported inside this function under a private alias so
    the two never collide. The engine is not connected yet -- ``StaticPool`` opens its one
    connection on first use, so the connect listener applies to it -- and ``echo`` keeps its default
    ``False``, since SQL echo would write statements into the captured stdout of whichever test
    triggered the connection. The accepted URL forms name the default driver, the same driver
    implicitly, or no database at all (``sqlite+pysqlite:///:memory:``, ``sqlite:///:memory:``,
    ``sqlite+pysqlite://``); anything else -- another backend, a database path, a string the driver
    cannot parse -- is rejected before an engine is built, so a caller never holds a half-configured
    engine.

    Args:
        database_url: A sqlite URL naming an in-memory database; the default is
            :data:`DEFAULT_DATABASE_URL`, overridden only to select another synchronous driver.
        foreign_keys: Whether to enforce declared foreign keys, ``True`` so a declarative schema
            behaves like the database it models and ``False`` for a test inserting orphaned rows.

    Returns:
        An engine over one shared connection to a private in-memory database, for
        :func:`dispose_engine` at teardown.

    Raises:
        FoundryError: If the URL cannot be parsed, names another backend, names a database on disk,
            or names a driver the interpreter cannot load. No message includes the URL or its
            userinfo.
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
    empty even when an earlier one committed. ``Engine.dispose()`` is idempotent, so a teardown that
    runs after a failed setup may call this safely. Nothing else needs releasing. The pool holds one
    connection and the engine owns no other file handle or cursor, so after this call a worker
    process carries nothing over from the test that just finished -- which is what keeps a long
    parallel run from accumulating connections.

    Args:
        engine: The engine :func:`create_engine` returned.
    """
    engine.dispose()
