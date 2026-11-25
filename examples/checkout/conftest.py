"""The example project's own plumbing: the offline guard, a deterministic echo server and the
``foundry_config`` override that points Foundry at the example's directories.

Run it with ``pytest examples/checkout`` **from the repository root**: that is what makes the root
directory the repository root, so node ids read ``examples/checkout/test_*.py::test_*`` -- the form
the cassettes under ``cassettes/`` are named by -- and the run writes ``artifacts/<run_id>/``,
``results.json``, ``junit.xml`` and ``report.html`` into the invocation directory. Those outputs are
the plugin's work (AAP 0.4.2); this file supplies the guard, the server and the settings.

The guard (AAP 0.14.3) is installed at import, before any test module loads, over
``socket.getaddrinfo``, ``socket.socket.connect`` and ``socket.socket.connect_ex``; resolution is
wrapped because a hostname fails there before ``connect`` is reached. A loopback target -- a
``127.``/``::1`` address, ``localhost`` or a blank host -- is counted and served, and any other is
counted and refused with an ``AssertionError`` naming it, before the syscall runs. The guard
publishes the two names ``foundry.diagnostics.socket_guard`` reads, and a second installation
adopts the first one's counters.

The server is a stdlib ``ThreadingHTTPServer`` on ``127.0.0.1:0`` serving four paths --
``GET /health``, ``POST /orders``, ``GET /private/profile`` and ``GET /orders/<id>`` -- plus a JSON
404 for anything else, on a daemon thread from ``pytest_configure`` to ``pytest_unconfigure``. A
replay run starts none: replay must prove it opens no socket (AAP 0.4.7). No configuration file
ships (A9), so the same-name ``foundry_config`` override (AAP 0.4.3) supplies ``http.base_url``, the
``bearer`` strategy against an environment-variable name, ``data.dir`` and ``cassette.dir``.

Because ``cassette.dir`` is the committed directory, **a recording run has to be aimed at a copy**:
``pytest examples/checkout --foundry-record --foundry-cassette.dir <dir>``. A bare
``--foundry-record`` would rewrite the seven tracked fixtures under ``cassettes/`` in place --
``recorded_at`` becomes the current time and the rewrite shows up in git -- so record into a scratch
directory, review what was recorded there, and copy the documents in.

The retry budget is the one setting that override cannot supply: the plugin resolves the run's
configuration once, in ``pytest_configure``, and the retry driver reads the budget from it
(AAP 0.14.2), while a fixture does not exist until an item is collected -- so ``execution.retries``
is stated in the environment layer at import, before that hook, which makes the retry test's first
attempt really be repeated in the documented flag-free run (criterion 3, assumption A2).

The terminal summary carries exactly one line from this file, ``checkout: socket-guard
non-loopback=<n> loopback=<n>``, which is how the end-to-end test observes that no non-loopback
connection was attempted.
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
from urllib.parse import parse_qsl, urlparse

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
"""Absolute, so discovery and replay find the committed cassettes; recording is aimed elsewhere.

The module docstring gives the exact form -- ``--foundry-cassette.dir <dir>`` -- because recording
into this directory in place would rewrite the tracked fixtures.
"""
REPLAY_BASE_URL: Final[str] = "http://127.0.0.1:8765"
"""What a replay run reports: no server runs there, and cassette matching ignores the host (A7)."""
BASE_URL_KEY: Final[pytest.StashKey[str]] = pytest.StashKey()
"""This process's base URL: the echo server's, or :data:`REPLAY_BASE_URL` in a replay run."""

_OPERATOR_LAYERS: Final[frozenset[str]] = frozenset({"cli", "env"})
"""The two layers that outrank this file's own directory settings (AAP 0.4.5).

:data:`TESTDATA_DIR` and :data:`CASSETTE_DIR` are defaults this example supplies, not values an
operator chose, so an explicit ``--foundry-cassette.dir`` / ``--foundry-data.dir`` or
``FOUNDRY_CASSETTE__DIR`` / ``FOUNDRY_DATA__DIR`` wins over them. Without that deference a record
run pointed at a temporary directory would rewrite the committed cassettes under ``cassettes/``
instead, which is why the end-to-end proof of criterion 8 can record the example at all.
"""

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
"""Repeats the retry test earns after its first attempt: one repeat is two attempts (A2)."""
_RETRIES_ENV: Final[str] = "FOUNDRY_EXECUTION__RETRIES"
"""The environment spelling of ``execution.retries``: ``FOUNDRY_`` prefix, ``__`` nesting."""
_CREDENTIAL_ENV: Final[str] = "CHECKOUT_API_TOKEN"
"""The variable ``http.auth.token_env`` names: the configuration carries the name, never a value."""
_DEMO_CREDENTIAL: Final[str] = "checkout-demo-token-" + hashlib.sha256(b"demo").hexdigest()[:6]
"""The fake token the echo server accepts: assembled at import from a fixed wording, so it is the
same in every process; it grants nothing and exists to prove the auth stage."""
_HEALTH: Final[dict[str, str]] = {"service": "checkout", "status": "ok", "version": "0.1.0"}

# Stated here, at import, because this is the last moment a layer can reach the run's retry budget:
# the plugin resolves the configuration in ``pytest_configure`` and the driver reads the budget from
# that one resolution, so a fixture -- the ``foundry_config`` override below included -- comes too
# late (AAP 0.14.2). Deliberately not ``setdefault``: the documented, flag-free run has to show the
# repeat whatever the operator's shell carries, or the example's outcome would depend on it.
os.environ[_RETRIES_ENV] = str(_RETRIES)

# ``foundry_api`` resolves it once per HTTP session; the token above is masked at every sink.
os.environ[_CREDENTIAL_ENV] = _DEMO_CREDENTIAL

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
    """Return a host as text, or ``None`` when it names no host: another type, or no text."""
    if isinstance(host, str):
        return host or None
    if isinstance(host, bytes):
        return host.decode("ascii", "ignore") or None
    return None


def _count(host: object) -> None:
    """Count a loopback target, or count and refuse any other with the reason it is refused.

    A host that names no host -- ``None``, ``""`` or ``b""`` -- counts as loopback: it cannot name a
    remote machine, so the guard is counted out of the way and the resolver's own answer stands. The
    two blank spellings have to agree, or only one of them is a false positive.
    """
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
    """Serve ``GET /health``, ``POST /orders``, ``GET /private/profile`` and ``GET /orders/<id>``,
    plus a JSON 404 for anything else; nothing here reads a clock or a counter.
    """

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
            self._send_json(200, {**_HEALTH, "query": _query_pairs(self.path)})
        elif path == "/orders" and self.command == "POST":
            self._create_order(raw)
        elif path == "/private/profile" and self.command == "GET":
            self._send_profile()
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

    def _send_profile(self) -> None:
        """Answer ``GET /private/profile`` from the credential the request presents.

        No ``Authorization`` header, or a blank one, is ``401``: nothing was presented. Any other
        value than the one the configured strategy applies is ``403``: a credential was presented
        and it is wrong, which is what a per-request header overriding the strategy's produces. The
        expected value is ``200``, and that reply is the proof the auth stage resolved a header and
        sent it here.
        """
        presented = self.headers.get("Authorization", "")
        if not presented:
            self._send_json(401, {"error": "unauthorized", "realm": "checkout"})
            return
        if presented != f"Bearer {_DEMO_CREDENTIAL}":
            self._send_json(403, {"error": "forbidden", "realm": "checkout"})
            return
        self._send_json(200, {"realm": "checkout", "user": "checkout-demo"})

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


def _query_pairs(target: str) -> list[list[str]]:
    """Return the target's query parameters as ``[name, value]`` pairs sorted by name then value.

    Order is deliberately dropped, so two requests carrying the same parameters in two different
    orders answer the same document -- which a recorded cassette then shows, and still replays.
    """
    pairs = parse_qsl(urlparse(target).query, keep_blank_values=True)
    return [[name, value] for name, value in sorted(pairs)]


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


def _operator_set(foundry_config: FoundryConfig, key: str) -> bool:
    """Return whether the environment or the command line set ``key`` explicitly (AAP 0.4.5)."""
    source = foundry_config.sources.get(key)
    return source is not None and source.layer in _OPERATOR_LAYERS


@pytest.fixture(scope="session")
def foundry_config(foundry_config: FoundryConfig, checkout_base_url: str) -> FoundryConfig:
    """Return the run's configuration with this example's base URL and directories in it.

    Pytest's documented same-name override (AAP 0.4.3): the parameter of that name is the public
    session fixture, one level up. ``http.base_url`` becomes the echo server's URL, and ``data.dir``
    and ``cassette.dir`` become this example's absolute directories -- unless the environment or the
    command line set one of those two itself, which outranks this file (AAP 0.4.5) and is how a
    record run aimed at a temporary cassette directory stays out of the committed fixtures.
    ``artifacts.dir`` keeps its default so ``artifacts/<run_id>/`` lands in the invocation
    directory, and every other key -- ``execution.*`` (the retry budget this file states at
    import), ``cassette.mode``, ``reporting.*``, ``logging.*``, ``secrets.*`` and ``profile`` --
    stays the parent's. The auth strategy is configured from an environment-variable name as well:
    ``http.auth.strategy`` becomes ``bearer`` and ``http.auth.token_env`` names the variable holding
    the demo token, so the auth stage runs and no credential value enters a configuration model. The
    parent's nested models are copied, never mutated, because the parent is shared.

    Foundry's five layers are the only supported provenance, and a fixture override is none of them,
    so the keys this file supplies are attributed to the ``file`` layer naming this conftest: the
    project file that supplied them, rather than the field defaults the parent recorded. A key it
    deferred keeps the parent's provenance, which already names the variable or option that set it,
    and ``execution.retries`` is not injected at all: the environment layer supplied it.
    """
    from foundry.config.layers import ConfigLayer
    from foundry.config.sources import ConfigSource

    origin = ConfigSource(layer=ConfigLayer.FILE, detail=str(Path(__file__).resolve()))
    sources = dict(foundry_config.sources)
    update: dict[str, object] = {}
    if not _operator_set(foundry_config, "cassette.dir"):
        sources["cassette.dir"] = origin
        update["cassette"] = foundry_config.cassette.model_copy(update={"dir": CASSETTE_DIR})
    if not _operator_set(foundry_config, "data.dir"):
        sources["data.dir"] = origin
        update["data"] = foundry_config.data.model_copy(update={"dir": TESTDATA_DIR})
    sources["http.base_url"] = origin
    sources["http.auth.strategy"] = origin
    sources["http.auth.token_env"] = origin
    update["http"] = foundry_config.http.model_copy(
        update={
            "base_url": checkout_base_url,
            "auth": foundry_config.http.auth.model_copy(
                update={"strategy": "bearer", "token_env": _CREDENTIAL_ENV}
            ),
        }
    )
    return foundry_config.model_copy(update=update).with_sources(sources)


def pytest_terminal_summary(
    terminalreporter: TerminalReporter, exitstatus: int, config: pytest.Config
) -> None:
    """Print this file's one line: the guard's counters, which the offline proof matches on."""
    terminalreporter.write_line(
        f"checkout: socket-guard non-loopback={_GUARD.non_loopback_attempts} "
        f"loopback={_GUARD.loopback_attempts}"
    )
