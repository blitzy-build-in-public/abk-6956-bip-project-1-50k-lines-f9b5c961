"""The public fixture surface of ``foundry``: twelve fixtures and the six types they return.

``pytest-foundry`` publishes these names through its ``pytest11`` entry point, so an installed
suite requests a fixture by name and imports nothing from Foundry itself (AAP 0.4.2). The names,
their scopes and their return types are the contract of AAP 0.4.3, and AAP 0.4.9 makes them
additive only: a later release may add a fixture or a facade type, and none of these may be renamed
or removed.

========================  ==========  ============================================================
Fixture                   Scope       Returns
========================  ==========  ============================================================
``foundry_config``        session     ``FoundryConfig`` (resolved keys and their sources)
``foundry_profile``       session     ``str`` (``default``, ``local``, ``ci``, ``staging``)
``foundry_run_id``        session     ``str`` (the ``YYYYmmddTHHMMSS-<6 hex>`` run id)
``foundry_artifacts``     session     ``FoundryArtifacts`` (``dir_for``, ``write``, ``manifest``)
``foundry_http``          function    ``FoundrySession`` (unauthenticated; retries from config)
``foundry_api``           function    ``FoundrySession`` (base URL, auth, headers, cassettes)
``foundry_data``          function    ``FoundryDataLoader`` (``cases``, ``matrix``)
``foundry_db``            function    ``sqlalchemy.orm.Session`` (in-memory sqlite, rolled back)
``foundry_factory``       function    ``FoundryFactory`` (Faker seeded from run id and node id)
``foundry_clock``         function    ``FoundryClock`` (frozen; ``now``, ``advance``, ``set``)
``foundry_log_capture``   function    ``FoundryLogCapture`` (``tail`` over a bounded ring)
``foundry_poll``          function    ``FoundryPoll`` (``eventually`` with an injected sleep)
========================  ==========  ============================================================

The six facade types are re-exported beside the fixtures, so a test annotates its parameter without
reaching into the capability package that defines it:

* ``FoundryArtifacts`` -- the artifact writer ``foundry_artifacts`` returns: ``dir_for``, ``write``
  and ``manifest`` over ``artifacts/<run_id>/``.
* ``FoundryDataLoader`` -- the loader ``foundry_data`` returns: ``cases`` and ``matrix``.
* ``FoundryFactory`` -- the Faker-backed factory ``foundry_factory`` returns.
* ``FoundryClock`` -- the frozen clock ``foundry_clock`` returns: ``now``, ``advance``, ``set``.
* ``FoundryLogCapture`` -- the bounded log ring ``foundry_log_capture`` returns: ``tail`` and
  ``records``.
* ``FoundryPoll`` -- the poller ``foundry_poll`` returns: ``eventually``.

Each is defined in the module named beside its fixture above, as a protocol or a frozen dataclass,
and the object its fixture returns satisfies it structurally.

**Registration is not this module's job.** ``foundry.fixtures.plugin`` registers the twelve fixtures
with pytest during ``pytest_configure`` (AAP 0.4.2), and this barrel deliberately does not import
it: a package is initialised before any of its submodules finishes importing, so an import of the
registration module from here would be the package's one cycle. Importing this module defines names
only -- no ``pytest_plugins`` declaration, no ``pytest.fixture`` call, no I/O, no logger
configuration and no module state -- so ``import foundry.fixtures`` never changes a session.

**Import form.** Every name below is imported as ``X as X``: that spelling is the explicit
re-export ``mypy --strict``'s no-implicit-reexport requires, and with this isort configuration a
single block of aliased names is reported as unsorted (I001), so each name keeps its own statement.

**Import cost.** The re-exports are eager on purpose. The nine sibling modules defer every
third-party import (``httpx``, ``SQLAlchemy``, ``Faker``, ``pydantic``, ``jsonschema``) into the
fixture bodies that need them, so the barrel adds no distribution to the ``pytest_configure`` path,
which is where the registration module pulls the whole package in (AAP 0.14.2, implicit requirement
I5).

**Layering** (AAP 0.4.8). The nine fixture modules below are this file's only Foundry imports. The
package may import :mod:`foundry.core`, :mod:`foundry.obs`, the capability rung beneath it and
:mod:`foundry.config` -- the single upward edge AAP 0.4.8 grants it, exercised inside
``foundry.fixtures.config`` rather than here -- and never :mod:`foundry.execution`,
:mod:`foundry.reporting`, :mod:`foundry.cli` or :mod:`foundry.plugin`.
"""

from __future__ import annotations

# The run identity and the artifact tree: ``dir_for``, ``write`` and ``manifest``.
from foundry.fixtures.artifacts import FoundryArtifacts as FoundryArtifacts
from foundry.fixtures.artifacts import foundry_artifacts as foundry_artifacts
from foundry.fixtures.artifacts import foundry_run_id as foundry_run_id

# A frozen, advanceable clock.
from foundry.fixtures.clock import FoundryClock as FoundryClock
from foundry.fixtures.clock import foundry_clock as foundry_clock

# The resolved configuration, and the profile that selected part of it.
from foundry.fixtures.config import foundry_config as foundry_config
from foundry.fixtures.config import foundry_profile as foundry_profile

# The CSV/JSON/YAML/JSONL case and matrix loader over ``data.dir``.
from foundry.fixtures.data import FoundryDataLoader as FoundryDataLoader
from foundry.fixtures.data import foundry_data as foundry_data

# One transactional sqlite session per test, rolled back at teardown.
from foundry.fixtures.db import foundry_db as foundry_db

# A Faker instance seeded from the run id and the node id, so two workers cannot interleave draws.
from foundry.fixtures.factory import FoundryFactory as FoundryFactory
from foundry.fixtures.factory import foundry_factory as foundry_factory

# The two HTTP clients: unauthenticated, and the configured API client with auth and cassettes.
from foundry.fixtures.http import foundry_api as foundry_api
from foundry.fixtures.http import foundry_http as foundry_http

# The bounded ring of records the ``foundry`` logger emitted during the test.
from foundry.fixtures.logs import FoundryLogCapture as FoundryLogCapture
from foundry.fixtures.logs import foundry_log_capture as foundry_log_capture

# Polling with the sleeper chosen by the caller.
from foundry.fixtures.polling import FoundryPoll as FoundryPoll
from foundry.fixtures.polling import foundry_poll as foundry_poll

__all__ = [
    "FoundryArtifacts",
    "FoundryClock",
    "FoundryDataLoader",
    "FoundryFactory",
    "FoundryLogCapture",
    "FoundryPoll",
    "foundry_api",
    "foundry_artifacts",
    "foundry_clock",
    "foundry_config",
    "foundry_data",
    "foundry_db",
    "foundry_factory",
    "foundry_http",
    "foundry_log_capture",
    "foundry_poll",
    "foundry_profile",
    "foundry_run_id",
]
