"""The example project's own plumbing: the offline guard, an echo server and the configuration.

AAP 0.8.1 gives ``examples/checkout`` this file, and it does the three things the example cannot do
from a data file: it keeps the run offline, it serves the API tests a real HTTP endpoint, and it
points Foundry's configuration at the example's own directories.

**The example carries no ``foundry.toml``.** The documented command is ``pytest examples/checkout``
run from the repository root, where configuration discovery looks at the invocation directory and
would never read a file inside the example (AAP 0.8.1, assumption A9). The ``foundry_config``
override below -- pytest's documented same-name override of the public session-scoped fixture (AAP
0.4.3) -- is how ``data.dir``, ``cassette.dir`` and ``http.base_url`` reach the example instead,
which is also why its data and cassettes sit beside this file. ``artifacts.dir`` keeps its
default, so ``artifacts/<run_id>/`` appears in the invocation directory as the criterion requires.

**The offline guard** (AAP 0.14.3) patches ``socket.socket.connect`` and
``socket.socket.connect_ex`` at import and permits only a loopback INET target: every address in
``127.0.0.0/8``, ``::1``, and an IPv4-mapped loopback such as ``::ffff:127.0.0.1``. Any other
target is recorded and refused *before* the syscall runs, so a test that tries to reach the network
fails with a message naming it rather than hanging on it. The module publishes the two names
``foundry.diagnostics.socket_guard`` reads -- ``FOUNDRY_SOCKET_GUARD`` and the live refusal count
``FOUNDRY_NON_LOOPBACK_ATTEMPTS`` -- so ``foundry doctor`` and the proof that the count is zero see
what this guard refused. Installation is idempotent: a second import adopts the first import's
state instead of wrapping the socket again.

**The echo server** is a real ``http.server.ThreadingHTTPServer`` bound to ``127.0.0.1:0`` and
served from a daemon thread, so nothing is mocked and every request is a loopback round trip. It
echoes the request back as JSON, which lets an API test assert on what it actually sent. It is not
started when the resolved ``cassette.mode`` is ``replay``, because a replay run has to prove it
opens no socket at all (AAP 0.4.7).
"""

from __future__ import annotations

import ipaddress
import json
import socket
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Never
from urllib.parse import parse_qs, urlparse

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

    from foundry.config.enums import CassetteMode
    from foundry.config.models import FoundryConfig

# --- The loopback-only guard ------------------------------------------------

_INET_FAMILIES: Final[frozenset[int]] = frozenset({socket.AF_INET, socket.AF_INET6})
"""The families the guard polices: every other family is local by construction."""

_GUARD_MARK: Final[str] = "_foundry_checkout_socket_guard"
"""Attribute holding this module's state on each wrapper: what makes installation idempotent."""

_OFFLINE_RULE: Final[str] = "AAP 0.14.3"
"""The constraint the guard is the mechanism for."""


@dataclass
class CheckoutSocketGuard:
    """Live state of the example's loopback-only guard: refused targets and their ``host:port``."""

    non_loopback_attempts: int = 0
    violations: list[str] = field(default_factory=list)


_GUARD = CheckoutSocketGuard()
"""The state this module counts into; a re-executed module body adopts the installed one instead."""

FOUNDRY_SOCKET_GUARD = _GUARD
"""Announces this module to ``foundry.diagnostics.socket_guard`` as the holder of a live guard."""


def __getattr__(name: str) -> Any:
    """Serve ``FOUNDRY_NON_LOOPBACK_ATTEMPTS`` live, so the count is read as it stands now.

    Any other name raises the ``AttributeError`` a module without that attribute raises.
    """
    if name == "FOUNDRY_NON_LOOPBACK_ATTEMPTS":
        return _GUARD.non_loopback_attempts
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _is_loopback(host: str) -> bool:
    """Return True when ``host`` is a loopback IP literal, judged by :mod:`ipaddress`.

    ``127.0.0.0/8`` and ``::1`` are flagged loopback; an IPv4-mapped ``::ffff:127.0.0.1`` is judged
    by its mapped form. A hostname is refused: resolving it would itself reach a name server.
    """
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if address.is_loopback:
        return True
    mapped = getattr(address, "ipv4_mapped", None)
    return bool(mapped is not None and mapped.is_loopback)


def _refused_target(family: int, address: object) -> tuple[str, object] | None:
    """Return the ``(host, port)`` to refuse for ``address``, or ``None`` when it may proceed.

    Only ``AF_INET`` and ``AF_INET6`` are policed: ``AF_UNIX``, every other family and a non-INET
    address are local by construction. A bytes host is judged as text, so neither spelling of a
    target slips past the check.
    """
    if family not in _INET_FAMILIES:
        return None
    if isinstance(address, tuple) and len(address) >= 2:
        host, port = address[0], address[1]
    else:
        host, port = address, 0
    if isinstance(host, bytes):
        host = host.decode("ascii", "ignore")
    if not isinstance(host, str) or _is_loopback(host):
        return None
    return host, port


def _refuse(host: str, port: object) -> Never:
    """Record one refused attempt on the guard and raise the error that names it."""
    _GUARD.non_loopback_attempts += 1
    _GUARD.violations.append(f"{host}:{port}")
    raise RuntimeError(
        f"examples/checkout: refused the non-loopback target {host}:{port}. The example opens no "
        f"non-loopback socket ({_OFFLINE_RULE}): its HTTP calls are served by the in-process echo "
        "server on 127.0.0.1, or replayed from examples/checkout/cassettes."
    )


# Saved before any patching, so the wrappers can connect without recursing into themselves.
_ORIGINAL_CONNECT = socket.socket.connect
_ORIGINAL_CONNECT_EX = socket.socket.connect_ex


def _guarded_connect(self: socket.socket, address: object) -> None:
    """Connect to a loopback target only, refusing the rest before the syscall runs."""
    refused = _refused_target(self.family, address)
    if refused is not None:
        _refuse(*refused)
    _ORIGINAL_CONNECT(self, address)


def _guarded_connect_ex(self: socket.socket, address: object) -> int:
    """Behave as :meth:`socket.socket.connect_ex` for a loopback target, and refuse any other."""
    refused = _refused_target(self.family, address)
    if refused is not None:
        _refuse(*refused)
    return int(_ORIGINAL_CONNECT_EX(self, address))


def _install_guard() -> None:
    """Patch :mod:`socket` once per process, so a second import wraps it exactly once.

    Each wrapper carries the live state under :data:`_GUARD_MARK`, which is both the idempotence
    marker and how a module body executed a second time finds the counters it adopts: the state is
    published on the wrapper, so two executions report one count rather than two. A guard another
    module installed first is left in place beneath these wrappers, since both checks refuse the
    same targets.
    """
    global FOUNDRY_SOCKET_GUARD, _GUARD
    installed = getattr(socket.socket.connect, _GUARD_MARK, None)
    # The mark holds the state the installed wrappers count into, so a module body executed a
    # second time adopts that instance rather than measuring a second, empty guard of its own. The
    # attribute is never set by anything else, and it is not tested with isinstance: a second
    # execution defines its own CheckoutSocketGuard class, which the first instance is not one of.
    if installed is not None:
        _GUARD = installed
        FOUNDRY_SOCKET_GUARD = installed
        return
    for wrapper in (_guarded_connect, _guarded_connect_ex):
        setattr(wrapper, _GUARD_MARK, _GUARD)
    socket.socket.connect = _guarded_connect
    socket.socket.connect_ex = _guarded_connect_ex


_install_guard()

# --- The in-process echo server ---------------------------------------------

_ECHO_HOST: Final[str] = "127.0.0.1"
"""The only address the example serves from, and one the guard above permits."""

_REPLAY_BASE_URL: Final[str] = "http://127.0.0.1"
"""The base URL reported in replay, where no server runs and no socket may be opened.

Cassette matching ignores the host (assumption A7), so a replayed request never dials the URL the
configuration carries; a fixed loopback value keeps ``http.base_url`` a valid absolute URL.
"""

_SHUTDOWN_TIMEOUT: Final[float] = 5.0
"""Seconds the teardown waits for the serving thread, so a wedged loop cannot hang the run."""


class CheckoutEchoHandler(BaseHTTPRequestHandler):
    """Echo one request back as JSON, so a test can assert on what it actually sent.

    The document carries the method, the path, the parsed query, the headers under their received
    spelling and under a lower-cased mapping, and the body -- decoded when it is JSON and as text
    otherwise. Nothing is interpreted and no request is answered differently, which is what makes
    the server a reference for the request a session built.
    """

    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        """Answer a GET with the echo document."""
        self._echo()

    def do_POST(self) -> None:
        """Answer a POST with the echo document."""
        self._echo()

    def do_PUT(self) -> None:
        """Answer a PUT with the echo document."""
        self._echo()

    def do_PATCH(self) -> None:
        """Answer a PATCH with the echo document."""
        self._echo()

    def do_DELETE(self) -> None:
        """Answer a DELETE with the echo document."""
        self._echo()

    def log_message(self, format: str, *args: object) -> None:
        """Write no access log: the example's output stays its own (AAP 0.13.1)."""

    def _echo(self) -> None:
        """Write the echo document for the request this handler was dispatched for."""
        raw_length = self.headers.get("Content-Length")
        length = int(raw_length) if raw_length is not None and raw_length.isdigit() else 0
        raw_body = self.rfile.read(length) if length else b""
        target = urlparse(self.path)
        payload = {
            "method": self.command,
            "path": target.path,
            "query": parse_qs(target.query),
            "headers": dict(self.headers.items()),
            "headers_lower": {name.lower(): value for name, value in self.headers.items()},
            "body": _decoded_body(raw_body),
        }
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _decoded_body(raw: bytes) -> object:
    """Return the request body decoded as JSON, or as text when it is not JSON."""
    if not raw:
        return None
    text = raw.decode("utf-8", "replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _invocation_dir(config: pytest.Config) -> Path:
    """Return the directory the run was invoked from, which configuration discovery is anchored to.

    A blank record falls back to ``rootpath``; a relative one is resolved by the configuration
    package against the process working directory, which at startup is the invocation directory
    itself (AAP 0.4.5).
    """
    recorded = str(config.invocation_params.dir).strip()
    if not recorded:
        return Path(config.rootpath)
    return Path(recorded)


def _cassette_mode(request: pytest.FixtureRequest) -> CassetteMode:
    """Return the ``cassette.mode`` this run resolved, without going through ``foundry_config``.

    The override below requests the echo server for ``http.base_url``, so the echo server cannot
    ask ``foundry_config`` for the mode without a cycle. It resolves the same two inputs the
    fixture's body does -- the ``--foundry-*`` overrides and the invocation directory (AAP 0.4.5) --
    and a :class:`foundry.core.exceptions.ConfigError` propagates unchanged.
    """
    from foundry.config import resolve
    from foundry.config.cli_layer import collect_from_pytest

    resolved = resolve(
        cli_overrides=collect_from_pytest(request.config),
        cwd=_invocation_dir(request.config),
    )
    return resolved.cassette.mode


@pytest.fixture(scope="session")
def echo_server(request: pytest.FixtureRequest) -> Iterator[str]:
    """Session scope: the base URL of the in-process echo server, for as long as the run lasts.

    A :class:`http.server.ThreadingHTTPServer` bound to ``127.0.0.1:0`` -- the kernel picks a free
    port, so concurrent example runs cannot collide -- served from a daemon thread. It starts in
    this fixture's body and is shut down here (``shutdown()``, ``server_close()``, then a bounded
    join), so no port outlives the session that opened it.

    **In replay it does not start at all**: a replay run has to prove it opens no socket (AAP
    0.4.7), so the mode is resolved first (see :func:`_cassette_mode` for why not through
    ``foundry_config``) and the fixed loopback placeholder is reported instead of binding anything.

    Args:
        request: The fixture request, read for the pytest configuration the mode is resolved from.

    Returns:
        ``http://127.0.0.1:<port>`` of the running server, or the replay placeholder.

    Raises:
        foundry.core.exceptions.ConfigError: The run's configuration cannot be resolved.
    """
    from foundry.config.enums import CassetteMode

    if _cassette_mode(request) is CassetteMode.REPLAY:
        yield _REPLAY_BASE_URL
        return
    server = ThreadingHTTPServer((_ECHO_HOST, 0), CheckoutEchoHandler)
    thread = threading.Thread(target=server.serve_forever, name="checkout-echo", daemon=True)
    thread.start()
    try:
        # The URL names the host the server was bound to and the port the kernel assigned, so it is
        # the address the guard above already permits rather than a reading of server_address.
        yield f"http://{_ECHO_HOST}:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=_SHUTDOWN_TIMEOUT)


# --- The example's configuration --------------------------------------------


@pytest.fixture(scope="session")
def foundry_config(foundry_config: FoundryConfig, echo_server: str) -> FoundryConfig:
    """Return the run's configuration with this example's directories and base URL in it.

    Pytest's documented same-name override of the public session-scoped fixture (AAP 0.4.3): it
    requests the parent instance, copies it, and changes exactly three things -- ``http.base_url``
    becomes the echo server's URL; ``data.dir`` becomes ``examples/checkout/testdata``, resolved
    here rather than relative to wherever pytest was invoked, which is what lets
    ``foundry_data.cases("cart_pricing.csv")`` read the example's CSV by its bare name; and
    ``cassette.dir`` becomes ``examples/checkout/cassettes``, where the committed cassettes live
    (AAP 0.4.6) and which is not created here.

    ``artifacts.dir`` keeps its default so ``artifacts/<run_id>/`` lands in the invocation
    directory, and every other key, ``sources`` included, stays the parent's. The nested models are
    copied rather than mutated: the parent instance is shared with the rest of the session.

    Args:
        foundry_config: The parent fixture's resolved configuration, session scoped and shared.
        echo_server: The base URL of the in-process echo server, or the replay placeholder.

    Returns:
        A copy of the parent configuration with the three example-specific values applied.

    Raises:
        foundry.core.exceptions.ConfigError: The parent's resolution fault propagates unchanged.
    """
    example_dir = Path(__file__).resolve().parent
    return foundry_config.model_copy(
        update={
            "http": foundry_config.http.model_copy(update={"base_url": echo_server}),
            "data": foundry_config.data.model_copy(update={"dir": example_dir / "testdata"}),
            "cassette": foundry_config.cassette.model_copy(
                update={"dir": example_dir / "cassettes"},
            ),
        }
    )
