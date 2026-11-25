"""The example project's own plumbing: the offline guard, a deterministic echo server and the
``foundry_config`` override that points Foundry at the example's directories.

Run it with ``pytest examples/checkout`` **from the repository root**: that is what makes the root
directory the repository root, so node ids read ``examples/checkout/test_*.py::test_*`` -- the form
the cassettes under ``cassettes/`` are named by -- and the run writes ``artifacts/<run_id>/``,
``results.json``, ``junit.xml`` and ``report.html`` into the invocation directory. Those outputs are
the plugin's work (AAP 0.4.2); this file supplies the guard, the server and the settings.

The guard (AAP 0.14.3) is installed at import, before any test module loads, over
``socket.getaddrinfo``, ``socket.socket.connect`` and ``socket.socket.connect_ex``. Resolution is
wrapped because a hostname fails there before ``connect`` is reached, so a connect-only guard would
measure nothing. A loopback target is counted and served; any other is counted and refused with an
``AssertionError`` naming it, before the syscall runs. The guard publishes the two names
``foundry.diagnostics.socket_guard`` reads -- ``FOUNDRY_SOCKET_GUARD`` and a live
``FOUNDRY_NON_LOOPBACK_ATTEMPTS`` -- and a second installation adopts the first one's counters.

The server is a stdlib ``ThreadingHTTPServer`` on ``127.0.0.1:0`` serving five routes from a daemon
thread, started in ``pytest_configure`` and stopped in ``pytest_unconfigure``. A replay run starts
none, because replay has to prove it opens no socket (AAP 0.4.7). The example ships no configuration
file (A9), so the same-name override of the public ``foundry_config`` fixture (AAP 0.4.3) supplies
``http.base_url``, ``data.dir``, ``cassette.dir`` and ``execution.retries``, while
``artifacts.dir`` keeps its default. The terminal summary carries exactly one line from this file,
``checkout: socket-guard non-loopback=<n> loopback=<n>``, which is how the end-to-end test observes
that no non-loopback connection was attempted.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import urlparse

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

    from _pytest.terminal import TerminalReporter

    from foundry.config.models import FoundryConfig

# --- The example's own locations --------------------------------------------
HERE: Final[Path] = Path(__file__).resolve().parent
"""This example's directory, the anchor of the two absolute directory settings below."""
TESTDATA_DIR: Final[Path] = HERE / "testdata"
"""Absolute, so a bare data-file name resolves here and not against the invocation directory."""
CASSETTE_DIR: Final[Path] = HERE / "cassettes"
"""Absolute, so the committed cassettes are found and a normal run never writes into them."""
REPLAY_BASE_URL: Final[str] = "http://127.0.0.1:8765"
"""What a replay run reports: no server runs there, and cassette matching ignores the host (A7)."""
BASE_URL_KEY: Final[pytest.StashKey[str]] = pytest.StashKey()
"""This process's base URL: the echo server's, or :data:`REPLAY_BASE_URL` in a replay run."""

_SERVER_KEY: Final[pytest.StashKey[ThreadingHTTPServer]] = pytest.StashKey()
_THREAD_KEY: Final[pytest.StashKey[threading.Thread]] = pytest.StashKey()
_INET_FAMILIES: Final[frozenset[int]] = frozenset({socket.AF_INET, socket.AF_INET6})
_LOOPBACK_NAMES: Final[frozenset[str]] = frozenset({"127.0.0.1", "::1", "localhost"})
_LOOPBACK_PREFIXES: Final[tuple[str, ...]] = ("127.", "::ffff:127.")
_GUARD_MARK: Final[str] = "_foundry_checkout_guard"
_MAX_BLOCKED: Final[int] = 20
_OFFLINE_RULE: Final[str] = "the example suite opens no non-loopback socket (AAP 0.14.3)"
_ECHO_HOST: Final[str] = "127.0.0.1"
_SHUTDOWN_TIMEOUT: Final[float] = 5.0
_ORDERS_PATH: Final[str] = "/orders/"
_RETRIES: Final[int] = 1
_HEALTH: Final[dict[str, str]] = {"service": "checkout", "status": "ok", "version": "0.1.0"}

# --- The loopback-only guard -------------------------------------------------


@dataclass
class CheckoutSocketGuard:
    """What the guard counted: served loopback calls, refused targets and the hosts behind them."""

    loopback_attempts: int = 0
    non_loopback_attempts: int = 0
    blocked_hosts: list[str] = field(default_factory=list)

    def __getattr__(self, name: str) -> Any:
        """Serve the two names socket_guard reads, live rather than as an import-time snapshot."""
        if name == "FOUNDRY_SOCKET_GUARD":
            return True
        if name == "FOUNDRY_NON_LOOPBACK_ATTEMPTS":
            return self.non_loopback_attempts
        raise AttributeError(name)


_GUARD = CheckoutSocketGuard()

FOUNDRY_SOCKET_GUARD = _GUARD
"""Announces the guard to :func:`foundry.diagnostics.socket_guard.detect_socket_guard`."""


def __getattr__(name: str) -> Any:
    """Serve ``FOUNDRY_NON_LOOPBACK_ATTEMPTS`` live, so the count is read as it stands now."""
    if name == "FOUNDRY_NON_LOOPBACK_ATTEMPTS":
        return _GUARD.non_loopback_attempts
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _host_text(host: object) -> str | None:
    """Return a host as text when it is a string or a byte string, else ``None``."""
    if isinstance(host, str):
        return host
    if isinstance(host, bytes):
        return host.decode("ascii", "ignore") or None
    return None


def _count(host: object) -> None:
    """Count a loopback target, or count and refuse any other with the reason it is refused."""
    text = _host_text(host)
    if text is None or text in _LOOPBACK_NAMES or text.startswith(_LOOPBACK_PREFIXES):
        _GUARD.loopback_attempts += 1
        return
    _GUARD.non_loopback_attempts += 1
    if len(_GUARD.blocked_hosts) < _MAX_BLOCKED:
        _GUARD.blocked_hosts.append(text)
    raise AssertionError(
        f"examples/checkout: refused the non-loopback host {text!r}: {_OFFLINE_RULE}. Serve the "
        "request from the in-process echo server on 127.0.0.1, or replay it from cassettes/."
    )


def _check(family: int, address: object) -> None:
    """Police a connect target: only an INET address is judged, and only its host."""
    if family in _INET_FAMILIES:
        _count(address[0] if isinstance(address, tuple) and address else None)


# Saved before the patching below, so the wrappers delegate without recursing into themselves.
_ORIGINAL_GETADDRINFO = socket.getaddrinfo
_ORIGINAL_CONNECT = socket.socket.connect
_ORIGINAL_CONNECT_EX = socket.socket.connect_ex


def _guarded_getaddrinfo(
    host: object, port: object, family: int = 0, type: int = 0, proto: int = 0, flags: int = 0
) -> list[tuple[int, int, int, str, tuple[Any, ...]]]:
    """Resolve a loopback host only: any other is refused before the resolver opens a socket."""
    if host is not None:
        _count(host)
    return _ORIGINAL_GETADDRINFO(host, port, family, type, proto, flags)


def _guarded_connect(self: socket.socket, address: object) -> None:
    """Connect to a loopback target only, refusing any other before the syscall runs."""
    _check(self.family, address)
    _ORIGINAL_CONNECT(self, address)


def _guarded_connect_ex(self: socket.socket, address: object) -> int:
    """Behave as ``socket.socket.connect_ex`` for a loopback target, and refuse any other."""
    _check(self.family, address)
    return int(_ORIGINAL_CONNECT_EX(self, address))


def _install_guard() -> None:
    """Patch :mod:`socket` once per process; a second import adopts the first one's counters."""
    global FOUNDRY_SOCKET_GUARD, _GUARD
    adopted = getattr(socket.socket.connect, _GUARD_MARK, None)
    if adopted is not None:
        _GUARD = FOUNDRY_SOCKET_GUARD = adopted
        return
    for wrapper in (_guarded_getaddrinfo, _guarded_connect, _guarded_connect_ex):
        setattr(wrapper, _GUARD_MARK, _GUARD)
    socket.getaddrinfo = _guarded_getaddrinfo
    socket.socket.connect = _guarded_connect
    socket.socket.connect_ex = _guarded_connect_ex


_install_guard()

# --- The in-process echo server ----------------------------------------------


def _canonical(document: object) -> bytes:
    """Serialise ``document`` byte-stably: sorted keys, no spaces, UTF-8."""
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")


class CheckoutEchoHandler(BaseHTTPRequestHandler):
    """Serve the example's five deterministic routes; nothing here reads a clock or a counter."""

    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        self._dispatch()

    def do_POST(self) -> None:
        self._dispatch()

    def do_PUT(self) -> None:
        self._dispatch()

    def do_PATCH(self) -> None:
        self._dispatch()

    def do_DELETE(self) -> None:
        self._dispatch()

    def log_message(self, format: str, *args: object) -> None:
        """Write no access log, so server chatter never reaches pytest's captured output."""

    def _dispatch(self) -> None:
        """Route one request, reading its body first so a kept-alive connection stays in step."""
        raw = self._read_body()
        path = urlparse(self.path).path
        if path == "/health" and self.command == "GET":
            self._send_json(200, dict(_HEALTH))
        elif path == "/orders" and self.command == "POST":
            self._create_order(raw)
        elif path == "/private/profile" and self.command == "GET":
            self._send_json(401, {"error": "unauthorized", "realm": "checkout"})
        elif path.startswith(_ORDERS_PATH) and self.command == "GET":
            self._send_json(200, _stored_order(path[len(_ORDERS_PATH) :]))
        else:
            self._send_json(404, {"error": "not_found", "path": path})

    def _read_body(self) -> bytes:
        """Return the request body, read exactly as ``Content-Length`` states it."""
        raw_length = self.headers.get("Content-Length", "")
        length = int(raw_length) if raw_length.isdigit() else 0
        return self.rfile.read(length) if length else b""

    def _send_json(self, status: int, document: object) -> None:
        """Write the response with no ``Date`` or ``Server`` header, so cassettes stay stable."""
        body = _canonical(document)
        self.send_response_only(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _create_order(self, raw: bytes) -> None:
        """Answer ``POST /orders`` from the body alone: a hash-derived id and the summed total."""
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = None
        items = payload.get("items") if isinstance(payload, dict) else None
        total = _order_total(items)
        if not isinstance(payload, dict) or total is None:
            self._send_json(400, {"error": "invalid_order"})
            return
        self._send_json(
            201,
            {
                "currency": payload.get("currency"),
                "items": items,
                "order_id": f"ord-{hashlib.sha256(_canonical(payload)).hexdigest()[:12]}",
                "status": "created",
                "total_cents": total,
            },
        )


def _order_total(items: object) -> int | None:
    """Return ``sum(quantity * unit_price_cents)`` over ``items``, or ``None`` when unusable."""
    if not isinstance(items, list):
        return None
    total = 0
    for item in items:
        if not isinstance(item, dict):
            return None
        quantity, price = item.get("quantity"), item.get("unit_price_cents")
        if not isinstance(quantity, int) or not isinstance(price, int):
            return None
        total += quantity * price
    return total


def _stored_order(order_id: str) -> dict[str, object]:
    """Return the fixed document ``GET /orders/<id>`` answers with, echoing ``order_id`` back."""
    return {
        "currency": "EUR",
        "items": [],
        "order_id": order_id,
        "status": "created",
        "total_cents": 0,
    }


# --- The server's lifetime ---------------------------------------------------


def _replay_requested(config: pytest.Config) -> bool:
    """Return whether this run replays, read without resolving project configuration.

    Resolution belongs to the plugin and hook ordering is not guaranteed, so the three inputs an
    operator sets replay with are read directly: ``--foundry-replay``, the generic
    ``--foundry-cassette.mode replay`` override and ``FOUNDRY_CASSETTE__MODE``.
    """
    if config.getoption("--foundry-replay", default=False):
        return True
    mode = config.getoption("--foundry-cassette.mode", default=None)
    if isinstance(mode, str) and mode.strip().lower() == "replay":
        return True
    return os.environ.get("FOUNDRY_CASSETTE__MODE", "").strip().lower() == "replay"


def pytest_configure(config: pytest.Config) -> None:
    """Start the echo server for a live run, or record the replay placeholder and bind nothing."""
    if _replay_requested(config):
        config.stash[BASE_URL_KEY] = REPLAY_BASE_URL
        return
    server = ThreadingHTTPServer((_ECHO_HOST, 0), CheckoutEchoHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, name="checkout-echo", daemon=True)
    thread.start()
    config.stash[_SERVER_KEY] = server
    config.stash[_THREAD_KEY] = thread
    config.stash[BASE_URL_KEY] = f"http://{_ECHO_HOST}:{server.server_address[1]}"


def pytest_unconfigure(config: pytest.Config) -> None:
    """Stop the server and join its thread, so no port and no thread outlives the session."""
    server = config.stash.get(_SERVER_KEY, None)
    if server is None:
        return
    server.shutdown()
    server.server_close()
    thread = config.stash.get(_THREAD_KEY, None)
    if thread is not None:
        thread.join(timeout=_SHUTDOWN_TIMEOUT)


# --- The example's fixtures and settings -------------------------------------


@pytest.fixture(scope="session")
def checkout_base_url(request: pytest.FixtureRequest) -> str:
    """Session scope: the echo server's URL for this run, or the replay placeholder."""
    return request.config.stash.get(BASE_URL_KEY, REPLAY_BASE_URL)


@pytest.fixture(scope="session", autouse=True)
def _offline_guard() -> Iterator[None]:
    """Fail the session, rather than warn, if any non-loopback connection was attempted."""
    yield
    assert _GUARD.non_loopback_attempts == 0, (
        f"examples/checkout: {_GUARD.non_loopback_attempts} non-loopback connection attempt(s): "
        f"{_GUARD.blocked_hosts}"
    )


@pytest.fixture(scope="session")
def foundry_config(foundry_config: FoundryConfig, checkout_base_url: str) -> FoundryConfig:
    """Return the run's configuration with this example's base URL, directories and retries in it.

    Pytest's documented same-name override (AAP 0.4.3): the parameter of that name is the public
    session fixture, one level up. ``http.base_url`` becomes the echo server's URL, ``data.dir`` and
    ``cassette.dir`` become this example's absolute directories, and ``execution.retries`` becomes
    one repeat, which exercises the retry driver in the documented run (A2). ``artifacts.dir`` keeps
    its default so ``artifacts/<run_id>/`` lands in the invocation directory, and every other key
    -- ``cassette.mode``, ``reporting.*``, ``logging.*``, ``secrets.*`` and ``profile`` -- stays the
    parent's. The parent's nested models are copied, never mutated, because it is shared.

    Foundry's five layers are the only supported provenance, and a fixture override is none of them,
    so the four injected keys are attributed to the ``file`` layer naming this conftest: the project
    file that supplied them, rather than the field defaults the parent recorded.
    """
    from foundry.config.layers import ConfigLayer
    from foundry.config.sources import ConfigSource

    origin = ConfigSource(layer=ConfigLayer.FILE, detail=str(Path(__file__).resolve()))
    sources = dict(foundry_config.sources)
    for key in ("cassette.dir", "data.dir", "execution.retries", "http.base_url"):
        sources[key] = origin
    updated = foundry_config.model_copy(
        update={
            "http": foundry_config.http.model_copy(update={"base_url": checkout_base_url}),
            "data": foundry_config.data.model_copy(update={"dir": TESTDATA_DIR}),
            "cassette": foundry_config.cassette.model_copy(update={"dir": CASSETTE_DIR}),
            "execution": foundry_config.execution.model_copy(update={"retries": _RETRIES}),
        }
    )
    return updated.with_sources(sources)


def pytest_terminal_summary(
    terminalreporter: TerminalReporter, exitstatus: int, config: pytest.Config
) -> None:
    """Print this file's one line: the guard's counters, which the offline proof matches on."""
    terminalreporter.write_line(
        f"checkout: socket-guard non-loopback={_GUARD.non_loopback_attempts} "
        f"loopback={_GUARD.loopback_attempts}"
    )
