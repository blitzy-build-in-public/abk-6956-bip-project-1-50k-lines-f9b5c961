"""``foundry.cassette``: the document, the matcher, the store and the four modes, with the published
keys, the match rule (method, path, decoded query pairs, body hash), the ``***`` marker, the typed
miss and the committed fixture's round trip all pinned. A redacted query value never reaches the
committed file in the clear: capture writes the marker, names the parameter in ``redacted_keys`` and
replay matches on that same marker."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, cast

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
from foundry.cassette.matcher import describe, matches_recorded, matching_indices
from foundry.cassette.models import body_sha256_of, from_json_text, normalise_query, to_json_text
from foundry.cassette.paths import cassette_filename
from foundry.cassette.transport import fingerprint_from_request
from foundry.core.exceptions import ArtifactError, CassetteMissError, FoundryError

# One fixed timestamp, no clock read, and ``***``: the marker every scrubbed value becomes.
_RECORDED_AT: Final[datetime] = datetime(2024, 3, 1, 9, 30, tzinfo=UTC)
_REDACTED: Final[str] = "***"
_SENTINEL, _COOKIE_VALUE = "s3cr3t-sentinel", "session=cookie-value-8d2f"
_SENTINEL_HEADER: Final[str] = f"Bearer {_SENTINEL}"  # the Authorization value capture must scrub
_BODY: Final[bytes] = httpx.Request(
    "POST", "http://cassette.test/echo", json={"message": "hello"}
).read()
_DIGEST: Final[str | None] = body_sha256_of(_BODY)
_NODEID: Final[str] = "tests/test_api.py::test_checkout[ticket-1]"  # carries /, :: and [
# A host that resolves nowhere: the guard refuses it, so no session below reaches a socket.
_REPLAY_HOST: Final[str] = "http://cassette.invalid:9999"
_PROBE_URL: Final[str] = "http://127.0.0.1:54321/echo?page=2&limit=10"
_FLAGS: Final[tuple[str, ...]] = ("reads", "writes", "network", "miss_raises", "falls_through")
# Query-value redaction: assembled from parts, so no secret scanner reads a test value as a leak.
_QUERY_SENTINEL: Final[str] = "-".join(("S3NT1NEL", "capture", "4a91"))
_QUERY_OTHER: Final[str] = "-".join(("S3NT1NEL", "capture", "b3ef"))
_QUERY_LEGACY: Final[str] = "-".join(("S3NT1NEL", "plaintext", "earlier"))
_QUERY_NODEID: Final[str] = "tests/test_health.py::test_health[ticket-1]"
_JSON_BODY: Final[str] = '{"ok": true}'
_OTHER_HOST: Final[str] = "https://127.0.0.1:1"


def _request(
    *,
    method: str = "GET",
    query: str = "page=2&limit=10",
    body_sha256: str | None = None,
    headers: Mapping[str, str] | None = None,
) -> RecordedRequest:
    allowed = {"accept": "application/json"} if headers is None else headers
    return RecordedRequest(method, "/echo", normalise_query(query), body_sha256, allowed)


def _response(*, status: int = 200, body: str = '{"ok": true}') -> RecordedResponse:
    return RecordedResponse(status, {"content-type": "application/json"}, body)


_GET_INTERACTION: Final[Interaction] = Interaction(_request(), _response())
_POST_INTERACTION: Final[Interaction] = Interaction(
    _request(method="POST", query="", body_sha256=_DIGEST),
    _response(status=201, body='{"echo": {"method": "POST"}, "ok": true}'),
)


def _document(*interactions: Interaction, redacted_keys: tuple[str, ...] = ()) -> CassetteDocument:
    return CassetteDocument(_RECORDED_AT, redacted_keys, interactions)


def _parsed(text: str) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(text))


def _get(transport: httpx.BaseTransport, url: str) -> httpx.Response:
    with httpx.Client(transport=transport) as client:
        return client.get(url)


def _live_stub(*, body: bytes = b"live") -> tuple[httpx.MockTransport, list[httpx.Request]]:
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, headers={"content-type": "text/plain"}, content=body)

    return httpx.MockTransport(handle), calls


def _json_stub() -> httpx.MockTransport:
    return _live_stub(body=_JSON_BODY.encode("utf-8"))[0]


@pytest.fixture
def store(tmp_path: Path) -> CassetteStore:
    (tmp_path / "cassettes").mkdir()
    return CassetteStore(tmp_path / "cassettes")


class TestCassette:
    def test_the_document_round_trips_with_the_published_keys(self, repo_root: Path) -> None:
        document = _document(
            _POST_INTERACTION, _GET_INTERACTION, redacted_keys=("cookie", "authorization")
        )
        text = to_json_text(document)
        assert from_json_text(text) == document
        assert document.redacted_keys == ("authorization", "cookie")
        assert to_json_text(from_json_text(text)) == text
        payload = _parsed(text)
        keys = {"recorded_at", "redacted_keys", "interactions"}
        assert (set(payload), payload["recorded_at"]) == (keys, "2024-03-01T09:30:00.000+00:00")
        path = repo_root / "tests" / "fixtures" / "cassettes" / "replay_echo.json"
        before = path.read_bytes()
        stored = from_json_text(path.read_text(encoding="utf-8"))
        get, post = stored.interactions
        assert (stored.recorded_at, stored.redacted_keys) == (_RECORDED_AT, ("authorization",))
        assert (get.request.method, get.request.path) == ("GET", "/echo")
        assert get.request.query == (("limit", "10"), ("page", "2"))
        folded = {n.casefold(): v for n, v in get.request.headers_subset.items()}
        assert (folded["authorization"], folded["accept"]) == (_REDACTED, "application/json")
        assert (get.request.body_sha256, post.request.body_sha256) == (None, _DIGEST)
        assert matches_recorded(RequestFingerprint("GET", "/echo", get.request.query), get.request)
        assert path.read_bytes() == before, f"{path} was modified: this module only reads it"

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
        self, recorded_query: str, requested_query: str, expected: bool
    ) -> None:
        recorded = _request(query=recorded_query)
        asked = RequestFingerprint("GET", "/echo", normalise_query(requested_query))
        assert matches_recorded(asked, recorded) is expected

    def test_a_match_and_the_order_it_is_served_in_are_the_published_rules(self) -> None:
        recorded, posted = _request(), _request(method="POST", query="", body_sha256=_DIGEST)
        assert not matches_recorded(RequestFingerprint("POST", "/echo", recorded.query), recorded)
        assert not matches_recorded(RequestFingerprint("GET", "/other", recorded.query), recorded)
        assert matches_recorded(RequestFingerprint("POST", "/echo", body_sha256=_DIGEST), posted)
        changed = RequestFingerprint("POST", "/echo", body_sha256=body_sha256_of(b"x"))
        assert not matches_recorded(changed, posted) and body_sha256_of(b"") is None
        assert not matches_recorded(RequestFingerprint("POST", "/echo"), posted)
        headers = {"accept": "text/html", "x-trace": "abc"}
        one = fingerprint_from_request(httpx.Request("GET", _PROBE_URL))
        another = fingerprint_from_request(
            httpx.Request("GET", f"{_REPLAY_HOST}/echo?limit=10&page=2", headers=headers)
        )
        assert one == another and one.path == "/echo"
        assert matches_recorded(one, recorded) and matches_recorded(another, recorded)
        pairs = (_GET_INTERACTION, Interaction(_request(), _response(body='{"n": 2}')))
        match = RequestFingerprint("GET", "/echo", normalise_query("page=2&limit=10"))
        assert matching_indices(match, pairs) == (0, 1)
        assert match_interaction(match, pairs) == (0, pairs[0])
        assert [match_interaction(match, pairs, consumed=n)[1] for n in (1, 9)] == [pairs[1]] * 2
        missing = RequestFingerprint("DELETE", "/nowhere")
        assert (matching_indices(missing, pairs), match_interaction(missing, pairs)) == ((), None)
        assert describe(missing) == "DELETE /nowhere"

    def test_capture_redacts_denied_headers_and_their_names(self, store: CassetteStore) -> None:
        headers = {"Accept": "application/json", "x-trace": "abc-123"}
        denied = {"Authorization": _SENTINEL_HEADER, "Cookie": _COOKIE_VALUE}
        capture = Interaction(_request(headers=headers | denied), _response())
        written = store.save(_NODEID, [capture], recorded_at=_RECORDED_AT)
        text = store.path_for(_NODEID).read_text(encoding="utf-8")
        assert written is not None and _SENTINEL not in text and _COOKIE_VALUE not in text
        request = _parsed(text)["interactions"][0]["request"]
        folded = {n.casefold(): v for n, v in request["headers_subset"].items()}
        assert [folded[name] for name in ("authorization", "cookie")] == [_REDACTED] * 2
        assert (folded["accept"], folded["x-trace"]) == ("application/json", "abc-123")
        assert store.load(_NODEID).redacted_keys == ("authorization", "cookie")
        assert store.save_document(_NODEID, _document(redacted_keys=("x-api-key",))) is not None
        assert store.load(_NODEID).redacted_keys == ("x-api-key",)
        with_body = Interaction(_request(), _response(body='{"token": "abc123"}'))
        assert store.save(_NODEID, [with_body], recorded_at=_RECORDED_AT) is not None
        assert store.load(_NODEID).redacted_keys == ("token",)
        assert "abc123" not in store.path_for(_NODEID).read_text(encoding="utf-8")

    def test_a_hostile_nodeid_and_symlink_stay_inside_the_store(self, store: CassetteStore) -> None:
        escaping = store.path_for("../../etc/passwd")
        assert escaping.parent == store.cassette_dir.resolve() and "/" not in escaping.name
        assert escaping.name == cassette_filename("../../etc/passwd")
        assert escaping != store.path_for("..%2F..%2Fetc%2Fpasswd")
        outside, sandbox = store.cassette_dir.parent / "outside.json", store.cassette_dir.resolve()
        outside.write_text("{}\n", encoding="utf-8")
        (sandbox / cassette_filename(_NODEID)).symlink_to(outside)
        with pytest.raises(ArtifactError) as raised:
            store.path_for(_NODEID)
        assert (raised.value.attempted_path, raised.value.run_dir) == (outside.resolve(), sandbox)

    def test_absent_is_empty_and_a_malformed_one_names_the_file(self, store: CassetteStore) -> None:
        absent = CassetteStore(store.cassette_dir.parent / "not-created")
        empty = absent.load(_NODEID)
        assert (empty.redacted_keys, empty.interactions) == ((), ())
        assert absent.exists(_NODEID) is False and not absent.cassette_dir.exists()
        path = store.path_for(_NODEID)
        path.write_text('{"interactions": []}\n', encoding="utf-8")
        with pytest.raises(FoundryError) as raised:
            store.load(_NODEID)
        assert str(path) in raised.value.message and "recorded_at" in raised.value.message

    def test_behaviour_table_matches_the_four_row_table(self) -> None:
        expected = {
            "off": (False, False, True, False, False),
            "record": (False, True, True, False, False),
            "replay": (True, False, False, True, False),
            "passthrough": (True, False, True, False, True),
        }
        assert set(BEHAVIOURS) == set(expected)
        for name, behaviour in BEHAVIOURS.items():
            assert isinstance(behaviour, CassetteBehaviour) and behaviour.name == name
            assert tuple(getattr(behaviour, flag) for flag in _FLAGS) == expected[name], name
        assert behaviour_for(" REPLAY ") is BEHAVIOURS["replay"]
        with pytest.raises(FoundryError) as raised:
            behaviour_for("sometimes")
        assert "sometimes" in raised.value.message and "passthrough" in raised.value.message

    def test_off_uses_the_live_transport_and_record_wraps_it(self, store: CassetteStore) -> None:
        live, calls = _live_stub(body=b'{"ok": true}')
        off = CassetteSession(_NODEID, mode="off", store=store, live_transport=live)
        assert off.behaviour is BEHAVIOURS["off"]
        assert off.transport() is live and off.transport() is live
        assert (off.finish(), calls) == (None, [])
        off.close()
        for mode in ("off", "record", "passthrough"):
            with pytest.raises(FoundryError) as raised:
                CassetteSession(_NODEID, mode=mode, store=store)
            assert "live transport" in raised.value.message
        record = CassetteSession(_NODEID, mode="record", store=store, live_transport=live)
        assert isinstance(record.transport(), RecordingTransport)
        assert _get(record.transport(), f"{_REPLAY_HOST}/echo?page=2&limit=10").status_code == 200
        assert len(calls) == 1
        captured = record.session_requests()
        assert [(e.request.method, e.request.path) for e in captured] == [("GET", "/echo")]
        written = record.finish()
        assert written is not None and written.is_file() and record.finish() is None
        assert [path.name for path in store.cassette_dir.glob("*.json")] == [written.name]
        body = _parsed(written.read_text(encoding="utf-8"))["interactions"][0]["response"]["body"]
        assert body == '{"ok": true}'

    def test_replay_and_passthrough_serve_the_cassette(self, store: CassetteStore) -> None:
        assert store.save_document(_NODEID, _document(_GET_INTERACTION)) is not None
        live, calls = _live_stub()
        replay = CassetteSession(_NODEID, mode="replay", store=store, live_transport=live)
        served = _get(replay.transport(), f"{_REPLAY_HOST}/echo?page=2&limit=10")
        with pytest.raises(CassetteMissError) as raised:
            _get(replay.transport(), f"{_REPLAY_HOST}/echo?limit=10&page=9")
        assert isinstance(replay.transport(), ReplayTransport)
        assert (served.status_code, served.text) == (200, '{"ok": true}')
        assert served.headers["content-type"] == "application/json"
        assert served.extensions[SOURCE_EXTENSION_KEY] == SOURCE_CASSETTE
        expected = cassette_path(_NODEID, cassette_dir=store.cassette_dir)
        miss = raised.value
        assert (miss.path, miss.cassette_path) == ("/echo?limit=10&page=9", expected)
        assert miss.method == "GET" and "GET" in miss.message and str(expected) in miss.message
        assert calls == [] and replay.finish() is None
        passing = CassetteSession(_NODEID, mode="passthrough", store=store, live_transport=live)
        kept = _get(passing.transport(), f"{_REPLAY_HOST}/echo?limit=10&page=2")
        passed = _get(passing.transport(), f"{_REPLAY_HOST}/echo?limit=10&page=9")
        key = SOURCE_EXTENSION_KEY
        assert [kept.extensions[key], passed.extensions[key]] == [SOURCE_CASSETTE, SOURCE_LIVE]
        assert (passed.text, [call.url.path for call in calls]) == ("live", ["/echo"])
        assert passing.finish() is None
        assert [p.name for p in store.cassette_dir.glob("*.json")] == [cassette_filename(_NODEID)]

    def test_session_arguments_are_validated(self, store: CassetteStore) -> None:
        live = _live_stub()[0]
        for nodeid, mode in (("   ", "off"), (_NODEID, "sometimes")):
            with pytest.raises(FoundryError):
                CassetteSession(nodeid, mode=mode, store=store, live_transport=live)


class TestQueryRedaction:
    @pytest.mark.parametrize(
        ("query", "document_query", "keys"),
        [
            (f"token={_QUERY_SENTINEL}&page=2", [["page", "2"], ["token", _REDACTED]], ["token"]),
            (
                f"token={_QUERY_SENTINEL}&page=2&token={_QUERY_OTHER}",
                [["page", "2"], ["token", _REDACTED], ["token", _REDACTED]],
                ["token"],
            ),
        ],
    )
    def test_a_recorded_query_holds_the_marker_and_names_the_parameter(
        self, store: CassetteStore, query: str, document_query: list[list[str]], keys: list[str]
    ) -> None:
        live = _json_stub()
        session = CassetteSession(_QUERY_NODEID, mode="record", store=store, live_transport=live)
        response = _get(session.transport(), f"{_REPLAY_HOST}/health?{query}")
        written = session.finish()
        session.close()
        assert response.text == _JSON_BODY and written is not None
        text = written.read_text(encoding="utf-8")
        payload = _parsed(text)
        assert payload["interactions"][0]["request"]["path"] == "/health"
        assert payload["interactions"][0]["request"]["query"] == document_query
        assert payload["redacted_keys"] == keys
        assert _QUERY_SENTINEL not in text and _QUERY_OTHER not in text

    @pytest.mark.parametrize(
        ("query", "source", "miss_path"),
        [
            (f"token={_QUERY_SENTINEL}&page=2", SOURCE_CASSETTE, None),
            (f"token={_QUERY_OTHER}&page=2", SOURCE_CASSETTE, None),
            (f"token={_QUERY_SENTINEL}&page=3", None, f"/health?page=3&token={_REDACTED}"),
        ],
    )
    def test_a_recorded_token_bearing_request_replays_and_a_changed_plain_value_misses(
        self, store: CassetteStore, query: str, source: str | None, miss_path: str | None
    ) -> None:
        live = _json_stub()
        record = CassetteSession(_QUERY_NODEID, mode="record", store=store, live_transport=live)
        _get(record.transport(), f"{_REPLAY_HOST}/health?token={_QUERY_SENTINEL}&page=2")
        assert record.finish() is not None
        record.close()
        replay = CassetteSession(_QUERY_NODEID, mode="replay", store=store)
        transport = replay.transport()
        try:
            if source is None:
                with pytest.raises(CassetteMissError) as raised:
                    _get(transport, f"{_OTHER_HOST}/health?{query}")
                error = raised.value
                expected = (miss_path, store.path_for(_QUERY_NODEID))
                assert (error.method, error.path, error.cassette_path) == ("GET", *expected)
                assert "GET" in error.message and str(expected[1]) in error.message
                assert _QUERY_SENTINEL not in error.message and _QUERY_SENTINEL not in error.path
            else:
                response = _get(transport, f"{_OTHER_HOST}/health?{query}")
                assert response.extensions[SOURCE_EXTENSION_KEY] == source
                assert response.text == _JSON_BODY
        finally:
            replay.close()

    def test_a_cassette_holding_a_plaintext_query_value_still_replays(
        self, store: CassetteStore
    ) -> None:
        path = store.path_for(_QUERY_NODEID)
        query = normalise_query(f"token={_QUERY_LEGACY}&page=2")
        request = RecordedRequest(method="GET", path="/health", query=query)
        recorded = RecordedResponse(status=200, body=_JSON_BODY)
        path.write_text(to_json_text(_document(Interaction(request, recorded))), encoding="utf-8")
        before = path.read_text(encoding="utf-8")
        assert _QUERY_LEGACY in before
        session = CassetteSession(_QUERY_NODEID, mode="replay", store=store)
        url = f"{_OTHER_HOST}/health?token={_QUERY_SENTINEL}&page=2"
        try:
            response = _get(session.transport(), url)
        finally:
            session.close()
        assert response.extensions[SOURCE_EXTENSION_KEY] == SOURCE_CASSETTE
        assert response.text == _JSON_BODY and path.read_text(encoding="utf-8") == before
