"""Unit tests for ``foundry.http``: the session, the response, the transport seam and the dumps.

AAP 0.10.1 places the HTTP layer's unit module here, and what it pins is AAP 0.7.3's request
pipeline as a test observes it -- the header merge, the timeout extension, transport selection, the
retry loop and the response wrapper -- rather than as the docstrings describe it.

* A non-2xx reply is a value (AAP 0.4.3): a ``404``, a ``500`` and an unfollowed ``302`` each come
  back as a :class:`~foundry.http.response.FoundryResponse`. The only escapes are
  :class:`~foundry.core.exceptions.TransportError` and, once the attempts are spent,
  :class:`~foundry.core.exceptions.RetryExhaustedError`.
* The headers merge as ``http.headers`` -> the auth strategy's pair -> the per-request mapping, the
  later assignment winning case-insensitively, so one name is one header (AAP 0.7.3).
* The backoff of attempt *n* is ``min(backoff_base * 2 ** (n - 1), backoff_max)`` and the sleep is
  injected, so both retry tests assert the delays instead of paying them (AAP 0.7.3, AAP 0.14.2).
* :func:`~foundry.http.transport.select_transport` is the whole effect of the cassette mode on this
  package, and a ``replay`` session needs no live transport to exist at all, which is what makes
  offline replay a property of the object graph (AAP 0.2.1, assumption A8).
* The committed reference document ``tests/fixtures/cassettes/replay_echo.json`` replays through the
  public ``session.post(path, json=...)`` path it illustrates -- its recorded hash is the digest of
  the bytes ``httpx`` encodes -- and a body it does not hold raises the typed miss (AAP 0.4.6).
* ``request_dump()`` and ``response_dump()`` carry exactly the published keys and mask every
  configured secret name with the ``***`` marker (AAP 0.3.2, AAP 0.4.6).
* One request goes through a real :class:`~foundry.http.transport.LiveTransport` to an in-process
  ``http.server`` on ``127.0.0.1:0``, because the one component a mock cannot vouch for is the one
  that reaches ``httpcore``.

Everything else is driven through :class:`httpx.MockTransport`, no test opens a non-loopback socket
(AAP 0.14.3), no test sleeps, and the only file any test writes is the copy of the committed
reference cassette it makes under ``tmp_path``.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Final

import httpx
import pytest

from foundry.cassette import CassetteSession, CassetteStore, cassette_path
from foundry.core.exceptions import (
    CassetteMissError,
    ConfigError,
    FoundryError,
    RetryExhaustedError,
    TransportError,
)
from foundry.http import (
    AuthSettings,
    FoundryResponse,
    FoundrySession,
    RetryPolicy,
    SessionSettings,
)
from foundry.http.transport import (
    LiveTransport,
    build_live_transport,
    close_quietly,
    select_transport,
    timeout_extension,
)
from foundry.obs.redaction import MAX_EXCERPT_BYTES, REDACTED, TRUNCATION_MARKER

_TOKEN_VARIABLE: Final[str] = "FOUNDRY_SPEC_BEARER"
"""Name of the variable the bearer strategy reads. The session is given the mapping, never the
process environment, so no test here depends on what the shell exports."""

_SENTINEL: Final[str] = "s3cr3t-b34r3r"  # no real credential; a scanner-safe stand-in
_API_KEY: Final[str] = "s3cr3t-h34d3r"  # the value a header strategy would send
_BASE: Final[str] = "http://peer.test"  # a name that resolves nowhere: every send is mocked
_NODEID: Final[str] = "tests/test_api.py::test_echo"  # carries / and ::, so the path is encoded
_QUERY: Final[Mapping[str, object]] = {"page": 2, "limit": 10}  # what the recorded GET carries
_RECORDED_TEXT: Final[str] = '{"echo":{"method":"GET","path":"/echo"},"ok":true}'
"""Body of the committed cassette's ``GET /echo`` reply, so a replay test names a real value."""
_RECORDED_POST_TEXT: Final[str] = (
    '{"echo":{"body":{"message":"hello"},"method":"POST","path":"/echo"},"ok":true}'
)
"""Body of the committed cassette's ``POST /echo`` reply, the one its body hash selects."""
_RECORDED_BODY: Final[Mapping[str, object]] = {"message": "hello"}
"""The JSON body the committed cassette records, so the ``json=`` request below is that exchange."""
_HEX64: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{64}")


class _RefusingTransport(httpx.BaseTransport):
    """A live-transport stand-in that records every call and refuses to serve one.

    ``replay`` must reach neither ``httpcore`` nor the transport the session built for the mode
    selection, so a delegation attempt raises instead of answering. A plain ``RuntimeError`` is
    raised rather than a locally defined subclass, so every Foundry exception still lives in
    ``foundry/core/exceptions.py`` (AAP 0.14.3).
    """

    def __init__(self) -> None:
        """Start with no recorded call."""
        self.calls: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        """Record the attempt and refuse it."""
        self.calls.append(request)
        raise RuntimeError(f"the live transport must not be reached: {request.method}")

    def close(self) -> None:
        """Accept the teardown a network mode would perform."""


# --- Builders -------------------------------------------------------------


def _recorder(
    status: int = 200,
    *,
    body: bytes = b"",
    headers: Mapping[str, str] | None = None,
) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    """Return a transport answering ``status`` beside the list of requests it was handed."""

    received: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        received.append(request)
        return httpx.Response(status, headers=headers, content=body)

    return httpx.MockTransport(handle), received


def _faults(
    make: Callable[[httpx.Request], httpx.HTTPError],
) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    """Return a transport raising a fresh ``httpx`` fault per attempt, beside its request list."""

    received: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        received.append(request)
        raise make(request)

    return httpx.MockTransport(handle), received


def _statuses(codes: Sequence[int]) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    """Return a transport answering ``codes`` in order, beside the list of requests it received."""

    received: list[httpx.Request] = []
    remaining = list(codes)

    def handle(request: httpx.Request) -> httpx.Response:
        received.append(request)
        return httpx.Response(remaining.pop(0))

    return httpx.MockTransport(handle), received


def _values(headers: Mapping[str, str], name: str) -> list[str]:
    """Return every value sent under ``name``, matched case-insensitively as HTTP matches it."""
    pairs = getattr(headers, "multi_items", None)
    items: Sequence[tuple[str, str]] = headers.items() if pairs is None else pairs()
    return [value for key, value in items if key.casefold() == name.casefold()]


def _no_sleep(seconds: float) -> None:
    """Perform no wait; every session here is built with an injected sleeper (AAP 0.14.2)."""


def _session(
    settings: SessionSettings | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
    sleep: Callable[[float], None] = _no_sleep,
) -> FoundrySession:
    """Return a session over ``transport``, with the credential mapping no shell can influence."""
    return FoundrySession(
        SessionSettings() if settings is None else settings,
        transport=transport,
        sleep=sleep,
        environ={_TOKEN_VARIABLE: _SENTINEL},
    )


def _replay_client(
    cassette: CassetteSession, monkeypatch: pytest.MonkeyPatch
) -> tuple[FoundrySession, _RefusingTransport]:
    """Return a session over ``cassette`` beside the live transport it must never reach."""
    live = _RefusingTransport()
    monkeypatch.setattr("foundry.http.session.build_live_transport", lambda: live)
    session = FoundrySession(SessionSettings(base_url=_BASE), cassette=cassette, sleep=_no_sleep)
    return session, live


class _PeerHandler(BaseHTTPRequestHandler):
    """The loopback peer's three routes: ``/echo``, ``/fail`` and ``/redirect``."""

    def do_GET(self) -> None:
        """Answer ``/echo`` from the request seen, ``/fail`` with 500, ``/redirect`` with 302."""
        if self.path.startswith("/fail"):
            self._send(500, {"error": "boom"})
        elif self.path.startswith("/redirect"):
            self.send_response(302)
            self.send_header("Location", "/echo")
            self.end_headers()
        else:
            self._send(200, {"path": self.path, "trace": self.headers.get("X-Trace", "")})

    def _send(self, status: int, payload: Mapping[str, object]) -> None:
        """Write one JSON reply with an explicit length, so the client reads it as one body."""
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        """Keep the peer quiet: a test's output is the suite's, not the server's."""


# --- Fixtures -------------------------------------------------------------


@pytest.fixture
def cassette_dir(tmp_path: Path) -> Path:
    """Function scope: an existing, empty cassette directory inside ``tmp_path``."""
    directory = tmp_path / "cassettes"
    directory.mkdir()
    return directory


@pytest.fixture
def replay_session(repo_root: Path, cassette_dir: Path) -> CassetteSession:
    """Function scope: a ``replay`` session over a copy of the committed reference cassette."""
    source = repo_root / "tests" / "fixtures" / "cassettes" / "replay_echo.json"
    target = cassette_path(_NODEID, cassette_dir=cassette_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(source.read_bytes())
    return CassetteSession(_NODEID, mode="replay", store=CassetteStore(cassette_dir))


@pytest.fixture
def loopback_peer() -> Iterator[str]:
    """Function scope: a real ``http.server`` on an ephemeral ``127.0.0.1`` port, stopped after."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _PeerHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)


# --- The header merge (AAP 0.7.3) ----------------------------------------


def test_the_headers_merge_configured_then_auth_then_per_request() -> None:
    """A later source replaces an earlier one, so the peer sees one value per name."""
    transport, received = _recorder()
    settings = SessionSettings(
        headers=(("X-Trace", "cfg"), ("X-Static", "1")),
        auth=AuthSettings(strategy="bearer", token_env=_TOKEN_VARIABLE),
    )
    session = _session(settings, transport=transport)
    session.get(f"{_BASE}/echo", headers={"x-trace": "req"})
    sent = received[0].headers
    assert _values(sent, "x-trace") == ["req"]
    assert _values(sent, "x-static") == ["1"]
    assert _values(sent, "authorization") == [f"Bearer {_SENTINEL}"]


def test_a_lower_case_per_request_name_replaces_the_configured_one() -> None:
    """``x-trace`` and ``X-Trace`` are one header name, so the request carries exactly one value."""
    transport, received = _recorder()
    session = _session(SessionSettings(headers=(("X-Trace", "cfg"),)), transport=transport)
    session.get(f"{_BASE}/echo", headers={"x-trace": "req"})
    assert _values(received[0].headers, "X-TRACE") == ["req"]


def test_the_per_request_headers_beat_the_auth_header() -> None:
    """A caller's own credential wins over the configured strategy, and only it is sent."""
    transport, received = _recorder()
    session = _session(
        SessionSettings(auth=AuthSettings(strategy="bearer", token_env=_TOKEN_VARIABLE)),
        transport=transport,
    )
    session.get(f"{_BASE}/echo", headers={"authorization": "Bearer manual"})
    assert _values(received[0].headers, "authorization") == ["Bearer manual"]
    assert _SENTINEL not in json.dumps(dict(received[0].headers))


# --- Every status is a value (AAP 0.4.3) ---------------------------------


@pytest.mark.parametrize("status", [404, 500, 302])
def test_every_status_comes_back_as_a_response(status: int) -> None:
    """A client error, a server error and an unfollowed redirect are returned, never raised."""
    transport, _received = _recorder(status, body=b'{"error": "boom"}')
    response = _session(transport=transport).get(f"{_BASE}/echo")
    assert isinstance(response, FoundryResponse)
    assert response.status_code == status
    assert response.source == "live"


def test_the_response_members_describe_the_exchange() -> None:
    """The status, the case-insensitive headers, the text, the JSON body and the elapsed time."""
    payload = {"ok": True, "echo": {"method": "GET"}}
    body = json.dumps(payload).encode()
    transport, _received = _recorder(200, body=body, headers={"Content-Type": "application/json"})
    response = _session(transport=transport).get(f"{_BASE}/echo")
    assert response.text == body.decode()
    assert response.json() == payload
    assert response.headers["content-type"] == "application/json"
    assert response.headers["Content-Type"] == "application/json"
    assert isinstance(response.elapsed_ms, float)
    assert response.elapsed_ms > 0.0
    assert response.source == "live"


# --- The timeout and its extension ---------------------------------------


def test_the_timeout_extension_deadlines_the_whole_request() -> None:
    """Every phase of a bare request carries the resolved deadline, which is what httpx reads."""
    transport, received = _recorder()
    _session(SessionSettings(timeout=2.5), transport=transport).get(f"{_BASE}/echo")
    assert received[0].extensions["timeout"] == timeout_extension(2.5)
    assert timeout_extension(2.5) == {"connect": 2.5, "read": 2.5, "write": 2.5, "pool": 2.5}


def test_a_per_request_timeout_replaces_the_configured_one() -> None:
    """``http.timeout`` is the default; a per-request value is the deadline actually sent."""
    transport, received = _recorder()
    _session(SessionSettings(timeout=10.0), transport=transport).get(f"{_BASE}/echo", timeout=1.5)
    assert received[0].extensions["timeout"] == timeout_extension(1.5)


@pytest.mark.parametrize("value", [-1.0, 0.0, 301.0])
def test_a_timeout_outside_its_range_is_refused_as_a_config_error(value: float) -> None:
    """An unusable deadline names ``http.timeout`` and its range instead of reaching the peer."""
    transport, received = _recorder()
    with pytest.raises(ConfigError) as raised:
        _session(transport=transport).get(f"{_BASE}/echo", timeout=value)
    assert raised.value.key == "http.timeout"
    assert "0 < t <= 300" in str(raised.value)
    assert received == []


# --- Transport faults and the retry loop (AAP 0.7.3) ----------------------


def test_a_transport_fault_the_policy_does_not_retry_raises_the_typed_error() -> None:
    """A connect fault under a policy that cannot retry it escapes as ``TransportError``."""
    transport, received = _faults(lambda request: httpx.ConnectError("refused", request=request))
    with pytest.raises(TransportError) as raised:
        _session(
            SessionSettings(
                base_url=_BASE,
                retry=RetryPolicy(max_attempts=1, retry_on=frozenset({"status_5xx"})),
            ),
            transport=transport,
        ).get("/health")
    assert not isinstance(raised.value, RetryExhaustedError)
    assert (raised.value.method, raised.value.path, raised.value.cause) == (
        "GET",
        "/health",
        "connect",
    )
    assert len(received) == 1


def test_a_read_timeout_is_classified_as_a_timeout() -> None:
    """``timeout`` is the predicate a ``httpx.ReadTimeout`` earns, and the attempt count is one."""
    transport, received = _faults(lambda request: httpx.ReadTimeout("timed out", request=request))
    with pytest.raises(TransportError) as raised:
        _session(SessionSettings(retry=RetryPolicy(max_attempts=1)), transport=transport).get(
            f"{_BASE}/slow"
        )
    assert raised.value.cause == "timeout"
    assert len(received) == 1


def test_the_retry_loop_runs_the_configured_attempts_and_records_the_backoff() -> None:
    """Three attempts, backoff ``[0.2, 0.4]`` from a recording sleeper, one record per attempt."""
    transport, received = _faults(lambda request: httpx.ConnectError("refused", request=request))
    delays: list[float] = []
    with pytest.raises(RetryExhaustedError) as raised:
        _session(
            SessionSettings(base_url=_BASE, retry=RetryPolicy(max_attempts=3)),
            transport=transport,
            sleep=delays.append,
        ).get("/health")
    assert len(received) == 3
    assert delays == [0.2, 0.4]
    attempts = raised.value.attempts
    assert [record.number for record in attempts] == [1, 2, 3]
    assert {record.retry_on for record in attempts} == {"connect"}
    assert all(record.elapsed_ms >= 0.0 for record in attempts)
    assert (raised.value.method, raised.value.path, raised.value.cause) == (
        "GET",
        "/health",
        "connect",
    )
    assert "GET /health" in str(raised.value)


def test_a_retried_status_is_returned_once_the_attempts_run_out() -> None:
    """A 503 under ``status_5xx`` is retried and then returned as a value, not raised."""
    transport, received = _statuses([503, 503, 200])
    delays: list[float] = []
    response = _session(
        SessionSettings(retry=RetryPolicy(max_attempts=2)),
        transport=transport,
        sleep=delays.append,
    ).get(f"{_BASE}/flaky")
    assert response.status_code == 503
    assert len(received) == 2
    assert delays == [0.2]


def test_a_status_outside_the_predicates_is_not_retried() -> None:
    """A 404 with only ``status_5xx`` configured makes exactly one attempt."""
    transport, received = _statuses([404, 200])
    delays: list[float] = []
    response = _session(
        SessionSettings(retry=RetryPolicy(max_attempts=3, retry_on=frozenset({"status_5xx"}))),
        transport=transport,
        sleep=delays.append,
    ).get(f"{_BASE}/missing")
    assert response.status_code == 404
    assert len(received) == 1
    assert delays == []


# --- Transport selection (AAP 0.7.3, assumption A8) ----------------------


def test_build_live_transport_returns_the_one_socket_capable_transport() -> None:
    """The package builds exactly one kind of transport that can open a socket, and closes it."""
    transport = build_live_transport()
    try:
        assert isinstance(transport, LiveTransport)
    finally:
        close_quietly(transport)
    close_quietly(None)


def test_select_transport_hands_back_the_callers_own_transport(cassette_dir: Path) -> None:
    """No cassette, and a cassette in ``off`` mode, both answer with the very object offered.

    ``live`` is a factory (AAP 0.7.3), so a session that answers from a cassette never builds the
    transport that can reach a peer: the first call builds it once, and the cassette's own transport
    answers the second without calling the factory at all.
    """
    transport = _RefusingTransport()
    built: list[int] = []

    def factory() -> httpx.BaseTransport:
        """Build ``transport`` and count that the live path was taken."""
        built.append(1)
        return transport

    assert select_transport(factory) is transport
    assert len(built) == 1
    off_mode = CassetteSession(
        "tests/test_api.py::test_off",
        mode="off",
        store=CassetteStore(cassette_dir),
        live_transport=transport,
    )
    assert select_transport(factory, off_mode) is transport
    assert len(built) == 1, "the cassette's transport answered; no live transport was built"


def test_select_transport_builds_the_replay_transport_for_a_replay_session(
    replay_session: CassetteSession,
) -> None:
    """A ``replay`` session answers with a transport of its own and never the live one."""
    live = _RefusingTransport()
    chosen = select_transport(lambda: live, replay_session)
    assert chosen is not live
    assert type(chosen).__name__ == "ReplayTransport"
    assert live.calls == []


def test_a_replay_session_needs_no_live_transport_and_serves_the_recording(
    replay_session: CassetteSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replay holds no live transport at all: the one the session builds is never reached."""
    live = _RefusingTransport()
    monkeypatch.setattr("foundry.http.session.build_live_transport", lambda: live)
    session = FoundrySession(SessionSettings(base_url=_BASE), cassette=replay_session)
    try:
        response = session.get("/echo", params=_QUERY)
    finally:
        session.close()
    assert response.status_code == 200
    assert response.source == "cassette"
    assert response.text == _RECORDED_TEXT
    assert response.json() == {"echo": {"method": "GET", "path": "/echo"}, "ok": True}
    assert live.calls == []


# --- The committed reference cassette (AAP 0.4.6, criterion 8) -----------


def test_the_committed_reference_cassette_replays_a_json_post_through_the_session(
    replay_session: CassetteSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The documented ``session.post(json=...)`` path replays the fixture, and its GET beside it."""
    session, live = _replay_client(replay_session, monkeypatch)
    try:
        posted = session.post("/echo", json=_RECORDED_BODY)
        recorded = session.get("/echo", params=_QUERY)
    finally:
        session.close()
    assert posted.status_code == 201
    assert posted.source == "cassette"
    assert posted.text == _RECORDED_POST_TEXT
    assert posted.json() == {
        "echo": {"body": {"message": "hello"}, "method": "POST", "path": "/echo"},
        "ok": True,
    }
    # The recording holds ``limit`` before ``page``; the request sends the opposite order (A7).
    assert recorded.status_code == 200
    assert (recorded.source, recorded.text) == ("cassette", _RECORDED_TEXT)
    assert live.calls == []


def test_a_body_the_reference_cassette_does_not_hold_is_a_typed_miss(
    replay_session: CassetteSession, cassette_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A changed JSON body misses by hash, naming the method, the path and the document searched."""
    session, live = _replay_client(replay_session, monkeypatch)
    try:
        with pytest.raises(CassetteMissError) as raised:
            session.post("/echo", json={"message": "goodbye"})
    finally:
        session.close()
    error = raised.value
    document = cassette_path(_NODEID, cassette_dir=cassette_dir)
    assert (error.method, error.path, error.cassette_path) == ("POST", "/echo", document)
    assert "POST" in error.message
    assert "/echo" in error.message
    assert str(document) in error.message
    assert live.calls == []


# --- The two dumps (AAP 0.4.6, AAP 0.3.2) --------------------------------


def test_request_dump_carries_the_published_keys_and_masks_the_credential() -> None:
    """The request payload reports the exchange and shows the configured secret as its marker."""
    transport, _received = _recorder()
    session = _session(
        SessionSettings(
            redact_keys=("authorization", "token"),
            auth=AuthSettings(strategy="bearer", token_env=_TOKEN_VARIABLE),
        ),
        transport=transport,
    )
    response = session.post(f"{_BASE}/echo", params={"q": "x"}, json={"message": "hello"})
    dump = response.request_dump()
    assert set(dump) == {"method", "path", "query", "headers", "body_sha256", "body"}
    assert dump["method"] == "POST"
    # AAP 0.7.3 gives ``path`` the meaning the cassette document gives it -- the path alone -- and
    # the decoded pairs are the one place the query appears in the dump.
    assert dump["path"] == "/echo"
    assert dump["query"] == [["q", "x"]]
    headers = dump["headers"]
    assert isinstance(headers, dict)
    assert headers["authorization"] == REDACTED
    assert _SENTINEL not in json.dumps(dump)
    body_sha256 = dump["body_sha256"]
    assert isinstance(body_sha256, str)
    assert _HEX64.fullmatch(body_sha256) is not None
    assert dump["body"] == '{"message": "hello"}'


def test_response_dump_carries_the_published_keys_and_masks_the_credential() -> None:
    """The response payload reports the status and source and masks a deny-listed header and key."""
    body = json.dumps({"token": _API_KEY, "ok": True}).encode()
    transport, _received = _recorder(
        500, body=body, headers={"X-Api-Key": _API_KEY, "Content-Type": "application/json"}
    )
    response = _session(
        SessionSettings(redact_keys=("x-api-key", "token")), transport=transport
    ).get(f"{_BASE}/fail")
    dump = response.response_dump()
    assert set(dump) == {"status", "headers", "body", "source"}
    assert (dump["status"], dump["source"]) == (500, "live")
    headers = dump["headers"]
    assert isinstance(headers, dict)
    assert headers["x-api-key"] == REDACTED
    dumped_body = dump["body"]
    assert isinstance(dumped_body, str)
    assert _API_KEY not in dumped_body
    assert REDACTED in dumped_body


def test_the_dumps_bound_the_body_excerpt() -> None:
    """A body longer than the excerpt budget is cut and marked, so a dump stays a diagnostic."""
    transport, _received = _recorder(200, body=b"x" * 5000)
    dump = _session(transport=transport).get(f"{_BASE}/big").response_dump()
    excerpt = dump["body"]
    assert isinstance(excerpt, str)
    assert excerpt.endswith(TRUNCATION_MARKER)
    assert len(excerpt) == MAX_EXCERPT_BYTES + len(TRUNCATION_MARKER)


# --- The closed-session guard -------------------------------------------


def test_a_closed_session_refuses_the_next_request_and_close_is_idempotent() -> None:
    """``close()`` twice is one teardown, and a request afterwards raises ``FoundryError``."""
    transport, received = _recorder()
    session = _session(transport=transport)
    session.close()
    session.close()
    assert session.closed is True
    with pytest.raises(FoundryError) as raised:
        session.get(f"{_BASE}/echo")
    assert "FoundrySession is closed" in str(raised.value)
    assert received == []


# --- One real exchange over the loopback peer ----------------------------


def test_a_request_reaches_a_loopback_peer_through_the_live_transport(loopback_peer: str) -> None:
    """The real transport sends the merged headers and returns the peer's own reply."""
    session = FoundrySession(SessionSettings(timeout=5.0, headers=(("X-Trace", "req"),)))
    try:
        response = session.get(f"{loopback_peer}/echo")
    finally:
        session.close()
    assert response.status_code == 200
    assert response.source == "live"
    assert response.elapsed_ms > 0.0
    assert response.json() == {"path": "/echo", "trace": "req"}
    assert response.headers["content-type"] == "application/json"


def test_a_server_error_from_the_loopback_peer_is_returned(loopback_peer: str) -> None:
    """A 500 from a real peer comes back as a value with its body, exactly as a 200 would."""
    session = FoundrySession(SessionSettings(timeout=5.0))
    try:
        response = session.get(f"{loopback_peer}/fail")
    finally:
        session.close()
    assert response.status_code == 500
    assert response.source == "live"
    assert response.json() == {"error": "boom"}
