"""The ``foundry_db`` fixture: a transactional session on a private sqlite database.

``foundry_db`` is function scoped and yields a plain :class:`sqlalchemy.orm.Session` over a database
that belongs to that one test: a fresh in-memory sqlite engine, a schema created from the models the
test registered, and one outer transaction rolled back at teardown.

**What a test gets, and what survives it.** Inside the test its own writes are visible, and at
teardown the transaction is rolled back, the session closed and the engine disposed, so no row, no
schema and no committed batch reaches another test. :mod:`foundry.db.session` states that contract
in full: :func:`foundry.db.session.open_session` creates the schema and begins the transaction, and
:func:`foundry.db.session.rollback_and_close` ends them.

**How a test registers its models.** No table is created unless the test asks for it, and there is
no marker or configuration key to ask with, so pytest's own indirect parametrisation delivers the
value as ``request.param``::

    @pytest.mark.parametrize("foundry_db", [Widget], indirect=True)
    def test_a_commit_is_visible_in_the_test(foundry_db: Session) -> None:
    foundry_db.add(Widget(id=1))
    foundry_db.commit()

The value names mapped classes and :class:`~sqlalchemy.Table` objects, alone or in an iterable, as
:mod:`foundry.db.schema` reads them; an abstract declarative base owns no table and is rejected by
:func:`foundry.db.schema.normalize_models`. Without the parametrisation the fixture yields a session
over an empty schema.

**Isolation and lazy import.** Every request builds its own engine, so nothing is shared or cached
at module or session scope, and the teardown runs in a ``finally`` -- after a failed test and after
a ``KeyboardInterrupt`` just as after a passing one. ``SQLAlchemy`` is imported inside the fixture,
outside the plugin's 150 ms startup budget (implicit requirement I5). The engine
:func:`foundry.db.engine.create_engine` accepts nothing but an in-memory sqlite URL, so there is no
server, driver install, migration step or socket, which is why the URL is not a parameter here.
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
    ``param`` attribute at all, and that is a legitimate request rather than an error. Nothing is
    validated here: :func:`foundry.db.schema.normalize_models` normalises ``None``, one declarative
    model class, one :class:`~sqlalchemy.Table` and an iterable of either, and raises
    :class:`~foundry.core.exceptions.FoundryError` naming a value it cannot use.

    Returns:
        Whatever the test passed as the parametrisation value, or ``None`` when it passed nothing.
    """
    models: ModelSpec = getattr(request, "param", None)
    return models


@pytest.fixture
def foundry_db(request: pytest.FixtureRequest) -> Iterator[OrmSession]:
    """Yield a transactional ``Session`` over a private in-memory sqlite database.

    Function scoped: every test that requests the fixture gets a fresh engine, a fresh schema and a
    fresh transaction, and nothing survives into the next test. The schema holds the tables of the
    models the test passed by indirect parametrisation -- ``@pytest.mark.parametrize("foundry_db",
    [Widget], indirect=True)`` -- or no table at all when it passed nothing. Writes the test makes,
    including ones it commits, are rolled back at teardown, and the session's connection, with the
    in-memory database, is released on the failing and interrupted paths as well as the passing one.
    The yielded value is a plain :class:`sqlalchemy.orm.Session`: query it with SQLAlchemy 2.0 typed
    statements, add rows with ``session.add`` and commit with ``session.commit``.

    Yields:
        An open session, bound to the test's own engine, whose outer transaction has already begun
        and is rolled back at teardown.

    Raises:
        FoundryError: If the parametrisation value is neither a declarative model class nor a
            :class:`~sqlalchemy.Table`, or if the requested schema cannot be created.
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
