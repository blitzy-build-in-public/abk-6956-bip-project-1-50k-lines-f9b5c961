"""The twelve public fixtures in one module, for registration by ``foundry.plugin``.

``foundry.plugin`` registers the whole fixture family in a single call during ``pytest_configure``
(AAP 0.4.2), which makes the fixtures available before collection and lets a consuming suite use
them with no ``-p`` flag, no ``conftest.py`` declaration and no configuration file. This module is
the object that call registers: pytest's fixture discovery scans the attributes of whatever object
it is handed, and a re-exported fixture function is discovered exactly as one defined here would be,
because each binding refers to the same function object rather than to a wrapper.

The family, with the scope AAP 0.4.3 publishes for each::

    foundry_config session resolved configuration, carrying a source per key
    foundry_profile session active profile name
    foundry_run_id session YYYYmmddTHHMMSS-<6 hex> identity of the run
    foundry_artifacts session per-test writer under artifacts/<run_id>/
    foundry_http function unauthenticated session, no base URL
    foundry_api function base URL, auth, static headers and cassette mode applied
    foundry_data function loader for CSV, JSON, YAML and JSONL files under data.dir
    foundry_db function transactional in-memory sqlite session
    foundry_factory function Faker-backed factory seeded from the run id and node id
    foundry_clock function frozen clock that only its test moves
    foundry_log_capture function bounded ring of records from the ``foundry`` logger
    foundry_poll function poller carrying an injectable sleep

**This module adds no behaviour.** Every function lives in the sibling module its import names and
carries its own docstring, scope and wiring there; what stands here is the imports,
:data:`FIXTURE_NAMES`, ``__all__`` and this docstring. Nothing is decorated, wrapped or rebound, no
import is conditional, no configuration is read and no I/O happens. Registration belongs to
``foundry.plugin`` alone -- listing fixture modules in a non-root conftest is deprecated -- so this
module declares no plugin list and no hook, and the siblings are named directly rather than through
the ``foundry.fixtures`` barrel, so no cycle is possible.

**Stability.** The twelve names and their scopes are the published contract, spelled in test
signatures and registered by ``foundry.plugin`` under exactly these names, so they may be added to
but never renamed or removed.
"""

from __future__ import annotations

from typing import Final

from foundry.fixtures.artifacts import foundry_artifacts as foundry_artifacts
from foundry.fixtures.artifacts import foundry_run_id as foundry_run_id
from foundry.fixtures.clock import foundry_clock as foundry_clock
from foundry.fixtures.config import foundry_config as foundry_config
from foundry.fixtures.config import foundry_profile as foundry_profile
from foundry.fixtures.data import foundry_data as foundry_data
from foundry.fixtures.db import foundry_db as foundry_db
from foundry.fixtures.factory import foundry_factory as foundry_factory
from foundry.fixtures.http import foundry_api as foundry_api
from foundry.fixtures.http import foundry_http as foundry_http
from foundry.fixtures.logs import foundry_log_capture as foundry_log_capture
from foundry.fixtures.polling import foundry_poll as foundry_poll

# One name per re-export above, sorted so registration and the public-contract test iterate the
# family in a fixed order without reflecting on the module. The tuple and the re-exports must stay
# in sync; tests/plugin/test_public_contract.py checks both against the published fixture set.
FIXTURE_NAMES: Final[tuple[str, ...]] = (
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
)

__all__ = [
    "FIXTURE_NAMES",
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
