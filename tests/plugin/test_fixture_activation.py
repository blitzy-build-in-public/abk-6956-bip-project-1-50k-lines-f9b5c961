"""A real session resolves the ten delivered fixtures through a consuming ``pytest_plugins`` list.

Autoload stays off, so the child proves that path alone; the second child is the control."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final

import pytest

import foundry

_MODULES: Final[str] = "artifacts clock config data db factory logs polling"
_CONFTEST: Final[str] = f"pytest_plugins = {[f'foundry.fixtures.{m}' for m in _MODULES.split()]}\n"
_FIXTURES: Final[str] = (
    "foundry_config foundry_profile foundry_run_id foundry_artifacts foundry_data "
    "foundry_db foundry_factory foundry_clock foundry_log_capture foundry_poll"
)
_SESSION_SCOPED: Final[str] = "foundry_artifacts foundry_config foundry_profile foundry_run_id"
_UNACTIVATED: Final[str] = "def test_foundry_clock(foundry_clock):\n    assert foundry_clock\n"
_API_CONFTEST: Final[str] = (
    'pytest_plugins = ["foundry.fixtures.config", "foundry.fixtures.http"]\n'
)
_RECORDING: Final[str] = '[cassette]\nmode = "record"\n'
_NO_BASE_URL: Final[str] = "def test_api_needs_a_base_url(foundry_api):\n    assert foundry_api\n"

_CHILD: Final[str] = f"""
import json, pathlib

import foundry, pytest

NAMES = ("foundry_config foundry_profile foundry_run_id foundry_artifacts foundry_data "
         "foundry_db foundry_factory foundry_clock foundry_log_capture foundry_poll").split()
REPORT = pathlib.Path(__file__).with_name("report.json")
RECORDS = {{}}

def test_every_fixture_resolves(request) -> None:
    RECORDS["first"] = {{name: request.getfixturevalue(name) for name in NAMES}}
    assert len(RECORDS["first"]) == {len(_FIXTURES.split())}

def test_the_published_scopes_hold(request, pytestconfig) -> None:
    second = {{name: request.getfixturevalue(name) for name in NAMES}}
    shared = sorted(name for name in second if second[name] is RECORDS["first"][name])
    document = dict(resolved=sorted(second), shared=shared, module=foundry.__file__,
                    entry_point_plugin=pytestconfig.pluginmanager.hasplugin("foundry"))
    REPORT.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
"""


def test_a_fresh_session_activates_the_ten_delivered_fixtures(
    pytester: pytest.Pytester, clean_foundry_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    pytester.makeconftest(_CONFTEST)
    pytester.makepyfile(test_activation=_CHILD)
    pytester.runpytest("-q").assert_outcomes(passed=2)
    report = json.loads((pytester.path / "report.json").read_text(encoding="utf-8"))
    assert report["entry_point_plugin"] is False
    assert Path(report["module"]).resolve() == Path(foundry.__file__).resolve()
    assert report["resolved"] == sorted(_FIXTURES.split())
    assert report["shared"] == _SESSION_SCOPED.split()


def test_a_session_that_declares_nothing_resolves_no_foundry_fixture(
    pytester: pytest.Pytester, clean_foundry_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    pytester.makepyfile(test_unactivated=_UNACTIVATED)
    result = pytester.runpytest("-q")
    result.assert_outcomes(errors=1)
    assert "fixture 'foundry_clock' not found" in result.stdout.str()


def test_a_refused_configuration_releases_the_cassette_transport(
    pytester: pytest.Pytester, clean_foundry_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from foundry.http.transport import LiveTransport

    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    pytester.makeconftest(_API_CONFTEST)
    (pytester.path / "foundry.toml").write_text(_RECORDING, encoding="utf-8")
    pytester.makepyfile(test_api=_NO_BASE_URL)
    closed: list[str] = []
    release = LiveTransport.close

    def _observed_close(self: LiveTransport) -> None:
        closed.append("close")
        release(self)

    monkeypatch.setattr(LiveTransport, "close", _observed_close)
    result = pytester.runpytest("-q")
    output = result.stdout.str()
    assert result.ret == 1 and "ConfigError: http.base_url is not set" in output, output
    result.assert_outcomes(errors=1)
    assert closed == ["close"], f"the abandoned transport must be released once: {closed}"
    assert not (pytester.path / "cassettes").exists(), "a refused run records no cassette document"
