"""The ``foundry_db`` fixture: a transactional session on a private sqlite database.

``foundry_db`` is one of Foundry's twelve public fixtures (AAP 0.4.3). It is function scoped and
yields a plain :class:`sqlalchemy.orm.Session` -- the object a test queries with SQLAlchemy 2.0
typed statements -- over a database that belongs to that one test: a fresh in-memory sqlite
engine, a schema created from the models the test registered, and one outer transaction that is
rolled back at teardown (AAP 0.4.8, "Fixture lifecycle rule").

**What a test gets, and what survives it.** Inside the test its own writes are visible: an ``add``
followed by a ``flush`` is found by the test's own ``select``, and a ``commit`` leaves the rows in
place for the rest of that test. At teardown the transaction is rolled back, the session is closed
and the engine is disposed -- which for an in-memory sqlite database also destroys it -- so no
row, no schema and no committed batch is visible to another test or to a later run. None of that
work happens here: :func:`foundry.db.session.open_session` creates the schema and begins the
transaction, :func:`foundry.db.session.rollback_and_close` ends them, and this module composes the
three calls of :mod:`foundry.db` in their documented order.

**How a test registers its models.** No table is created unless the test asks for it, and there is
no marker or configuration key to ask with (AAP 0.4.2 registers ``foundry_quarantine`` and nothing
else; AAP 0.4.5 configures no database key). The mechanism is pytest's own indirect
parametrisation of this fixture, which delivers the value as ``request.param``::

    class Widget(Base):
        __tablename__ = "widgets"
        id: Mapped[int] = mapped_column(primary_key=True)

    @pytest.mark.parametrize("foundry_db", [Widget], indirect=True)
    def test_a_commit_is_visible_in_the_test(foundry_db: Session) -> None:
        foundry_db.add(Widget(id=1))
        foundry_db.commit()
        assert foundry_db.scalars(select(Widget.id)).all() == [1]

The value names models exactly as :mod:`foundry.db.schema` reads them: mapped classes -- declarative
classes that carry a table -- and :class:`~sqlalchemy.Table` objects, either alone or in an
iterable. Several classes go in one list and share one schema, and classes from different
declarative bases may be listed together. An abstract declarative base is not itself a model,
because it owns no table, so a test passes ``[Widget]`` rather than ``[Base]``; passing the base is
rejected by :func:`foundry.db.schema.normalize_models`. Without the parametrisation the fixture
yields a session over an empty schema, which is a valid and usable session: a test may create its
own tables through it, or use none at all.

**Isolation between tests.** Every request builds its own engine, so one test's rows and schema
cannot reach another, and nothing is shared or cached at module or session scope -- not an engine,
not a session factory, not a declarative base (AAP 0.4.8). A test that fails half way leaves
nothing behind either: the teardown below runs in a ``finally``, so it runs after a failed test and
after a ``KeyboardInterrupt`` just as it runs after a passing one, and the setup-failure path
disposes the engine before re-raising so a schema that cannot be built leaves no connection open.

**SQLAlchemy is imported lazily** (implicit requirement I5, AAP 0.13.1). This module is imported
during ``pytest_configure``, at every pytest start, inside a 150 ms budget (AAP 0.14.2), while only
a minority of tests request the database fixture. No SQLAlchemy name and no :mod:`foundry.db` name
is bound at module scope: both are imported inside the fixture that first needs them, and the names
used in annotations are imported under ``TYPE_CHECKING``. SQLAlchemy is still a declared runtime
dependency (AAP 0.5.1); this is about import timing, not availability.

**Layering** (AAP 0.4.8). This module imports :mod:`pytest` and, lazily, :mod:`foundry.db`, which is
the capability rung's allowance for ``foundry.fixtures``. It imports no ``foundry.config`` -- the
fixture takes no configuration, because AAP 0.4.5 has no database key to take -- and it imports no
``foundry.execution``, ``foundry.reporting``, ``foundry.cli``, ``foundry.plugin`` or
``foundry.http``.

**sqlite only, synchronously, offline** (AAP 0.3.1, AAP 0.9.2).
:func:`foundry.db.engine.create_engine` accepts nothing but an in-memory sqlite URL, so there is no
server to start, no driver to install, no migration step and no socket, and the fixture lifecycle
stays single-threaded and deterministic (AAP 0.4.8). A second backend or a file-backed database
would each break a promise this fixture makes -- the first AAP 0.9.2's scope, the second the
isolation below -- which is why the URL is not a parameter here.

Importing this module defines names only: the fixture and one private helper. It performs no I/O,
reads no environment variable, configures no logger and holds no module-level state (AAP 0.4.8).

Example:
    A test that uses the empty schema a bare request yields::

        from sqlalchemy import text

        def test_the_session_answers(foundry_db: Session) -> None:
            assert foundry_db.execute(text("select 1")).scalar() == 1
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from sqlalchemy.orm import Session as OrmSession

    from foundry.db.schema import ModelSpec

__all__ = ["foundry_db"]
"""The one public name this module contributes to the twelve-fixture set (AAP 0.4.3)."""


def _models(request: pytest.FixtureRequest) -> ModelSpec:
    """Return the models the test registered for this fixture, or ``None`` if it registered none.

    ``request.param`` exists only for a fixture parametrised indirectly, so ``getattr`` with a
    ``None`` default is the whole mechanism: a plain ``foundry_db: Session`` request carries no
    ``param`` attribute at all, and that is a legitimate request rather than an error to report.

    Nothing is validated here. :func:`foundry.db.schema.normalize_models` normalises ``None``, one
    declarative model class, one :class:`~sqlalchemy.Table` and an iterable of either, and raises
    :class:`~foundry.core.exceptions.FoundryError` naming the type of a value it cannot use, so a
    second opinion in this module would only create a place for the two to disagree (AAP 0.14.3).

    The value is annotated rather than cast because the attribute is untyped where it is read: an
    ``Any`` flowing into an annotated local is accepted by ``mypy --strict``, and a cast would
    assert what the schema module is about to check for itself.

    Args:
        request: The fixture request, whose ``param`` carries the indirect parametrisation when
            the test supplied one.

    Returns:
        Whatever the test passed as the parametrisation value, or ``None`` when it passed nothing.
    """
    models: ModelSpec = getattr(request, "param", None)
    return models


@pytest.fixture
def foundry_db(request: pytest.FixtureRequest) -> Iterator[OrmSession]:
    """Yield a transactional ``Session`` over a private in-memory sqlite database.

    Function scoped (AAP 0.4.3): every test that requests the fixture gets a fresh engine, a fresh
    schema and a fresh transaction, and nothing survives into the next test.

    The schema holds the tables of the models the test passed by indirect parametrisation --
    ``@pytest.mark.parametrize("foundry_db", [Widget], indirect=True)`` -- or no table at all when
    the test passed nothing. Writes the test makes, including ones it commits, are rolled back at
    teardown and are never visible to another test. The session's connection, and with it the
    in-memory database, is released at teardown on the failing path and the interrupted path as
    well as on the passing one, so a long parallel run accumulates nothing.

    The yielded value is a plain :class:`sqlalchemy.orm.Session`: query it with SQLAlchemy 2.0
    typed statements (``session.scalars(select(Widget))``), add rows with ``session.add`` and
    commit with ``session.commit``. Nothing wraps or subclasses it and no repository layer sits in
    front of it, so SQLAlchemy's own documentation applies unchanged.

    Args:
        request: The fixture request, read only for an indirect parametrisation.

    Yields:
        An open session, bound to the test's own engine, whose outer transaction has already
        begun and is rolled back at teardown.

    Raises:
        FoundryError: If the parametrisation value is neither a declarative model class nor a
            :class:`~sqlalchemy.Table`, or if the requested schema cannot be created on the sqlite
            database. The type is raised by :mod:`foundry.db` and propagates unchanged from here.
    """
    # Imported inside the fixture rather than at module scope: pytest imports `foundry.plugin` at
    # every start and SQLAlchemy costs more than the whole 150 ms startup budget allows (I5).
    from foundry.db import create_engine, open_session, rollback_and_close
    from foundry.db.engine import dispose_engine

    engine = create_engine()
    try:
        session = open_session(engine, _models(request))
    except BaseException:
        # Setup failed, so there is no session to hand to `rollback_and_close` and the engine it
        # was built for would otherwise outlive the fixture holding the connection the schema
        # attempt opened (AAP 0.4.8, "Fixture lifecycle rule"). Disposal is idempotent, so the
        # normal path below keeps sole ownership of the teardown call.
        dispose_engine(engine)
        raise

    try:
        yield session
    finally:
        rollback_and_close(session)
