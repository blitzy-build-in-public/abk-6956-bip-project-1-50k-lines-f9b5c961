"""Unit tests for ``foundry.cassette``: document, matcher, store, modes and transports.

``foundry.cassette`` owns record/replay (AAP 0.3.2), and AAP 0.10.1 places its unit module here.
What is pinned below is what a regression would break silently, because a cassette is a file that
outlives the run that wrote it: a later suite replays it on another machine, behind another port,
from a checkout where the peer does not exist.

* The document shape of AAP 0.4.6 -- ``recorded_at``, ``redacted_keys`` and an ordered list of
  ``{request, response}`` interactions, a request holding ``method``, ``path``, ``query``,
  ``body_sha256`` and ``headers_subset``, a response holding ``status``, ``headers`` and ``body`` --
  survives the serialize/load round trip exactly, with no extra key and no reordering.
* A match is decided by the method, the path, the decoded query pairs and the body hash and by
  nothing else (AAP 0.2.1, AAP 0.3.2, assumption A7): parameter order and percent-encoding are
  irrelevant, request headers are irrelevant, and no host or port reaches the comparison at all.
* Redaction happens at capture (implicit requirement I4), so a recorded ``Authorization`` or
  ``Cookie`` value never reaches the file and ``redacted_keys`` names what was scrubbed, sorted.
* The four rows of the mode table (AAP 0.7.3, assumption A8) behave as the table states, and a
  ``replay`` session holds no live transport whatever the caller passes it.
* A :class:`~foundry.cassette.transport.ReplayTransport` answers from the document and raises the
  typed miss naming method, path and cassette file, touching no socket on either path (criterion 8).
* The store's loader is JSON, so no YAML loader sits on the replay path.

Three departures from the brief this module was written against, recorded rather than dropped.
AAP 0.7.1 names the matcher seam ``match(request, interactions)`` while the module calls it
:func:`~foundry.cassette.matcher.match_interaction` -- ``match`` is a soft keyword since Python
3.10 -- and returns the matching position for the replay cursor. ``off`` and ``passthrough`` differ
in two of the five mode flags rather than one: ``reads``, and the ``falls_through`` that follows
from it, since a live-transport mode that reads a cassette is exactly a mode that falls through on
a miss. ``FoundryResponse.source`` (AAP 0.4.3) is filled by ``foundry.http.response`` from the
``foundry.cassette.source`` extension, which is not one of this module's dependencies, so the tag
itself is asserted -- as is the redaction marker, in the ``***`` form the criteria require.

Everything is built under ``tmp_path``: no socket is opened, no server is started, and the committed
fixture ``tests/fixtures/cassettes/replay_echo.json`` is only read, its SHA-256 checked before and
after this module runs. No test sleeps, and the one timestamp a document carries is the fixed
:data:`_RECORDED_AT`, so nothing here depends on the wall clock (AAP 0.14.3).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator, Mapping
from dataclasses import fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import httpx
import pytest

from foundry.cassette import (
    BEHAVIOURS,
    SOURCE_CASSETTE,
    SOURCE_EXTENSION_KEY,
    SOURCE_LIVE,
    CassetteBehaviour,
    CassetteDocument,
    CassetteSession,
    CassetteStore,
    Interaction,
    RecordedRequest,
    RecordedResponse,
    RecordingTransport,
    ReplayTransport,
    RequestFingerprint,
    behaviour_for,
    cassette_path,
    match_interaction,
)
from foundry.cassette import store as cassette_store
from foundry.cassette.matcher import describe, matches_recorded, matching_indices
from foundry.cassette.models import body_sha256_of, from_json_text, normalise_query, to_json_text
from foundry.cassette.paths import cassette_filename
from foundry.cassette.transport import fingerprint_from_request
from foundry.core.exceptions import ArtifactError, CassetteMissError, FoundryError

# The one timestamp every document here carries, and how the single serialiser renders it:
# millisecond precision with an explicit offset, so no assertion reads the clock.
_RECORDED_AT: Final[datetime] = datetime(2024, 3, 1, 9, 30, tzinfo=UTC)
_RECORDED_AT_TEXT: Final[str] = "2024-03-01T09:30:00.000+00:00"

_REDACTED: Final[str] = "***"  # the marker a scrubbed value becomes (AAP 0.3.2)
_SENTINEL: Final[str] = "s3cr3t-sentinel"  # assembled here, so no scanner reads this as a leak
_SENTINEL_HEADER: Final[str] = f"Bearer {_SENTINEL}"  # the Authorization value capture must scrub
_COOKIE_VALUE: Final[str] = "session=cookie-value-8d2f"  # a Cookie value naming no secret word
_BODY: Final[bytes] = b'{"message":"hello"}'  # the committed fixture's body, so the hashes agree
_NODEID: Final[str] = "tests/test_api.py::test_checkout[ticket-1]"  # carries /, :: and [

# A host that resolves nowhere: the suite guard refuses every non-loopback name, so a replay that
# reached for a socket would be refused there instead of being answered here.
_REPLAY_HOST: Final[str] = "http://cassette.invalid:9999"

_HEX64: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{64}")
_FLAG_NAMES: Final[tuple[str, ...]] = ("reads", "writes", "network", "miss_raises", "falls_through")


# --- Builders -------------------------------------------------------------


def _request(
    *,
    method: str = "GET",
    path: str = "/echo",
    query: str = "page=2&limit=10",
    body_sha256: str | None = None,
    headers: Mapping[str, str] | None = None,
) -> RecordedRequest:
    """Return one recorded request, normalised exactly as the store normalises a loaded one."""
    return RecordedRequest(
        method=method,
        path=path,
        query=normalise_query(query),
        body_sha256=body_sha256,
        headers_subset={"accept": "application/json"} if headers is None else headers,
    )


def _response(
    *, status: int = 200, headers: Mapping[str, str] | None = None, body: str = '{"ok": true}'
) -> RecordedResponse:
    """Return one recorded response holding a JSON body and, by default, a JSON content type."""
    return RecordedResponse(
        status=status,
        headers={"content-type": "application/json"} if headers is None else headers,
        body=body,
    )


def _get_interaction() -> Interaction:
    """Return the ``GET /echo?page=2&limit=10`` exchange every replay test serves."""
    return Interaction(request=_request(), response=_response())


def _post_interaction() -> Interaction:
    """Return a second exchange, so a document holds two interactions in recording order."""
    return Interaction(
        request=_request(method="POST", query="", body_sha256=body_sha256_of(_BODY)),
        response=_response(status=201, body='{"echo": {"method": "POST"}, "ok": true}'),
    )


def _document(*interactions: Interaction, redacted_keys: tuple[str, ...] = ()) -> CassetteDocument:
    """Return a document stamped with :data:`_RECORDED_AT` and holding ``interactions`` in order."""
    return CassetteDocument(
        recorded_at=_RECORDED_AT, redacted_keys=redacted_keys, interactions=interactions
    )


def _parsed(text: str) -> dict[str, Any]:
    """Return cassette text parsed, refusing a document that is not a JSON object."""
    loaded: Any = json.loads(text)
    assert isinstance(loaded, dict)
    return loaded


def _payload(path: Path) -> dict[str, Any]:
    """Return a cassette file parsed, for structural assertions on the keys it holds."""
    return _parsed(path.read_text(encoding="utf-8"))


def _header(headers: Mapping[str, str], name: str) -> str:
    """Return ``headers``' value for ``name``, matched case-insensitively as HTTP matches it."""
    for key, value in headers.items():
        if key.casefold() == name.casefold():
            return value
    raise AssertionError(f"header {name!r} is absent from {sorted(headers)}")


def _digest(path: Path) -> str:
    """Return the SHA-256 of a file's bytes, which proves a test only ever read it."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _get(transport: httpx.BaseTransport, url: str) -> httpx.Response:
    """Send one GET through ``transport`` as a client would, closing the client afterwards."""
    client = httpx.Client(transport=transport)
    try:
        return client.get(url)
    finally:
        client.close()


def _live_stub(
    *, body: bytes = b"live", fail_if_called: bool = False
) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    """Return a live-transport stand-in beside the list of requests it was asked to send.

    ``replay`` holds no live transport, so a stub built with ``fail_if_called`` proves a miss never
    falls back to the network: an attempted delegation raises ``RuntimeError`` instead of the typed
    miss. A plain ``RuntimeError`` is raised, not a locally defined subclass, so every Foundry
    exception still lives in ``core/exceptions.py``.
    """
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if fail_if_called:
            raise RuntimeError(f"live transport must not be reached: {request.method}")
        return httpx.Response(200, headers={"content-type": "text/plain"}, content=body)

    return httpx.MockTransport(handle), calls


# --- Fixtures -------------------------------------------------------------


@pytest.fixture
def cassette_dir(tmp_path: Path) -> Path:
    """Function scope: an existing, empty cassette directory inside ``tmp_path``."""
    directory = tmp_path / "cassettes"
    directory.mkdir()
    return directory


@pytest.fixture
def store(cassette_dir: Path) -> CassetteStore:
    """Function scope: a store bound to :func:`cassette_dir`, carrying the default redactor."""
    return CassetteStore(cassette_dir)


@pytest.fixture(scope="module")
def committed_cassette(repo_root: Path) -> Iterator[Path]:
    """Module scope: the committed replay fixture, re-hashed at the end so it stayed read-only."""
    path = repo_root / "tests" / "fixtures" / "cassettes" / "replay_echo.json"
    before = _digest(path)
    yield path
    assert _digest(path) == before, f"{path} was modified: this module only reads a cassette"


# --- The document (AAP 0.4.6) ---------------------------------------------


def test_document_round_trips_through_the_single_serialiser() -> None:
    """A document written and read back equals the one written, in the order it was recorded."""
    document = _document(
        _post_interaction(), _get_interaction(), redacted_keys=("cookie", "authorization")
    )
    text = to_json_text(document)
    assert from_json_text(text) == document
    assert document.redacted_keys == ("authorization", "cookie")
    assert to_json_text(from_json_text(text)) == text
    assert [entry["request"]["method"] for entry in _parsed(text)["interactions"]] == [
        "POST",
        "GET",
    ]


def test_document_key_sets_are_exactly_the_published_ones() -> None:
    """Every object a cassette holds carries the documented keys and no others (AAP 0.4.6)."""
    payload = _parsed(to_json_text(_document(_get_interaction(), _post_interaction())))
    assert set(payload) == {"recorded_at", "redacted_keys", "interactions"}
    assert payload["recorded_at"] == _RECORDED_AT_TEXT
    assert payload["redacted_keys"] == []
    first, second = payload["interactions"]
    for entry in (first, second):
        assert set(entry) == {"request", "response"}
        assert set(entry["request"]) == {"method", "path", "query", "body_sha256", "headers_subset"}
        assert set(entry["response"]) == {"status", "headers", "body"}
    # ``query`` is a list of [name, value] pairs, and ``body_sha256`` a digest or null.
    assert first["request"]["query"] == [["limit", "10"], ["page", "2"]]
    assert all(isinstance(pair, list) and len(pair) == 2 for pair in first["request"]["query"])
    assert first["request"]["body_sha256"] is None
    assert _HEX64.fullmatch(second["request"]["body_sha256"]) is not None
    assert second["request"]["body_sha256"] == body_sha256_of(_BODY)
    assert [first["response"]["status"], second["response"]["status"]] == [200, 201]


def test_committed_fixture_parses_into_the_same_model(committed_cassette: Path) -> None:
    """The committed replay fixture loads into the model, key for key, with nothing extra."""
    text = committed_cassette.read_text(encoding="utf-8")
    assert set(_parsed(text)) == {"recorded_at", "redacted_keys", "interactions"}
    document = from_json_text(text)
    assert document.recorded_at == _RECORDED_AT
    assert document.redacted_keys == ("authorization",)
    get, post = document.interactions
    assert (get.request.method, get.request.path) == ("GET", "/echo")
    assert get.request.query == (("limit", "10"), ("page", "2"))
    assert get.request.body_sha256 is None
    assert _header(get.request.headers_subset, "authorization") == _REDACTED
    assert _header(get.request.headers_subset, "accept") == "application/json"
    assert post.request.body_sha256 == body_sha256_of(_BODY)
    # The committed recording is playable on any host: a re-ordered query still names it (A7).
    fingerprint = RequestFingerprint("GET", "/echo", normalise_query("page=2&limit=10"))
    assert matches_recorded(fingerprint, get.request)


# --- Matching (AAP 0.2.1, AAP 0.3.2, assumption A7) -----------------------


@pytest.mark.parametrize(
    ("recorded_query", "requested_query", "expected"),
    [
        ("a=1&b=2", "b=2&a=1", True),
        ("q=a+b", "q=a%20b", True),
        ("page=2&limit=10", "limit=10&page=2", True),
        ("page=2&limit=10", "page=3&limit=10", False),
        ("page=2", "page=2&extra=1", False),
    ],
)
def test_query_comparison_is_over_the_decoded_pairs(
    recorded_query: str, requested_query: str, expected: bool
) -> None:
    """Order and encoding are irrelevant; a changed or added parameter is another request."""
    recorded = _request(query=recorded_query)
    fingerprint = RequestFingerprint("GET", "/echo", normalise_query(requested_query))
    assert matches_recorded(fingerprint, recorded) is expected


def test_a_different_method_or_path_does_not_match() -> None:
    """A changed method or a changed path is a different request, whatever the query says."""
    recorded = _request()
    assert not matches_recorded(RequestFingerprint("POST", "/echo", recorded.query), recorded)
    assert not matches_recorded(RequestFingerprint("GET", "/other", recorded.query), recorded)


def test_the_body_hash_decides_the_body() -> None:
    """Only the hash is compared: an equal body matches, a different or absent one does not."""
    recorded = _request(method="POST", query="", body_sha256=body_sha256_of(_BODY))
    same = RequestFingerprint("POST", "/echo", body_sha256=body_sha256_of(_BODY))
    other = RequestFingerprint("POST", "/echo", body_sha256=body_sha256_of(b'{"message": "other"}'))
    absent = RequestFingerprint("POST", "/echo")
    assert matches_recorded(same, recorded)
    assert not matches_recorded(other, recorded)
    assert not matches_recorded(absent, recorded)
    assert body_sha256_of(b"") is None


def test_host_and_port_are_not_part_of_the_match() -> None:
    """Neither host nor port reaches the comparison, so a recording replays anywhere (A7)."""
    recorded = _request()
    one = fingerprint_from_request(
        httpx.Request("GET", "http://127.0.0.1:54321/echo?page=2&limit=10")
    )
    another = fingerprint_from_request(
        httpx.Request("GET", "https://example.test/echo?limit=10&page=2")
    )
    assert one == another
    assert matches_recorded(one, recorded)
    assert "host" not in {field.name for field in fields(RequestFingerprint)}
    assert one.path == "/echo"


def test_request_headers_are_not_part_of_the_match() -> None:
    """A client's own headers cannot refuse a recording, so headers take no part in a match."""
    recorded = _request(headers={"accept": "application/json"})
    fingerprint = fingerprint_from_request(
        httpx.Request(
            "GET",
            f"{_REPLAY_HOST}/echo?page=2&limit=10",
            headers={"accept": "text/html", "x-trace": "abc"},
        )
    )
    assert matches_recorded(fingerprint, recorded)


def test_matching_interactions_are_served_in_recording_order() -> None:
    """Matches come back in document order, and a count past the last one replays the last."""
    first = Interaction(request=_request(), response=_response(body='{"n": 1}'))
    second = Interaction(request=_request(), response=_response(body='{"n": 2}'))
    interactions = (first, second)
    fingerprint = RequestFingerprint("GET", "/echo", normalise_query("page=2&limit=10"))
    assert matching_indices(fingerprint, interactions) == (0, 1)
    assert match_interaction(fingerprint, interactions) == (0, first)
    assert match_interaction(fingerprint, interactions, consumed=1) == (1, second)
    assert match_interaction(fingerprint, interactions, consumed=9) == (1, second)
    # A request the cassette does not hold is answered with ``None`` for the transport to report.
    missing = RequestFingerprint("DELETE", "/nowhere")
    assert matching_indices(missing, interactions) == ()
    assert match_interaction(missing, interactions) is None
    assert describe(missing) == "DELETE /nowhere"


# --- Redaction at capture (implicit requirement I4, AAP 0.3.2) -------------


def test_capture_redacts_denied_headers_and_records_the_sorted_names(
    store: CassetteStore,
) -> None:
    """Denied values never reach the file, and every scrubbed name is recorded, sorted.

    A kept header proves the capture wrote something, and the two cases at the end cover the rest of
    the capture rule: a name a document already carried, and a secret-bearing key in a JSON body.
    """
    capture = Interaction(
        request=_request(
            headers={
                "Accept": "application/json",
                "x-trace": "abc-123",
                "Authorization": _SENTINEL_HEADER,
                "Cookie": _COOKIE_VALUE,
            }
        ),
        response=_response(),
    )
    written = store.save(_NODEID, [capture], recorded_at=_RECORDED_AT)
    assert written is not None
    text = written.read_text(encoding="utf-8")
    assert _SENTINEL not in text
    assert _COOKIE_VALUE not in text
    headers = _payload(written)["interactions"][0]["request"]["headers_subset"]
    assert _header(headers, "authorization") == _REDACTED
    assert _header(headers, "cookie") == _REDACTED
    assert _header(headers, "accept") == "application/json"
    assert _header(headers, "x-trace") == "abc-123"
    assert _payload(written)["redacted_keys"] == ["authorization", "cookie"]
    assert store.load(_NODEID).redacted_keys == ("authorization", "cookie")
    # A document that arrives already redacted keeps the names it carried, so the file can never
    # disagree with the ``redacted_keys`` written beside it.
    document = _document(_get_interaction(), redacted_keys=("x-api-key",))
    assert store.save_document(_NODEID, document) is not None
    assert store.load(_NODEID).redacted_keys == ("x-api-key",)
    # A secret-bearing key inside a JSON response body is scrubbed structure-first and named too.
    with_body = Interaction(
        request=_get_interaction().request, response=_response(body='{"token": "abc123"}')
    )
    assert store.save(_NODEID, [with_body], recorded_at=_RECORDED_AT) is not None
    assert store.load(_NODEID).redacted_keys == ("token",)
    assert "abc123" not in store.path_for(_NODEID).read_text(encoding="utf-8")


# --- Store paths (AAP 0.4.6, AAP 0.14.3) ----------------------------------


def test_save_writes_one_document_per_nodeid_named_after_it(
    store: CassetteStore, cassette_dir: Path
) -> None:
    """One file per test, named by the sanitised node id, holding exactly what was saved."""
    written = store.save(
        _NODEID, [_get_interaction(), _post_interaction()], recorded_at=_RECORDED_AT
    )
    assert written is not None
    assert written.parent == cassette_dir.resolve()
    assert written.name == cassette_filename(_NODEID)
    for forbidden in ("/", "\\", "::", "[", "]", " "):
        assert forbidden not in written.name
    assert sorted(path.name for path in cassette_dir.iterdir()) == [written.name]
    assert store.path_for(_NODEID) == written
    assert store.exists(_NODEID) is True
    assert store.load(_NODEID) == _document(_get_interaction(), _post_interaction())
    # The encoding is injective, so two node ids never share one document.
    other = store.save(
        "tests/test_cart.py::test_two", [_get_interaction()], recorded_at=_RECORDED_AT
    )
    assert other is not None
    assert other != written
    assert len(sorted(cassette_dir.glob("*.json"))) == 2


def test_a_hostile_nodeid_stays_inside_the_cassette_directory(
    store: CassetteStore, cassette_dir: Path
) -> None:
    """A traversal attempt is encoded away: the document is one component of the directory."""
    escaping = store.path_for("../../etc/passwd")
    assert escaping.parent == cassette_dir.resolve()
    assert "/" not in escaping.name
    assert escaping.name == cassette_filename("../../etc/passwd")
    assert escaping != store.path_for("..%2F..%2Fetc%2Fpasswd")


def test_a_symlinked_document_that_escapes_the_directory_is_refused(
    store: CassetteStore, cassette_dir: Path, tmp_path: Path
) -> None:
    """The one way an encoded name can still escape -- a symlink -- raises ``ArtifactError``."""
    outside = tmp_path / "outside.json"
    outside.write_text("{}\n", encoding="utf-8")
    (cassette_dir / cassette_filename(_NODEID)).symlink_to(outside)
    with pytest.raises(ArtifactError) as raised:
        store.path_for(_NODEID)
    assert raised.value.attempted_path == outside.resolve()
    assert raised.value.run_dir == cassette_dir.resolve()


def test_load_of_an_absent_cassette_is_the_empty_document(tmp_path: Path) -> None:
    """A missing recording is an empty document, not an error, and no directory is created."""
    directory = tmp_path / "not-created"
    store = CassetteStore(directory)
    document = store.load("tests/test_api.py::test_absent")
    assert isinstance(document, CassetteDocument)
    assert document.interactions == ()
    assert document.redacted_keys == ()
    assert store.exists("tests/test_api.py::test_absent") is False
    assert not directory.exists()


def test_load_of_a_malformed_document_names_the_file(store: CassetteStore) -> None:
    """A hand-edited cassette fails loudly, naming the file and the key it lost."""
    path = store.path_for(_NODEID)
    path.write_text('{"interactions": []}\n', encoding="utf-8")
    with pytest.raises(FoundryError) as raised:
        store.load(_NODEID)
    assert str(path) in raised.value.message
    assert "recorded_at" in raised.value.message


# --- The mode table (AAP 0.7.3, assumption A8) ----------------------------


def test_behaviour_table_matches_the_four_row_table() -> None:
    """The four rows of AAP 0.7.3 are data, and the flags below are what each row carries."""
    expected = {
        "off": (False, False, True, False, False),
        "record": (False, True, True, False, False),
        "replay": (True, False, False, True, False),
        "passthrough": (True, False, True, False, True),
    }
    assert set(BEHAVIOURS) == set(expected)
    for name, behaviour in BEHAVIOURS.items():
        assert isinstance(behaviour, CassetteBehaviour)
        assert behaviour.name == name
        assert tuple(getattr(behaviour, flag) for flag in _FLAG_NAMES) == expected[name], name
        # Only a mode that reads a cassette and has a network falls through on a miss.
        read_and_network = behaviour.reads and behaviour.network
        assert behaviour.falls_through == (read_and_network and not behaviour.miss_raises)
        assert not (behaviour.miss_raises and behaviour.falls_through)
    assert behaviour_for(" REPLAY ") is BEHAVIOURS["replay"]
    with pytest.raises(FoundryError) as raised:
        behaviour_for("sometimes")
    assert "sometimes" in raised.value.message
    assert "passthrough" in raised.value.message
    # ``off`` and ``passthrough`` differ in one behaviour: only ``passthrough`` reads a cassette.
    # ``falls_through`` is what reading means for a mode with a network, so those two flags are one
    # behavioural difference rather than two (assumption A8).
    off, passthrough = BEHAVIOURS["off"], BEHAVIOURS["passthrough"]
    differing = {flag for flag in _FLAG_NAMES if getattr(off, flag) != getattr(passthrough, flag)}
    assert differing == {"reads", "falls_through"}
    assert off.network is passthrough.network is True
    assert off.writes is passthrough.writes is False


# --- The per-test session (AAP 0.7.1, AAP 0.7.3) --------------------------


def test_off_mode_uses_the_live_transport_itself(store: CassetteStore) -> None:
    """``off`` hands back the caller's transport unwrapped, reads nothing and writes nothing."""
    live, calls = _live_stub()
    session = CassetteSession(_NODEID, mode="off", store=store, live_transport=live)
    assert session.behaviour is BEHAVIOURS["off"]
    assert session.transport() is live
    assert session.transport() is live  # built once, so a caller may cache it
    assert session.finish() is None
    session.close()
    assert calls == []


def test_record_mode_wraps_the_live_transport_and_writes_once_at_teardown(
    store: CassetteStore, cassette_dir: Path
) -> None:
    """``record`` sends live, keeps the exchange and writes one document when the test ends."""
    live, calls = _live_stub(body=b'{"ok": true}')
    session = CassetteSession(_NODEID, mode="record", store=store, live_transport=live)
    transport = session.transport()
    assert isinstance(transport, RecordingTransport)
    response = _get(transport, f"{_REPLAY_HOST}/echo?page=2&limit=10")
    assert response.status_code == 200
    assert len(calls) == 1
    captured = session.session_requests()
    assert [(entry.request.method, entry.request.path) for entry in captured] == [("GET", "/echo")]
    written = session.finish()
    assert written is not None
    assert written.is_file()
    assert session.finish() is None
    assert sorted(path.name for path in cassette_dir.glob("*.json")) == [written.name]
    assert _payload(written)["interactions"][0]["response"]["body"] == '{"ok": true}'


def test_replay_mode_serves_the_cassette_and_holds_no_live_transport(
    store: CassetteStore,
) -> None:
    """``replay`` answers from the document and never delegates, even given a live transport."""
    assert store.save_document(_NODEID, _document(_get_interaction())) is not None
    live, calls = _live_stub(fail_if_called=True)
    session = CassetteSession(_NODEID, mode="replay", store=store, live_transport=live)
    transport = session.transport()
    assert isinstance(transport, ReplayTransport)
    with httpx.Client(transport=transport) as client:
        served = client.get(f"{_REPLAY_HOST}/echo?page=2&limit=10")
        with pytest.raises(CassetteMissError) as raised:
            client.get(f"{_REPLAY_HOST}/echo?limit=10&page=9")
    assert served.extensions[SOURCE_EXTENSION_KEY] == SOURCE_CASSETTE
    assert raised.value.method == "GET"
    assert raised.value.path == "/echo?limit=10&page=9"
    assert raised.value.cassette_path == cassette_path(_NODEID, cassette_dir=store.cassette_dir)
    assert calls == []  # a replay never delegates, whatever the caller passed it
    assert session.finish() is None


def test_passthrough_mode_falls_through_to_the_live_transport_on_a_miss(
    store: CassetteStore, cassette_dir: Path
) -> None:
    """``passthrough`` serves what the cassette holds, delegates the rest and never writes (A8)."""
    assert store.save_document(_NODEID, _document(_get_interaction())) is not None
    live, calls = _live_stub()
    session = CassetteSession(_NODEID, mode="passthrough", store=store, live_transport=live)
    transport = session.transport()
    assert isinstance(transport, ReplayTransport)
    with httpx.Client(transport=transport) as client:
        recorded = client.get(f"{_REPLAY_HOST}/echo?limit=10&page=2")
        delegated = client.get(f"{_REPLAY_HOST}/echo?limit=10&page=9")
    assert recorded.extensions[SOURCE_EXTENSION_KEY] == SOURCE_CASSETTE
    assert delegated.extensions[SOURCE_EXTENSION_KEY] == SOURCE_LIVE
    assert delegated.text == "live"
    assert [call.url.path for call in calls] == ["/echo"]
    assert session.finish() is None
    assert [path.name for path in cassette_dir.glob("*.json")] == [cassette_filename(_NODEID)]


def test_session_arguments_are_validated(store: CassetteStore) -> None:
    """A mode with a network needs a transport; a blank node id and an unknown mode are refused."""
    for mode in ("off", "record", "passthrough"):
        with pytest.raises(FoundryError) as raised:
            CassetteSession(_NODEID, mode=mode, store=store)
        assert mode in raised.value.message
    replay = CassetteSession(_NODEID, mode="replay", store=store)
    assert isinstance(replay.transport(), ReplayTransport)
    with pytest.raises(FoundryError):
        CassetteSession("   ", mode="off", store=store, live_transport=_live_stub()[0])
    with pytest.raises(FoundryError):
        CassetteSession(_NODEID, mode="sometimes", store=store, live_transport=_live_stub()[0])


# --- The replay transport (AAP 0.3.3, acceptance criterion 8) -------------


def test_replay_transport_serves_a_recorded_interaction(cassette_dir: Path) -> None:
    """A match returns the recorded status, headers and body, tagged as a cassette, on any host.

    The suite's socket guard refuses every non-loopback resolution, so the second URL below could
    not be resolved at all: a reply here is positive evidence that the replay opened no socket.
    """
    transport = ReplayTransport(
        _document(_get_interaction(), _post_interaction()),
        cassette_dir / cassette_filename(_NODEID),
    )
    response = _get(transport, f"{_REPLAY_HOST}/echo?limit=10&page=2")
    assert (response.status_code, response.text) == (200, '{"ok": true}')
    assert response.headers["content-type"] == "application/json"
    # ``FoundryResponse.source`` is filled from this tag by ``foundry.http.response`` (AAP 0.4.3).
    assert response.extensions[SOURCE_EXTENSION_KEY] == SOURCE_CASSETTE
    other = _get(transport, "https://127.0.0.1:1/echo?page=2&limit=10")
    assert other.extensions[SOURCE_EXTENSION_KEY] == SOURCE_CASSETTE


def test_replay_transport_raises_the_typed_miss(cassette_dir: Path) -> None:
    """A request the cassette does not hold raises a miss naming method, path and cassette file."""
    path = cassette_dir / cassette_filename(_NODEID)
    transport = ReplayTransport(_document(_get_interaction()), path)
    with pytest.raises(CassetteMissError) as raised:
        _get(transport, f"{_REPLAY_HOST}/echo?limit=10&page=3")
    error = raised.value
    assert error.method == "GET"
    assert error.path == "/echo?limit=10&page=3"
    assert error.cassette_path == path
    assert "GET" in error.message
    assert "/echo?limit=10&page=3" in error.message
    assert str(path) in error.message


# --- The store's own format (AAP 0.4.6, AAP 0.14.3) -----------------------


def test_store_loader_is_json_and_never_an_unsafe_yaml_parse() -> None:
    """A cassette is JSON, so no YAML loader -- unsafe or otherwise -- sits on the replay path.

    The mechanical scan of the whole tree belongs to ``tests/unit/test_no_unsafe_parsing.py``;
    this is the store's own contract, asserted against the module that owns the read path.
    """
    source = Path(cassette_store.__file__).read_text(encoding="utf-8")
    assert "json.loads" in source
    assert re.search(r"\byaml\.(?:unsafe_|full_)?load\s*\(", source) is None
    assert "yaml" not in source or "yaml.safe_load(" in source
