"""Shared fixtures and process-wide guards for Foundry's own test suite.

This is the only module the plan places directly under ``tests/`` (AAP 0.8.1 tree, AAP 0.8.2
"Self-test suite", AAP 0.10.1), and it carries three jobs.

**pytester activation.** ``pytest_plugins`` must be declared by the top-level conftest of the test
tree, so the declaration lives here and nowhere below ``tests/``.

**Session environment pinning.** ``TZ``, ``LC_ALL``, ``LANG`` and the ``PYTEST_ADDOPTS`` deletion
run at import, before any test module is imported, and ``locale.setlocale`` puts this interpreter's
own locale categories in ``C`` as well, the same pins ``subprocess_env`` gives every child: no
assertion in this suite may depend on the host's clock, its locale, or the options the outer
invocation happened to be given.

**Shared fixtures and the loopback-only socket guard.** The guard installed at import is the
suite-wide half of the offline control in AAP 0.14.3: ``connect``, ``connect_ex``,
``getaddrinfo`` and the datagram sends ``sendto`` and ``sendmsg`` all pass through it, which allows
``127.0.0.0/8`` and ``::1``, counts every other target and refuses it before the syscall runs. Its
counters are published in the protocol ``foundry.diagnostics.socket_guard`` reads, and to the
``socket_guard`` fixture, which ``tests/e2e/test_offline.py`` uses to prove a run of its own adds
no refusal.

The guard patches this interpreter only. A socket opened by a child process is invisible to it, so
the end-to-end tests isolate their children with the flags those children are given (``--no-index``,
``uv --offline``) rather than with this guard.
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
# run and every child process would apply those options again to a suite that does not expect them.
# Popping it at import is the whole fix: the value is deliberately dropped, never restored or
# asserted on, which is why nothing binds it.
os.environ.pop("PYTEST_ADDOPTS", None)

# Pinned before any test module is imported, so no assertion can depend on a wall-clock date or a
# locale; time.tzset re-reads TZ for the C library. The project is POSIX-only, and the hasattr guard
# merely keeps this file importable elsewhere.
os.environ["TZ"] = "UTC"
os.environ["LC_ALL"] = "C"
os.environ["LANG"] = "C"
if hasattr(time, "tzset"):
    time.tzset()

# os.environ re-runs nothing in this process: it only shapes what a child inherits, so the
# interpreter would keep the locale categories it started with and an in-process assertion would
# still see the host's. The AAP 0.14.3 pin is completed here, and failing to apply it is fatal
# rather than skipped, because a silently unpinned locale is exactly the dependence it rules out.
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

    A test that provokes a refusal on purpose takes one of these first and hands it back to
    :meth:`SocketGuardState.restore` in its teardown, so the count the offline proof reads is the
    one the session had before that test ran. The default instance is the zero reading
    :meth:`SocketGuardState.reset` restores.

    Attributes:
        loopback_attempts: The allowed loopback count at the moment of the reading.
        non_loopback_attempts: The refused-target count at the moment of the reading.
        violations: The ``"host:port"`` entries recorded by that moment, oldest first.
    """

    loopback_attempts: int = 0
    non_loopback_attempts: int = 0
    violations: tuple[str, ...] = ()


@dataclass
class SocketGuardState:
    """Live counters of this suite's loopback-only socket guard.

    ``installed`` is true once the guard has patched :mod:`socket`; ``loopback_attempts`` counts the
    loopback connections and datagram sends it allowed; ``non_loopback_attempts`` counts the
    targets it refused and ``violations`` lists each one as ``"host:port"``. The counters are
    process-global and, under ``-n auto``, belong to one worker, so the offline proof in
    ``tests/e2e/test_offline.py`` asserts ``installed is True`` and measures a delta with
    :meth:`refusals_since` around its own run instead of trusting the absolute count: a test that
    provokes a refusal takes ``before = guard.snapshot()`` and calls ``guard.restore(before)`` in
    its teardown, so neither collection order nor another worker's refusals can reach the proof.

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

    def snapshot(self) -> SocketGuardSnapshot:
        """Return an immutable reading of the counters and violations as they stand now."""
        return SocketGuardSnapshot(
            loopback_attempts=self.loopback_attempts,
            non_loopback_attempts=self.non_loopback_attempts,
            violations=tuple(self.violations),
        )

    def restore(self, snapshot: SocketGuardSnapshot) -> None:
        """Put the counters and violations back to ``snapshot``.

        What a test that provoked a refusal calls in its teardown, so the session's counters read
        exactly as they did before it ran and no later assertion inherits its refusal.
        """
        self.loopback_attempts = snapshot.loopback_attempts
        self.non_loopback_attempts = snapshot.non_loopback_attempts
        self.violations = list(snapshot.violations)

    def reset(self) -> None:
        """Clear the counters and violations, restoring the zero :class:`SocketGuardSnapshot`."""
        self.restore(SocketGuardSnapshot())

    def refusals_since(self, snapshot: SocketGuardSnapshot) -> int:
        """Return the refusals recorded since ``snapshot`` was taken.

        The delta ``tests/e2e/test_offline.py`` measures around its own run, which is independent of
        collection order and of the refusals this worker counted before it.
        """
        return self.non_loopback_attempts - snapshot.non_loopback_attempts


# Saved before any patching, so the wrappers can connect, resolve and send without recursing into
# themselves. A second import of this file in one process adopts the first import's guard and never
# re-wraps, so only the importing instance's copies are ever called.
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
    """Split a socket address into host text and port, tolerating a non-tuple address."""
    if isinstance(address, tuple) and len(address) >= 2:
        return _host_text(address[0]), address[1]
    return _host_text(address), 0


def _refused_target(family: int, address: object) -> tuple[str, object] | None:
    """Return the ``(host, port)`` to refuse for ``address``, or ``None`` when it may proceed.

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
    """Send a datagram to a loopback target only, refusing the rest before the syscall.

    ``sendto`` carries its address in the last positional argument, whether it is called as
    ``(data, address)`` or as ``(data, flags, address)``, so that argument is the one policed.
    """
    address = args[-1] if args else None
    refused = _refused_target(self.family, address)
    if refused is not None:
        _refuse(*refused)
    if self.family in _INET_FAMILIES:
        SOCKET_GUARD.loopback_attempts += 1
    return int(_ORIGINAL_SENDTO(self, *args))


def _guarded_sendmsg(self: socket.socket, *args: object) -> int:
    """Send one message to a loopback target only, refusing the rest before the syscall.

    ``sendmsg`` takes its address as an optional last argument -- ``(buffers, ancdata, flags,
    address)`` -- and leaves it out for a connected socket, where the missing target is local and
    the send is allowed.
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

    Each wrapper carries the live state under :data:`_GUARD_MARK`, which is both the idempotence
    marker and how a second import finds the counters it adopts. The state is bound to
    :data:`SOCKET_GUARD` first, so a wrapper can never run against an unbound global.
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

    Consumer: ``tests/e2e/test_offline.py``, which asserts ``installed is True`` and measures
    :meth:`SocketGuardState.refusals_since` around its own run rather than trusting the absolute
    session counter. That counter is process-global and belongs to one ``-n auto`` worker, so a test
    that provokes a refusal takes ``guard.snapshot()`` first and calls ``guard.restore(before)`` in
    its teardown; :meth:`SocketGuardState.reset` restores the zero reading outright.
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
def write_project(tmp_path: pathlib.Path) -> Callable[..., pathlib.Path]:
    """Function scope: write a throwaway project into a test-owned directory and return it.

    ``files`` maps relative paths to content, ``conftest`` writes ``conftest.py`` and
    ``foundry_toml`` writes ``foundry.toml``. Every file is UTF-8 with LF newlines and a trailing
    newline, and parents are created, so the result is byte-identical on any host.
    ``pyproject.toml`` is written only when a caller passes it, which is what makes a scaffold or a
    zero-configuration directory genuinely configuration-free.

    Containment is enforced, not merely documented: ``directory`` must lie inside the session's
    temporary base directory (``tmp_path``'s parent, which also holds the siblings
    ``tmp_path_factory`` creates), and every key must be a relative path with no ``..`` segment, so
    a mistaken target or a key such as ``"../foundry.toml"`` fails the test before a byte is
    written and ``examples/checkout`` can never be mutated.

    Consumers: the replay-miss and no-configuration scenarios of the end-to-end modules, which must
    never mutate ``examples/checkout``.
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
