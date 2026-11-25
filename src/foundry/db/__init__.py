"""The database fixture support package: the engine and the transactional session behind it.

What ``foundry_db`` (AAP 0.4.3) is built from: a fresh in-memory sqlite engine per test, a schema
created from the models that test itself registers, and a session whose outer transaction is rolled
back at teardown, so one test's rows, its schema and its committed batches are invisible to the next
(AAP 0.7.1). :mod:`foundry.db.engine` owns the connection and the pool holding it,
:mod:`foundry.db.session` the transaction that makes the promise true. It exports exactly the three
functions the fixture calls, in call order -- :func:`~foundry.db.engine.create_engine`,
:func:`~foundry.db.session.open_session` and :func:`~foundry.db.session.rollback_and_close` -- and
nothing else. The schema helpers stay reachable as ``foundry.db.schema.create_all``, ``drop_all``
and ``metadata_for`` but are not re-exported, the capability AAP 0.4.9 promises being the three-call
lifecycle and no caller benefiting from a barrel that pulled the schema module in at import time.
Layering (AAP 0.4.8): these modules import :mod:`foundry.core` and :mod:`foundry.obs` and nothing
else of Foundry, and nothing above them is imported from here. SQLAlchemy is a declared dependency
deferred to the functions that use it (AAP 0.5.1, implicit requirement I5), because the plugin loads
at every pytest start inside a 150 ms budget (AAP 0.14.2) while a minority of tests request the
database fixture. Only sqlite is accepted, synchronously and in memory (AAP 0.3.1, AAP 0.9.2): no
server lifecycle, driver installation, network or async driver, so the suite's offline guarantee
holds by construction and a per-test engine leaves nothing behind. The fixture lifecycle is
single-threaded and deterministic (AAP 0.4.8). Importing this module defines names only.
"""

from __future__ import annotations

from foundry.db.engine import create_engine as create_engine
from foundry.db.session import open_session as open_session
from foundry.db.session import rollback_and_close as rollback_and_close

__all__: list[str] = ["create_engine", "open_session", "rollback_and_close"]
