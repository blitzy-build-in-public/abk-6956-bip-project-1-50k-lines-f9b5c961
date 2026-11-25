"""A real pytest session activates the delivered fixtures through a ``pytest_plugins`` declaration.

AAP 0.4.3's fixture table is the compatibility promise, and AAP 0.14.1 asks for a test that
reaches each public fixture name through the real entry point rather than by calling its function.
This module is that entry point: it writes a throwaway project, declares the eight fixture modules
in its ``conftest.py`` as a consuming project does, and runs pytest over a test module that
requests all ten fixtures. The child run asserts that every one of the ten names resolves in a
session that started from nothing but that list -- no configuration, and no plugin loaded from the
``pytest11`` entry point of AAP 0.4.2 -- and that the four session-scoped fixtures hand two
consecutive tests the same object while the six function-scoped ones hand each test a fresh one,
which is the scope column observed rather than read.

Every child runs with ``PYTEST_DISABLE_PLUGIN_AUTOLOAD`` set, so what they prove is the
``pytest_plugins`` declaration path alone; autoloading is what an installed consuming environment
relies on, and AAP 0.14.1's zero-config test proves that. The child records two facts a green run
would not show -- that no plugin named ``foundry`` is loaded, and the module it imported, compared
with ``foundry.__file__`` -- so no substituted copy can stand in. A second child declares nothing
and requests one fixture, which must then be unresolvable.

``foundry_http`` is deliberately absent: the ten fixtures above are the ones the declaration path is
proved with. ``foundry_api`` is reached by a third child, which declares only its two modules and
configures the documented refusal -- a recording run with no ``http.base_url`` -- so the transport
that mode builds is observed being released rather than left open when the HTTP session it belongs
to is never constructed.
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

_API_CONFTEST: Final[str] = (
    'pytest_plugins = ["foundry.fixtures.config", "foundry.fixtures.http"]\n'
)
"""The declaration a project makes to hold the configured API client and its configuration."""

_RECORDING_TOML: Final[str] = '[cassette]\nmode = "record"\n'
"""A recording run: the mode whose cassette session builds the live transport it may send on."""

_NO_BASE_URL_MODULE: Final[str] = '''"""Requests the API client with no base URL."""


def test_api_needs_a_base_url(foundry_api: object) -> None:
    """The fixture refuses the configuration before the test body runs."""
    assert foundry_api
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


def test_a_refused_configuration_releases_the_cassette_transport(
    pytester: pytest.Pytester, clean_foundry_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recording run with no base URL errors once and leaves no transport open behind it.

    A recording cassette session is built before the HTTP session that owns it, and a mode whose row
    has a network already holds a live transport by then. The constructor refuses the configuration
    on its documented error row, so this asserts the whole of that path: the ``ConfigError`` naming
    ``http.base_url`` reaches the one test that requested the fixture and the run still ends with
    exit code 1, and the transport the abandoned cassette was handed is closed exactly once rather
    than outliving the run with its connection pool. ``LiveTransport.close`` is the observation
    because it is the release a leaked pool would never reach, and it is patched in this process
    because the child runs in it.
    """
    from foundry.http.transport import LiveTransport

    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    pytester.makeconftest(_API_CONFTEST)
    (pytester.path / "foundry.toml").write_text(_RECORDING_TOML, encoding="utf-8")
    pytester.makepyfile(test_api=_NO_BASE_URL_MODULE)

    closed: list[str] = []
    release = LiveTransport.close

    def _observed_close(self: LiveTransport) -> None:
        closed.append("close")
        release(self)

    monkeypatch.setattr(LiveTransport, "close", _observed_close)
    result = pytester.runpytest("-q")
    assert result.ret == 1, result.stdout.str()
    result.assert_outcomes(errors=1)
    assert "foundry.core.exceptions.ConfigError: http.base_url is not set" in result.stdout.str()
    assert closed == ["close"], (
        f"the transport the refused configuration abandoned must be released exactly once: {closed}"
    )
    assert not (pytester.path / "cassettes").exists(), "a refused run records no cassette document"
