"""The twelve public fixtures in one module, for registration by ``foundry.plugin``.

``foundry.plugin`` registers the whole fixture family in a single call during ``pytest_configure``
(AAP 0.4.2), which is what makes the fixtures available before collection and lets a consuming suite
use them with no ``-p`` flag, no ``conftest.py`` declaration and no configuration file. This module
is the object that call registers: pytest's fixture discovery scans the attributes of whatever
object it is handed, and a re-exported fixture function is discovered exactly as one defined here
would be, because each binding refers to the same function object rather than to a wrapper.

The family, with the scope AAP 0.4.3 publishes for each::

    foundry_config       session    resolved configuration, carrying a source per key
    foundry_profile      session    active profile name
    foundry_run_id       session    YYYYmmddTHHMMSS-<6 hex> identity of the run
    foundry_artifacts    session    per-test writer under artifacts/<run_id>/
    foundry_http         function   unauthenticated session, no base URL
    foundry_api          function   base URL, auth, static headers and cassette mode applied
    foundry_data         function   loader for CSV, JSON, YAML and JSONL files under data.dir
    foundry_db           function   transactional in-memory sqlite session
    foundry_factory      function   Faker-backed factory seeded from the run id and node id
    foundry_clock        function   frozen clock that only its test moves
    foundry_log_capture  function   bounded ring of records from the ``foundry`` logger
    foundry_poll         function   poller carrying an injectable sleep

**This module adds no behaviour.** Every function lives in the sibling module its import names and
carries its own docstring, scope and dependency wiring there. What stands here is the imports,
:data:`FIXTURE_NAMES`, ``__all__`` and this docstring: nothing is decorated, wrapped or rebound, no
import is conditional, no configuration is read and no I/O happens. Importing the module therefore
defines names only, and costs the plugin's startup budget nothing beyond the ten sibling modules it
pulls in (AAP 0.14.2, implicit requirement I5).

**Why a module rather than a plugin-list declaration.** Registration belongs to ``foundry.plugin``
alone (AAP 0.4.8, AAP 0.9.2): listing fixture modules in a non-root conftest is deprecated, and a
plugin shipped as a distribution registers itself, so this module declares no plugin list and no
hook.

**Layering.** Every import edge stays inside this package. ``foundry.fixtures`` is the one
capability rung package permitted to import ``foundry.config`` (AAP 0.4.8); the execution, reporting
and CLI packages stay out of reach, and the sibling modules are named directly rather than through
the ``foundry.fixtures`` package barrel, so ``import foundry.fixtures.plugin`` succeeds whatever
module is imported first and no cycle is possible.

**Stability.** The twelve names and their scopes are the published contract of AAP 0.4.3 and
AAP 0.4.9: a consuming suite spells them in test signatures and ``foundry.plugin`` registers them
under exactly these names, so they may be added to but never renamed or removed.
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
