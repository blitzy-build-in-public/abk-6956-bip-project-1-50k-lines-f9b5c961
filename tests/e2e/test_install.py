"""End-to-end proof that the distribution builds, installs offline and runs from a fresh venv.

AAP 0.14.1 criterion 1: ``uv build`` yields the wheel and the sdist, and the wheel installed with
``--no-index --find-links`` gives a console script printing ``foundry, version 0.1.0`` and exits 0.
This is the only module that installs anything (AAP 0.14.3); its archive listings are the proof."""

from __future__ import annotations

import email.parser
import json
import os
import re
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, NamedTuple

import pytest

DIST_NAME: Final[str] = "pytest-foundry"
IMPORT_NAME: Final[str] = "foundry"
EXPECTED_VERSION: Final[str] = "0.1.0"
WHEEL_NAME: Final[str] = "pytest_foundry-0.1.0-py3-none-any.whl"
SDIST_NAME: Final[str] = "pytest_foundry-0.1.0.tar.gz"
DIST_INFO_PREFIX: Final[str] = "pytest_foundry-0.1.0.dist-info/"
REQUIRED_WHEEL_ENTRIES: Final[tuple[str, ...]] = (
    "foundry/py.typed",
    "foundry/plugin.py",
    "foundry/cli/main.py",
    "foundry/reporting/templates/report.html.j2",
)
REQUIRED_SDIST_ENTRIES: Final[tuple[str, ...]] = ("src/foundry/py.typed", "src/foundry/plugin.py")
OFFLINE_FLAGS: Final[tuple[str, ...]] = ("--offline", "--no-index")
_WHEELHOUSE_VARIABLES: Final[tuple[str, ...]] = ("PYTEST_FOUNDRY_WHEELHOUSE", "FOUNDRY_WHEELHOUSE")
_SHARED_WHEELHOUSE: Final[Path] = Path("/opt/pytest-foundry-wheelhouse")
_CHILD_STRIPPED_PREFIXES: Final[tuple[str, ...]] = ("COV_CORE_", "COVERAGE_", "PYTEST_")
_EXTRA_MARKER: Final[re.Pattern[str]] = re.compile(r"\bextra\s*==")
_CACHE_HINT: Final[str] = (
    f"no usable offline wheelhouse at {_SHARED_WHEELHOUSE}: set {_WHEELHOUSE_VARIABLES[0]} to a "
    "directory of wheels. A missing cache fails, never skips: criterion 1 installs --no-index."
)
# Literal argument vectors, so tests/e2e/test_offline.py's AST scan finds the offline flags where
# they are run: uv build resolves hatchling from its own cache, without a socket.
_UV_BUILD: Final[tuple[str, ...]] = ("uv", "build")
_UV_INSTALL: Final[tuple[str, ...]] = ("uv", "pip", "install")
_VENV_CREATE: Final[tuple[str, ...]] = (sys.executable, "-m", "venv")
_PROBE: Final[str] = (
    "import importlib.metadata as m, json\n"
    'd = m.distribution("pytest-foundry")\n'
    "print(json.dumps({g: {e.name: e.value for e in d.entry_points if e.group == g} "
    'for g in ("pytest11", "console_scripts")}))\n'
)

_Built = tuple[Path, tuple]


@dataclass(frozen=True, slots=True)
class _Invocation:
    kind: str
    argv: tuple[str, ...]
    cwd: Path
    returncode: int
    stdout: str
    stderr: str

    def detail(self) -> str:
        return f"$ {' '.join(self.argv)} (cwd={self.cwd})\n{self.stdout}\n{self.stderr}"


class _Installation(NamedTuple):
    venv: Path
    workspace: Path
    invocations: tuple[_Invocation, ...]


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def _venv_bin(venv_dir: Path, name: str) -> Path:
    return venv_dir / "bin" / name


def _run(argv: Sequence[str], env: Mapping[str, str], kind: str, cwd: Path) -> _Invocation:
    options = {"cwd": str(cwd), "env": dict(env), "check": False, "text": True}
    try:
        done = subprocess.run(list(argv), capture_output=True, timeout=600.0, **options)
    except FileNotFoundError as exc:
        return _Invocation(kind, tuple(argv), cwd, -1, "", str(exc))
    return _Invocation(kind, tuple(argv), cwd, done.returncode, done.stdout, done.stderr)


def _installer_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if not k.startswith(_CHILD_STRIPPED_PREFIXES)}


def _wheelhouse() -> Path:
    override = next((os.environ[n] for n in _WHEELHOUSE_VARIABLES if os.environ.get(n)), "")
    found = Path(override) if override else _SHARED_WHEELHOUSE
    assert found.is_dir(), _CACHE_HINT
    return found


def _run_env(builder: Callable[..., dict[str, str]], venv_dir: Path) -> dict[str, str]:
    bin_dir = _venv_bin(venv_dir, "python").parent
    inherited = os.environ.get("PATH", "")
    return builder({"VIRTUAL_ENV": str(venv_dir), "PATH": f"{bin_dir}{os.pathsep}{inherited}"})


def _requirement_name(requirement: str) -> str:
    return _normalize(re.split(r"[<>=!~;\[( ]", requirement, maxsplit=1)[0])


def _installed_requirements(venv_dir: Path) -> dict[str, tuple[str, ...]]:
    found: dict[str, tuple[str, ...]] = {}
    for metadata in sorted(venv_dir.glob("lib/python*/site-packages/*.dist-info/METADATA")):
        with metadata.open("r", encoding="utf-8") as handle:
            message = email.parser.Parser().parse(handle)
        required = message.get_all("Requires-Dist") or ()
        found[_normalize(str(message["Name"]))] = tuple(
            _requirement_name(item) for item in required if not _EXTRA_MARKER.search(item)
        )
    return found


def _dependency_closure(direct: Sequence[str], meta: dict[str, tuple[str, ...]]) -> frozenset[str]:
    closure: set[str] = set()
    pending = list(direct)
    while pending:
        name = pending.pop()
        if name not in closure:
            closure.add(name)
            pending.extend(meta.get(name, ()))
    return frozenset(closure)


@pytest.fixture(scope="module")
def built_dist(repo_root: Path, tmp_path_factory: pytest.TempPathFactory) -> _Built:
    out = tmp_path_factory.mktemp("dist")
    build_argv = (*_UV_BUILD, "--out-dir", str(out), "--offline")
    invocation = _run(build_argv, _installer_env(), "build", repo_root)
    absent = [name for name in (WHEEL_NAME, SDIST_NAME) if not (out / name).is_file()]
    assert absent == [], f"the build wrote no {absent} into {out}:\n{invocation.detail()}"
    return out, (invocation,)


@pytest.fixture(scope="module")
def fresh_venv(
    built_dist: _Built,
    subprocess_env: Callable[..., dict[str, str]],
    tmp_path_factory: pytest.TempPathFactory,
) -> _Installation:
    directory, build_invocations = built_dist
    venv_dir = tmp_path_factory.mktemp("venv")
    workspace = tmp_path_factory.mktemp("workspace")
    find_links = ("--find-links", str(_wheelhouse()))
    env = _installer_env()
    invocations = [_run((*_VENV_CREATE, str(venv_dir)), env, "venv", workspace)]
    assert invocations[0].returncode == 0, invocations[0].detail()
    python, wheel = str(_venv_bin(venv_dir, "python")), str(directory / WHEEL_NAME)
    install_argv = (*_UV_INSTALL, "--python", python, *OFFLINE_FLAGS, *find_links, wheel)
    invocations.append(_run(install_argv, env, "install", workspace))
    assert _normalize(DIST_NAME) in _installed_requirements(venv_dir), (
        f"no {DIST_NAME} in the fresh environment at {venv_dir}: {_CACHE_HINT}"
    )
    return _Installation(venv_dir, workspace, (*build_invocations, *invocations))


def test_the_build_writes_both_artifacts_that_hold_only_first_party_content(
    built_dist: _Built, repo_root: Path
) -> None:
    dist = built_dist[0]
    produced = [p.name for p in dist.iterdir() if p.is_file() and p.name[0] != "."]
    assert sorted(produced) == sorted([WHEEL_NAME, SDIST_NAME]), f"the build produced {produced}"
    with zipfile.ZipFile(dist / WHEEL_NAME) as wheel:
        names = tuple(wheel.namelist())
    foreign = sorted(n for n in names if not n.startswith((f"{IMPORT_NAME}/", DIST_INFO_PREFIX)))
    assert foreign == [], f"the wheel carries {foreign[:10]} outside foundry/ and its metadata"
    missing = [entry for entry in REQUIRED_WHEEL_ENTRIES if entry not in names]
    assert missing == [], f"the wheel omits {missing}"
    for prefix in ("foundry/cli/templates/minimal/", "foundry/cli/templates/api/"):
        assert any(name.startswith(prefix) for name in names), f"nothing under {prefix}"
    with tarfile.open(dist / SDIST_NAME, "r:gz") as sdist:
        members = tuple(sdist.getnames())
    relative = {member.split("/", 1)[1] for member in members if "/" in member}
    manifest = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    selected = manifest["tool"]["hatch"]["build"]["targets"]["sdist"]["include"]
    allowed = {entry.split("/", 1)[0] for entry in selected} | {"PKG-INFO"}
    unexpected = sorted({path.split("/", 1)[0] for path in relative} - allowed)
    assert unexpected == [], f"the sdist carries {unexpected}, not what the manifest selects"
    missing = [entry for entry in REQUIRED_SDIST_ENTRIES if entry not in relative]
    assert missing == [], f"the sdist omits {missing}"


def test_both_entry_points_are_installed_and_the_console_script_reports_its_version(
    fresh_venv: _Installation, subprocess_env: Callable[..., dict[str, str]]
) -> None:
    venv, space = fresh_venv.venv, fresh_venv.workspace
    env = _run_env(subprocess_env, venv)
    banner = _run((str(_venv_bin(venv, "foundry")), "--version"), env, "probe", space)
    assert banner.returncode == 0, banner.detail()
    assert banner.stdout.strip() == f"foundry, version {EXPECTED_VERSION}", banner.stdout
    assert banner.stderr == "", f"the banner wrote to stderr: {banner.stderr!r}"
    probe = _run((str(_venv_bin(venv, "python")), "-c", _PROBE), env, "probe", space)
    assert probe.returncode == 0, probe.detail()
    document: Any = json.loads(probe.stdout)
    assert document["pytest11"].get(IMPORT_NAME) == "foundry.plugin", document["pytest11"]
    assert document["console_scripts"].get(IMPORT_NAME) == "foundry.cli.main:main", document


def test_the_install_used_offline_flags_and_the_closure_matches_the_manifest(
    fresh_venv: _Installation, repo_root: Path
) -> None:
    audited = [entry for entry in fresh_venv.invocations if entry.kind != "venv"]
    assert {entry.kind for entry in audited} == {"build", "install"}, audited
    # The build takes --offline alone: --no-index disables the index lookups uv build needs for
    # its backend. Both offline flags are the install's contract (AAP 0.14.1 criterion 1, I1).
    flags_by_kind = {"build": ("--offline",), "install": OFFLINE_FLAGS}
    for invocation in audited:
        required = flags_by_kind[invocation.kind]
        missing = [flag for flag in required if flag not in invocation.argv]
        assert missing == [], f"the {invocation.kind} ran without {missing}: {invocation.argv}"
    manifest = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    direct = tuple(_requirement_name(spec) for spec in manifest["project"]["dependencies"])
    installed = _installed_requirements(fresh_venv.venv)
    absent = sorted(set(direct) - set(installed))
    assert absent == [], f"the declared runtime dependencies {absent} are not installed"
    closure = _dependency_closure(direct, installed)
    baseline = frozenset({"pip", "setuptools", "wheel"}) | {_normalize(DIST_NAME)}
    undeclared = sorted(set(installed) - closure - baseline)
    assert undeclared == [], f"{undeclared} arrived outside the declared closure (AAP 0.5.2)"
