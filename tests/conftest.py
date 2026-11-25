"""Shared fixtures and process-wide guards for Foundry's own test suite.

Carries the ``pytest_plugins`` declaration only the top-level conftest may make, session
environment pinning at import (``TZ``, ``LC_ALL``, ``LANG``, the ``PYTEST_ADDOPTS`` deletion and
``locale.setlocale``) so no assertion depends on the host's clock or locale, the shared fixtures,
and the loopback-only socket guard, the suite-wide half of the offline control in AAP 0.14.3. The
guard polices ``connect``, ``connect_ex``, ``getaddrinfo``, ``sendto`` and ``sendmsg``, allowing
``127.0.0.0/8`` and ``::1`` and refusing every other target before the syscall; it patches this
interpreter only, so the end-to-end tests isolate their children by the flags they are given.
"""

from __future__ import annotations

import importlib.metadata
import ipaddress
import locale
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
# run and every child process would apply those options again. Popping it at import is the fix.
os.environ.pop("PYTEST_ADDOPTS", None)

# Pinned before any test module is imported, so no assertion depends on a wall-clock date or a
# locale; time.tzset re-reads TZ for the C library, and hasattr keeps this importable off POSIX.
os.environ["TZ"] = "UTC"
os.environ["LC_ALL"] = "C"
os.environ["LANG"] = "C"
if hasattr(time, "tzset"):
    time.tzset()

# os.environ re-runs nothing in this process, so the interpreter would keep its starting locale and
# an in-process assertion would still see the host's. Failing to apply the AAP 0.14.3 pin is fatal
# rather than skipped, because a silently unpinned locale is the dependence it rules out.
try:
    locale.setlocale(locale.LC_ALL, "C")
except locale.Error as exc:
    raise RuntimeError(
        f"foundry test suite: could not pin the process locale to 'C': {exc}"
    ) from exc

_INET_FAMILIES: Final[frozenset[int]] = frozenset({socket.AF_INET, socket.AF_INET6})
"""The families the guard polices; every other family is local by construction."""

_LOCAL_HOSTNAMES: Final[frozenset[str]] = frozenset(
    {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}
)
"""Names allowed without resolving them, because every address they carry is loopback."""

_GUARD_MARK: Final[str] = "_foundry_suite_socket_guard"
"""Attribute holding the live state on each wrapper, which is what makes installation idempotent."""

_OFFLINE_RULE: Final[str] = "AAP 0.14.3"


@dataclass(frozen=True)
class SocketGuardSnapshot:
    """An immutable reading of the guard's counters and violations.

    A test that provokes a refusal on purpose takes one first and hands it back to
    :meth:`SocketGuardState.restore`, so the offline proof reads the count from before that test.
    """

    loopback_attempts: int = 0
    non_loopback_attempts: int = 0
    violations: tuple[str, ...] = ()


@dataclass
class SocketGuardState:
    """Live counters of this suite's loopback-only socket guard.

    ``installed`` is true once the guard has patched the :mod:`socket` module; the counters and
    ``violations`` are process-global and belong to one ``-n auto`` worker, so the offline proof
    measures a delta with :meth:`refusals_since`. ``foundry.diagnostics.socket_guard`` recognises a
    guard by ``FOUNDRY_SOCKET_GUARD`` and ``FOUNDRY_NON_LOOPBACK_ATTEMPTS``, which
    :meth:`__getattr__` serves live, and ``examples/checkout/conftest.py`` publishes the same pair.
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

    def snapshot(self) -> SocketGuardSnapshot:
        """Return an immutable reading of the counters and violations as they stand now."""
        return SocketGuardSnapshot(
            loopback_attempts=self.loopback_attempts,
            non_loopback_attempts=self.non_loopback_attempts,
            violations=tuple(self.violations),
        )

    def restore(self, snapshot: SocketGuardSnapshot) -> None:
        """Put the counters and violations back to ``snapshot``."""
        self.loopback_attempts = snapshot.loopback_attempts
        self.non_loopback_attempts = snapshot.non_loopback_attempts
        self.violations = list(snapshot.violations)

    def reset(self) -> None:
        """Clear the counters and violations, restoring the zero :class:`SocketGuardSnapshot`."""
        self.restore(SocketGuardSnapshot())

    def refusals_since(self, snapshot: SocketGuardSnapshot) -> int:
        """Return the refusals recorded since ``snapshot`` was taken."""
        return self.non_loopback_attempts - snapshot.non_loopback_attempts


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


def _is_loopback_literal(host: str) -> bool:
    """Return True when ``host`` is a loopback IP literal.

    :mod:`ipaddress` flags every address in ``127.0.0.0/8`` and ``::1``; an IPv4-mapped IPv6 address
    is judged by its mapped form, because ``::ffff:127.0.0.1`` is not itself flagged loopback.
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
    """Split a socket address into host text and port, tolerating a non-tuple address."""
    if isinstance(address, tuple) and len(address) >= 2:
        return _host_text(address[0]), address[1]
    return _host_text(address), 0


def _refused_target(family: int, address: object) -> tuple[str, object] | None:
    """Return the ``(host, port)`` to refuse for ``address``, or ``None`` when it may proceed.

    Only ``AF_INET`` and ``AF_INET6`` are policed: any other family and any non-tuple address are
    local by construction. Every hostname that is not a loopback literal or a well-known local name
    is refused *before* the resolver runs, because resolving it would open the very socket refused.
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
        f"foundry test suite: refused a non-loopback target {host}:{port}. "
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


def _guarded_sendto(self: socket.socket, *args: object) -> int:
    """Send a datagram to a loopback target only, refusing the rest before the syscall."""
    address = args[-1] if args else None
    refused = _refused_target(self.family, address)
    if refused is not None:
        _refuse(*refused)
    if self.family in _INET_FAMILIES:
        SOCKET_GUARD.loopback_attempts += 1
    return int(_ORIGINAL_SENDTO(self, *args))


def _guarded_sendmsg(self: socket.socket, *args: object) -> int:
    """Send one message to a loopback target only, refusing the rest before the syscall.

    ``sendmsg`` takes its address as an optional last argument and omits it for a connected socket,
    where the missing target is local and the send is allowed.
    """
    address = args[-1] if args else None
    refused = _refused_target(self.family, address)
    if refused is not None:
        _refuse(*refused)
    if self.family in _INET_FAMILIES:
        SOCKET_GUARD.loopback_attempts += 1
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
    """Patch :mod:`socket` once per process, so a second import wraps it exactly once.

    Each wrapper carries the live state under :data:`_GUARD_MARK`, the marker a second import adopts
    instead of re-wrapping; the state is bound to :data:`SOCKET_GUARD` first, never unbound.
    """
    if getattr(socket.socket.connect, _GUARD_MARK, None) is SOCKET_GUARD:
        return
    for wrapper in (
        _guarded_getaddrinfo,
        _guarded_connect,
        _guarded_connect_ex,
        _guarded_sendto,
        _guarded_sendmsg,
    ):
        setattr(wrapper, _GUARD_MARK, SOCKET_GUARD)
    socket.getaddrinfo = _guarded_getaddrinfo
    socket.socket.connect = _guarded_connect
    socket.socket.connect_ex = _guarded_connect_ex
    socket.socket.sendto = _guarded_sendto
    socket.socket.sendmsg = _guarded_sendmsg


_install_guard()


def current_guard() -> SocketGuardState:
    """Return the live guard state of this process, which the ``socket_guard`` fixture yields."""
    return SOCKET_GUARD


@pytest.fixture(scope="session")
def socket_guard() -> SocketGuardState:
    """Session scope: the live guard installed at import of this module.

    Consumer: ``tests/e2e/test_offline.py``, which measures :meth:`SocketGuardState.refusals_since`
    around its own run rather than trusting the absolute session counter, because that counter
    belongs to one ``-n auto`` worker.
    """
    return current_guard()


@pytest.fixture(scope="session")
def repo_root() -> pathlib.Path:
    """Session scope: the repository root, checked for the markers its consumers need.

    Consumers: the static checks in ``tests/unit/`` and every end-to-end module.
    """
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
    """Session scope: the installed ``foundry`` console script, failing with the install command
    instead of a bare ``FileNotFoundError`` when the distribution is absent.
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

    ``os.environ`` is copied because ``PATH``, ``PYTHONPATH`` and ``VIRTUAL_ENV`` must survive,
    while the outer run's pytest options, xdist identity, Foundry configuration and coverage state
    are removed, so a child asserts against its own configuration alone.
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

    The environment comes from ``subprocess_env``, so the outer invocation's options never leak in.
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

    Consumers: every place only the real script satisfies the real-entry-point rule of AAP 0.14.5.
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

    Fails rather than skipping when it is absent: a missing example is a missing deliverable.
    """
    project = repo_root / "examples" / "checkout"
    if not project.is_dir():
        pytest.fail(f"the example project is missing: {project} is not a directory")
    return project


@pytest.fixture
def write_project(tmp_path: pathlib.Path) -> Callable[..., pathlib.Path]:
    """Function scope: write a throwaway project into a test-owned directory and return it.

    ``files`` maps relative paths to content, ``conftest`` writes ``conftest.py`` and
    ``foundry_toml`` writes ``foundry.toml``; every file is UTF-8 with LF newlines and a trailing
    newline. ``pyproject.toml`` is written only when a caller passes it, which is what keeps a
    scaffold or a zero-configuration directory genuinely configuration-free. Containment is
    enforced: ``directory`` must lie inside the session's temporary base directory and every key
    must be a relative path with no ``..`` segment, so ``examples/checkout`` can never be mutated.
    """

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
            pytest.fail(
                f"write_project refuses to write to {root}: it is not inside the session's "
                f"temporary base directory {base}. Writes stay there so no test can mutate "
                "examples/checkout."
            )
        payload = dict(files or {})
        if conftest is not None:
            payload["conftest.py"] = conftest
        if foundry_toml is not None:
            payload["foundry.toml"] = foundry_toml
        for key in payload:
            key_path = pathlib.PurePosixPath(key)
            if not key_path.parts:
                pytest.fail(
                    f"write_project refuses the file key {key!r}: it names no file inside {root}"
                )
            if key_path.is_absolute():
                pytest.fail(
                    f"write_project refuses the absolute file key {key!r}: writes stay inside "
                    f"{root}"
                )
            if ".." in key_path.parts:
                pytest.fail(
                    f"write_project refuses the file key {key!r}: its '..' segment could write "
                    f"outside {root}"
                )
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
    """Function scope: no ``FOUNDRY_*`` variable in the environment, ``TZ``/``LC_ALL`` pinned.

    Consumer: the configuration tests, which must not inherit a variable the developer's shell
    happens to export.
    """
    for name in [key for key in os.environ if key.startswith("FOUNDRY_")]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TZ", "UTC")
    monkeypatch.setenv("LC_ALL", "C")


@pytest.fixture(scope="session")
def installed_distribution() -> importlib.metadata.Distribution:
    """Session scope: the installed ``pytest-foundry`` distribution, entry point checked.

    Fails with the install command when it is absent and when the ``pytest11`` entry point named
    ``foundry`` does not target ``foundry.plugin``: zero-configuration activation is that point.
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
