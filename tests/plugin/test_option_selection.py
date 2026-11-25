"""Plugin tests: a ``--foundry-*`` option may not change what a run collects.

AAP 0.2.1 makes the collected node ids a property of the project rather than of the arguments a run
was handed, and AAP 0.4.4's generic option is an argument like any other: naming a directory with it
must leave ``config.args``, the root directory, the configuration file it resolves and the node ids
exactly as a run without it. The option group arrives through a ``pytest11`` entry point, which
pytest loads after ``Config._initini`` has already fixed the root directory and the configuration
file, so a value typed as its own token is unknown at that point and is read as a collection
argument. :func:`foundry.config.cli_layer.restore_preparse_state` undoes that in
``pytest_load_initial_conftests``; these tests pin what the repair has to deliver, and they fail on
the run that loses ``testpaths`` because ``examples/checkout``'s own suite would then run inside a
session that is not configured for it.

Every run is in process through :meth:`pytest.Pytester.inline_run`, the same ``pytest.main`` a real
invocation takes, so the option group is registered by the installed entry point rather than by
anything this module declares. Determinism (AAP 0.14.3): each project and its configuration file
live under ``pytester.path`` and every value typed under the test's own ``tmp_path``, which is what
makes the misread measurable; nothing reads a clock, opens a socket or spawns a process, and every
comparison is over sorted node ids and fixed tuples.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pytest

_PYPROJECT: Final[str] = """
[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-ra --strict-markers"
pythonpath = ["src"]
"""
"""The project's own configuration file: everything a lost configuration file takes with it.

``--strict-markers`` is the effective half of ``addopts``: it reaches the parsed namespace only if
pytest prepended that line to the argument list, which happens before a Foundry option is read, and
``pythonpath`` is the setting pytest applies straight to ``sys.path``.
"""

_SELECTED: Final[str] = """
def test_first() -> None:
    assert True


def test_second() -> None:
    assert True
"""
"""The module ``testpaths`` selects."""

_OUTSIDE: Final[str] = """
def test_outside_the_testpaths() -> None:
    assert True
"""
"""A module only a run that lost ``testpaths`` reaches: it sits beside the ``tests`` folder."""

_PATH_OPTIONS: Final[tuple[str, ...]] = (
    "--foundry-artifacts.dir",
    "--foundry-data.dir",
    "--foundry-cassette.dir",
)
"""The scalar keys whose value is a directory, so a typed value is a path on disk (AAP 0.4.5)."""

_ARTIFACTS_OPTION: Final[str] = "--foundry-artifacts.dir"
"""The option whose value the run uses at configure time, so a test can see it arrive."""

_TEXT_OPTION: Final[str] = "--foundry-profile"
"""A value option whose value is no path: folding it must change nothing at all."""

_TEXT_VALUE: Final[str] = "ci"
"""A profile name AAP 0.4.5 publishes, and one no filesystem entry answers to."""


@dataclass
class _Selection:
    """What one run's collection depended on, field by field, so two runs can be compared."""

    args: tuple[str, ...] = ()
    rootpath: Path | None = None
    inipath: Path | None = None
    addopts: tuple[str, ...] = ()
    strict_markers: bool = False
    nodeids: tuple[str, ...] = ()


class _Recorder:
    """Record the selection a run makes, whichever arguments the run was handed."""

    def __init__(self) -> None:
        self.selection = _Selection()
        self.pythonpath: tuple[str, ...] = ()

    def pytest_configure(self, config: pytest.Config) -> None:
        """Record the state the pre-parse left behind, before collection reads it."""
        self.selection.args = tuple(str(argument) for argument in config.args)
        self.selection.rootpath = config.rootpath
        self.selection.inipath = config.inipath
        self.selection.addopts = tuple(str(token) for token in config.getini("addopts"))
        self.selection.strict_markers = bool(config.getoption("strict_markers"))
        self.pythonpath = tuple(sys.path)

    def pytest_collection_modifyitems(self, items: list[pytest.Item]) -> None:
        """Record the node ids the run collected, sorted."""
        self.selection.nodeids = tuple(sorted(item.nodeid for item in items))


def _project(pytester: pytest.Pytester) -> None:
    """Write the configured project every run in this module is measured against."""
    pytester.makepyprojecttoml(_PYPROJECT)
    pytester.mkdir("src")
    pytester.makepyfile(
        **{"tests/test_selected": _SELECTED, "test_outside_the_testpaths": _OUTSIDE}
    )


def _value(tmp_path: Path) -> Path:
    """Return a directory outside the project, so the value typed is not below its root directory.

    A path inside ``pytester.path`` would leave the root directory where it was even when the
    pre-parse read the value as a collection argument, so the misread has to be measured against a
    directory that shares no ancestor below the project with the invocation directory.
    """
    value = tmp_path / "option-value"
    value.mkdir()
    return value


def _record(pytester: pytest.Pytester, *args: str) -> _Recorder:
    """Collect the project in process with ``args`` and return what the run recorded."""
    recorder = _Recorder()
    pytester.inline_run("--collect-only", "-q", *args, plugins=[recorder])
    return recorder


@pytest.mark.parametrize("spelling", _PATH_OPTIONS)
def test_a_path_option_leaves_the_selection_the_project_declares(
    pytester: pytest.Pytester, tmp_path: Path, spelling: str
) -> None:
    """A directory typed as an option value changes no part of the selection (AAP 0.2.1)."""
    _project(pytester)
    value = _value(tmp_path)
    expected = _record(pytester)
    actual = _record(pytester, spelling, str(value))
    assert expected.selection.args == ("tests",)
    assert expected.selection.nodeids == (
        "tests/test_selected.py::test_first",
        "tests/test_selected.py::test_second",
    )
    assert actual.selection == expected.selection
    assert str(pytester.path / "src") in actual.pythonpath


def test_the_inline_form_of_a_path_option_keeps_the_selection_too(
    pytester: pytest.Pytester, tmp_path: Path
) -> None:
    """``--opt=value`` must stay equivalent to the pair form the pre-parse can misread."""
    _project(pytester)
    value = _value(tmp_path)
    inline = _record(pytester, f"{_ARTIFACTS_OPTION}={value}")
    assert inline.selection == _record(pytester).selection


def test_a_value_that_is_no_path_folds_without_changing_anything(pytester: pytest.Pytester) -> None:
    """An option that configures no path leaves every field of the selection untouched."""
    _project(pytester)
    assert _record(pytester, _TEXT_OPTION, _TEXT_VALUE).selection == _record(pytester).selection


def test_the_artifacts_option_still_names_the_directory_the_run_writes_to(
    pytester: pytest.Pytester, tmp_path: Path
) -> None:
    """The repaired run is not merely unbroken: the value typed still reaches the configuration."""
    _project(pytester)
    value = _value(tmp_path)
    expected = _record(pytester)
    actual = _record(pytester, _ARTIFACTS_OPTION, str(value))
    assert actual.selection == expected.selection
    assert list(value.iterdir()), "the value typed did not reach the artifacts configuration"
