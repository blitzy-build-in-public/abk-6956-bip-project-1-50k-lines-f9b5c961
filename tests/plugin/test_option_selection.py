"""A ``--foundry-*`` option may not change what a run collects (AAP 0.2.1, AAP 0.4.4).

The option group arrives after the root directory and configuration file are fixed, so a typed path
reads as a collection argument; these tests pin the repair."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Final

import pytest

_PYPROJECT: Final[str] = (
    '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'
    'addopts = "-ra --strict-markers"\npythonpath = ["src"]\n'
)
_SELECTED: Final[str] = (
    "\ndef test_first() -> None:\n    assert True\n\ndef test_second() -> None:\n    assert True\n"
)
_OUTSIDE: Final[str] = "\ndef test_outside_the_testpaths() -> None:\n    assert True\n"
_PATH_OPTIONS: Final[str] = "--foundry-artifacts.dir --foundry-data.dir --foundry-cassette.dir"
_ARTIFACTS_OPTION: Final[str] = "--foundry-artifacts.dir"
_TEXT_OPTION: Final[str] = "--foundry-profile"
_NODEIDS: Final[str] = "tests/test_selected.py::test_first tests/test_selected.py::test_second"


class _Recorder:
    args: tuple[str, ...] = ()
    rest: tuple[object, ...] = ()
    nodeids: tuple[str, ...] = ()
    pythonpath: tuple[str, ...] = ()

    def pytest_configure(self, config: pytest.Config) -> None:
        self.args = tuple(str(argument) for argument in config.args)
        self.rest = (
            config.rootpath,
            config.inipath,
            config.getini("addopts"),
            config.getoption("strict_markers"),
        )
        self.pythonpath = tuple(sys.path)

    def pytest_collection_modifyitems(self, items: list[pytest.Item]) -> None:
        self.nodeids = tuple(sorted(item.nodeid for item in items))


def _project(pytester: pytest.Pytester) -> None:
    pytester.makepyprojecttoml(_PYPROJECT)
    pytester.mkdir("src")
    pytester.makepyfile(
        **{"tests/test_selected": _SELECTED, "test_outside_the_testpaths": _OUTSIDE}
    )


def _record(pytester: pytest.Pytester, *args: str) -> _Recorder:
    recorder = _Recorder()
    pytester.inline_run("--collect-only", "-q", *args, plugins=[recorder])
    return recorder


@pytest.mark.parametrize(
    ("spelling", "inline", "literal"),
    [
        *((name, False, "") for name in _PATH_OPTIONS.split()),
        (_ARTIFACTS_OPTION, True, ""),
        (_TEXT_OPTION, False, "ci"),
    ],
)
def test_no_option_changes_what_the_project_collects(
    pytester: pytest.Pytester, tmp_path: Path, spelling: str, inline: bool, literal: str
) -> None:
    _project(pytester)
    expected = _record(pytester)
    (tmp_path / "option-value").mkdir()
    value = literal or str(tmp_path / "option-value")
    actual = _record(pytester, *((f"{spelling}={value}",) if inline else (spelling, value)))
    assert (expected.args, expected.nodeids) == (("tests",), tuple(_NODEIDS.split()))
    assert actual.rest == expected.rest and str(pytester.path / "src") in actual.pythonpath


def test_the_artifacts_option_still_names_the_directory_the_run_writes_to(
    pytester: pytest.Pytester, tmp_path: Path
) -> None:
    _project(pytester)
    (tmp_path / "option-value").mkdir()
    value = str(tmp_path / "option-value")
    assert _record(pytester, _ARTIFACTS_OPTION, value).rest == _record(pytester).rest
    assert list((tmp_path / "option-value").iterdir()), "the typed directory stayed empty"
