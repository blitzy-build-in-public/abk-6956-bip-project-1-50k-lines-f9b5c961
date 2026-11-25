"""Unit proof of the published fixture family: twelve names, four scopes, one barrel, light imports.

AAP 0.4.3 publishes twelve fixtures with a scope and a return type, and AAP 0.4.9 makes their names
and scopes a compatibility promise. This module proves that declared surface and no behaviour: every
fact -- name, scope, ``autouse`` flag, parameters, barrel membership, import cost -- is read from
the artifact (pytest's own fixture marker, the module object, a child's ``sys.modules``, the ``ast``
of the package source) rather than restated from it, and no fixture is ever called.
"""

from __future__ import annotations

import ast
import importlib
import json
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pytest

_FIXTURE_PACKAGE: Final[str] = "foundry.fixtures"
"""The package that holds the family: the module a suite's fixtures are registered from."""

_BARREL: Final[str] = "foundry.fixtures.plugin"
"""The single object ``foundry.plugin`` hands to ``pluginmanager.register`` (AAP 0.4.2)."""

_BARREL_STEM: Final[str] = "plugin"
"""The barrel's module stem, excluded wherever the *defining* modules are counted."""

_HEAVY_MODULES: Final[tuple[str, ...]] = (
    "faker",
    "jinja2",
    "jsonschema",
    "rich",
    "sqlalchemy",
)
"""The five modules AAP 0.4.8 forbids at module scope on the ``foundry.plugin`` import path (I5)."""

_IMPORT_PROBE: Final[str] = (
    "import json\n"
    "import sys\n"
    "\n"
    "import {module}\n"
    "\n"
    "FORBIDDEN = {forbidden!r}\n"
    "print(json.dumps(sorted(name for name in FORBIDDEN if name in sys.modules)))\n"
)
"""The probe a fresh interpreter runs: import one module, report which heavy modules it loaded.

It prints a JSON list and nothing else, so a module name in an unrelated warning is not a hit.
"""

_CHILD_TIMEOUT_SECONDS: Final[float] = 30.0
"""A ceiling on the probe's child interpreter, so a hung import fails here instead of stalling.

The probe is the one child in this module that does not go through the harness, so nothing else
bounds it: an interpreter that stalls while importing ``foundry.plugin`` would hold the suite open
until the outer gate killed it. Thirty seconds is five times the ~0.5 s a boot costs on an idle
host, and the same ceiling ``tests/integration/test_startup_cost.py`` puts on its own children.
"""

_PUBLISHED: Final[tuple[tuple[str, str, str], ...]] = (
    # name, module stem inside foundry.fixtures, scope -- the fixture table of AAP 0.4.3.
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
"""The published family: twelve names, the module each is defined in, and each one's scope."""

_EXPECTED_MODULES: Final[dict[str, str]] = {name: stem for name, stem, _scope in _PUBLISHED}
"""Fixture name to the stem of the one module that defines it."""

_EXPECTED_SCOPES: Final[dict[str, str]] = {name: scope for name, _stem, scope in _PUBLISHED}
"""Fixture name to the scope AAP 0.4.3 publishes, which is what a suite caches by."""

_SESSION_SCOPED: Final[tuple[str, ...]] = (
    "foundry_artifacts",
    "foundry_config",
    "foundry_profile",
    "foundry_run_id",
)
"""The four session-scoped fixtures, sorted: one resolution, one identity and one tree per run."""


@dataclass(frozen=True, slots=True)
class _Declaration:
    """One fixture as pytest itself records it, read off the marker the decorator attached."""

    name: str
    module: str
    scope: str
    autouse: bool
    params: object
    docstring: str


def _package_dir() -> Path:
    """Return the directory the ``foundry.fixtures`` package is imported from, resolved."""
    package = importlib.import_module(_FIXTURE_PACKAGE)
    location = getattr(package, "__file__", None)
    assert location is not None, f"{_FIXTURE_PACKAGE} was imported without a source file"
    return Path(location).resolve().parent


def _module_stems() -> tuple[str, ...]:
    """Return the stems of the package's modules, ``__init__`` excluded, sorted.

    One stem per module file on disk, so a module added without a published fixture is scanned.
    """
    stems = (path.stem for path in _package_dir().glob("*.py") if path.stem != "__init__")
    return tuple(sorted(stems))


def _declarations(stem: str) -> dict[str, _Declaration]:
    """Return every fixture one module of the package declares, keyed by its fixture name.

    The name, scope, ``autouse`` and ``params`` are read from the marker pytest attached to the
    function, so a renamed or rescoped fixture fails here rather than in a consumer.
    """
    module = importlib.import_module(f"{_FIXTURE_PACKAGE}.{stem}")
    declared: dict[str, _Declaration] = {}
    for attribute, value in vars(module).items():
        marker = getattr(value, "_fixture_function_marker", None)
        if marker is None:
            continue
        name = str(getattr(marker, "name", None) or attribute)
        declared[name] = _Declaration(
            name=name,
            module=stem,
            scope=str(getattr(marker, "scope", "")),
            autouse=bool(getattr(marker, "autouse", False)),
            params=getattr(marker, "params", None),
            docstring=str(getattr(value, "__doc__", "") or ""),
        )
    return declared


def _definition_homes() -> dict[str, tuple[str, ...]]:
    """Return each declared fixture with the non-barrel modules that declare it, sorted.

    The barrel re-exports the family and so carries the same markers; excluding it leaves the
    modules that own the behaviour.
    """
    homes: dict[str, list[str]] = {}
    for stem in _module_stems():
        if stem == _BARREL_STEM:
            continue
        for name in _declarations(stem):
            homes.setdefault(name, []).append(stem)
    return {name: tuple(sorted(modules)) for name, modules in homes.items()}


def _declaration_of(name: str) -> _Declaration:
    """Return one published fixture as its own defining module declares it."""
    expected = _EXPECTED_MODULES[name]
    declared = _declarations(expected)
    assert name in declared, f"{expected} declares no fixture named {name}"
    return declared[name]


def _registration_findings() -> list[str]:
    """Return every plugin registration and hook definition found in the fixture package's source.

    AAP 0.4.8 keeps registration in :mod:`foundry.plugin` alone: a ``pytest_plugins`` assignment or
    a ``pytest_*`` function here would be a second, unreviewed registration path. The scan parses
    the source, so a mention in prose is not a finding while an assignment or a definition is one.
    """
    findings: list[str] = []
    for path in sorted(_package_dir().glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            targets: list[ast.expr] = []
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id == "pytest_plugins":
                    findings.append(f"{path.name}:{node.lineno}: assigns pytest_plugins")
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith(
                "pytest_"
            ):
                findings.append(f"{path.name}:{node.lineno}: defines hook {node.name}")
    return sorted(findings)


def _heavy_modules_after_import(
    module: str,
    repo_root: Path,
    build_env: Callable[..., dict[str, str]],
) -> list[str]:
    """Import ``module`` in a fresh interpreter and return the heavy modules it left loaded.

    This process has already imported pytest and its siblings, so its ``sys.modules`` says nothing
    about import cost; the child runs in the checkout with ``src`` first on ``PYTHONPATH``, so it
    exercises the source tree. A child that fails to import, or that hangs past
    :data:`_CHILD_TIMEOUT_SECONDS`, is an assertion failure, never a skip.
    """
    source_root = repo_root / "src"
    inherited = os.environ.get("PYTHONPATH", "")
    env = build_env(
        {"PYTHONPATH": os.pathsep.join(part for part in (str(source_root), inherited) if part)}
    )
    try:
        completed = subprocess.run(
            [sys.executable, "-c", _IMPORT_PROBE.format(module=module, forbidden=_HEAVY_MODULES)],
            cwd=str(repo_root),
            env=env,
            capture_output=True,
            text=True,
            timeout=_CHILD_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(
            f"importing {module} in a fresh interpreter hung for longer than "
            f"{_CHILD_TIMEOUT_SECONDS:g} s, so the probe never reported which heavy modules "
            "the import loaded"
        )
    assert completed.returncode == 0, (
        f"importing {module} failed in a fresh interpreter:\n{completed.stderr}"
    )
    loaded = json.loads(completed.stdout)
    assert isinstance(loaded, list), f"the probe printed something unexpected: {completed.stdout!r}"
    return [str(name) for name in loaded]


# --- The published set (AAP 0.4.3, AAP 0.4.9) -----------------------------


def test_the_package_declares_exactly_the_twelve_published_fixtures() -> None:
    """The package declares the twelve published names and nothing else, each in one module."""
    homes = _definition_homes()
    assert sorted(homes) == sorted(_EXPECTED_SCOPES), (
        "the fixture package declares "
        f"{sorted(homes)}; AAP 0.4.3 publishes {sorted(_EXPECTED_SCOPES)}"
    )
    shared = {name: list(modules) for name, modules in homes.items() if len(modules) != 1}
    assert shared == {}, f"a fixture must be defined in exactly one module, not {shared}"


def test_each_fixture_is_defined_in_the_module_the_plan_places_it_in() -> None:
    """The name-to-module map equals AAP 0.10.1's, so a fixture moved between modules fails."""
    homes = {name: modules[0] for name, modules in _definition_homes().items()}
    assert homes == _EXPECTED_MODULES


def test_the_published_scopes_are_four_session_and_eight_function() -> None:
    """Every declared scope equals the published one, and the split is four session to eight."""
    scopes = {name: _declaration_of(name).scope for name in _EXPECTED_SCOPES}
    assert scopes == _EXPECTED_SCOPES
    session_scoped = sorted(name for name, scope in scopes.items() if scope == "session")
    assert session_scoped == sorted(_SESSION_SCOPED)
    assert len(session_scoped) == 4
    assert len(scopes) - len(session_scoped) == 8


# --- The barrel (AAP 0.4.2) ----------------------------------------------


def test_the_barrel_reexports_exactly_the_twelve_fixtures() -> None:
    """``foundry.fixtures.plugin`` publishes the twelve names and no other ``foundry_`` one."""
    barrel = importlib.import_module(_BARREL)
    reexported = sorted(name for name in vars(barrel) if name.startswith("foundry_"))
    assert reexported == sorted(_EXPECTED_SCOPES)


@pytest.mark.parametrize("name", sorted(_EXPECTED_SCOPES))
def test_each_barrel_reexport_is_the_function_its_own_module_defines(name: str) -> None:
    """The barrel binds the function object itself, not a copy or a wrapper around it.

    A downstream ``conftest.py`` overriding a fixture by name keeps working only while the name
    refers to the one function its own module defines.
    """
    barrel = importlib.import_module(_BARREL)
    home = importlib.import_module(f"{_FIXTURE_PACKAGE}.{_EXPECTED_MODULES[name]}")
    assert getattr(barrel, name) is getattr(home, name)


def test_the_barrel_holds_no_logic_of_its_own() -> None:
    """The barrel is imports, names and a docstring: it defines nothing a fixture could hide behind.

    A function or class defined inside it would be a fixture nobody published, and its ``__all__``
    has to cover the twelve for ``import *`` to be complete.
    """
    barrel = importlib.import_module(_BARREL)
    source = (_package_dir() / f"{_BARREL_STEM}.py").read_text(encoding="utf-8")
    defined = sorted(
        node.name
        for node in ast.parse(source).body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
    )
    assert defined == [], f"{_BARREL} defines {defined} of its own"
    assert set(barrel.__all__) >= set(_EXPECTED_SCOPES)


# --- Documentation (AAP 0.4.3, AAP 0.13.2) --------------------------------


@pytest.mark.parametrize("name", sorted(_EXPECTED_SCOPES))
def test_every_published_fixture_carries_a_docstring(name: str) -> None:
    """Each fixture function documents itself: the docstring is its whole published manual."""
    docstring = _declaration_of(name).docstring.strip()
    assert docstring, f"{name} publishes no docstring"
    assert docstring.splitlines()[0].strip(), f"{name} has a docstring with no summary line"


# --- Import cost (implicit requirement I5, AAP 0.4.8) ---------------------


@pytest.mark.parametrize("module", ["foundry", _FIXTURE_PACKAGE, _BARREL, "foundry.plugin"])
def test_importing_a_fixture_module_pulls_no_heavy_dependency(
    module: str,
    repo_root: Path,
    subprocess_env: Callable[..., dict[str, str]],
) -> None:
    """A fresh interpreter that imports one module holds none of the five heavy third-party ones.

    ``foundry`` proves the package surface stays light, the package and barrel prove the family is
    importable at ``pytest_configure`` cost, and ``foundry.plugin`` is what the 150 ms budget
    measures.
    """
    assert _heavy_modules_after_import(module, repo_root, subprocess_env) == []


# --- Registration and lifecycle discipline (AAP 0.4.8) --------------------


def test_the_fixture_package_declares_no_plugin_list_and_no_hook() -> None:
    """Registration and every hook belong to ``foundry.plugin``; the package does neither."""
    scanned = {path.name for path in _package_dir().glob("*.py")}
    expected = {f"{stem}.py" for stem in _EXPECTED_MODULES.values()} | {
        "__init__.py",
        f"{_BARREL_STEM}.py",
    }
    assert expected <= scanned, f"the scan missed {sorted(expected - scanned)}"
    assert _registration_findings() == []


def test_no_fixture_is_autouse_and_none_is_parametrised() -> None:
    """The set is the twelve published fixtures: none runs unasked, none multiplies into params."""
    declarations = {name: _declaration_of(name) for name in _EXPECTED_SCOPES}
    unasked = sorted(name for name, declared in declarations.items() if declared.autouse)
    assert unasked == [], f"these fixtures would run unasked: {unasked}"
    multiplied = {
        name: declared.params
        for name, declared in declarations.items()
        if declared.params is not None
    }
    assert multiplied == {}, f"these fixtures declare params: {multiplied}"
