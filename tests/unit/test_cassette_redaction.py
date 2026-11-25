"""Unit tests for query-value redaction in ``foundry.cassette``: capture, replay and the miss.

A redacted query parameter never reaches the committed file in the clear: capture writes the ``***``
marker, names the parameter in ``redacted_keys`` and replay matches on that same marker.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import httpx
import pytest

from foundry.cassette import (
    SOURCE_CASSETTE,
    SOURCE_EXTENSION_KEY,
    CassetteDocument,
    CassetteSession,
    CassetteStore,
    Interaction,
    RecordedRequest,
    RecordedResponse,
)
from foundry.cassette.models import normalise_query, to_json_text
from foundry.core.exceptions import CassetteMissError

# Assembled from parts, so no secret scanner reads a test value as a leaked credential.
_SENTINEL: Final[str] = "-".join(("S3NT1NEL", "capture", "4a91"))
_OTHER: Final[str] = "-".join(("S3NT1NEL", "capture", "b3ef"))
_LEGACY: Final[str] = "-".join(("S3NT1NEL", "plaintext", "earlier"))
_MASKED: Final[str] = "***"  # the marker a redacted value becomes (AAP 0.3.2)
_NODEID: Final[str] = "tests/test_health.py::test_health[ticket-1]"
_RECORDED_AT: Final[datetime] = datetime(2024, 3, 1, 9, 30, tzinfo=UTC)
_BODY: Final[str] = '{"ok": true}'
# The host is irrelevant to a match (assumption A7) and no request below is sent on it.
_HOST: Final[str] = "http://cassette.invalid:9999"
_OTHER_HOST: Final[str] = "https://127.0.0.1:1"


def _live() -> httpx.MockTransport:
    return httpx.MockTransport(
        lambda request: httpx.Response(
            200, headers={"content-type": "application/json"}, content=_BODY.encode("utf-8")
        )
    )


def _get(transport: httpx.BaseTransport, url: str) -> httpx.Response:
    with httpx.Client(transport=transport) as client:
        return client.get(url)


def _document(*interactions: Interaction) -> CassetteDocument:
    return CassetteDocument(recorded_at=_RECORDED_AT, interactions=interactions)


@pytest.fixture
def store(tmp_path: Path) -> CassetteStore:
    directory = tmp_path / "cassettes"
    directory.mkdir()
    return CassetteStore(directory)


@pytest.mark.parametrize(
    ("query", "document_query", "keys"),
    [
        (f"token={_SENTINEL}&page=2", [["page", "2"], ["token", _MASKED]], ["token"]),
        (
            f"token={_SENTINEL}&page=2&token={_OTHER}",
            [["page", "2"], ["token", _MASKED], ["token", _MASKED]],
            ["token"],
        ),
    ],
)
def test_a_recorded_query_holds_the_marker_and_names_the_parameter(
    store: CassetteStore, query: str, document_query: list[list[str]], keys: list[str]
) -> None:
    session = CassetteSession(_NODEID, mode="record", store=store, live_transport=_live())
    response = _get(session.transport(), f"{_HOST}/health?{query}")
    written = session.finish()
    session.close()
    assert response.text == _BODY and written is not None
    text = written.read_text(encoding="utf-8")
    payload: Any = json.loads(text)
    assert payload["interactions"][0]["request"]["path"] == "/health"
    assert payload["interactions"][0]["request"]["query"] == document_query
    assert payload["redacted_keys"] == keys
    assert _SENTINEL not in text and _OTHER not in text


@pytest.mark.parametrize(
    ("query", "source", "miss_path"),
    [
        (f"token={_SENTINEL}&page=2", SOURCE_CASSETTE, None),
        (f"token={_OTHER}&page=2", SOURCE_CASSETTE, None),
        (f"token={_SENTINEL}&page=3", None, f"/health?page=3&token={_MASKED}"),
    ],
)
def test_a_recorded_token_bearing_request_replays_and_a_changed_plain_value_misses(
    store: CassetteStore, query: str, source: str | None, miss_path: str | None
) -> None:
    record = CassetteSession(_NODEID, mode="record", store=store, live_transport=_live())
    _get(record.transport(), f"{_HOST}/health?token={_SENTINEL}&page=2")
    assert record.finish() is not None
    record.close()
    replay = CassetteSession(_NODEID, mode="replay", store=store)
    transport = replay.transport()
    try:
        if source is None:
            with pytest.raises(CassetteMissError) as raised:
                _get(transport, f"{_OTHER_HOST}/health?{query}")
            error = raised.value
            expected = (miss_path, store.path_for(_NODEID))
            assert (error.method, error.path, error.cassette_path) == ("GET", *expected)
            assert "GET" in error.message and str(expected[1]) in error.message
            assert _SENTINEL not in error.message and _SENTINEL not in error.path
        else:
            response = _get(transport, f"{_OTHER_HOST}/health?{query}")
            assert (response.extensions[SOURCE_EXTENSION_KEY], response.text) == (source, _BODY)
    finally:
        replay.close()


def test_a_cassette_holding_a_plaintext_query_value_still_replays(store: CassetteStore) -> None:
    path = store.path_for(_NODEID)
    earlier = _document(
        Interaction(
            request=RecordedRequest(
                method="GET", path="/health", query=normalise_query(f"token={_LEGACY}&page=2")
            ),
            response=RecordedResponse(status=200, body=_BODY),
        )
    )
    path.write_text(to_json_text(earlier), encoding="utf-8")
    before = path.read_text(encoding="utf-8")
    assert _LEGACY in before
    session = CassetteSession(_NODEID, mode="replay", store=store)
    try:
        response = _get(session.transport(), f"{_OTHER_HOST}/health?token={_SENTINEL}&page=2")
    finally:
        session.close()
    assert (response.extensions[SOURCE_EXTENSION_KEY], response.text) == (SOURCE_CASSETTE, _BODY)
    assert path.read_text(encoding="utf-8") == before
