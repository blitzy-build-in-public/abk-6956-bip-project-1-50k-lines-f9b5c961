"""Unit tests for ``foundry.http``: URL joining, the header merge, retries, auth and responses.

AAP 0.10.1 places the HTTP layer's unit module here. What it pins is AAP 0.7.3's request pipeline
and AAP 0.4.3's response contract as a caller observes them:

* :func:`foundry.http.url.join_url` keeps the base URL's path, replaces it for an absolute path,
  accepts an absolute URL unchanged and appends a query exactly once; a relative path with no
  ``http.base_url`` raises :class:`~foundry.core.exceptions.ConfigError` naming that key, both at
  the request and, for ``foundry_api``, at session construction (AAP 0.4.5, AAP 0.4.7).
* :func:`foundry.http.url.redact_url` strips URL userinfo and masks a deny-listed query
  parameter, which is what keeps a credential out of a dump (AAP 0.3.2).
* Headers merge ``http.headers`` -> the auth strategy's pair -> the per-request mapping, the later
  source winning case-insensitively (AAP 0.7.3).
* A request carries the per-request deadline when given and ``http.timeout`` otherwise, asserted
  on the ``timeout`` extension the transport receives.
* Retry predicates map ``timeout`` to :class:`httpx.TimeoutException`, ``connect`` to
  :class:`httpx.ConnectError`, ``status_5xx`` to any status from 500 up and ``status_429`` to 429
  alone; the backoff of attempt *n* is ``min(base * 2 ** (n - 1), max)`` and the sleep is injected,
  so the delays are asserted instead of paid (AAP 0.7.3, AAP 0.14.2).
* An exhausted request raises :class:`~foundry.core.exceptions.RetryExhaustedError` carrying one
  :class:`~foundry.core.types.AttemptRecord` per attempt (AAP 0.4.3).
* The four auth strategies resolve the environment variable *names* configuration holds, and no
  representation of the settings or of the resolved header carries the value (AAP 0.3.3, I9).
* A 4xx, a 5xx and an unfollowed 3xx are returned as values; the five verbs delegate to
  :meth:`FoundrySession.request` (AAP 0.4.3, assumption A13).

Recorded differences from the plan's names, as the pipeline is spelled on disk: the retry settings
are :class:`foundry.http.retries.RetryPolicy` -- a frozen dataclass of ``max_attempts``,
``backoff_base``, ``backoff_max`` and a ``retry_on`` frozenset -- with ``delay_for(attempt)``,
beside ``classify``, ``should_retry`` and ``backoff``; credentials resolve through
``resolve_auth(AuthSettings, environ=...)`` into an ``AuthHeader`` or ``None``; ``foundry_api``'s
mandatory base URL is ``SessionSettings(require_base_url=True)``, refused at construction; and
``FoundryResponse`` is built by ``from_httpx``, so every response asserted here came from a real
request call.

Everything is served by :class:`httpx.MockTransport`, so no test opens a socket (AAP 0.14.3). No
test imports ``foundry.reporting`` (AAP 0.4.8), and no test waits: every wait is an injected
recorder, which is why the module finishes in well under a second (AAP 0.14.2).
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict
from typing import Final

import httpx
import pytest

from foundry.core.exceptions import ConfigError, ContractError, RetryExhaustedError, TransportError
from foundry.http import (
    AuthSettings,
    FoundryResponse,
    FoundrySession,
    RetryPolicy,
    SessionSettings,
)
from foundry.http.auth import AuthHeader, resolve_auth
from foundry.http.retries import backoff, classify, should_retry
from foundry.http.url import join_url, redact_url, require_base_url, strip_userinfo

_BASE: Final[str] = "http://peer.test"
"""A name that resolves nowhere: the suite guard refuses it, so a real send would fail loudly."""

_TOKEN_VARIABLE: Final[str] = "FOUNDRY_SPEC_BEARER"
_USER_VARIABLE: Final[str] = "FOUNDRY_SPEC_USER"
_PASSWORD_VARIABLE: Final[str] = "FOUNDRY_SPEC_PASSWORD"
_HEADER_VARIABLE: Final[str] = "FOUNDRY_SPEC_API_KEY"
"""The names configuration holds; the values never enter the settings (implicit requirement I9)."""

_SENTINEL: Final[str] = "s3cr3t-b34r3r"  # no real credential; a scanner-safe stand-in
_USER_VALUE: Final[str] = "spec-user"
_PASSWORD_VALUE: Final[str] = "spec-password"
_HEADER_VALUE: Final[str] = "s3cr3t-h34d3r"
_QUERY_TOKEN: Final[str] = "s3cr3t-qu3ry"

_ENVIRON: Final[Mapping[str, str]] = {
    _TOKEN_VARIABLE: _SENTINEL,
    _USER_VARIABLE: _USER_VALUE,
    _PASSWORD_VARIABLE: _PASSWORD_VALUE,
    _HEADER_VARIABLE: _HEADER_VALUE,
}
"""The credential mapping every session here is given, so no test depends on the shell's exports."""

_METHODS: Final[tuple[str, ...]] = ("get", "post", "put", "patch", "delete")


# --- Builders -------------------------------------------------------------


def _recorder(
    status: int = 200, *, body: bytes = b"", headers: Mapping[str, str] | None = None
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
    """Return a transport answering ``codes`` in order, beside the requests it received."""

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
    """Perform no wait: every session here carries an injected sleeper instead (AAP 0.14.2)."""


def _session(
    settings: SessionSettings | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
    sleep: Callable[[float], None] = _no_sleep,
) -> FoundrySession:
    """Return a session over ``transport`` with the credential mapping no shell can influence."""
    return FoundrySession(
        SessionSettings() if settings is None else settings,
        transport=transport,
        sleep=sleep,
        environ=_ENVIRON,
    )


# --- URL joining (AAP 0.4.5, AAP 0.4.7) ----------------------------------


@pytest.mark.parametrize(
    ("base", "target", "expected"),
    [
        (f"{_BASE}:8080/api", "orders", f"{_BASE}:8080/api/orders"),
        (f"{_BASE}:8080/api/", "orders", f"{_BASE}:8080/api/orders"),
        (f"{_BASE}/api", "/orders", f"{_BASE}/orders"),
        (f"{_BASE}/api", "orders?page=1#frag", f"{_BASE}/api/orders?page=1"),
        (None, f"{_BASE}/echo?page=2", f"{_BASE}/echo?page=2"),
    ],
)
def test_a_target_is_resolved_against_the_base_url(
    base: str | None, target: str, expected: str
) -> None:
    """The base path is kept, a rooted path replaces it, and a fragment is never sent."""
    assert join_url(base, target) == expected


def test_query_parameters_merge_without_duplicating_the_one_in_the_path() -> None:
    """The path's query and ``params`` become one query, and a repeated name keeps one value."""
    transport, received = _recorder()
    with _session(SessionSettings(base_url=_BASE), transport=transport) as session:
        session.get("/echo?page=1", params={"page": 2, "limit": 10})
    sent = received[0]
    assert str(sent.url) == f"{_BASE}/echo?page=2&limit=10"
    assert sent.url.params.multi_items() == [("page", "2"), ("limit", "10")]


def test_a_relative_path_without_a_base_url_is_a_config_error() -> None:
    """The fault names the key and never reaches a transport, which is what exit 2 needs."""
    transport, received = _recorder()
    with _session(transport=transport) as session, pytest.raises(ConfigError) as raised:
        session.get("/echo")
    assert raised.value.key == "http.base_url"
    assert "http.base_url" in str(raised.value)
    assert received == []


def test_a_session_that_requires_a_base_url_refuses_to_be_built_without_one() -> None:
    """``foundry_api``'s path (AAP 0.4.7): the fault lands before the first request is sent."""
    with pytest.raises(ConfigError) as raised:
        FoundrySession(SessionSettings(require_base_url=True), environ=_ENVIRON)
    assert raised.value.key == "http.base_url"


@pytest.mark.parametrize(
    "base", ["", "   ", "peer.test/api", "ftp://peer.test/api", "http://alice:hunter2@peer.test"]
)
def test_an_unusable_base_url_is_refused_naming_the_key(base: str) -> None:
    """An absent, relative, wrong-scheme or credential-carrying base URL is one typed fault."""
    with pytest.raises(ConfigError) as raised:
        require_base_url(base)
    assert raised.value.key == "http.base_url"
    assert "hunter2" not in str(raised.value)


def test_a_url_loses_its_userinfo_and_its_deny_listed_parameters_before_a_dump() -> None:
    """The redacted URL is what a log line, a cassette and a report receive (AAP 0.3.2)."""
    cleaned = redact_url(f"http://alice:hunter2@peer.test/echo?token={_QUERY_TOKEN}&page=2")
    serialised = json.dumps({"url": cleaned})
    assert "alice" not in serialised
    assert "hunter2" not in serialised
    assert _QUERY_TOKEN not in serialised
    assert "***" in serialised
    assert "peer.test" in serialised
    assert "page=2" in serialised
    assert strip_userinfo("http://alice:hunter2@peer.test/echo") == "http://peer.test/echo"
    assert redact_url(f"{_BASE}/echo?page=2") == f"{_BASE}/echo?page=2"


# --- The header merge (AAP 0.7.3) ----------------------------------------


def test_the_headers_merge_configured_then_auth_then_per_request() -> None:
    """A later source replaces an earlier one, so the peer sees one value per name."""
    transport, received = _recorder()
    settings = SessionSettings(
        base_url=_BASE,
        headers=(("X-Trace", "cfg"), ("X-Static", "1")),
        auth=AuthSettings(strategy="bearer", token_env=_TOKEN_VARIABLE),
    )
    with _session(settings, transport=transport) as session:
        session.get("/echo", headers={"x-trace": "req"})
    sent = received[0].headers
    assert _values(sent, "x-trace") == ["req"]
    assert _values(sent, "x-static") == ["1"]
    assert _values(sent, "authorization") == [f"Bearer {_SENTINEL}"]


def test_the_per_request_headers_beat_the_auth_header() -> None:
    """A caller's own credential wins over the configured strategy, and only it is sent."""
    transport, received = _recorder()
    settings = SessionSettings(
        base_url=_BASE, auth=AuthSettings(strategy="bearer", token_env=_TOKEN_VARIABLE)
    )
    with _session(settings, transport=transport) as session:
        session.get("/echo", headers={"Authorization": "Bearer manual"})
    assert _values(received[0].headers, "authorization") == ["Bearer manual"]
    assert _SENTINEL not in json.dumps(dict(received[0].headers))


# --- The deadline a request carries --------------------------------------


@pytest.mark.parametrize(
    ("configured", "requested", "expected"), [(2.5, None, 2.5), (10.0, 1.5, 1.5)]
)
def test_the_deadline_is_the_per_request_value_or_the_configured_one(
    configured: float, requested: float | None, expected: float
) -> None:
    """Every phase of the request carries ``http.timeout`` unless the caller names a deadline."""
    transport, received = _recorder()
    with _session(SessionSettings(timeout=configured), transport=transport) as session:
        session.get(f"{_BASE}/echo", timeout=requested)
    assert received[0].extensions["timeout"] == {
        "connect": expected,
        "read": expected,
        "write": expected,
        "pool": expected,
    }


@pytest.mark.parametrize("value", [-1.0, 0.0, 301.0])
def test_a_timeout_outside_its_range_is_refused_as_a_config_error(value: float) -> None:
    """An unusable deadline names ``http.timeout`` and its range instead of reaching the peer."""
    transport, received = _recorder()
    with _session(transport=transport) as session, pytest.raises(ConfigError) as raised:
        session.get(f"{_BASE}/echo", timeout=value)
    assert raised.value.key == "http.timeout"
    assert "0 < t <= 300" in str(raised.value)
    assert received == []


# --- The retry predicates (AAP 0.7.3) ------------------------------------


@pytest.mark.parametrize(
    ("make", "expected"),
    [
        (lambda: httpx.ConnectTimeout("slow connect"), "timeout"),
        (lambda: httpx.ReadTimeout("slow read"), "timeout"),
        (lambda: httpx.ConnectError("refused"), "connect"),
        (lambda: httpx.RemoteProtocolError("broken"), None),
        (lambda: httpx.DecodingError("undecodable"), None),
    ],
)
def test_a_transport_fault_classifies_into_its_predicate(
    make: Callable[[], httpx.HTTPError], expected: str | None
) -> None:
    """``timeout`` covers every :class:`httpx.TimeoutException`, ``connect`` a connect failure."""
    assert classify(make(), None) == expected


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (500, "status_5xx"),
        (503, "status_5xx"),
        (599, "status_5xx"),
        (429, "status_429"),
        (400, None),
        (404, None),
        (200, None),
    ],
)
def test_a_status_classifies_into_its_predicate(status: int, expected: str | None) -> None:
    """``status_5xx`` is any status from 500 up and ``status_429`` is 429 alone."""
    assert classify(None, status) == expected


def test_a_predicate_set_without_status_429_does_not_retry_a_rate_limited_reply() -> None:
    """The predicate must be configured explicitly, so ``status_5xx`` does not retry a 429."""
    assert should_retry("status_429", {"status_5xx"}) is False
    assert should_retry("status_429", {"status_5xx", "status_429"}) is True
    assert should_retry(None, {"status_5xx"}) is False
    assert RetryPolicy(retry_on=frozenset({"status_5xx"})).should_retry("status_429") is False


# --- The backoff ladder (AAP 0.7.3, AAP 0.14.2) ---------------------------


def test_the_backoff_doubles_from_the_base_and_is_clamped_by_the_cap() -> None:
    """The published ladder for ``backoff_base=0.2`` and ``backoff_max=2.0``, attempts 1 to 5."""
    policy = RetryPolicy(backoff_base=0.2, backoff_max=2.0)
    assert [policy.delay_for(attempt) for attempt in (1, 2, 3, 4, 5)] == [0.2, 0.4, 0.8, 1.6, 2.0]
    assert backoff(1, base=0.2, maximum=2.0) == 0.2
    assert backoff(5, base=0.2, maximum=2.0) == 2.0
    assert backoff(2, base=10.0, maximum=60.0) == 20.0


def test_a_failing_request_waits_the_ladder_through_the_injected_sleep() -> None:
    """The delays are recorded, never paid: five attempts sleep 0.2, 0.4, 0.8 and 1.6 seconds."""
    transport, received = _faults(lambda request: httpx.ConnectError("refused", request=request))
    delays: list[float] = []
    settings = SessionSettings(base_url=_BASE, retry=RetryPolicy(max_attempts=5))
    with (
        _session(settings, transport=transport, sleep=delays.append) as session,
        pytest.raises(RetryExhaustedError),
    ):
        session.get("/health")
    assert delays == [0.2, 0.4, 0.8, 1.6]
    assert len(received) == 5


def test_a_zero_backoff_policy_never_waits() -> None:
    """``backoff_base = 0`` retries immediately, so every recorded delay is zero."""
    transport, _received = _faults(lambda request: httpx.ConnectError("refused", request=request))
    delays: list[float] = []
    settings = SessionSettings(base_url=_BASE, retry=RetryPolicy(backoff_base=0.0, max_attempts=2))
    with (
        _session(settings, transport=transport, sleep=delays.append) as session,
        pytest.raises(RetryExhaustedError),
    ):
        session.get("/health")
    assert delays == [0.0]


# --- Exhaustion (AAP 0.4.3, AAP 0.4.7) -----------------------------------


def test_an_exhausted_request_reports_one_record_per_attempt() -> None:
    """Three attempts give three records, each naming its number, outcome, delay and message."""
    transport, received = _faults(lambda request: httpx.ConnectError("refused", request=request))
    delays: list[float] = []
    settings = SessionSettings(base_url=_BASE, retry=RetryPolicy(max_attempts=3))
    with (
        _session(settings, transport=transport, sleep=delays.append) as session,
        pytest.raises(RetryExhaustedError) as raised,
    ):
        session.get("/health")
    error = raised.value
    assert isinstance(error, TransportError)
    assert (error.method, error.path, error.cause) == ("GET", "/health", "connect")
    assert "GET /health" in str(error)
    assert len(received) == 3
    assert delays == [0.2, 0.4]
    assert [record.number for record in error.attempts] == [1, 2, 3]
    for record in error.attempts:
        assert record.outcome == "connect"
        assert record.retry_on == "connect"
        assert record.elapsed_ms >= 0.0
        assert record.message is not None and "ConnectError" in record.message


def test_a_transport_fault_the_policy_does_not_retry_raises_the_plain_error() -> None:
    """A connect fault under a policy that cannot retry it escapes as ``TransportError``."""
    transport, received = _faults(lambda request: httpx.ConnectError("refused", request=request))
    policy = RetryPolicy(max_attempts=1, retry_on=frozenset({"status_5xx"}))
    with (
        _session(SessionSettings(base_url=_BASE, retry=policy), transport=transport) as session,
        pytest.raises(TransportError) as raised,
    ):
        session.get("/health")
    assert not isinstance(raised.value, RetryExhaustedError)
    assert raised.value.cause == "connect"
    assert len(received) == 1


# --- The four credential strategies (AAP 0.3.3, implicit requirement I9) ---


def test_the_bearer_strategy_sends_the_token_from_the_named_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``bearer`` reads the variable ``http.auth.token_env`` names and sends the RFC 6750 form."""
    monkeypatch.setenv(_TOKEN_VARIABLE, _SENTINEL)
    header = resolve_auth(AuthSettings(strategy="bearer", token_env=_TOKEN_VARIABLE))
    assert isinstance(header, AuthHeader)
    assert (header.name, header.value) == ("Authorization", f"Bearer {_SENTINEL}")


def test_the_basic_strategy_encodes_the_two_named_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``basic`` builds the RFC 7617 ``user:password`` token from UTF-8 bytes."""
    monkeypatch.setenv(_USER_VARIABLE, _USER_VALUE)
    monkeypatch.setenv(_PASSWORD_VARIABLE, _PASSWORD_VALUE)
    settings = AuthSettings(
        strategy="basic", user_env=_USER_VARIABLE, password_env=_PASSWORD_VARIABLE
    )
    header = resolve_auth(settings)
    assert header is not None
    expected = base64.b64encode(f"{_USER_VALUE}:{_PASSWORD_VALUE}".encode()).decode("ascii")
    assert (header.name, header.value) == ("Authorization", f"Basic {expected}")


def test_the_header_strategy_sends_the_configured_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """``header`` sends ``http.auth.header_name`` with the value of the variable it names."""
    monkeypatch.setenv(_HEADER_VARIABLE, _HEADER_VALUE)
    settings = AuthSettings(
        strategy="header", header_name="X-Api-Key", header_value_env=_HEADER_VARIABLE
    )
    header = resolve_auth(settings)
    assert header is not None
    assert (header.name, header.value) == ("X-Api-Key", _HEADER_VALUE)


@pytest.mark.parametrize("strategy", ["none", ""])
def test_the_none_strategy_sends_no_credential(strategy: str) -> None:
    """``none``, and a blank strategy, resolve to no header -- what ``foundry_http`` relies on."""
    assert resolve_auth(AuthSettings(strategy=strategy), environ=_ENVIRON) is None
    assert resolve_auth(None, environ=_ENVIRON) is None


def test_no_representation_of_the_settings_or_the_header_holds_the_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Configuration stores the variable's name; the resolved header keeps its value out of repr."""
    monkeypatch.setenv(_TOKEN_VARIABLE, _SENTINEL)
    settings = AuthSettings(strategy="bearer", token_env=_TOKEN_VARIABLE)
    header = resolve_auth(settings)
    assert header is not None
    assert _SENTINEL not in repr(settings)
    assert _SENTINEL not in json.dumps(asdict(settings))
    assert asdict(settings)["token_env"] == _TOKEN_VARIABLE
    assert _SENTINEL not in repr(header)
    assert header.masked() == "***"


def test_an_absent_credential_variable_is_a_config_error_naming_only_the_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The message names the variable and its state; no value is read, so none can be quoted."""
    monkeypatch.delenv(_TOKEN_VARIABLE, raising=False)
    with pytest.raises(ConfigError) as raised:
        resolve_auth(AuthSettings(strategy="bearer", token_env=_TOKEN_VARIABLE))
    message = str(raised.value)
    assert _TOKEN_VARIABLE in message
    assert raised.value.key == "http.auth.token_env"
    assert _SENTINEL not in message


def test_the_credential_is_resolved_once_at_session_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two requests after the variable changes both carry the value the session started with."""
    transport, received = _recorder()
    monkeypatch.setenv(_TOKEN_VARIABLE, _SENTINEL)
    settings = SessionSettings(
        base_url=_BASE, auth=AuthSettings(strategy="bearer", token_env=_TOKEN_VARIABLE)
    )
    with FoundrySession(settings, transport=transport, sleep=_no_sleep) as session:
        monkeypatch.setenv(_TOKEN_VARIABLE, "rotated-value")
        session.get("/echo")
        session.get("/echo")
    sent = [_values(request.headers, "authorization") for request in received]
    assert sent == [[f"Bearer {_SENTINEL}"], [f"Bearer {_SENTINEL}"]]


# --- The response wrapper (AAP 0.4.3, AAP 0.4.6) --------------------------


def test_the_response_members_describe_the_exchange() -> None:
    """The status, the case-insensitive headers, the text, the JSON body and the elapsed time."""
    payload = {"ok": True, "echo": {"method": "GET"}}
    body = json.dumps(payload).encode()
    transport, _received = _recorder(200, body=body, headers={"Content-Type": "application/json"})
    with _session(transport=transport) as session:
        response = session.get(f"{_BASE}/echo")
    assert isinstance(response, FoundryResponse)
    assert response.status_code == 200
    assert response.text == body.decode()
    assert response.json() == payload
    assert response.headers["content-type"] == "application/json"
    assert response.headers["Content-Type"] == "application/json"
    assert isinstance(response.elapsed_ms, float)
    assert response.elapsed_ms >= 0.0
    assert response.source == "live"


def test_json_refuses_a_body_that_is_not_json() -> None:
    """A non-JSON body is a ``ContractError`` at ``$``, which is what exit 1 names (AAP 0.4.7)."""
    transport, _received = _recorder(500, body=b"<html>boom</html>")
    with _session(transport=transport) as session:
        response = session.get(f"{_BASE}/fail")
    assert response.status_code == 500
    with pytest.raises(ContractError) as raised:
        response.json()
    assert raised.value.json_path == "$"
    assert "not JSON" in str(raised.value)


def test_request_dump_reports_the_exchange_with_every_credential_masked() -> None:
    """The dump carries the published keys, and the header and the token parameter read ``***``."""
    transport, _received = _recorder()
    settings = SessionSettings(
        base_url=_BASE,
        redact_keys=("authorization", "token"),
        auth=AuthSettings(strategy="bearer", token_env=_TOKEN_VARIABLE),
    )
    with _session(settings, transport=transport) as session:
        response = session.post(
            "/echo", params={"token": _QUERY_TOKEN, "page": 2}, json={"message": "hello"}
        )
    dump = response.request_dump()
    assert set(dump) == {"method", "path", "query", "headers", "body_sha256", "body"}
    assert (dump["method"], dump["path"]) == ("POST", "/echo")
    assert dump["query"] == [["page", "2"], ["token", "***"]]
    headers = dump["headers"]
    assert isinstance(headers, dict)
    assert headers["authorization"] == "***"
    body_sha256 = dump["body_sha256"]
    assert isinstance(body_sha256, str)
    assert len(body_sha256) == 64
    serialised = json.dumps(dump)
    assert _SENTINEL not in serialised
    assert _QUERY_TOKEN not in serialised


# --- Every status is a value (AAP 0.4.3) ---------------------------------


@pytest.mark.parametrize("status", [200, 302, 404, 500])
def test_every_status_comes_back_as_a_response(status: int) -> None:
    """A client error, a server error and an unfollowed redirect are returned, never raised."""
    transport, received = _recorder(status, body=b'{"error": "boom"}')
    settings = SessionSettings(base_url=_BASE, retry=RetryPolicy(max_attempts=1))
    with _session(settings, transport=transport) as session:
        response = session.get("/echo")
    assert isinstance(response, FoundryResponse)
    assert response.status_code == status
    assert response.source == "live"
    assert len(received) == 1


def test_a_retried_status_is_returned_once_the_attempts_run_out() -> None:
    """A 503 under ``status_5xx`` is retried and then returned as a value, not raised."""
    transport, received = _statuses([503, 503, 200])
    delays: list[float] = []
    settings = SessionSettings(base_url=_BASE, retry=RetryPolicy(max_attempts=2))
    with _session(settings, transport=transport, sleep=delays.append) as session:
        response = session.get("/flaky")
    assert response.status_code == 503
    assert len(received) == 2
    assert delays == [0.2]


def test_a_status_outside_the_predicates_is_not_retried() -> None:
    """A 404 with only ``status_5xx`` configured makes exactly one attempt."""
    transport, received = _statuses([404, 200])
    delays: list[float] = []
    policy = RetryPolicy(max_attempts=3, retry_on=frozenset({"status_5xx"}))
    settings = SessionSettings(base_url=_BASE, retry=policy)
    with _session(settings, transport=transport, sleep=delays.append) as session:
        response = session.get("/missing")
    assert response.status_code == 404
    assert len(received) == 1
    assert delays == []


@pytest.mark.parametrize("verb", _METHODS)
def test_each_verb_delegates_to_request_with_its_method_and_arguments(verb: str) -> None:
    """The five verbs are conveniences over :meth:`FoundrySession.request` (assumption A13)."""
    transport, received = _recorder()
    with _session(transport=transport) as session:
        getattr(session, verb)(f"{_BASE}/echo", params={"page": 2}, json={"message": "hello"})
    sent = received[0]
    assert sent.method == verb.upper()
    assert sent.url.params.get("page") == "2"
    assert json.loads(sent.content) == {"message": "hello"}
