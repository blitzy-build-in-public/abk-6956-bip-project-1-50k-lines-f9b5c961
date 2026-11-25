"""End-to-end proof that the distribution builds, installs offline, and runs from a fresh venv.

AAP 0.14.1 criterion 1 is one observation: the build produces the wheel and the sdist, and the wheel
installed into a fresh virtual environment yields a ``foundry`` console script printing
``foundry, version 0.1.0`` and exiting 0. This is the only module in the project that installs
anything (AAP 0.14.3), and its archive listings are the no-vendored-code proof of AAP 0.5.2.

Both expensive steps are module-scoped fixtures, so a worker builds once and installs once, and a
missing wheelhouse fails the run rather than skipping it because criterion 1 depends on that cache.
The fresh environment is the one place ``run_foundry`` and ``run_pytest`` cannot reach, so children
are started with :mod:`subprocess` here and nowhere else (AAP 0.14.3, AAP 0.14.5).
"""

from __future__ import annotations

import email.parser
import hashlib
import json
import os
import re
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pytest

DIST_NAME: Final[str] = "pytest-foundry"
IMPORT_NAME: Final[str] = "foundry"
EXPECTED_VERSION: Final[str] = "0.1.0"
WHEEL_NAME: Final[str] = "pytest_foundry-0.1.0-py3-none-any.whl"
SDIST_NAME: Final[str] = "pytest_foundry-0.1.0.tar.gz"
DIST_INFO_PREFIX: Final[str] = "pytest_foundry-0.1.0.dist-info/"
"""Names and version fixed by AAP 0.4.1, with assumption A1's PEP 427 normalisation applied.

A1 keeps the distribution named ``pytest-foundry``, so both artifacts carry the normalised
``pytest_foundry`` prefix; the literal ``foundry-0.1.0-py3-none-any.whl`` that AAP 0.14.1 quotes is
the displaced alternative, and the artifact assertion below says so.
"""

REQUIRED_WHEEL_ENTRIES: Final[tuple[str, ...]] = (
    "foundry/__init__.py",
    "foundry/py.typed",
    "foundry/plugin.py",
    "foundry/cli/main.py",
    "foundry/reporting/templates/report.html.j2",
)
"""Modules and package data the installed distribution cannot work without (AAP 0.4.1)."""

REQUIRED_WHEEL_PREFIXES: Final[tuple[str, ...]] = (
    f"{IMPORT_NAME}/",
    "foundry/cli/templates/minimal/",
    "foundry/cli/templates/api/",
)
"""The package root plus the two ``foundry init`` template trees the wheel carries (AAP 0.4.1)."""

_WHEEL_ALLOWED_PREFIXES: Final[tuple[str, ...]] = (f"{IMPORT_NAME}/", DIST_INFO_PREFIX)
"""Every wheel entry starts with one of these: the import package or the metadata directory."""

_SDIST_NAMES: Final[str] = (
    "src tests examples scripts docs pyproject.toml uv.lock README.md CHANGELOG.md LICENSE "
    ".gitignore PKG-INFO"
)
"""The first-party trees and root metadata the manifest's sdist selection covers (AAP 0.5.2)."""

SDIST_ALLOWED_TOP_LEVEL: Final[frozenset[str]] = frozenset(
    _SDIST_NAMES.split()  # noqa: SIM905 - one line keeps the twelve names beside their contract
)
"""The top-level entries an sdist of this repository is allowed to hold."""

REQUIRED_SDIST_ENTRIES: Final[tuple[str, ...]] = (
    "src/foundry/py.typed",
    "src/foundry/plugin.py",
    "src/foundry/reporting/templates/report.html.j2",
)
"""Files the sdist must hold for a source build to reproduce the installed wheel."""

VENV_BASELINE_DISTRIBUTIONS: Final[frozenset[str]] = frozenset({"pip", "setuptools", "wheel"})
"""What ``python -m venv`` itself installs, which no manifest requirement accounts for."""

OFFLINE_FLAGS: Final[tuple[str, ...]] = ("--offline", "--no-index")
"""What a uv command carries: ``uv build`` takes only ``--offline``, ``uv pip install`` both."""

_NO_ISOLATION: Final[str] = "--no-isolation"
"""Required on the fallback build: an isolated one creates a venv and installs the backend."""

_WHEELHOUSE_VARIABLES: Final[tuple[str, ...]] = ("PYTEST_FOUNDRY_WHEELHOUSE", "FOUNDRY_WHEELHOUSE")
"""Where an operator points this test at a wheelhouse, in the order they are read.

The first name stays outside Foundry's own ``FOUNDRY_*`` namespace because that layer reads every
variable it carries as a configuration key: a session started with the second name set exits 2
naming ``wheelhouse`` as an unknown key before this module runs. It is read only where pytest runs
without the plugin loaded.
"""

_SHARED_WHEELHOUSE: Final[Path] = Path("/opt/pytest-foundry-wheelhouse")
"""The read-only wheelhouse this project's build environment publishes."""

_TIMEOUT_SECONDS: Final[float] = 600.0
"""A build, install or console-script run outlasting this has hung rather than worked."""

_EXTRA_MARKER: Final[re.Pattern[str]] = re.compile(r"\bextra\s*==")
"""A ``Requires-Dist`` carrying this names an optional extra, which a plain install omits."""

_CHILD_STRIPPED_PREFIXES: Final[tuple[str, ...]] = ("COV_CORE_", "COVERAGE_", "PYTEST_")
"""What a build or install child must not inherit: the outer run's pytest and coverage state."""

_CACHE_HINT: Final[str] = (
    f"no usable offline wheelhouse: set {_WHEELHOUSE_VARIABLES[0]} to a directory of wheels, or "
    f"populate the setup-time cache ({_SHARED_WHEELHOUSE} on this host, otherwise the directory "
    "`uv cache dir` prints). uv.lock is the resolution record it must satisfy, and criterion 1 "
    "installs with --no-index, so a missing cache is a failure, never a skip."
)
"""The diagnostic every missing-cache path prints: it names the cache, never skips."""

# Literal argument vectors, so `tests/e2e/test_offline.py`'s AST scan finds the offline flags where
# they are run. `uv build --offline` resolves hatchling from its own cache without a socket;
# `python -m build` is the fallback for a host without uv, needing `--no-isolation` to stay offline.
_UV_BUILD: Final[tuple[str, ...]] = ("uv", "build")
_MODULE_BUILD: Final[tuple[str, ...]] = (sys.executable, "-m", "build")
_UV_INSTALL: Final[tuple[str, ...]] = ("uv", "pip", "install")
_VENV_CREATE: Final[tuple[str, ...]] = (sys.executable, "-m", "venv")

_ENTRY_POINT_PROBE: Final[str] = """
import importlib.metadata
import json

import foundry

distribution = importlib.metadata.distribution("pytest-foundry")
groups = ("pytest11", "console_scripts")
found = {g: {e.name: e.value for e in distribution.entry_points if e.group == g} for g in groups}
found["version"] = foundry.__version__
print(json.dumps(found))
"""
"""The child program reading the installed distribution's version and both entry points."""


@dataclass(frozen=True, slots=True)
class _Invocation:
    """One recorded child-process invocation: its kind, argv, working directory and streams."""

    kind: str
    argv: tuple[str, ...]
    cwd: Path
    returncode: int
    stdout: str
    stderr: str

    @property
    def succeeded(self) -> bool:
        return self.returncode == 0

    def detail(self) -> str:
        """Return a copy-pasteable account: the command line, its status and both streams."""
        return (
            f"$ {' '.join(self.argv)}  (cwd={self.cwd})\nexited {self.returncode}\n"
            f"--- stdout ---\n{self.stdout}\n--- stderr ---\n{self.stderr}"
        )


@dataclass(frozen=True, slots=True)
class _Build:
    """The build output directory with every invocation that produced it."""

    directory: Path
    invocations: tuple[_Invocation, ...]


@dataclass(frozen=True, slots=True)
class _Installation:
    """A fresh virtual environment holding the installed distribution, plus what installed it.

    ``workspace`` is an empty directory the children run in, so no run discovers a ``foundry.toml``
    from the checkout, and ``invocations`` carries the build steps beside the install ones so a test
    can assert the offline flags of the whole chain.
    """

    venv: Path
    wheel: Path
    workspace: Path
    invocations: tuple[_Invocation, ...]


def _normalize(name: str) -> str:
    """Return ``name`` in PEP 503 normal form: lowercase, ``-``/``_``/``.`` runs collapsed."""
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def _venv_bin(venv_dir: Path, name: str) -> Path:
    """Return ``name`` from ``venv_dir``'s ``bin`` directory, the only layout AAP 0.4.1 supports."""
    return venv_dir / "bin" / name


def _run(argv: tuple[str, ...], *, cwd: Path, kind: str, env: Mapping[str, str]) -> _Invocation:
    """Run ``argv`` in a child process without a shell and record the outcome.

    This module starts its children with :mod:`subprocess` rather than through ``run_foundry`` and
    ``run_pytest`` because those fixtures are bound to the running suite's interpreter and console
    script, while criterion 1 is a claim about a *different* virtual environment (AAP 0.14.3 and
    0.14.5). A missing executable is recorded rather than raised, so the caller can fall back and
    report both attempts. Every caller states the child's whole environment, so no child inherits
    state by accident.
    """
    try:
        completed = subprocess.run(
            list(argv),
            cwd=str(cwd),
            env=dict(env),
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError as exc:
        return _Invocation(kind, argv, cwd, -1, "", str(exc))
    return _Invocation(kind, argv, cwd, completed.returncode, completed.stdout, completed.stderr)


def _installer_env() -> dict[str, str]:
    """Return the environment a build or install child runs with.

    This process's own environment minus the outer run's pytest and coverage state, so a child is
    started with no options of ours and writes no coverage data into the checkout. ``FOUNDRY_*``
    stays: one accepted wheelhouse override carries that prefix.
    """
    return {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(_CHILD_STRIPPED_PREFIXES)
    }


def _wheelhouse() -> Path:
    """Return the directory the installer reads wheels from, failing when none is usable.

    An explicit override wins outright: the operator named one location, and silently reading
    another would install from a cache they did not choose.
    """
    for name in _WHEELHOUSE_VARIABLES:
        override = os.environ.get(name)
        if override:
            path = Path(override)
            if not path.is_dir():
                pytest.fail(f"{name}={override} names no directory; {_CACHE_HINT}")
            return path
    for candidate in (_SHARED_WHEELHOUSE, Path.home() / ".cache" / "uv"):
        if candidate.is_dir():
            return candidate
    pytest.fail(_CACHE_HINT)


def _run_env(builder: Callable[..., dict[str, str]], venv_dir: Path) -> dict[str, str]:
    """Return a child environment for a program installed inside ``venv_dir``.

    ``subprocess_env`` strips every ``FOUNDRY_*`` variable so a child sees only its own
    configuration, and ``VIRTUAL_ENV``/``PATH`` are repointed at the fresh environment rather than
    the checkout's ``.venv``, which the console script is not running from.
    """
    bin_dir = _venv_bin(venv_dir, "python").parent
    inherited = os.environ.get("PATH", "")
    return builder({"VIRTUAL_ENV": str(venv_dir), "PATH": f"{bin_dir}{os.pathsep}{inherited}"})


def _requirement_name(requirement: str) -> str:
    """Return the normalised project name of a PEP 508 requirement or ``Requires-Dist`` value."""
    return _normalize(re.split(r"[<>=!~;\[( ]", requirement, maxsplit=1)[0])


def _installed_requirements(venv_dir: Path) -> dict[str, tuple[str, ...]]:
    """Map every installed distribution's normalised name to the names its metadata requires.

    Optional extras (``extra == ...``) are skipped because a plain install does not pull them.
    Environment markers on ``python_version``/``sys_platform`` are followed conservatively: the
    wheelhouse covers the supported platforms, so a conditional requirement that is not installed
    only widens the closure and can never hide a vendored distribution.
    """
    installed: dict[str, tuple[str, ...]] = {}
    for site_packages in sorted(venv_dir.glob("lib/python*/site-packages")):
        for metadata in sorted(site_packages.glob("*.dist-info/METADATA")):
            with metadata.open("r", encoding="utf-8") as handle:
                message = email.parser.Parser().parse(handle)
            installed[_normalize(str(message["Name"]))] = tuple(
                _requirement_name(requirement)
                for requirement in message.get_all("Requires-Dist") or ()
                if not _EXTRA_MARKER.search(requirement)
            )
    return installed


def _dependency_closure(
    direct: tuple[str, ...], installed: Mapping[str, tuple[str, ...]]
) -> frozenset[str]:
    """Return the transitive requirement closure of ``direct`` over the installed metadata."""
    closure: set[str] = set()
    pending = list(direct)
    while pending:
        name = pending.pop()
        if name not in closure:
            closure.add(name)
            pending.extend(installed.get(name, ()))
    return frozenset(closure)


def _tree_snapshot(root: Path) -> tuple[tuple[str, int, str], ...] | None:
    """Return a content snapshot of ``root``, or ``None`` when ``root`` does not exist.

    Every regular file under ``root`` is captured, nothing skipped: its POSIX relative path, its
    size in bytes and the SHA-256 hex digest of its content, in a tuple sorted by relative path so
    two snapshots compare deterministically. ``None`` records absence, which no directory state
    equals, so a build that creates or deletes ``root`` itself also changes the snapshot.
    """
    if not root.exists():
        return None
    entries: list[tuple[str, int, str]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if path.is_file():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            entries.append((path.relative_to(root).as_posix(), path.stat().st_size, digest))
    return tuple(entries)


def _snapshot_delta(
    before: tuple[tuple[str, int, str], ...] | None,
    after: tuple[tuple[str, int, str], ...] | None,
) -> str:
    """Return how ``after`` differs from ``before``, naming what appeared, vanished or changed."""
    if before is None:
        return "a directory this run created" if after is not None else "no difference"
    if after is None:
        return "a directory this run deleted"
    old = {path: (size, digest) for path, size, digest in before}
    new = {path: (size, digest) for path, size, digest in after}
    changes = (
        ("added", sorted(new.keys() - old.keys())),
        ("removed", sorted(old.keys() - new.keys())),
        ("rewritten", sorted(path for path in old.keys() & new.keys() if old[path] != new[path])),
    )
    return ", ".join(f"{label} {paths}" for label, paths in changes if paths) or "no difference"


@pytest.fixture(scope="module")
def built_dist(tmp_path_factory: pytest.TempPathFactory, repo_root: Path) -> _Build:
    """Module scope: build once into a test-owned directory and record every attempt.

    The in-tree ``dist/`` is snapshotted before the build and compared after it rather than
    required absent: a checkout may legitimately hold the output of an earlier documented build
    (AAP 0.4.1), and the snapshot is what proves this run added nothing to it, because every
    artifact the tests read comes from the test-owned directory the build is given below.
    """
    out = tmp_path_factory.mktemp("dist")
    in_tree = repo_root / "dist"
    before = _tree_snapshot(in_tree)
    env = _installer_env()
    build_argv = (*_UV_BUILD, "--out-dir", str(out), "--offline")
    fallback_argv = (*_MODULE_BUILD, _NO_ISOLATION, "--outdir", str(out))
    invocations = [_run(build_argv, cwd=repo_root, kind="build", env=env)]
    if not invocations[0].succeeded:
        invocations.append(_run(fallback_argv, cwd=repo_root, kind="build", env=env))
    absent = [name for name in (WHEEL_NAME, SDIST_NAME) if not (out / name).is_file()]
    if absent:
        pytest.fail(
            f"the build wrote no {absent} into {out}; {_CACHE_HINT}\n"
            + "\n".join(entry.detail() for entry in invocations)
        )
    after = _tree_snapshot(in_tree)
    assert after == before, (
        f"the build changed {in_tree}: every artifact belongs in {out}, the test-owned directory "
        "it was given, so this run must leave the checkout exactly as it found it. A checkout may "
        "legitimately hold the output of an earlier documented build (AAP 0.4.1); the snapshot "
        "taken before the build is what proves this run added nothing to it, and it now shows "
        f"{_snapshot_delta(before, after)}"
    )
    return _Build(directory=out, invocations=tuple(invocations))


@pytest.fixture(scope="module")
def fresh_venv(
    tmp_path_factory: pytest.TempPathFactory,
    built_dist: _Build,
    subprocess_env: Callable[..., dict[str, str]],
) -> _Installation:
    """Module scope: install the built wheel into a pristine virtual environment, offline.

    The installer keeps this process's own environment rather than ``subprocess_env``, which strips
    every ``FOUNDRY_*`` variable: one accepted wheelhouse override carries that prefix, so a child
    that could not see it would install from a cache the operator did not choose.
    """
    venv_dir = tmp_path_factory.mktemp("venv")
    workspace = tmp_path_factory.mktemp("workspace")
    wheel = built_dist.directory / WHEEL_NAME
    wheel_path = str(wheel)
    wheelhouse = _wheelhouse()
    find_links = ("--find-links", str(wheelhouse))
    env = _installer_env()
    invocations = [_run((*_VENV_CREATE, str(venv_dir)), cwd=workspace, kind="venv", env=env)]
    if not invocations[0].succeeded:
        pytest.fail(f"python -m venv failed in {workspace}:\n{invocations[0].detail()}")
    venv_python = str(_venv_bin(venv_dir, "python"))
    uv_argv = (*_UV_INSTALL, "--python", venv_python, *OFFLINE_FLAGS, *find_links, wheel_path)
    invocations.append(_run(uv_argv, cwd=workspace, kind="install", env=env))
    if not invocations[-1].succeeded:
        pip_argv = (venv_python, "-m", "pip", "install", "--no-index", *find_links, wheel_path)
        invocations.append(_run(pip_argv, cwd=workspace, kind="install", env=env))
    if _normalize(DIST_NAME) not in _installed_requirements(venv_dir):
        pytest.fail(
            f"the fresh environment at {venv_dir} holds no {DIST_NAME} distribution after an "
            f"install from {wheelhouse}; {_CACHE_HINT}\n"
            + "\n".join(entry.detail() for entry in invocations)
        )
    return _Installation(
        venv=venv_dir,
        wheel=wheel,
        workspace=workspace,
        invocations=(*built_dist.invocations, *invocations),
    )


def test_build_produces_exactly_the_two_documented_artifacts(built_dist: _Build) -> None:
    """Criterion 1: the build writes the wheel and the sdist, and nothing else."""
    produced = sorted(
        path.name
        for path in built_dist.directory.iterdir()
        if path.is_file() and not path.name.startswith(".")
    )
    assert set(produced) == {WHEEL_NAME, SDIST_NAME}, (
        f"the build produced {produced}; criterion 1 expects exactly {[WHEEL_NAME, SDIST_NAME]}. "
        f"Assumption A1 keeps the distribution named {DIST_NAME!r}, so PEP 427 normalises both "
        "names to underscore form and the literal 'foundry-0.1.0-py3-none-any.whl' that AAP 0.14.1 "
        "quotes is displaced. Hidden files are ignored: `uv build` writes a .gitignore marker into "
        "its output directory."
    )


def test_wheel_ships_only_the_foundry_package(built_dist: _Build) -> None:
    """AAP 0.5.2: the wheel's top level is the import package and its metadata, nothing else."""
    with zipfile.ZipFile(built_dist.directory / WHEEL_NAME) as archive:
        names = tuple(archive.namelist())
    foreign = sorted(name for name in names if not name.startswith(_WHEEL_ALLOWED_PREFIXES))
    assert not foreign, (
        f"the wheel carries {len(foreign)} entr(ies) outside {list(_WHEEL_ALLOWED_PREFIXES)}: "
        f"{foreign[:10]}. No third-party code is vendored (AAP 0.5.2), so `unzip -l` on the wheel "
        "shows the import package and the metadata directory only."
    )
    missing = [entry for entry in REQUIRED_WHEEL_ENTRIES if entry not in names]
    assert not missing, (
        f"the wheel omits {missing}: without them an installed distribution cannot activate, shell "
        "out or render a report (AAP 0.4.1)"
    )
    for prefix in REQUIRED_WHEEL_PREFIXES[1:]:
        assert any(name.startswith(prefix) for name in names), (
            f"the wheel ships nothing under {prefix}: `foundry init` writes those templates and "
            "they ship as package data (AAP 0.4.1)"
        )
    junk = sorted(name for name in names if "__pycache__" in name or name.endswith(".pyc"))
    assert not junk, f"the wheel ships bytecode {junk[:5]}: the build inputs hold no __pycache__"


def test_sdist_contains_only_first_party_trees(built_dist: _Build) -> None:
    """AAP 0.5.2: the sdist carries the first-party trees the manifest selects, nothing more."""
    with tarfile.open(built_dist.directory / SDIST_NAME, "r:gz") as archive:
        members = tuple(archive.getnames())
    roots = {member.split("/", 1)[0] for member in members}
    assert len(roots) == 1, f"the sdist has {len(roots)} root components {sorted(roots)}, not one"
    relative = {member.split("/", 1)[1] for member in members if "/" in member}
    unexpected = sorted({path.split("/", 1)[0] for path in relative} - SDIST_ALLOWED_TOP_LEVEL)
    assert not unexpected, (
        f"the sdist carries {unexpected}, which the manifest's selection "
        f"{sorted(SDIST_ALLOWED_TOP_LEVEL)} does not allow. A top-level segment no first-party "
        "tree accounts for is vendored third-party code, which AAP 0.5.2 excludes."
    )
    missing = [entry for entry in REQUIRED_SDIST_ENTRIES if entry not in relative]
    assert not missing, (
        f"the sdist omits {missing}: a source build would then reproduce a different wheel from "
        "the one this suite installs (AAP 0.4.1)"
    )


def test_installed_console_script_reports_the_documented_version(
    fresh_venv: _Installation, subprocess_env: Callable[..., dict[str, str]]
) -> None:
    """Criterion 1: the installed console script prints the documented banner and exits 0."""
    result = _run(
        (str(_venv_bin(fresh_venv.venv, "foundry")), "--version"),
        cwd=fresh_venv.workspace,
        kind="probe",
        env=_run_env(subprocess_env, fresh_venv.venv),
    )
    assert result.returncode == 0, result.detail()
    assert result.stdout.strip() == f"foundry, version {EXPECTED_VERSION}", (
        f"the banner reads {result.stdout.strip()!r}: AAP 0.4.1 fixes 'foundry, version "
        f"{EXPECTED_VERSION}' exactly"
    )
    assert result.stderr == "", f"the banner wrote to stderr: {result.stderr!r}"


def test_installed_package_exposes_both_entry_points(
    fresh_venv: _Installation, subprocess_env: Callable[..., dict[str, str]]
) -> None:
    """AAP 0.4.2: the installed distribution declares both entry points at their exact targets."""
    result = _run(
        (str(_venv_bin(fresh_venv.venv, "python")), "-c", _ENTRY_POINT_PROBE),
        cwd=fresh_venv.workspace,
        kind="probe",
        env=_run_env(subprocess_env, fresh_venv.venv),
    )
    assert result.returncode == 0, result.detail()
    try:
        document = json.loads(result.stdout)
    except json.JSONDecodeError:
        pytest.fail(f"the entry-point probe printed no JSON document:\n{result.detail()}")
    assert document["version"] == EXPECTED_VERSION, (
        f"the installed foundry.__version__ is {document['version']!r}, not {EXPECTED_VERSION!r}"
    )
    assert document["pytest11"].get(IMPORT_NAME) == "foundry.plugin", (
        f"the pytest11 entry point named {IMPORT_NAME!r} targets "
        f"{document['pytest11'].get(IMPORT_NAME)!r}, not 'foundry.plugin': activating with no -p "
        "flag and no configuration file is that target (AAP 0.4.2)"
    )
    assert document["console_scripts"].get(IMPORT_NAME) == "foundry.cli.main:main", (
        f"the console_scripts entry point named {IMPORT_NAME!r} targets "
        f"{document['console_scripts'].get(IMPORT_NAME)!r}, not 'foundry.cli.main:main' "
        "(AAP 0.4.2)"
    )


def test_install_used_offline_flags_only(fresh_venv: _Installation) -> None:
    """AAP 0.14.3: no build or install resolved anything through an index."""
    builds = [entry for entry in fresh_venv.invocations if entry.kind == "build"]
    installs = [entry for entry in fresh_venv.invocations if entry.kind == "install"]
    assert builds and installs, (
        "recorded no build and install pair, only "
        f"{[entry.kind for entry in fresh_venv.invocations]}"
    )
    for invocation in (*builds, *installs):
        offline = any(flag in invocation.argv for flag in OFFLINE_FLAGS)
        in_place = invocation.argv[1:3] == ("-m", "build") and _NO_ISOLATION in invocation.argv
        command = " ".join(invocation.argv)
        assert offline or in_place, (
            f"{command} can reach an index: `uv` takes --offline or --no-index, and "
            "`python -m build` takes --no-isolation so it installs no backend (AAP 0.14.3)"
        )


def test_installed_dependency_closure_matches_the_manifest(
    fresh_venv: _Installation, repo_root: Path
) -> None:
    """AAP 0.5.2: everything installed arrives through the declared dependency closure."""
    manifest = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    direct = tuple(_requirement_name(spec) for spec in manifest["project"]["dependencies"])
    assert direct, "pyproject.toml declares no runtime dependency"
    installed = _installed_requirements(fresh_venv.venv)
    absent = sorted(set(direct) - set(installed))
    assert not absent, (
        f"the declared runtime dependencies {absent} are not installed in {fresh_venv.venv}: an "
        "install that drops one leaves the suite exercising a different closure (AAP 0.5.1)"
    )
    closure = _dependency_closure(direct, installed)
    baseline = VENV_BASELINE_DISTRIBUTIONS | {_normalize(DIST_NAME)}
    undeclared = sorted(set(installed) - closure - baseline)
    assert not undeclared, (
        f"the fresh environment holds {len(undeclared)} distribution(s) outside the declared "
        f"closure: {undeclared}. A distribution no declared requirement reaches is a vendoring "
        "defect (AAP 0.5.2): the wheel ships only foundry/, so every other installed project "
        "arrives through pyproject.toml."
    )
