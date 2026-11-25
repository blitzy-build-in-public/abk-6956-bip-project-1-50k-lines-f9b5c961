"""Unit tests for query-value redaction in ``foundry.cassette``: capture, replay and the miss.

``foundry.cassette`` owns record/replay (AAP 0.3.2), and a cassette is a file this plan commits,
so a query parameter whose name the redaction engine redacts must never be written in the clear
(AAP 0.2.1, AAP 0.3.2 "Security architecture"). Masking it is only half the rule: the marker a
document holds has to be the marker the replay lookup compares against, or the recording stops
replaying the run that wrote it, and AAP 0.4.9 forbids a matching change that invalidates a
cassette already on disk.

What is pinned here: a ``record`` run writes the marker in the document's ``query`` and names the
parameter in ``redacted_keys``, keeping the other parameters and a repeated name (AAP 0.4.6); a
token-bearing recording replays through the same session stack with the same token and with a
different one, because both are the same parameter, while a changed value under a name the engine
does not redact is another request and misses; a miss carries the marker in the message and in the
``path`` attribute while still naming the method, the path and the cassette file (AAP 0.4.7), which
keeps the value out of ``failures[].message`` and out of the report's panel; and a cassette written
before this rule existed, holding a plaintext query value, still replays, because the matcher reads
a marker on either side as a value that matches any (AAP 0.4.9).

The cases live here rather than in ``tests/unit/test_cassette.py``, which stands at its 700-line
ceiling (``scripts/count_lines.sh``, AAP 0.14.2). Every cassette is built under ``tmp_path`` and
every exchange goes through ``httpx.MockTransport``, so no socket is opened and nothing sleeps.
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
    ReplayTransport,
)
from foundry.cassette.models import normalise_query, to_json_text
from foundry.cassette.paths import cassette_filename
from foundry.core.exceptions import CassetteMissError

# Assembled from parts, so no secret scanner reads a test value as a leaked credential.
_SENTINEL: Final[str] = "-".join(("S3NT1NEL", "capture", "4a91"))
_OTHER: Final[str] = "-".join(("S3NT1NEL", "capture", "b3ef"))
_LEGACY: Final[str] = "-".join(("S3NT1NEL", "plaintext", "earlier"))

_MASKED: Final[str] = "***"  # the marker a redacted value becomes (AAP 0.3.2)
_NODEID: Final[str] = "tests/test_health.py::test_health[ticket-1]"
_RECORDED_AT: Final[datetime] = datetime(2024, 3, 1, 9, 30, tzinfo=UTC)
_BODY: Final[str] = '{"ok": true}'

# The host is irrelevant to a match (assumption A7) and no request below is sent on it: a record
# exchange is answered by the mock transport and a replay exchange by the cassette.
_HOST: Final[str] = "http://cassette.invalid:9999"
_OTHER_HOST: Final[str] = "https://127.0.0.1:1"


# --- Builders and fixtures ------------------------------------------------


def _live(body: str = _BODY) -> httpx.MockTransport:
    """Return a live-transport stand-in that answers every request with ``body``."""

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "application/json"}, content=body.encode("utf-8")
        )

    return httpx.MockTransport(handle)


def _get(transport: httpx.BaseTransport, url: str) -> httpx.Response:
    """Send one GET through ``transport`` as a client would, closing the client afterwards."""
    client = httpx.Client(transport=transport)
    try:
        return client.get(url)
    finally:
        client.close()


def _payload(path: Path) -> dict[str, Any]:
    """Return a cassette file parsed, refusing a document that is not a JSON object."""
    loaded: Any = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _document(*interactions: Interaction) -> CassetteDocument:
    """Return a document stamped with :data:`_RECORDED_AT`, holding ``interactions`` in order."""
    return CassetteDocument(recorded_at=_RECORDED_AT, interactions=interactions)


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


# --- Capture (AAP 0.2.1, AAP 0.4.6) ---------------------------------------


@pytest.mark.parametrize(
    ("query", "expected_query", "expected_keys"),
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
    store: CassetteStore,
    query: str,
    expected_query: list[list[str]],
    expected_keys: list[str],
) -> None:
    """The capture masks a secret value, keeps the other parameters and names the key."""
    session = CassetteSession(_NODEID, mode="record", store=store, live_transport=_live())
    response = _get(session.transport(), f"{_HOST}/health?{query}")
    captured = session.session_requests()
    written = session.finish()
    session.close()

    assert response.text == _BODY
    assert written is not None
    # The capture itself is masked, so nothing downstream can put the value back in the clear.
    assert list(captured[0].request.query) == [tuple(pair) for pair in expected_query]
    # The written document masks it too, and names the parameter beside the value it replaced.
    payload = _payload(written)
    assert payload["interactions"][0]["request"]["path"] == "/health"
    assert payload["interactions"][0]["request"]["query"] == expected_query
    assert payload["redacted_keys"] == expected_keys
    text = written.read_text(encoding="utf-8")
    assert _SENTINEL not in text
    assert _OTHER not in text


# --- Replay (AAP 0.2.1, AAP 0.4.6, AAP 0.4.9) -----------------------------


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
    """Record then replay: the masked parameter matches, a changed plain one is another request."""
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
            assert raised.value.path == miss_path
            assert _SENTINEL not in raised.value.message
        else:
            response = _get(transport, f"{_OTHER_HOST}/health?{query}")
            assert response.extensions[SOURCE_EXTENSION_KEY] == source
            assert response.text == _BODY
    finally:
        replay.close()


# --- The miss diagnostic (AAP 0.4.7) --------------------------------------


@pytest.mark.parametrize(
    ("query", "expected_target"),
    [
        (f"token={_SENTINEL}", f"/echo?token={_MASKED}"),
        (f"page=2&token={_SENTINEL}", f"/echo?page=2&token={_MASKED}"),
    ],
)
def test_a_miss_on_a_token_bearing_request_carries_the_marker(
    cassette_dir: Path, query: str, expected_target: str
) -> None:
    """The message and the ``path`` attribute show the marker and still name what a person needs."""
    path = cassette_dir / cassette_filename(_NODEID)
    transport = ReplayTransport(
        _document(
            Interaction(
                request=RecordedRequest(
                    method="GET", path="/other", query=normalise_query("page=2")
                ),
                response=RecordedResponse(status=200, body=_BODY),
            )
        ),
        path,
    )
    with pytest.raises(CassetteMissError) as raised:
        _get(transport, f"{_HOST}/echo?{query}")
    error = raised.value

    assert error.method == "GET"
    assert error.path == expected_target
    assert error.cassette_path == path
    assert "GET" in error.message
    assert expected_target in error.message
    assert str(path) in error.message
    assert _SENTINEL not in error.message
    assert _SENTINEL not in error.path


# --- An older recording (AAP 0.4.9) ---------------------------------------


def test_a_cassette_holding_a_plaintext_query_value_still_replays(cassette_dir: Path) -> None:
    """A recording made before the masking rule replays: the marker matches any value."""
    path = cassette_dir / cassette_filename(_NODEID)
    earlier = _document(
        Interaction(
            request=RecordedRequest(
                method="GET", path="/health", query=normalise_query(f"token={_LEGACY}&page=2")
            ),
            response=RecordedResponse(status=200, body=_BODY),
        )
    )
    path.write_text(to_json_text(earlier), encoding="utf-8")
    assert _LEGACY in path.read_text(encoding="utf-8")

    session = CassetteSession(_NODEID, mode="replay", store=CassetteStore(cassette_dir))
    try:
        response = _get(session.transport(), f"{_OTHER_HOST}/health?token={_SENTINEL}&page=2")
    finally:
        session.close()

    assert response.extensions[SOURCE_EXTENSION_KEY] == SOURCE_CASSETTE
    assert response.text == _BODY
    # Replaying reads the recording and never rewrites it, so the plaintext file is untouched.
    assert _LEGACY in path.read_text(encoding="utf-8")
