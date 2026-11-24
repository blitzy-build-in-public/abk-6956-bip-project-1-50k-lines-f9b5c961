"""The database fixture support package: the engine and the transactional session behind it.

This package is what ``foundry_db`` (AAP 0.4.3) is built from. Every test that requests the
fixture gets a fresh in-memory sqlite engine, a schema created from the models that test itself
registers, and a session whose outer transaction is rolled back at teardown, so one test's rows,
its schema and its committed batches are invisible to the next (AAP 0.7.1). Two modules carry
that: :mod:`foundry.db.engine` owns the connection and the pool that holds it, and
:mod:`foundry.db.session` owns the transaction that makes the promise true. This module is the
barrel that names their public entry points, so a caller writes
``from foundry.db import create_engine, open_session, rollback_and_close`` instead of reaching
into either sibling.

**What it exports, and what it deliberately does not.** Exactly the three functions the fixture
calls, in call order: :func:`~foundry.db.engine.create_engine`,
:func:`~foundry.db.session.open_session` and :func:`~foundry.db.session.rollback_and_close`. The
schema helpers stay reachable as ``foundry.db.schema.create_all``, ``drop_all`` and
``metadata_for`` for the code that needs them, but they are not re-exported here: the capability
this package promises (AAP 0.4.9) is the three-call lifecycle, and a barrel that also pulled the
schema module in at import time would widen that promise for no caller's benefit. Nothing else
lives here either -- no version attribute (that belongs to :mod:`foundry`), no constant, no helper
and no module-level state (AAP 0.4.8).

**Layering** (AAP 0.4.8). ``foundry.db`` sits on the foundation rung: the modules under it import
:mod:`foundry.core` and :mod:`foundry.obs` and nothing else of Foundry, and nothing above them --
not ``foundry.fixtures``, not ``foundry.execution``, not ``foundry.reporting`` -- is imported
from here. This barrel's own imports are its two siblings, so it adds no edge the layering table
does not already allow.

**SQLAlchemy is a declared dependency, imported lazily** (AAP 0.5.1, implicit requirement I5).
The distribution declares ``SQLAlchemy`` and ships no ORM model of its own; the models are always
the test's. Every module in this package defers the SQLAlchemy import to the function that runs
it and annotates with names imported under ``TYPE_CHECKING``, which is what keeps importing
``foundry.db`` -- or anything that merely mentions it -- free of SQLAlchemy. The plugin is loaded
at every pytest start inside a 150 ms budget (AAP 0.14.2) and the database fixture is requested by
a minority of tests, so that cost belongs to those tests rather than to startup; a test holds the
property by asserting ``"sqlalchemy" not in sys.modules`` after importing this module.

**sqlite only, synchronously** (AAP 0.3.1, AAP 0.9.2). One backend, in memory, with no server
lifecycle, no driver installation, no network and no async driver: an in-memory database needs
neither a socket nor a file, so it satisfies the suite's offline guarantee by construction, and a
per-test engine leaves nothing behind for a later test or a later run to find. The fixture
lifecycle is single-threaded and deterministic (AAP 0.4.8, "Fixture lifecycle rule"), so the
connection is deliberately not opened for cross-thread use.

**Importing this module does no work** (AAP 0.4.8). It defines names only: no I/O, no environment
read, no logger configuration and no module-level mutable state.

Example:
    The lifecycle ``foundry_db`` runs, for a schema the test declares itself::

        from foundry.db import create_engine, open_session, rollback_and_close

        engine = create_engine()
        session = open_session(engine, [MyModel])
        try:
            session.add(MyModel(name="widget"))
            session.commit()
        finally:
            rollback_and_close(session)
"""

from __future__ import annotations

from foundry.db.engine import create_engine as create_engine
from foundry.db.session import open_session as open_session
from foundry.db.session import rollback_and_close as rollback_and_close

__all__: list[str] = ["create_engine", "open_session", "rollback_and_close"]
