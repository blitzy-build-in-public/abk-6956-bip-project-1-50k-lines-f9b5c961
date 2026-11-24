"""Shared fixtures and process-wide guards for Foundry's own test suite.

This is the only module the plan places directly under ``tests/`` (AAP 0.8.1 tree, AAP 0.8.2
"Self-test suite", AAP 0.10.1), and it carries three jobs.

**pytester activation.** ``pytest_plugins`` must be declared by the top-level conftest of the test
tree, so the declaration lives here and nowhere below ``tests/``.

**Session environment pinning.** ``TZ``, ``LC_ALL``, ``LANG`` and the ``PYTEST_ADDOPTS`` deletion
run at import, before any test module is imported: no assertion in this suite may depend on the
host's clock, its locale, or the options the outer invocation happened to be given.

**Shared fixtures and the loopback-only socket guard.** The guard installed at import is the
suite-wide half of the offline control in AAP 0.14.3: it allows ``127.0.0.0/8`` and ``::1``, counts
every other target and refuses the connection. Its counters are published in the protocol
``foundry.diagnostics.socket_guard`` reads, and to the ``socket_guard`` fixture whose count
``tests/e2e/test_offline.py`` asserts is zero.

The guard patches this interpreter only. A socket opened by a child process is invisible to it, so
the end-to-end tests isolate their children with the flags those children are given (``--no-index``,
``uv --offline``) rather than with this guard.
"""

from __future__ import annotations

import importlib.metadata
import ipaddress
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Never

import pytest

# Declared here and only here: pytest aborts the run with "Defining 'pytest_plugins' in a
# non-top-level conftest" when the same declaration appears in a nested conftest.
pytest_plugins = ["pytester"]

# The outer invocation has already parsed its own PYTEST_ADDOPTS, while every in-process pytester
# run and every child process would apply those options again to a suite that does not expect them.
_OUTER_PYTEST_ADDOPTS: Final[str | None] = os.environ.pop("PYTEST_ADDOPTS", None)

# Pinned before any test module is imported, so no assertion can depend on a wall-clock date or a
# locale; time.tzset re-reads TZ for the C library. The project is POSIX-only, and the hasattr guard
# merely keeps this file importable elsewhere.
os.environ["TZ"] = "UTC"
os.environ["LC_ALL"] = "C"
os.environ["LANG"] = "C"
if hasattr(time, "tzset"):
    time.tzset()

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
    """Live counters of this suite's loopback-only socket guard.

    ``installed`` is true once the guard has patched :mod:`socket`; ``loopback_attempts`` counts the
    loopback connections it allowed; ``non_loopback_attempts`` counts the targets it refused and
    ``violations`` lists each one as ``"host:port"``. ``tests/e2e/test_offline.py`` asserts
    ``installed is True`` and ``non_loopback_attempts == 0``.

    ``foundry.diagnostics.socket_guard`` recognises a guard by two names -- ``FOUNDRY_SOCKET_GUARD``
    published by the announcing module and ``FOUNDRY_NON_LOOPBACK_ATTEMPTS`` carrying the count --
    and ``examples/checkout/conftest.py`` publishes the same pair, so ``foundry doctor`` reads
    either. :meth:`__getattr__` serves them here so the count stays live instead of being a value
    snapshotted at import.
    """

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


# Saved before any patching, so the wrappers can connect and resolve without recursing into
# themselves. A second import of this file in one process adopts the first import's guard and never
# re-wraps, so only the importing instance's copies are ever called.
_ORIGINAL_GETADDRINFO = socket.getaddrinfo
_ORIGINAL_CONNECT = socket.socket.connect
_ORIGINAL_CONNECT_EX = socket.socket.connect_ex


def _host_text(host: object) -> str | None:
    """Return ``host`` as text when it is a string or a byte string, else ``None``."""
    if isinstance(host, str):
        return host
    if isinstance(host, bytes):
        return host.decode("ascii", "ignore") or None
    return None


def _is_loopback_literal(host: str) -> bool:
    """Return True when ``host`` is a loopback IP literal.

    :mod:`ipaddress` flags every address in ``127.0.0.0/8`` and ``::1``; an IPv4-mapped IPv6 address
    is judged by its mapped form because ``::ffff:127.0.0.1`` is not itself flagged loopback.
    """
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if address.is_loopback:
        return True
    mapped = getattr(address, "ipv4_mapped", None)
    return bool(mapped is not None and mapped.is_loopback)


def _is_local_host(host: str) -> bool:
    """Return True when ``host`` is a loopback literal or a well-known local hostname."""
    return _is_loopback_literal(host) or host.lower() in _LOCAL_HOSTNAMES


def _split_target(address: object) -> tuple[str | None, object]:
    """Split a connect address into host text and port, tolerating a non-tuple address."""
    if isinstance(address, tuple) and len(address) >= 2:
        return _host_text(address[0]), address[1]
    return _host_text(address), 0


def _refused_target(family: int, address: object) -> tuple[str, object] | None:
    """Return the ``(host, port)`` to refuse for ``address``, or ``None`` when it may connect.

    Only ``AF_INET`` and ``AF_INET6`` are policed: ``AF_UNIX``, any other family and any address
    that is not an INET-style tuple are local by construction. A loopback literal and a well-known
    local hostname are allowed; every other hostname is refused *before* the resolver runs, because
    resolving it would itself open the non-loopback socket this guard exists to prevent.
    """
    if family not in _INET_FAMILIES:
        return None
    host, port = _split_target(address)
    if host is None or _is_local_host(host):
        return None
    return host, port


def _refuse(host: str, port: object) -> Never:
    """Record one refused attempt on the guard and raise the error that names it."""
    SOCKET_GUARD.non_loopback_attempts += 1
    SOCKET_GUARD.violations.append(f"{host}:{port}")
    raise RuntimeError(
        f"foundry test suite: refused a non-loopback connection to {host}:{port}. "
        f"The suite opens no non-loopback socket ({_OFFLINE_RULE}): serve HTTP from an in-process "
        "http.server bound to 127.0.0.1 on an ephemeral port, or replay a cassette, instead."
    )


def _guarded_getaddrinfo(
    host: object,
    port: object,
    family: int = 0,
    type: int = 0,
    proto: int = 0,
    flags: int = 0,
) -> Any:
    """Resolve a local name only; refuse any other name before the resolver runs."""
    name = _host_text(host)
    policed = family == 0 or family in _INET_FAMILIES
    if name is not None and policed and not _is_local_host(name):
        _refuse(name, port)
    return _ORIGINAL_GETADDRINFO(host, port, family, type, proto, flags)


def _guarded_connect(self: socket.socket, address: object) -> None:
    """Connect to a loopback target only, counting the permitted attempt and refusing the rest."""
    refused = _refused_target(self.family, address)
    if refused is not None:
        _refuse(*refused)
    if self.family in _INET_FAMILIES:
        SOCKET_GUARD.loopback_attempts += 1
    _ORIGINAL_CONNECT(self, address)


def _guarded_connect_ex(self: socket.socket, address: object) -> int:
    """Behave as :meth:`socket.socket.connect_ex` for a local target, and refuse any other."""
    refused = _refused_target(self.family, address)
    if refused is not None:
        _refuse(*refused)
    if self.family in _INET_FAMILIES:
        SOCKET_GUARD.loopback_attempts += 1
    return int(_ORIGINAL_CONNECT_EX(self, address))


def _adopt_or_create_guard() -> SocketGuardState:
    """Return the state an earlier import of this file published, or a fresh one."""
    published: SocketGuardState | None = getattr(socket.socket.connect, _GUARD_MARK, None)
    return published if published is not None else SocketGuardState()


SOCKET_GUARD: SocketGuardState = _adopt_or_create_guard()
"""The one guard of this process: its counters and the protocol ``foundry doctor`` reads."""

FOUNDRY_SOCKET_GUARD: SocketGuardState = SOCKET_GUARD
"""Announces this module as the guard holder to ``foundry.diagnostics.socket_guard``."""


def _install_guard() -> None:
    """Patch :mod:`socket` once per process, so a second import wraps it exactly once.

    Each wrapper carries the live state under :data:`_GUARD_MARK`, which is both the idempotence
    marker and how a second import finds the counters it adopts. The state is bound to
    :data:`SOCKET_GUARD` first, so a wrapper can never run against an unbound global.
    """
    if getattr(socket.socket.connect, _GUARD_MARK, None) is SOCKET_GUARD:
        return
    for wrapper in (_guarded_getaddrinfo, _guarded_connect, _guarded_connect_ex):
        setattr(wrapper, _GUARD_MARK, SOCKET_GUARD)
    socket.getaddrinfo = _guarded_getaddrinfo
    socket.socket.connect = _guarded_connect
    socket.socket.connect_ex = _guarded_connect_ex


_install_guard()


def current_guard() -> SocketGuardState:
    """Return the live guard state of this process, which the ``socket_guard`` fixture yields."""
    return SOCKET_GUARD


@pytest.fixture(scope="session")
def socket_guard() -> SocketGuardState:
    """Session scope: the live guard installed at import of this module.

    Consumer: ``tests/e2e/test_offline.py``, which asserts ``installed is True`` and
    ``non_loopback_attempts == 0``. ``loopback_attempts`` is order-dependent, so nothing asserts it.
    """
    return current_guard()


@pytest.fixture(scope="session")
def repo_root() -> pathlib.Path:
    """Session scope: the repository root, checked for the markers its consumers need.

    Consumers: the static checks in ``tests/unit/`` (layering, sleep policy, ``-p`` flag, unsafe
    parsing, exception boundary) and every end-to-end module.
    """
    root = pathlib.Path(__file__).resolve().parents[1]
    missing = [name for name in ("pyproject.toml", "src/foundry") if not (root / name).exists()]
    if missing:
        pytest.fail(f"repository root {root} is missing {', '.join(missing)}")
    return root


@pytest.fixture(scope="session")
def foundry_python() -> str:
    """Session scope: the interpreter under test, ``sys.executable``.

    Consumer: any test that drives a subprocess against an explicit interpreter rather than through
    ``run_pytest``.
    """
    return sys.executable


@pytest.fixture(scope="session")
def foundry_script() -> pathlib.Path:
    """Session scope: the installed ``foundry`` console script.

    Consumers: ``tests/e2e/test_init_project.py``, ``tests/e2e/test_install.py`` and the
    installed-entry-point cases of ``tests/plugin/test_cli_surface.py``. Fails with the install
    command instead of a bare ``FileNotFoundError`` when the distribution is absent.
    """
    on_path = shutil.which("foundry")
    script = pathlib.Path(on_path) if on_path else pathlib.Path(sys.executable).parent / "foundry"
    if not script.exists():
        pytest.fail(
            f"the foundry console script is missing: looked for {script}. Install the distribution "
            "into this environment with `uv sync --extra dev`, or with "
            "`uv pip install dist/pytest_foundry-0.1.0-py3-none-any.whl`, and rerun."
        )
    return script


_STRIPPED_VARIABLES: Final[tuple[str, ...]] = (
    "PYTEST_ADDOPTS",
    "PYTEST_XDIST_WORKER",
    "PYTEST_XDIST_WORKER_COUNT",
    "PYTEST_CURRENT_TEST",
    "PYTEST_PLUGINS",
)
"""Outer-run variables a child process must not inherit."""

_STRIPPED_PREFIXES: Final[tuple[str, ...]] = ("FOUNDRY_", "COV_CORE_", "COVERAGE_")
"""Prefixes removed from every child environment: the parent's configuration and coverage state."""


@pytest.fixture(scope="session")
def subprocess_env() -> Callable[..., dict[str, str]]:
    """Session scope: a builder of child-process environments, merging an optional ``extra`` map.

    Consumers: every end-to-end module and ``tests/integration/test_startup_cost.py``. Copying
    ``os.environ`` is deliberate -- ``PATH``, ``PYTHONPATH`` and ``VIRTUAL_ENV`` must survive or
    neither the plugin entry point nor the console script resolves -- while the outer run's pytest
    options, xdist identity, Foundry configuration and coverage state are removed, so a child
    asserts against its own configuration alone.
    """

    def build(extra: Mapping[str, str] | None = None) -> dict[str, str]:
        env = dict(os.environ)
        for name in _STRIPPED_VARIABLES:
            env.pop(name, None)
        for name in [key for key in env if key.startswith(_STRIPPED_PREFIXES)]:
            del env[name]
        env.update(
            {
                "TZ": "UTC",
                "LC_ALL": "C",
                "LANG": "C",
                "PYTHONIOENCODING": "utf-8",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        if extra:
            env.update(extra)
        return env

    return build


@pytest.fixture(scope="session")
def run_pytest(
    subprocess_env: Callable[..., dict[str, str]],
) -> Callable[..., subprocess.CompletedProcess[str]]:
    """Session scope: run ``python -m pytest`` in a child process and capture its output.

    Consumers: ``tests/integration/test_data_expansion.py`` (two ``--collect-only -q`` runs),
    ``tests/integration/test_startup_cost.py``, ``tests/e2e/test_sharding.py``,
    ``tests/e2e/test_example_project.py`` and ``tests/e2e/test_zero_config_subprocess.py``. The
    environment comes from ``subprocess_env``, so the outer invocation's options never leak in.
    """

    def run(
        *args: str,
        cwd: pathlib.Path,
        env: Mapping[str, str] | None = None,
        timeout: float = 300.0,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "pytest", *args],
            cwd=str(cwd),
            env=subprocess_env(env),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    return run


@pytest.fixture(scope="session")
def run_foundry(
    foundry_script: pathlib.Path,
    subprocess_env: Callable[..., dict[str, str]],
) -> Callable[..., subprocess.CompletedProcess[str]]:
    """Session scope: run the installed ``foundry`` console script and capture its output.

    Consumers: ``tests/e2e/test_init_project.py`` and the installed-entry-point cases of
    ``tests/plugin/test_cli_surface.py`` -- the two places where only the real script satisfies the
    real-entry-point rule of AAP 0.14.5.
    """

    def run(
        *args: str,
        cwd: pathlib.Path,
        env: Mapping[str, str] | None = None,
        timeout: float = 300.0,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(foundry_script), *args],
            cwd=str(cwd),
            env=subprocess_env(env),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    return run


@pytest.fixture(scope="session")
def example_project(repo_root: pathlib.Path) -> pathlib.Path:
    """Session scope: ``examples/checkout``, the shipped 24-test project.

    Consumers: ``tests/e2e/test_example_project.py``, ``tests/e2e/test_sharding.py`` and
    ``tests/e2e/test_determinism.py``. Fails rather than skipping when the project is absent,
    because a missing example is a missing deliverable.
    """
    project = repo_root / "examples" / "checkout"
    if not project.is_dir():
        pytest.fail(f"the example project is missing: {project} is not a directory")
    return project


@pytest.fixture
def write_project() -> Callable[..., pathlib.Path]:
    """Function scope: write a throwaway project into a test-owned directory and return it.

    ``files`` maps relative paths to content, ``conftest`` writes ``conftest.py`` and
    ``foundry_toml`` writes ``foundry.toml``. Every file is UTF-8 with LF newlines and a trailing
    newline, and parents are created, so the result is byte-identical on any host.
    ``pyproject.toml`` is written only when a caller passes it, which is what makes a scaffold or a
    zero-configuration directory genuinely configuration-free.

    Consumers: the replay-miss and no-configuration scenarios of the end-to-end modules, which must
    never mutate ``examples/checkout``.
    """

    def write(
        directory: pathlib.Path,
        *,
        files: Mapping[str, str] | None = None,
        conftest: str | None = None,
        foundry_toml: str | None = None,
    ) -> pathlib.Path:
        directory.mkdir(parents=True, exist_ok=True)
        payload = dict(files or {})
        if conftest is not None:
            payload["conftest.py"] = conftest
        if foundry_toml is not None:
            payload["foundry.toml"] = foundry_toml
        for relative in sorted(payload):
            target = directory / relative
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
    """Function scope: no ``FOUNDRY_*`` variable in the environment, ``TZ``/``LC_ALL`` pinned.

    Consumers: ``tests/integration/test_config_precedence.py`` and
    ``tests/plugin/test_config_errors.py``, which assert what configuration resolves to and must not
    inherit a variable the developer's shell happens to export.
    """
    for name in [key for key in os.environ if key.startswith("FOUNDRY_")]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TZ", "UTC")
    monkeypatch.setenv("LC_ALL", "C")


@pytest.fixture(scope="session")
def installed_distribution() -> importlib.metadata.Distribution:
    """Session scope: the installed ``pytest-foundry`` distribution, with its entry point checked.

    Consumers: ``tests/e2e/test_install.py``, ``tests/plugin/test_reachability.py`` and
    ``tests/plugin/test_public_contract.py``. Fails with the install command when the distribution
    is absent, and fails when the ``pytest11`` entry point named ``foundry`` does not target
    ``foundry.plugin``, because zero-configuration activation is that entry point.
    """
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
