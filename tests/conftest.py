"""Shared fixtures and process-wide guards for Foundry's own test suite.
Holds the top-level ``pytest_plugins`` declaration, the session environment pinning (``TZ``,
``LC_ALL``, ``LANG``, ``PYTEST_ADDOPTS``, ``locale``, ``PYTEST_DEBUG_TEMPROOT``/``TMPDIR``), the
shared fixtures, and the loopback-only socket guard of AAP 0.14.3, which refuses every non-loopback
target before the syscall."""

import importlib.metadata
import ipaddress
import locale
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Never

import pytest

# Declared here and only here: pytest aborts a run whose nested conftest repeats this declaration.
pytest_plugins = ["pytester"]

# The outer invocation has parsed PYTEST_ADDOPTS already; a pytester or child run would reapply it.
os.environ.pop("PYTEST_ADDOPTS", None)

# Pinned before any test module is imported; time.tzset re-reads TZ for the C library.
os.environ["TZ"] = "UTC"
os.environ["LC_ALL"] = "C"
os.environ["LANG"] = "C"
if hasattr(time, "tzset"):
    time.tzset()

# os.environ re-runs nothing here, so the starting locale must also be replaced; a pin that cannot
# be applied is fatal rather than skipped, because an unpinned locale is the dependence ruled out.
try:
    locale.setlocale(locale.LC_ALL, "C")
except locale.Error as exc:  # pragma: no cover - the C locale exists on every supported host
    raise RuntimeError(f"foundry test suite: could not pin the process locale: {exc}") from exc

# The session runs under a private temporary root: the pytest 8.4.2 that AAP 0.5.1 pins builds the
# predictable <tempdir>/pytest-of-<user> of GHSA-6w46-j5rx-g56g (docs/verification.md).
_PRIVATE_TEMPROOT: Final[pathlib.Path | None] = (
    None
    if os.environ.get("PYTEST_DEBUG_TEMPROOT")
    else pathlib.Path(tempfile.mkdtemp(prefix="foundry-pytest-"))
)
"""The private root this process created, or ``None`` when a caller pinned one of its own."""
if _PRIVATE_TEMPROOT is not None:
    os.environ["PYTEST_DEBUG_TEMPROOT"] = str(_PRIVATE_TEMPROOT)
    os.environ.setdefault("TMPDIR", str(_PRIVATE_TEMPROOT))


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    """Point the session's basetemp inside the private root whenever the caller named none.
    A given basetemp is used directly, so ``_pytest.tmpdir`` never enters the ``pytest-of-<user>``
    branch this mitigation avoids; the hook runs first because that plugin reads the option in its
    own configure hook."""
    if _PRIVATE_TEMPROOT is not None and config.option.basetemp is None:
        config.option.basetemp = str(_PRIVATE_TEMPROOT / "basetemp")


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Remove the private root after a clean run, and keep it after a failing one to inspect.
    A failing run's tree is evidence, so it survives and the controlling process names it; a worker
    stays quiet, because the controller's line is the one a reader can act on."""
    if _PRIVATE_TEMPROOT is None:
        return
    if exitstatus == 0:
        shutil.rmtree(_PRIVATE_TEMPROOT, ignore_errors=True)
        return
    if not os.environ.get("PYTEST_XDIST_WORKER"):
        print(f"foundry test suite: kept the temporary root {_PRIVATE_TEMPROOT} for inspection")


_INET_FAMILIES: Final[frozenset[int]] = frozenset({socket.AF_INET, socket.AF_INET6})
"""The families the guard polices; every other family is local by construction."""

_LOCAL_HOSTNAMES: Final[frozenset[str]] = frozenset(
    {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}
)
"""Names allowed without resolving them, because every address they carry is loopback."""

_GUARD_MARK: Final[str] = "_foundry_suite_socket_guard"
"""Attribute holding the live state on each wrapper, which is what makes installation idempotent."""

_OFFLINE_RULE: Final[str] = "AAP 0.14.3"


@dataclass
class SocketGuardState:
    """Live counters of this suite's loopback-only guard.
    Process-global, so one ``-n auto`` worker owns them and the offline proof measures a delta.
    ``foundry.diagnostics.socket_guard`` recognises a guard by ``FOUNDRY_SOCKET_GUARD`` and
    ``FOUNDRY_NON_LOOPBACK_ATTEMPTS``, which :meth:`__getattr__` serves live."""

    installed: bool = True
    loopback_attempts: int = 0
    non_loopback_attempts: int = 0
    violations: list[str] = field(default_factory=list)

    def __getattr__(self, name: str) -> Any:
        if name == "FOUNDRY_SOCKET_GUARD":
            return True
        if name == "FOUNDRY_NON_LOOPBACK_ATTEMPTS":
            return object.__getattribute__(self, "non_loopback_attempts")
        raise AttributeError(name)


# Saved before any patching, so the wrappers can connect, resolve and send without recursing into
# themselves. A second import of this file in one process adopts the first import's guard.
_ORIGINAL_GETADDRINFO = socket.getaddrinfo
_ORIGINAL_CONNECT = socket.socket.connect
_ORIGINAL_CONNECT_EX = socket.socket.connect_ex
_ORIGINAL_SENDTO = socket.socket.sendto
_ORIGINAL_SENDMSG = socket.socket.sendmsg


def _host_text(host: object) -> str | None:
    """Return ``host`` as text when it is a string or a byte string, else ``None``."""
    if isinstance(host, str):
        return host
    if isinstance(host, bytes):
        return host.decode("ascii", "ignore") or None
    return None


def _is_local_host(host: str) -> bool:
    """Return True when ``host`` is a loopback literal (or IPv4-mapped loopback) or a local name."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host.lower() in _LOCAL_HOSTNAMES
    mapped = getattr(address, "ipv4_mapped", None)
    return address.is_loopback or bool(mapped is not None and mapped.is_loopback)


def _refused_target(family: int, address: object) -> tuple[str, object] | None:
    """Return the ``(host, port)`` to refuse, or ``None`` when the target may proceed.
    Only ``AF_INET``/``AF_INET6`` are policed, and a name is refused *before* the resolver runs,
    because resolving it would open the very socket being refused."""
    if family not in _INET_FAMILIES:
        return None
    pair = address if isinstance(address, tuple) and len(address) >= 2 else ()
    host = _host_text(pair[0]) if pair else _host_text(address)
    if host is None or _is_local_host(host):
        return None
    return host, pair[1] if pair else 0


def _refuse(host: str, port: object) -> Never:
    """Record one refused attempt on the guard and raise the error that names it."""
    SOCKET_GUARD.non_loopback_attempts += 1
    SOCKET_GUARD.violations.append(f"{host}:{port}")
    raise RuntimeError(
        f"foundry test suite: refused a non-loopback target {host}:{port}. "
        f"The suite opens no non-loopback socket ({_OFFLINE_RULE}): serve HTTP from an in-process "
        "http.server bound to 127.0.0.1 on an ephemeral port, or replay a cassette, instead."
    )


def _guarded_getaddrinfo(
    host: object, port: object, family: int = 0, type: int = 0, proto: int = 0, flags: int = 0
) -> Any:
    """Resolve a local name only; refuse any other name before the resolver runs."""
    name = _host_text(host)
    policed = family == 0 or family in _INET_FAMILIES
    if name is not None and policed and not _is_local_host(name):
        _refuse(name, port)
    return _ORIGINAL_GETADDRINFO(host, port, family, type, proto, flags)


def _admit(self: socket.socket, address: object) -> None:
    """Refuse a non-loopback target, or count the loopback attempt that is about to proceed."""
    refused = _refused_target(self.family, address)
    if refused is not None:
        _refuse(*refused)
    if self.family in _INET_FAMILIES:
        SOCKET_GUARD.loopback_attempts += 1


def _guarded_connect(self: socket.socket, address: object) -> None:
    """Connect to a loopback target only, counting the permitted attempt and refusing the rest."""
    _admit(self, address)
    _ORIGINAL_CONNECT(self, address)


def _guarded_connect_ex(self: socket.socket, address: object) -> int:
    """Behave as :meth:`socket.socket.connect_ex` for a local target, and refuse any other."""
    _admit(self, address)
    return int(_ORIGINAL_CONNECT_EX(self, address))


def _guarded_sendto(self: socket.socket, *args: object) -> int:
    """Send a datagram to a loopback target only, refusing the rest before the syscall."""
    _admit(self, args[-1] if args else None)
    return int(_ORIGINAL_SENDTO(self, *args))


def _guarded_sendmsg(self: socket.socket, *args: object) -> int:
    """Send one message to a loopback target only; a connected socket names no target."""
    _admit(self, args[-1] if args else None)
    return int(_ORIGINAL_SENDMSG(self, *args))


def _adopt_or_create_guard() -> SocketGuardState:
    """Return the state an earlier import of this file published, or a fresh one."""
    published: SocketGuardState | None = getattr(socket.socket.connect, _GUARD_MARK, None)
    return published if published is not None else SocketGuardState()


SOCKET_GUARD: SocketGuardState = _adopt_or_create_guard()
"""The one guard of this process: its counters and the protocol ``foundry doctor`` reads."""

FOUNDRY_SOCKET_GUARD: SocketGuardState = SOCKET_GUARD
"""Announces this module as the guard holder to ``foundry.diagnostics.socket_guard``."""


def _install_guard() -> None:
    """Patch :mod:`socket` once per process; a second import adopts :data:`_GUARD_MARK`'s state."""
    if getattr(socket.socket.connect, _GUARD_MARK, None) is SOCKET_GUARD:
        return
    patches = (
        (socket, "getaddrinfo", _guarded_getaddrinfo),
        (socket.socket, "connect", _guarded_connect),
        (socket.socket, "connect_ex", _guarded_connect_ex),
        (socket.socket, "sendto", _guarded_sendto),
        (socket.socket, "sendmsg", _guarded_sendmsg),
    )
    for owner, name, wrapper in patches:
        setattr(wrapper, _GUARD_MARK, SOCKET_GUARD)
        setattr(owner, name, wrapper)


_install_guard()


@pytest.fixture(scope="session")
def socket_guard() -> SocketGuardState:
    """Session scope: the live guard installed at import, with its counters and protocol names."""
    return SOCKET_GUARD


@pytest.fixture(scope="session")
def repo_root() -> pathlib.Path:
    """Session scope: the repository root, checked for the markers its consumers need."""
    root = pathlib.Path(__file__).resolve().parents[1]
    missing = [name for name in ("pyproject.toml", "src/foundry") if not (root / name).exists()]
    if missing:
        pytest.fail(f"repository root {root} is missing {', '.join(missing)}")
    return root


@pytest.fixture(scope="session")
def foundry_python() -> str:
    """Session scope: the interpreter under test, ``sys.executable``."""
    return sys.executable


@pytest.fixture(scope="session")
def foundry_script() -> pathlib.Path:
    """Session scope: the installed ``foundry`` console script, failing with the install command."""
    on_path = shutil.which("foundry")
    script = pathlib.Path(on_path) if on_path else pathlib.Path(sys.executable).parent / "foundry"
    if not script.exists():
        pytest.fail(
            f"the foundry console script is missing: looked for {script}. Install the distribution "
            "into this environment with `uv sync --extra dev`, or with "
            "`uv pip install dist/pytest_foundry-0.1.0-py3-none-any.whl`, and rerun."
        )
    return script


_STRIPPED_VARIABLES: Final[str] = (
    "PYTEST_ADDOPTS PYTEST_XDIST_WORKER PYTEST_XDIST_WORKER_COUNT PYTEST_CURRENT_TEST "
    "PYTEST_PLUGINS"
)
"""Outer-run variables a child process must not inherit."""

_STRIPPED_PREFIXES: Final[tuple[str, ...]] = ("FOUNDRY_", "COV_CORE_", "COVERAGE_")
"""Prefixes removed from every child environment: the parent's configuration and coverage state."""


@pytest.fixture(scope="session")
def subprocess_env() -> Callable[..., dict[str, str]]:
    """Session scope: a child-environment builder dropping this run's pytest, xdist, Foundry and
    coverage state while keeping ``PATH`` and ``PYTHONPATH``."""

    def build(extra: Mapping[str, str] | None = None) -> dict[str, str]:
        env = dict(os.environ)
        for name in _STRIPPED_VARIABLES.split():
            env.pop(name, None)
        for name in [key for key in env if key.startswith(_STRIPPED_PREFIXES)]:
            del env[name]
        env.update(
            TZ="UTC", LC_ALL="C", LANG="C", PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1"
        )
        if extra:
            env.update(extra)
        return env

    return build


def _capture(
    command: list[str], *, cwd: pathlib.Path, env: Mapping[str, str], timeout: float
) -> subprocess.CompletedProcess[str]:
    """Run ``command`` in a child process with its output captured and its status unraised."""
    return subprocess.run(
        command,
        cwd=str(cwd),
        env=dict(env),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


@pytest.fixture(scope="session")
def run_pytest(
    subprocess_env: Callable[..., dict[str, str]],
) -> Callable[..., subprocess.CompletedProcess[str]]:
    """Session scope: run ``python -m pytest`` in a child built from ``subprocess_env``'s output."""

    def run(
        *args: str, cwd: pathlib.Path, env: Mapping[str, str] | None = None, timeout: float = 300.0
    ) -> subprocess.CompletedProcess[str]:
        return _capture(
            [sys.executable, "-m", "pytest", *args],
            cwd=cwd,
            env=subprocess_env(env),
            timeout=timeout,
        )

    return run


@pytest.fixture(scope="session")
def run_foundry(
    foundry_script: pathlib.Path, subprocess_env: Callable[..., dict[str, str]]
) -> Callable[..., subprocess.CompletedProcess[str]]:
    """Session scope: run the installed console script, the real entry point of AAP 0.14.5."""

    def run(
        *args: str, cwd: pathlib.Path, env: Mapping[str, str] | None = None, timeout: float = 300.0
    ) -> subprocess.CompletedProcess[str]:
        return _capture(
            [str(foundry_script), *args], cwd=cwd, env=subprocess_env(env), timeout=timeout
        )

    return run


@pytest.fixture(scope="session")
def example_project(repo_root: pathlib.Path) -> pathlib.Path:
    """Session scope: ``examples/checkout``, the shipped 24-test project, which must exist."""
    project = repo_root / "examples" / "checkout"
    if not project.is_dir():
        pytest.fail(f"the example project is missing: {project} is not a directory")
    return project


@pytest.fixture
def write_project(tmp_path: pathlib.Path) -> Callable[..., pathlib.Path]:
    """Function scope: write a throwaway project into a test-owned directory and return it.
    Every file is UTF-8 with LF newlines and a trailing newline, ``pyproject.toml`` is written only
    when a caller passes it (so a zero-configuration directory stays configuration free), and
    containment is enforced so ``examples/checkout`` can never be mutated."""
    base = tmp_path.parent.resolve()

    def write(
        directory: pathlib.Path,
        *,
        files: Mapping[str, str] | None = None,
        conftest: str | None = None,
        foundry_toml: str | None = None,
    ) -> pathlib.Path:
        root = directory.resolve()
        if root != base and base not in root.parents:
            pytest.fail(f"write_project refuses to write to {root}: it is not inside {base}")
        payload = dict(files or {})
        if conftest is not None:
            payload["conftest.py"] = conftest
        if foundry_toml is not None:
            payload["foundry.toml"] = foundry_toml
        for key in payload:
            key_path = pathlib.PurePosixPath(key)
            if not key_path.parts or key_path.is_absolute() or ".." in key_path.parts:
                pytest.fail(f"write_project refuses the key {key!r}: it must stay inside {root}")
        root.mkdir(parents=True, exist_ok=True)
        for relative in sorted(payload):
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            content = payload[relative]
            target.write_text(
                content if content.endswith("\n") else f"{content}\n",
                encoding="utf-8",
                newline="\n",
            )
        return directory

    return write


@pytest.fixture
def clean_foundry_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Function scope: no ``FOUNDRY_*`` variable in the environment, ``TZ``/``LC_ALL`` pinned."""
    for name in [key for key in os.environ if key.startswith("FOUNDRY_")]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TZ", "UTC")
    monkeypatch.setenv("LC_ALL", "C")


@pytest.fixture(scope="session")
def installed_distribution() -> importlib.metadata.Distribution:
    """Session scope: the installed distribution, whose ``pytest11`` point targets the plugin."""
    try:
        distribution = importlib.metadata.distribution("pytest-foundry")
    except importlib.metadata.PackageNotFoundError:
        pytest.fail(
            "the 'pytest-foundry' distribution is not installed in this environment: install it "
            "with `uv sync --extra dev` and rerun"
        )
    targets = [
        entry_point.value
        for entry_point in distribution.entry_points
        if entry_point.group == "pytest11" and entry_point.name == "foundry"
    ]
    if not targets:
        pytest.fail(
            "the 'pytest-foundry' distribution declares no 'pytest11' entry point named 'foundry', "
            "so the plugin cannot activate without a -p flag (AAP 0.4.2)"
        )
    if targets[0] != "foundry.plugin":
        pytest.fail(
            f"the 'pytest11' entry point named 'foundry' targets {targets[0]!r}, not "
            "'foundry.plugin' (AAP 0.4.2)"
        )
    return distribution
