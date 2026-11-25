"""Unit tests for ``foundry.http``: URL joining, headers, retries, auth and the response wrapper."""

from __future__ import annotations

import base64
import json
from collections.abc import Callable, Mapping
from dataclasses import asdict
from typing import Final

import httpx
import pytest

from foundry.core.exceptions import ConfigError, ContractError, RetryExhaustedError, TransportError
from foundry.http import AuthSettings, FoundryResponse, FoundrySession, RetryPolicy, SessionSettings
from foundry.http.auth import resolve_auth
from foundry.http.retries import backoff, classify, should_retry
from foundry.http.url import join_url, redact_url, require_base_url, strip_userinfo
from foundry.obs.redaction import MAX_EXCERPT_BYTES, REDACTED, TRUNCATION_MARKER

_BASE: Final[str] = "http://peer.test"  # resolves nowhere: a real send is refused loudly
_TOKEN_ENV, _USER_ENV = ("FOUNDRY_SPEC_BEARER", "FOUNDRY_SPEC_USER")
_PASSWORD_ENV, _HEADER_ENV = ("FOUNDRY_SPEC_PASSWORD", "FOUNDRY_SPEC_API_KEY")
_SENTINEL: Final[str] = "s3cr3t-b34r3r"  # no real credential; a scanner-safe stand-in
_USER_VALUE, _PASSWORD_VALUE = ("spec-user", "spec-password")
_HEADER_VALUE, _QUERY_TOKEN = ("s3cr3t-h34d3r", "s3cr3t-qu3ry")
_BASIC: Final[str] = base64.b64encode(f"{_USER_VALUE}:{_PASSWORD_VALUE}".encode()).decode("ascii")
_ENVIRON_NAMES: Final[tuple[str, ...]] = (_TOKEN_ENV, _USER_ENV, _PASSWORD_ENV, _HEADER_ENV)
_ENVIRON_VALUES: Final[tuple[str, ...]] = (_SENTINEL, _USER_VALUE, _PASSWORD_VALUE, _HEADER_VALUE)
_ENVIRON: Final[Mapping[str, str]] = dict(zip(_ENVIRON_NAMES, _ENVIRON_VALUES, strict=True))
_PHASES: Final[tuple[str, ...]] = ("connect", "read", "write", "pool")
_FIVE_XX: Final[frozenset[str]] = frozenset({"status_5xx"})
_BEARER = AuthSettings(strategy="bearer", token_env=_TOKEN_ENV)
_HDR_AUTH = AuthSettings(strategy="header", header_name="X-Api-Key", header_value_env=_HEADER_ENV)
_BASIC_AUTH = AuthSettings(strategy="basic", user_env=_USER_ENV, password_env=_PASSWORD_ENV)
_LONG_VALUE: Final[str] = "n" * 1_200
_JSON: Final[dict[str, str]] = {"Content-Type": "application/json"}
_Mock = tuple[httpx.MockTransport, list[httpx.Request]]
_BAD_BASES = ("", "   ", "peer.test/api", "ftp://peer.test/api", "http://a:hunter2@peer.test")
_FOREIGN_BASE: Final[str] = "http://elsewhere.test"  # a different origin from ``_BASE``

#: Pairs that differ only in the origin one side names, so one parameterised test covers every
#: route out of the configured origin: another host, another port, a scheme swap either way, a base
#: URL that carries a path of its own, and the two loopback ports the QA probe used.
_OFF_ORIGIN_CASES: Final[tuple[tuple[str, str], ...]] = (
    ("http://peer.test", "http://elsewhere.test/steal"),
    ("http://peer.test:8080/api", "http://peer.test:8081/steal"),
    ("http://peer.test", "http://peer.test:81/steal"),
    ("http://peer.test:8080", "http://peer.test/steal"),
    ("http://peer.test", "https://peer.test/steal"),
    ("https://peer.test", "http://peer.test/steal"),
    ("http://127.0.0.1:45381", "http://127.0.0.1:39327/steal"),
)

#: Pairs that share one origin, the third to fifth writing the scheme's default port on one side
#: only (RFC 6454 folds it away), and the last differing in the host's case alone.
_SAME_ORIGIN_CASES: Final[tuple[tuple[str, str, str], ...]] = (
    ("http://peer.test", "http://peer.test/echo", "http://peer.test/echo"),
    ("http://peer.test/api", "http://peer.test/echo?page=2#frag", "http://peer.test/echo?page=2"),
    ("http://peer.test", "http://peer.test:80/echo", "http://peer.test:80/echo"),
    ("http://peer.test:80", "http://peer.test/echo", "http://peer.test/echo"),
    ("https://peer.test", "https://peer.test:443/echo", "https://peer.test:443/echo"),
    ("http://PEER.test", "http://peer.TEST/echo", "http://peer.TEST/echo"),
)


def _mock(respond: Callable[[httpx.Request], httpx.Response]) -> _Mock:
    sent: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return respond(request)

    return httpx.MockTransport(handle), sent


def _recorder(status: int = 200, body: bytes = b"") -> _Mock:
    return _mock(lambda request: httpx.Response(status, content=body))


def _faults(error: type[httpx.HTTPError]) -> _Mock:
    def fail(request: httpx.Request) -> httpx.Response:
        raise error("refused", request=request)

    return _mock(fail)


def _echo(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content) if request.content else {}
    echoed = {"echo": body, "received_bytes": len(request.content)}
    return httpx.Response(201, headers=_JSON, content=json.dumps(echoed).encode())


def _session(
    settings: SessionSettings | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
    sleep: Callable[[float], None] = lambda seconds: None,
) -> FoundrySession:
    chosen = SessionSettings() if settings is None else settings
    return FoundrySession(chosen, transport=transport, sleep=sleep, environ=_ENVIRON)


def test_the_session_requires_a_base_url_and_bounds_its_deadline() -> None:
    assert join_url(f"{_BASE}:8080/api", "orders") == f"{_BASE}:8080/api/orders"
    assert join_url(f"{_BASE}:8080/api/", "orders") == f"{_BASE}:8080/api/orders"
    assert join_url(f"{_BASE}/api", "/orders") == f"{_BASE}/orders"
    assert join_url(f"{_BASE}/api", "orders?page=1#frag") == f"{_BASE}/api/orders?page=1"
    assert join_url(None, f"{_BASE}/echo?page=2") == f"{_BASE}/echo?page=2"
    transport, received = _recorder()
    with _session(SessionSettings(base_url=_BASE), transport=transport) as session:
        session.get("/echo?page=1", params={"page": 2, "limit": 10})
    assert str(received[0].url) == f"{_BASE}/echo?page=2&limit=10"
    assert received[0].url.params.multi_items() == [("page", "2"), ("limit", "10")]
    for base in _BAD_BASES:
        with pytest.raises(ConfigError) as raised:
            require_base_url(base)
        assert (raised.value.key, "hunter2" in str(raised.value)) == ("http.base_url", False)
    for configured, requested, expected in ((2.5, None, 2.5), (10.0, 1.5, 1.5)):
        transport, received = _recorder()
        with _session(SessionSettings(timeout=configured), transport=transport) as session:
            session.get(f"{_BASE}/echo", timeout=requested)
        assert received[0].extensions["timeout"] == dict.fromkeys(_PHASES, expected)
    for value in (-1.0, 0.0, 301.0):
        transport, received = _recorder()
        with _session(transport=transport) as session, pytest.raises(ConfigError) as raised:
            session.get(f"{_BASE}/echo", timeout=value)
        assert raised.value.key == "http.timeout" and "0 < t <= 300" in str(raised.value)
        assert received == []
    cleaned = redact_url(f"http://alice:hunter2@peer.test/echo?token={_QUERY_TOKEN}&page=2")
    serialised = json.dumps({"url": cleaned})
    assert all(secret not in serialised for secret in ("alice", "hunter2", _QUERY_TOKEN))
    assert "***" in serialised and "peer.test" in serialised and "page=2" in serialised
    assert strip_userinfo("http://alice:hunter2@peer.test/echo") == "http://peer.test/echo"


def test_the_dumps_describe_the_exchange_and_mask_every_credential() -> None:
    transport, received = _mock(_echo)
    settings = SessionSettings(
        base_url=_BASE,
        headers=(("X-Trace", "cfg"), ("X-Static", "1")),
        auth=_BEARER,
        redact_keys=("authorization", "token", "password"),
    )
    request_body = {"message": "hello", "note": _LONG_VALUE, "password": _SENTINEL}
    with _session(settings, transport=transport) as session:
        session.get("/echo", headers={"x-trace": "req"})
        session.get("/echo", headers={"Authorization": "Bearer manual"})
        posted = session.post("/echo", params={"token": _QUERY_TOKEN, "page": 2}, json=request_body)
    merged, own = received[0].headers, received[1].headers
    assert merged.get_list("x-trace") == ["req"] and merged.get_list("x-static") == ["1"]
    assert merged.get_list("authorization") == [f"Bearer {_SENTINEL}"]
    assert own.get_list("authorization") == ["Bearer manual"]
    dump = posted.request_dump()
    assert set(dump) == {"method", "path", "query", "headers", "body_sha256", "body"}
    assert (dump["method"], dump["path"]) == ("POST", "/echo")
    assert dump["query"] == [["page", "2"], ["token", "***"]]
    headers = dump["headers"]
    assert isinstance(headers, dict) and headers["authorization"] == "***"
    assert isinstance(dump["body_sha256"], str) and len(dump["body_sha256"]) == 64
    sent = received[2].content.decode()
    assert min(len(sent.encode()), len(posted.text.encode())) > MAX_EXCERPT_BYTES
    dumped = [dump["body"], posted.response_dump()["body"]]
    assert dumped == [
        sent.replace(_SENTINEL, REDACTED),
        posted.text.replace(_SENTINEL, REDACTED),
    ]
    for text_body in dumped:
        assert isinstance(text_body, str) and _LONG_VALUE in text_body
        assert _SENTINEL not in text_body and TRUNCATION_MARKER not in text_body
    payload = {"ok": True, "echo": {"method": "GET"}}
    body = json.dumps(payload).encode()
    transport, _ = _mock(lambda request: httpx.Response(200, headers=_JSON, content=body))
    with _session(transport=transport) as session:
        response = session.get(f"{_BASE}/echo")
    assert isinstance(response, FoundryResponse) and response.status_code == 200
    assert (response.text, response.json()) == (body.decode(), payload)
    assert (
        response.headers["content-type"] == response.headers["Content-Type"] == "application/json"
    )
    assert isinstance(response.elapsed_ms, float) and response.elapsed_ms >= 0.0
    with _session(transport=_recorder(500, b"<html>boom</html>")[0]) as session:
        failed = session.get(f"{_BASE}/fail")
    with pytest.raises(ContractError, match="not JSON") as raised:
        failed.json()
    assert (failed.status_code, raised.value.json_path) == (500, "$")


def test_the_predicate_and_backoff_tables_are_the_published_ones() -> None:
    for fault, status, expected in (
        (httpx.ConnectTimeout, None, "timeout"),
        (httpx.ConnectError, None, "connect"),
        (httpx.RemoteProtocolError, None, None),
        (None, 500, "status_5xx"),
        (None, 599, "status_5xx"),
        (None, 429, "status_429"),
        (None, 404, None),
        (None, 200, None),
    ):
        assert classify(None if fault is None else fault("fault"), status) == expected
    assert should_retry("status_429", {"status_5xx"}) is False
    assert should_retry("status_429", {"status_5xx", "status_429"}) is True
    assert should_retry(None, {"status_5xx"}) is False
    assert RetryPolicy(retry_on=_FIVE_XX).should_retry("status_429") is False
    policy = RetryPolicy(backoff_base=0.2, backoff_max=2.0)
    assert [policy.delay_for(attempt) for attempt in range(1, 6)] == [0.2, 0.4, 0.8, 1.6, 2.0]
    assert (backoff(1, base=0.2, maximum=2.0), backoff(5, base=0.2, maximum=2.0)) == (0.2, 2.0)
    assert backoff(2, base=10.0, maximum=60.0) == 20.0


def test_a_failing_request_waits_the_ladder_and_reports_every_attempt() -> None:
    for policy, delays, retried in (
        (RetryPolicy(max_attempts=5), [0.2, 0.4, 0.8, 1.6], True),
        (RetryPolicy(backoff_base=0.0, max_attempts=2), [0.0], True),
        (RetryPolicy(max_attempts=1, retry_on=_FIVE_XX), [], False),
    ):
        mock, received = _faults(httpx.ConnectError)
        recorded: list[float] = []
        settings = SessionSettings(base_url=_BASE, retry=policy)
        ctx = _session(settings, transport=mock, sleep=recorded.append)
        with ctx as session, pytest.raises(TransportError) as raised:
            session.get("/health")
        error = raised.value
        assert (recorded, len(received)) == (delays, len(delays) + 1)
        assert (error.method, error.path, error.cause) == ("GET", "/health", "connect")
        assert isinstance(error, RetryExhaustedError) is retried
        if not retried:
            continue
        rows = error.attempts
        assert [(r.number, r.outcome, r.retry_on) for r in rows] == [
            (number, "connect", "connect") for number in range(1, len(delays) + 2)
        ]
        assert all(r.elapsed_ms >= 0.0 and "ConnectError" in (r.message or "") for r in rows)


def test_every_status_is_returned_and_retried_only_under_its_predicate() -> None:
    for codes, policy, want in (
        ([302, 302], RetryPolicy(max_attempts=1), []),
        ([500, 500], RetryPolicy(max_attempts=1), []),
        ([503, 503, 200], RetryPolicy(max_attempts=2), [0.2]),
        ([404, 200], RetryPolicy(max_attempts=3, retry_on=_FIVE_XX), []),
    ):
        transport, received = _mock(lambda request, todo=list(codes): httpx.Response(todo.pop(0)))
        recorded: list[float] = []
        settings = SessionSettings(base_url=_BASE, retry=policy)
        with _session(settings, transport=transport, sleep=recorded.append) as session:
            response = session.get("/echo")
        expected = (codes[len(want)], len(want) + 1, want)
        assert (response.status_code, len(received), recorded) == expected


def test_each_strategy_resolves_its_header_once(monkeypatch: pytest.MonkeyPatch) -> None:
    for settings, expected in (
        (_BEARER, ("Authorization", f"Bearer {_SENTINEL}")),
        (_HDR_AUTH, ("X-Api-Key", _HEADER_VALUE)),
        (_BASIC_AUTH, ("Authorization", f"Basic {_BASIC}")),
        (AuthSettings(strategy="none"), None),
        (None, None),
    ):
        header = resolve_auth(settings, environ=_ENVIRON)
        assert (None if header is None else (header.name, header.value)) == expected
    header = resolve_auth(_BEARER, environ=_ENVIRON)
    assert header is not None and header.masked() == "***"
    with pytest.raises(ConfigError) as missing:
        resolve_auth(_BEARER, environ={})
    assert missing.value.key == "http.auth.token_env" and _TOKEN_ENV in str(missing.value)
    surfaces = [repr(_BEARER), json.dumps(asdict(_BEARER)), repr(header), str(missing.value)]
    assert not [one for one in surfaces if _SENTINEL in one]
    assert asdict(_BEARER)["token_env"] == _TOKEN_ENV
    transport, received = _recorder()
    monkeypatch.setenv(_TOKEN_ENV, _SENTINEL)
    settings = SessionSettings(base_url=_BASE, auth=_BEARER)
    with FoundrySession(settings, transport=transport, sleep=lambda seconds: None) as session:
        monkeypatch.setenv(_TOKEN_ENV, "rotated-value")
        session.get("/echo")
        session.get("/echo")
    sent = [request.headers.get_list("authorization") for request in received]
    assert sent == [[f"Bearer {_SENTINEL}"]] * 2


@pytest.mark.parametrize(("base_url", "target"), _OFF_ORIGIN_CASES)
def test_an_absolute_target_off_the_base_origin_is_refused_and_quotes_no_value(
    base_url: str, target: str
) -> None:
    with pytest.raises(ConfigError) as raised:
        join_url(base_url, target)
    error = raised.value
    assert error.key == "http.base_url"
    assert str(error) == "request URL must share http.base_url's origin"
    hint = error.hint
    assert hint is not None and "relative path" in hint and "change http.base_url" in hint
    surfaces = (str(error), error.format_message(), repr(error))
    assert [text for text in surfaces if target in text or _SENTINEL in text] == []


@pytest.mark.parametrize(("base_url", "target", "expected"), _SAME_ORIGIN_CASES)
def test_an_absolute_target_on_the_base_origin_is_returned_unchanged(
    base_url: str, target: str, expected: str
) -> None:
    assert join_url(base_url, target) == expected


@pytest.mark.parametrize("base_url", (None, "", "   "))
@pytest.mark.parametrize(
    "target", ("http://peer.test/echo?page=2", "https://elsewhere.test:8443/steal")
)
def test_a_session_without_a_base_url_still_issues_an_absolute_target(
    base_url: str | None, target: str
) -> None:
    assert join_url(base_url, target) == target


def test_a_foreign_target_carrying_userinfo_is_refused_and_never_echoed() -> None:
    target = "http://alice:hunter2@elsewhere.test/steal"
    with pytest.raises(ConfigError) as raised:
        join_url(_BASE, target)
    error = raised.value
    assert error.key == "http.base_url"
    surfaces = (str(error), error.format_message(), repr(error))
    assert [word for word in ("alice", "hunter2", target) if word in " ".join(surfaces)] == []


def test_the_session_refuses_an_off_origin_target_even_when_the_join_is_bypassed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport, received = _recorder()
    settings = SessionSettings(base_url=_BASE, auth=_BEARER)

    def bypass(base_url: str | None, path: str) -> str:
        return path

    monkeypatch.setattr("foundry.http.session.join_url", bypass)
    with _session(settings, transport=transport) as session, pytest.raises(ConfigError) as raised:
        session.get(f"{_FOREIGN_BASE}/steal")
    assert raised.value.key == "http.base_url"
    assert str(raised.value) == "request URL must share http.base_url's origin"
    assert received == []
