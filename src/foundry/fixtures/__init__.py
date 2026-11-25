"""The public fixture surface of ``foundry``: twelve fixtures and the six types they return.

``pytest-foundry`` publishes these names through its ``pytest11`` entry point, so an installed suite
requests a fixture by name and imports nothing from Foundry itself. ``foundry.fixtures.plugin``
lists each fixture with the scope and return type published for it and registers the family with
pytest; the names are additive only, so a later release may add a fixture or a facade type and none
of these may be renamed or removed. The six facade types are re-exported beside the fixtures, so a
test annotates its parameter without reaching into the capability package that defines it:
``FoundryArtifacts``, ``FoundryDataLoader``, ``FoundryFactory``, ``FoundryClock``,
``FoundryLogCapture`` and ``FoundryPoll``. Each is a protocol or a frozen dataclass in the module
named beside its fixture, and the object its fixture returns satisfies it structurally.

**Registration is not this module's job.** ``foundry.fixtures.plugin`` registers the twelve fixtures
during ``pytest_configure``, and this barrel does not import it: a package is initialised before any
submodule finishes importing, so that import would be the package's one cycle. Importing this module
defines names only -- no ``pytest_plugins`` declaration, no ``pytest.fixture`` call, no I/O, no
logger configuration, no module state.

**Import form.** Every name below is imported as ``X as X``: that spelling is the explicit re-export
``mypy --strict``'s no-implicit-reexport requires, and with this isort configuration a single block
of aliased names is reported as unsorted (I001), so each name keeps its own statement.

**Import cost and layering.** The re-exports are eager on purpose: the nine sibling modules defer
every third-party import (``httpx``, ``SQLAlchemy``, ``Faker``, ``pydantic``, ``jsonschema``) into
the fixture bodies that need them, so the barrel adds nothing to the ``pytest_configure`` path.
Those nine modules are this file's only Foundry imports; the one upward edge, ``foundry.config``, is
exercised inside ``foundry.fixtures.config``, so :mod:`foundry.execution`,
:mod:`foundry.reporting`, :mod:`foundry.cli` and :mod:`foundry.plugin` stay unreachable.
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
