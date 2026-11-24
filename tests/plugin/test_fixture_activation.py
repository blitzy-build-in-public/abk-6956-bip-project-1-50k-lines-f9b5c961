"""A real pytest session activates the delivered fixtures through a ``pytest_plugins`` declaration.

AAP 0.4.3's fixture table is the compatibility promise, and AAP 0.14.1 asks for a test that reaches
each public fixture name through the real entry point rather than by calling its function. This
module is that entry point: it writes a throwaway project, declares the eight fixture modules in
its ``conftest.py`` exactly as a consuming project does, and runs pytest over a test module that
requests all ten fixtures. The child run asserts what a consuming suite depends on:

* every one of the ten names resolves in a session that started from nothing but that
  ``pytest_plugins`` list -- no configuration, and no plugin loaded from the ``pytest11`` entry
  point of AAP 0.4.2;
* the four session-scoped fixtures (``foundry_config``, ``foundry_profile``, ``foundry_run_id`` and
  ``foundry_artifacts``) hand two consecutive tests the same object, and the six function-scoped
  ones hand each test a fresh object, which is the scope column observed rather than read.

Both children run with ``PYTEST_DISABLE_PLUGIN_AUTOLOAD`` set, so they deliberately autoload
nothing and what they prove is the ``pytest_plugins`` declaration path alone -- the same result in a
source checkout and in an environment that installed the distribution and therefore publishes
``foundry = "foundry.plugin"``. Autoloading is what an installed consuming environment relies on,
and it is AAP 0.14.1's zero-config test that proves it, not this module. The child records the one
fact a green run alone would not show -- that no plugin named ``foundry`` is loaded -- so the
declaration is demonstrably what registered the fixtures.

The child also records the module it imported, and the parent compares that path with
``foundry.__file__`` resolved: both are the package the run actually uses, so no substituted copy
can stand in, in a source checkout and in an installed environment alike. Those values are held in
the child and compared with ``is``, because a released object's address, and so its ``id()``, can be
handed straight to the next one.

A second child run declares nothing and requests one fixture, which must then be unresolvable: the
green run above cannot be a run in which fixtures resolve by accident.

``foundry_http`` and ``foundry_api`` are deliberately absent: ``src/foundry/fixtures/http.py``,
``api.py`` and ``plugin.py`` are sibling deliverables still in flight, and those two fixtures land
with their modules. Nothing here requests or names them.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final

import pytest

import foundry

_FIXTURE_MODULES: Final[tuple[str, ...]] = (
    "foundry.fixtures.artifacts",
    "foundry.fixtures.clock",
    "foundry.fixtures.config",
    "foundry.fixtures.data",
    "foundry.fixtures.db",
    "foundry.fixtures.factory",
    "foundry.fixtures.logs",
    "foundry.fixtures.polling",
)

_CONFTEST: Final[str] = (
    "pytest_plugins = [\n" + "".join(f'    "{module}",\n' for module in _FIXTURE_MODULES) + "]\n"
)
"""The declaration a consuming project writes, listing the eight delivered fixture modules."""

_FIXTURE_NAMES: Final[tuple[str, ...]] = (
    "foundry_config",
    "foundry_profile",
    "foundry_run_id",
    "foundry_artifacts",
    "foundry_data",
    "foundry_db",
    "foundry_factory",
    "foundry_clock",
    "foundry_log_capture",
    "foundry_poll",
)
"""The ten fixtures the tree delivers, in AAP 0.4.3's order."""

_SESSION_SCOPED: Final[list[str]] = [
    "foundry_artifacts",
    "foundry_config",
    "foundry_profile",
    "foundry_run_id",
]
"""The four the table publishes session scoped, sorted; the remaining six are function scoped."""

_ARGUMENTS: Final[str] = ",\n    ".join(_FIXTURE_NAMES)

_VALUES: Final[str] = ",\n        ".join(f"{name}={name}" for name in _FIXTURE_NAMES)

_CHILD_MODULE: Final[str] = f'''"""Requests every delivered fixture twice."""

import json
import pathlib

import foundry
import pytest

REPORT = pathlib.Path(__file__).with_name("report.json")
RECORDS: dict[str, dict[str, object]] = {{}}


def _values(**values: object) -> dict[str, object]:
    """Return the fixture values themselves, kept referenced so identity stays meaningful."""
    return dict(values)


def test_every_fixture_resolves(
    {_ARGUMENTS}
) -> None:
    """All ten names resolve in a session that declared only the fixture modules."""
    RECORDS["first"] = _values(
        {_VALUES}
    )
    assert len(RECORDS["first"]) == {len(_FIXTURE_NAMES)}
    assert foundry_profile == "default"
    assert foundry_run_id.count("-") == 1


def test_the_published_scopes_hold(
    pytestconfig: pytest.Config,
    {_ARGUMENTS}
) -> None:
    """The session-scoped four are shared; the six others are fresh; no plugin was autoloaded."""
    second = _values(
        {_VALUES}
    )
    first = RECORDS["first"]
    shared = sorted(name for name in first if second[name] is first[name])
    entry_point_plugin = pytestconfig.pluginmanager.hasplugin("foundry")
    REPORT.write_text(
        json.dumps(
            {{
                "entry_point_plugin": entry_point_plugin,
                "module": foundry.__file__,
                "resolved": sorted(first),
                "shared": shared,
            }},
            sort_keys=True,
        ),
        encoding="utf-8",
    )
'''

_UNACTIVATED_MODULE: Final[str] = '''"""Requests a fixture nothing declared."""


def test_foundry_clock(foundry_clock: object) -> None:
    """A fixture no plugin registered cannot be requested."""
    assert foundry_clock
'''


def test_a_fresh_session_activates_the_ten_delivered_fixtures(
    pytester: pytest.Pytester, clean_foundry_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A child run declares the fixture modules, resolves all ten and honours their scopes."""
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    pytester.makeconftest(_CONFTEST)
    pytester.makepyfile(test_activation=_CHILD_MODULE)
    result = pytester.runpytest("-q")
    result.assert_outcomes(passed=2)
    report = json.loads((pytester.path / "report.json").read_text(encoding="utf-8"))
    assert report["entry_point_plugin"] is False
    assert Path(report["module"]).resolve() == Path(foundry.__file__).resolve()
    assert report["resolved"] == sorted(_FIXTURE_NAMES)
    assert report["shared"] == _SESSION_SCOPED


def test_a_session_that_declares_nothing_resolves_no_foundry_fixture(
    pytester: pytest.Pytester, clean_foundry_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control for the run above: an undeclared fixture is an error, not a resolution."""
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    pytester.makepyfile(test_unactivated=_UNACTIVATED_MODULE)
    result = pytester.runpytest("-q")
    result.assert_outcomes(errors=1)
    assert "fixture 'foundry_clock' not found" in result.stdout.str()
