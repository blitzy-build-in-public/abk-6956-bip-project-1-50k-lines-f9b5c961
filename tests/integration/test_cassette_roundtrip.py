"""Record then replay through a loopback echo server: criteria 7 and 8 of AAP 0.14.1.

The contract-level proof of record and replay, placed here by AAP 0.13.1 because it needs a real
``http.server``, real cassette files and real pytest sessions. The record arm asserts the document
AAP 0.4.6 fixes -- three top-level keys, two halves per interaction, a recomputed ``body_sha256``,
``null`` for a bodyless request and no host field anywhere (assumption A7) -- and requirement I4:
a sentinel bearer token reaches no byte of the cassette while the non-secret static header does.
The replay arms run the same test with the peer stopped, a different closed port and a
``conftest.py`` refusing *every* ``connect``, proving AAP 0.7.3's order-insensitive query rule,
``FoundryResponse.source == "cassette"`` and the typed ``CassetteMissError`` of criterion 8.
Every produced file lands inside ``tmp_path``; nothing writes into the checkout.
"""

from __future__ import annotations

import hashlib
import json
import re
import socket
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socketserver import TCPServer
from typing import TYPE_CHECKING, Final, Protocol
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest

from foundry.cassette.paths import cassette_path
from foundry.core.paths import sanitize_nodeid

if TYPE_CHECKING:
    import subprocess
    from collections.abc import Callable, Iterator

__all__: Final[tuple[str, ...]] = ("EchoServer", "RecordedRun", "echo_server", "recorded_run")

# --- The fixed facts of the scenario --------------------------------------

TEST_MODULE: Final[str] = "test_api.py"
"""The generated test module's name, which both projects must share: it is the cassette's name."""

TEST_FUNCTION: Final[str] = "test_echo"
"""The test function's name, fixed for the same reason: the node id has to match across projects."""

NODEID: Final[str] = f"{TEST_MODULE}::{TEST_FUNCTION}"
"""The node id both arms run, and the only key the recorded cassette is stored under."""

ECHO_PATH: Final[str] = "/echo"
"""The path the echo server answers like any other, and the only path the record arm uses."""

MISSING_PATH: Final[str] = "/not-recorded"
"""A path the record arm never sends, so the miss arm has something no interaction matches."""

RECORD_QUERY: Final[tuple[tuple[str, str], ...]] = (("page", "2"), ("limit", "10"))
"""The query the record arm sends, unsorted: the document stores these pairs sorted by name."""

REPLAY_QUERY: Final[tuple[tuple[str, str], ...]] = (("limit", "10"), ("page", "2"))
"""The same two parameters reversed: equality is by value, not position (AAP 0.7.3)."""

STORED_QUERY: Final[tuple[tuple[str, str], ...]] = (("limit", "10"), ("page", "2"))
"""The order the document stores, sorted by name whatever order the client sent (AAP 0.4.6)."""

POST_QUERY: Final[tuple[tuple[str, str], ...]] = (("trace", "abc"),)
"""The posted request's query, so its record differs from the bodyless GET's in two fields."""

POST_BODY: Final[dict[str, object]] = {"message": "hello"}
"""The JSON body the POST sends; its bytes are what ``body_sha256`` hashes."""

_BODY_PROBE_URL: Final[str] = "http://127.0.0.1/"
"""A loopback URL used only to have ``httpx`` encode :data:`POST_BODY`; nothing is sent to it."""

TOKEN_ENV: Final[str] = "ECHO_TEST_BEARER_TOKEN"
"""The variable ``http.auth.token_env`` names, carrying no ``FOUNDRY_`` prefix on purpose: the
suite strips that prefix from a child's environment, and this credential must reach the child."""

TOKEN_SENTINEL: Final[str] = "sentinel-bearer-token-5f3a9c41d7"
"""An obviously fake credential: the value the cassette must never hold (requirement I4)."""

STATIC_HEADER_NAME: Final[str] = "x-foundry-client"
STATIC_HEADER_VALUE: Final[str] = "foundry-cassette-roundtrip"
"""A non-secret static header from ``http.headers``, which ``headers_subset`` must keep (I4)."""

REDACTED: Final[str] = "***"
"""The marker the redaction engine substitutes, as AAP 0.4.5's ``secrets.*`` contract states."""

_RUN_TIMEOUT_S: Final[float] = 180.0
"""How long one child pytest run may take before the test fails instead of hanging."""

_JOIN_TIMEOUT_S: Final[float] = 15.0
"""How long the server thread may run on after ``shutdown`` before the process is left to it."""

_SHA256_HEX: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{64}")
"""The only shape a recorded ``body_sha256`` may take: lowercase hexadecimal, 64 characters."""

_HOST_KEYS: Final[frozenset[str]] = frozenset(
    {"authority", "base-url", "base_url", "host", "hostname", "origin", "port", "scheme", "url"}
)
"""Key names that would pin a recording to one address, which assumption A7 forbids it to hold."""


# --- Small pure helpers ----------------------------------------------------


def _pairs_literal(pairs: tuple[tuple[str, str], ...]) -> str:
    """Return ``pairs`` as the Python literal a generated test module carries."""
    return "[" + ", ".join(f"({name!r}, {value!r})" for name, value in pairs) + "]"


def _json_body_bytes(payload: dict[str, object]) -> bytes:
    """Return the bytes ``httpx`` puts on the wire for ``json=payload``.

    Built through ``httpx`` itself rather than by restating its encoding, so the digest below is a
    recomputation of what the client actually sent and not a second guess at how it serialises
    (AAP 0.4.6's ``body_sha256`` is the hash of the request as it went out).
    """
    return httpx.Request("POST", _BODY_PROBE_URL, json=payload).content


def _sha256(body: bytes) -> str:
    """Return the lowercase hexadecimal SHA-256 of ``body``."""
    return hashlib.sha256(body).hexdigest()


def _output(completed: subprocess.CompletedProcess[str]) -> str:
    """Return a child run's stdout and stderr together, for an assertion's message."""
    return f"{completed.stdout}\n{completed.stderr}"


def _all_keys(value: object) -> Iterator[str]:
    """Yield every key name inside a JSON-shaped value, at any depth (assumption A7's scan)."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _all_keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _all_keys(item)


def _result_records(results_path: Path) -> list[dict[str, object]]:
    """Return the ``result`` elements of a ``results.json`` document, in document order."""
    elements = json.loads(results_path.read_text(encoding="utf-8"))
    assert isinstance(elements, list), f"{results_path} does not hold a JSON array"
    return [element for element in elements if element.get("record_type") == "result"]


def _assert_within(path: Path, root: Path) -> None:
    """Assert ``path`` resolves inside ``root``, so no arm wrote into the repository checkout.

    AAP 0.14.3's rule is asserted from the paths a run reports rather than by shelling out to
    ``git status``: a produced path outside ``tmp_path`` is the whole failure.
    """
    resolved_root = root.resolve()
    resolved = path.resolve()
    assert resolved == resolved_root or resolved_root in resolved.parents, (
        f"{resolved} lies outside the test's temporary directory {resolved_root}"
    )


def _arm_options(*, directory: Path, cassette_dir: Path, base_url: str) -> list[str]:
    """Return the options every arm passes: three output paths inside ``directory`` and a base URL.

    ``--foundry-<dotted.key>`` is the generic override AAP 0.4.4 registers for every scalar key, so
    all three exist as written; keeping every path inside ``directory`` is what holds
    ``results.json``, ``junit.xml``, ``report.html`` and ``artifacts/`` out of the checkout.
    """
    return [
        f"--foundry-cassette.dir={cassette_dir}",
        f"--foundry-artifacts.dir={directory / 'artifacts'}",
        f"--foundry-json-report={directory / 'results.json'}",
        f"--foundry-http.base_url={base_url}",
    ]


def _closed_loopback_base_url() -> str:
    """Return a base URL on a loopback port nothing listens on, differing from the recorded one.

    Claimed from the kernel and released at once, while the echo server still holds its own port.
    A fallback to the live transport would be refused by the operating system, and the replay
    project's guard refuses it first; the port differs from the recording's, which is the
    port-independence assumption A7 claims.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    return f"http://127.0.0.1:{port}"


# --- The loopback echo server ---------------------------------------------


class _Guard(Protocol):
    """The one member read from the suite's guard in ``tests/conftest.py``: its attempt counter."""

    non_loopback_attempts: int
    """Loopback-refused connection attempts this process has recorded so far."""


class EchoServer:
    """A loopback echo server the record arm reaches and the replay arm must be unable to.

    ``port`` and ``base_url`` stay readable after :meth:`stop`, so a test can still name the address
    the recording used and compare it with the different, closed port its replay addresses.
    """

    __slots__ = ("_server", "_thread")

    def __init__(self, server: ThreadingHTTPServer, thread: threading.Thread) -> None:
        """Bind the wrapper to a started server and to the thread serving it."""
        self._server = server
        self._thread = thread

    @property
    def port(self) -> int:
        """Return the port the kernel assigned, readable after :meth:`stop`."""
        return int(self._server.server_address[1])

    @property
    def base_url(self) -> str:
        """Return the server's absolute base URL: loopback, on a kernel-assigned port."""
        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        """Stop listening and join the serving thread, so nothing listens past this call.

        Idempotent, so the fixture's teardown runs it unconditionally (AAP 0.14.3).
        """
        if self._thread.is_alive():
            self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=_JOIN_TIMEOUT_S)


class _QuietEchoServer(ThreadingHTTPServer):
    """A threaded HTTP server that never resolves its own address to a fully-qualified name.

    ``HTTPServer.server_bind`` calls ``socket.getfqdn`` on the bound host, a reverse lookup the
    loopback-only guard does not police and the offline rule cannot afford (AAP 0.14.3).
    """

    def server_bind(self) -> None:
        """Bind the socket and record the address as given, skipping the reverse lookup."""
        TCPServer.server_bind(self)
        self.server_name = str(self.server_address[0])
        self.server_port = int(self.server_address[1])


class _EchoHandler(BaseHTTPRequestHandler):
    """Answer a request with the method, path, sorted query pairs, received headers and body."""

    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        """Answer a GET with the echo document."""
        self._respond()

    def do_POST(self) -> None:
        """Answer a POST with the echo document."""
        self._respond()

    def log_message(self, format_string: str, *args: object) -> None:
        """Silence the access log, whose lines would otherwise reach the run's captured stderr."""

    def _respond(self) -> None:
        """Write the echo document as JSON, with the two headers a reader needs.

        ``send_response_only`` rather than ``send_response``: the latter adds a ``Date`` header,
        and a capture that carried the wall clock would make two runs of one recording differ.
        """
        payload = json.dumps(_echo_payload(self), sort_keys=True).encode("utf-8")
        self.send_response_only(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def _echo_payload(handler: BaseHTTPRequestHandler) -> dict[str, object]:
    """Return the request as the peer saw it: method, path, sorted query pairs, headers and body."""
    target = urlsplit(handler.path)
    return {
        "method": handler.command,
        "path": target.path,
        "query": [
            [name, value] for name, value in sorted(parse_qsl(target.query, keep_blank_values=True))
        ],
        "headers": {name.casefold(): handler.headers[name] for name in handler.headers},
        "body": _decoded_body(handler),
    }


def _decoded_body(handler: BaseHTTPRequestHandler) -> object:
    """Return the request body decoded as JSON, as text when it is not JSON, or ``None``."""
    declared = handler.headers.get("Content-Length")
    raw = handler.rfile.read(int(declared)) if declared else b""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return raw.decode("utf-8", errors="replace")


@pytest.fixture
def echo_server() -> Iterator[EchoServer]:
    """Serve the echo document from ``127.0.0.1`` on a kernel-assigned port, stopped at teardown.

    Port ``0`` so two workers never contend for a port; stopped, closed and joined in ``finally``.
    """
    server = _QuietEchoServer(("127.0.0.1", 0), _EchoHandler)
    thread = threading.Thread(target=server.serve_forever, name="foundry-echo-server", daemon=True)
    thread.start()
    running = EchoServer(server, thread)
    try:
        yield running
    finally:
        running.stop()


# --- The three throwaway projects -----------------------------------------

_RECORD_TOML: Final[str] = f"""
[http]
timeout = 5.0

[http.auth]
strategy = "bearer"
token_env = "{TOKEN_ENV}"

[http.headers]
"{STATIC_HEADER_NAME}" = "{STATIC_HEADER_VALUE}"
"""
"""The record project's configuration file.

``http.auth.strategy`` selects the bearer strategy, whose credential the session resolves from the
variable ``http.auth.token_env`` *names* -- no value lives here (AAP 0.4.5). ``http.headers`` is a
mapping, so only the file layer can carry the static header.
"""

_RECORD_MODULE: Final[str] = f'''
"""Record one test's two exchanges against the loopback echo server."""

from foundry.http import FoundrySession


def test_echo(foundry_api: FoundrySession) -> None:
    """Issue a bodyless GET and a JSON POST, and assert the live peer answered both."""
    listing = foundry_api.get({ECHO_PATH!r}, params={_pairs_literal(RECORD_QUERY)})
    assert listing.status_code == 200, listing.text
    assert listing.source == "live"
    assert listing.json()["query"] == [["limit", "10"], ["page", "2"]]

    created = foundry_api.post(
        {ECHO_PATH!r}, params={_pairs_literal(POST_QUERY)}, json={POST_BODY!r}
    )
    assert created.status_code == 200, created.text
    assert created.source == "live"
    assert created.json()["body"] == {POST_BODY!r}
'''
"""The record project's test module: one test, two exchanges, the same node id the arms replay."""

_REPLAY_MODULE: Final[str] = f'''
"""Replay the recorded exchanges with the peer stopped and every connect refused."""

from foundry.http import FoundrySession


def test_echo(foundry_api: FoundrySession) -> None:
    """Re-send the same two requests, query reordered, and read the recording back."""
    created = foundry_api.post(
        {ECHO_PATH!r}, params={_pairs_literal(POST_QUERY)}, json={POST_BODY!r}
    )
    listing = foundry_api.get({ECHO_PATH!r}, params={_pairs_literal(REPLAY_QUERY)})

    assert created.source == "cassette"
    assert created.status_code == 200
    assert created.json()["method"] == "POST"
    assert created.json()["body"] == {POST_BODY!r}

    assert listing.source == "cassette"
    assert listing.status_code == 200
    assert listing.json()["method"] == "GET"
    assert listing.json()["path"] == {ECHO_PATH!r}
    assert listing.json()["query"] == [["limit", "10"], ["page", "2"]]
'''
"""The replay project's test module: same node id, opposite request order, reordered parameters."""

_MISS_MODULE: Final[str] = f'''"""Ask for a path no cassette holds."""

from foundry.http import FoundrySession


def test_echo(foundry_api: FoundrySession) -> None:
    """Request a path the recording does not hold, which must raise the typed miss."""
    foundry_api.get({MISSING_PATH!r})
'''
"""The miss project's test module: the recorded node id, a request the cassette never captured."""

_ALL_CONNECT_GUARD: Final[str] = '''
"""Refuse every socket connection in this process, so a live attempt fails the run.

A refused connection is indistinguishable from a stopped server by its symptom alone. Any
``connect`` or ``connect_ex`` at all -- loopback included -- raises inside the test that attempted
it, so a replay that fell back to the live transport fails loudly instead of passing by luck.
"""

from __future__ import annotations

import socket


def _refuse(address: object) -> None:
    """Raise for any target, so nothing in this process can reach a peer."""
    raise RuntimeError(
        f"the replay project attempted a socket connection to {address!r}: a replay answers from "
        "the cassette alone (AAP 0.2.1)"
    )


def _guarded_connect(self: socket.socket, address: object) -> None:
    """Refuse every ``connect``, whatever the family and whatever the target."""
    _refuse(address)


def _guarded_connect_ex(self: socket.socket, address: object) -> int:
    """Refuse every ``connect_ex``, whatever the family and whatever the target."""
    _refuse(address)


socket.socket.connect = _guarded_connect
socket.socket.connect_ex = _guarded_connect_ex
'''
"""The ``conftest.py`` both replay projects carry: the decisive zero-socket proof (criterion 8)."""


# --- The record arm --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RecordedRun:
    """Everything one record arm produced, so both tests assert against one real recording."""

    root: Path
    """The test's temporary directory, which every produced path must stay inside."""
    project: Path
    """The throwaway project the record run executed."""
    cassette_dir: Path
    """The resolved cassette directory holding the recording the replay arms read."""
    cassette_paths: tuple[Path, ...]
    """Every ``*.json`` file the record run left there, sorted."""
    document: dict[str, object]
    """The parsed cassette document, empty when the arm wrote no single document."""
    text: str
    """The whole cassette file as text, which the requirement I4 grep scans."""
    results_path: Path
    """The record run's ``results.json``."""
    artifacts_dir: Path
    """The record run's ``artifacts`` parent directory."""
    completed: subprocess.CompletedProcess[str]
    """The finished child process, whose code and output the tests assert on."""


@pytest.fixture
def recorded_run(
    echo_server: EchoServer,
    tmp_path: Path,
    write_project: Callable[..., Path],
    run_pytest: Callable[..., subprocess.CompletedProcess[str]],
    clean_foundry_env: None,
) -> RecordedRun:
    """Write the record project, run it against the echo server, and read the cassette back.

    The run records into ``<tmp>/cassettes`` and writes its reports and artifacts inside its own
    project directory, so the cassette outlives the project for the replay arms. The bearer token
    reaches the child through ``env`` only, under the name ``http.auth.token_env`` gives it.
    """
    root = tmp_path.resolve()
    cassette_dir = (root / "cassettes").resolve()
    project = write_project(
        root / "record",
        files={TEST_MODULE: _RECORD_MODULE},
        foundry_toml=_RECORD_TOML,
    )
    completed = run_pytest(
        "--foundry-record",
        *_arm_options(directory=project, cassette_dir=cassette_dir, base_url=echo_server.base_url),
        cwd=project,
        env={TOKEN_ENV: TOKEN_SENTINEL},
        timeout=_RUN_TIMEOUT_S,
    )
    written = tuple(sorted(cassette_dir.glob("*.json")))
    text = written[0].read_text(encoding="utf-8") if len(written) == 1 else ""
    document: dict[str, object] = json.loads(text) if text else {}
    return RecordedRun(
        root=root,
        project=project,
        cassette_dir=cassette_dir,
        cassette_paths=written,
        document=document,
        text=text,
        results_path=project / "results.json",
        artifacts_dir=project / "artifacts",
        completed=completed,
    )


# --- The two arms, as assertions -------------------------------------------


def test_the_record_arm_writes_the_documented_cassette(recorded_run: RecordedRun) -> None:
    """Assert criterion 7's green run, AAP 0.4.6's document and requirement I4's redaction."""
    # Criterion 7: recording a test against the loopback peer exits 0.
    assert recorded_run.completed.returncode == 0, _output(recorded_run.completed)
    # AAP 0.14.3: every produced path stays inside this test's own temporary directory.
    _assert_within(recorded_run.project, recorded_run.root)
    _assert_within(recorded_run.cassette_dir, recorded_run.root)
    _assert_within(recorded_run.results_path, recorded_run.root)
    _assert_within(recorded_run.artifacts_dir, recorded_run.root)

    # AAP 0.4.6: exactly one document per test, at cassettes/<sanitized-nodeid>.json.
    expected = cassette_path(NODEID, cassette_dir=recorded_run.cassette_dir)
    # AAP 0.4.6: percent-encoded separators, so a node id cannot introduce a directory boundary.
    assert sanitize_nodeid(NODEID) == "test_api.py%3A%3Atest_echo"
    assert expected.name == f"{sanitize_nodeid(NODEID)}.json"
    assert expected.name == "test_api.py%3A%3Atest_echo.json"
    assert recorded_run.cassette_paths == (expected,)

    # Criterion 7: the run's own record agrees that the test passed.
    records = _result_records(recorded_run.results_path)
    assert [str(record["nodeid"]) for record in records] == [NODEID]
    assert records[0]["outcome"] == "passed"
    # AAP 0.4.6: the run directory is artifacts/<run_id>/ under the configured artifacts.dir.
    assert (recorded_run.artifacts_dir / str(records[0]["run_id"])).is_dir()

    document = recorded_run.document
    # AAP 0.4.6: exactly the three documented top-level keys, and no others.
    assert set(document) == {"recorded_at", "redacted_keys", "interactions"}
    # AAP 0.4.6: recorded_at is one ISO-8601 instant, timezone-aware and in UTC.
    assert datetime.fromisoformat(str(document["recorded_at"])).utcoffset() == timedelta(0)
    # AAP 0.13.1: redacted_keys is sorted, so two recordings of one test serialise alike.
    redacted_keys = document["redacted_keys"]
    assert isinstance(redacted_keys, list)
    assert redacted_keys == sorted(redacted_keys)

    # AAP 0.4.6: the two exchanges, in the order the test issued them, each with the two halves.
    interactions = document["interactions"]
    assert isinstance(interactions, list)
    assert len(interactions) == 2
    listing, created = interactions
    for interaction in interactions:
        assert set(interaction) == {"request", "response"}
        request_keys = {"method", "path", "query", "body_sha256", "headers_subset"}
        assert set(interaction["request"]) == request_keys
        assert set(interaction["response"]) == {"status", "headers", "body"}
        assert interaction["response"]["status"] == 200
        assert str(interaction["response"]["body"])

    # AAP 0.4.6: the query is a list of pairs, stored sorted by name and then value; the path is
    # stored without scheme, host or query, which is what assumption A7 requires.
    listing_request = listing["request"]
    assert listing_request["method"] == "GET"
    assert listing_request["path"] == ECHO_PATH
    assert listing_request["query"] == [list(pair) for pair in STORED_QUERY]
    assert listing_request["query"] != [list(pair) for pair in RECORD_QUERY]
    # AAP 0.4.6: a request with no body records null, not the hash of the empty string.
    assert listing_request["body_sha256"] is None

    created_request = created["request"]
    assert created_request["method"] == "POST"
    assert created_request["query"] == [list(pair) for pair in POST_QUERY]
    digest = created_request["body_sha256"]
    # AAP 0.4.6: lowercase 64-character hex, recomputed here from the bytes httpx sent.
    assert isinstance(digest, str)
    assert _SHA256_HEX.fullmatch(digest) is not None
    assert digest == digest.lower()
    assert digest == _sha256(_json_body_bytes(POST_BODY))

    # Assumption A7: no host, port, scheme or base URL *field* anywhere in the document, which is
    # what makes a cassette recorded against one port replayable against another. A recorded
    # response body is the peer's own text and is stored as sent; matching never reads it.
    leaked = sorted({key.casefold() for key in _all_keys(document)} & _HOST_KEYS)
    assert leaked == []

    # Implicit requirement I4: the sentinel credential reaches no byte of the cassette file.
    assert TOKEN_SENTINEL not in recorded_run.text
    # Requirement I4: the engine records the name it scrubbed, in its own case-folded spelling.
    assert "authorization" in [str(name).casefold() for name in redacted_keys]
    # Requirement I4: the denied header's value is the marker...
    assert listing_request["headers_subset"]["authorization"] == REDACTED
    # ...while the non-secret static header survives, so the grep above cannot pass by recording
    # nothing at all.
    assert listing_request["headers_subset"][STATIC_HEADER_NAME] == STATIC_HEADER_VALUE
    assert STATIC_HEADER_VALUE in recorded_run.text


def test_the_replay_arm_answers_from_the_cassette_and_a_miss_names_it(
    recorded_run: RecordedRun,
    echo_server: EchoServer,
    tmp_path: Path,
    write_project: Callable[..., Path],
    run_pytest: Callable[..., subprocess.CompletedProcess[str]],
    socket_guard: _Guard,
) -> None:
    """Assert criterion 8's offline replay, AAP 0.7.3's query rule and AAP 0.4.7's typed miss."""
    # Assumption A7: the replay addresses a *different* loopback port, claimed and released before
    # the peer is stopped, which is only possible because a cassette records the path and not the
    # host it was recorded against.
    replay_base_url = _closed_loopback_base_url()
    # Criterion 8: the peer is stopped before the replay arm starts, so a live answer is impossible.
    echo_server.stop()
    assert replay_base_url != echo_server.base_url
    root = tmp_path.resolve()
    cassette_dir = recorded_run.cassette_dir

    replay_project = write_project(
        root / "replay",
        files={TEST_MODULE: _REPLAY_MODULE},
        conftest=_ALL_CONNECT_GUARD,
    )
    before = socket_guard.non_loopback_attempts
    replay = run_pytest(
        "--foundry-replay",
        *_arm_options(
            directory=replay_project, cassette_dir=cassette_dir, base_url=replay_base_url
        ),
        cwd=replay_project,
        timeout=_RUN_TIMEOUT_S,
    )
    # AAP 0.14.3: this in-process guard cannot see a child process, so its delta is only a floor;
    # the replay project's own all-connect guard is the decisive proof, and exit 0 below means no
    # connection was attempted at all.
    assert socket_guard.non_loopback_attempts == before

    # Criterion 8: the replay exits 0, the recorded test passing with every reply served from the
    # cassette -- the child asserts FoundryResponse.source == "cassette" for both requests, and
    # AAP 0.7.3's matching rule is what lets its reordered query match at all.
    assert replay.returncode == 0, _output(replay)
    _assert_within(replay_project / "results.json", root)
    _assert_within(replay_project / "report.html", root)
    replay_records = _result_records(replay_project / "results.json")
    assert [str(record["nodeid"]) for record in replay_records] == [NODEID]
    assert replay_records[0]["outcome"] == "passed"
    assert replay_records[0]["attempts"] == 1

    miss_project = write_project(
        root / "miss",
        files={TEST_MODULE: _MISS_MODULE},
        conftest=_ALL_CONNECT_GUARD,
    )
    missed = run_pytest(
        "--foundry-replay",
        *_arm_options(directory=miss_project, cassette_dir=cassette_dir, base_url=replay_base_url),
        cwd=miss_project,
        timeout=_RUN_TIMEOUT_S,
    )
    # AAP 0.4.7: a replay miss fails that test -- exit 1 -- and never crashes the session.
    assert missed.returncode == 1, _output(missed)
    _assert_within(miss_project / "results.json", root)
    miss_records = _result_records(miss_project / "results.json")
    assert len(miss_records) == 1
    assert miss_records[0]["outcome"] == "failed"
    failures = miss_records[0]["failures"]
    assert isinstance(failures, list)
    assert len(failures) == 1
    # Criterion 8: the surfaced failure names the exception type, the method, the path and the
    # cassette document the lookup searched -- asserted from results.json, not from the terminal.
    message = str(failures[0]["message"])
    assert "CassetteMissError" in message
    assert f"GET {MISSING_PATH}" in message
    assert str(cassette_path(NODEID, cassette_dir=cassette_dir)) in message
    # AAP 0.4.7: the request failed in the call phase, of the one test that made it, and the
    # recording the lookup searched was present rather than absent.
    assert failures[0]["phase"] == "call"
    assert cassette_path(NODEID, cassette_dir=cassette_dir).is_file()
