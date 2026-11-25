"""The published fixture family: twelve names, their scopes, one barrel, light imports.
Read from pytest's marker, the modules, a child's ``sys.modules`` and the package ``ast``."""

import ast
import importlib
import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Final, NamedTuple

import pytest

_PACKAGE: Final[str] = "foundry.fixtures"
_BARREL: Final[str] = "foundry.fixtures.plugin"
_BARREL_STEM: Final[str] = _BARREL.rpartition(".")[2]
_HEAVY: Final[tuple[str, ...]] = ("faker", "jinja2", "jsonschema", "rich", "sqlalchemy")
_PUBLISHED: Final[tuple[tuple[str, str, str], ...]] = (
    # name, defining module stem inside foundry.fixtures, scope -- the fixture table of AAP 0.4.3.
    ("foundry_config", "config", "session"),
    ("foundry_profile", "config", "session"),
    ("foundry_run_id", "artifacts", "session"),
    ("foundry_artifacts", "artifacts", "session"),
    ("foundry_http", "http", "function"),
    ("foundry_api", "http", "function"),
    ("foundry_data", "data", "function"),
    ("foundry_db", "db", "function"),
    ("foundry_factory", "factory", "function"),
    ("foundry_clock", "clock", "function"),
    ("foundry_log_capture", "logs", "function"),
    ("foundry_poll", "polling", "function"),
)
_HOMES: Final[dict[str, str]] = {name: stem for name, stem, _scope in _PUBLISHED}
_SCOPES: Final[dict[str, str]] = {name: scope for name, _stem, scope in _PUBLISHED}
_PROBE: Final[str] = (
    "import json, sys; import {module}; "
    "print(json.dumps(sorted(n for n in {forbidden!r} if n in sys.modules)))\n"
)


class _Declaration(NamedTuple):
    scope: str
    autouse: bool
    params: object
    docstring: str


def _package_dir() -> Path:
    location = getattr(importlib.import_module(_PACKAGE), "__file__", None)
    assert location is not None, f"{_PACKAGE} was imported without a source file"
    return Path(location).resolve().parent


def _declarations(stem: str) -> dict[str, _Declaration]:
    declared: dict[str, _Declaration] = {}
    for attribute, value in vars(importlib.import_module(f"{_PACKAGE}.{stem}")).items():
        marker = getattr(value, "_fixture_function_marker", None)
        if marker is None:
            continue
        name = str(getattr(marker, "name", None) or attribute)
        declared[name] = _Declaration(
            str(getattr(marker, "scope", "")),
            bool(getattr(marker, "autouse", False)),
            getattr(marker, "params", None),
            str(getattr(value, "__doc__", "") or ""),
        )
    return declared


def _definition_homes() -> dict[str, tuple[str, ...]]:
    homes: dict[str, list[str]] = {}
    for path in sorted(_package_dir().glob("*.py")):
        if path.stem in ("__init__", _BARREL_STEM):
            continue
        for name in _declarations(path.stem):
            homes.setdefault(name, []).append(path.stem)
    return {name: tuple(sorted(stems)) for name, stems in homes.items()}


def _registration_findings() -> list[str]:
    findings: list[str] = []
    for path in sorted(_package_dir().glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
            assigned = getattr(node, "targets", None) or [getattr(node, "target", None)]
            if any(isinstance(one, ast.Name) and one.id == "pytest_plugins" for one in assigned):
                findings.append(f"{path.name}:{node.lineno}: assigns pytest_plugins")
            hooks = isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            if hooks and node.name.startswith("pytest_"):
                findings.append(f"{path.name}:{node.lineno}: defines hook {node.name}")
    return sorted(findings)


def test_the_package_declares_exactly_the_twelve_published_fixtures() -> None:
    homes = _definition_homes()
    assert homes == {name: (stem,) for name, stem in _HOMES.items()}, f"declares {sorted(homes)}"


@pytest.mark.parametrize(("name", "stem", "scope"), _PUBLISHED)
def test_each_fixture_row_matches_the_published_table(name: str, stem: str, scope: str) -> None:
    declared = _declarations(stem)
    assert name in declared, f"{stem} declares no fixture named {name}"
    assert declared[name].scope == scope
    assert declared[name].autouse is False, f"{name} would run unasked"
    assert declared[name].params is None, f"{name} declares params"
    assert declared[name].docstring.strip(), f"{name} publishes no docstring"


def test_the_barrel_reexports_the_twelve_functions_and_holds_no_logic() -> None:
    barrel = importlib.import_module(_BARREL)
    assert sorted(one for one in vars(barrel) if one.startswith("foundry_")) == sorted(_SCOPES)
    assert all(
        getattr(barrel, name) is getattr(importlib.import_module(f"{_PACKAGE}.{stem}"), name)
        for name, stem in _HOMES.items()
    )
    source = (_package_dir() / f"{_BARREL_STEM}.py").read_text(encoding="utf-8")
    defined = sorted(
        node.name
        for node in ast.parse(source).body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
    )
    assert defined == [], f"{_BARREL} defines {defined} of its own"
    assert set(barrel.__all__) >= set(_SCOPES)


@pytest.mark.parametrize("module", ["foundry", _PACKAGE, _BARREL, "foundry.plugin"])
def test_importing_a_fixture_module_pulls_no_heavy_dependency(
    module: str, repo_root: Path, subprocess_env: Callable[..., dict[str, str]]
) -> None:
    inherited = os.environ.get("PYTHONPATH", "")
    env = subprocess_env(
        {"PYTHONPATH": os.pathsep.join(one for one in (str(repo_root / "src"), inherited) if one)}
    )
    completed = subprocess.run(
        [sys.executable, "-c", _PROBE.format(module=module, forbidden=_HEAVY)],
        cwd=str(repo_root),
        env=env,
        capture_output=True,
        text=True,
        timeout=30.0,
        check=False,
    )
    assert completed.returncode == 0, f"importing {module} failed:\n{completed.stderr}"
    loaded = json.loads(completed.stdout)
    assert isinstance(loaded, list), f"the probe printed something unexpected: {completed.stdout!r}"
    assert [str(name) for name in loaded] == []


def test_the_fixture_package_declares_no_plugin_list_and_no_hook() -> None:
    scanned = {one.name for one in _package_dir().glob("*.py")}
    expected = {f"{stem}.py" for stem in _HOMES.values()} | {"__init__.py", f"{_BARREL_STEM}.py"}
    assert expected <= scanned, f"the scan missed {sorted(expected - scanned)}"
    assert _registration_findings() == []
