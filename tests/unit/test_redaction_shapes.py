"""Unit tests for the redaction shapes a name-keyed rule cannot see on its own.

A secret reaches Foundry in shapes carrying no ``name=value`` separator on one line: a decoded pair
list (``[["token", "<secret>"]]``), an RFC 7578 multipart part whose name is a
``Content-Disposition`` parameter, and a value under a name no deny-list enumerates -- the header a
peer echoes a credential back in is usually one the peer invented (``x-echo: token=...``). AAP 0.3.2
makes ``foundry.obs.redaction`` the one engine every sink calls, so the engine owns all three shapes
rather than each dump, cassette and assertion message growing a rule of its own.

Pinned here, all through the public engine: the structural path masks a pair whose first element is
a secret name, at any depth, and leaves other arrays alone; the free-text path masks the quoted pair
entry and the multipart body while keeping the name and the punctuation readable; a kept part's own
nested body is scrubbed in turn and the descent stops at :data:`MAX_DEPTH`; an unlisted header's
value is scrubbed as free text while a media type, a date or an identifier is untouched; a second
pass over masked text returns it unchanged; text dropped for its depth is marked with
:data:`NESTING_TRUNCATION_MARKER` rather than reading as a scrubbed secret (AAP 0.4.6, 0.7.4); the
cassette store names what it scrubbed in ``redacted_keys``; and the multipart scan costs time in the
text's length, measured as a ratio so the pin holds on a host of any speed (AAP 0.14.2).

Separate from ``tests/unit/test_obs.py``, which already sits at the 700-line ceiling AAP 0.14.2 sets
and ``scripts/count_lines.sh`` enforces. No socket is opened and nothing sleeps: every value is a
literal, the store writes into ``tmp_path``, and the cost test reads this process's CPU time.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import pytest

from foundry.cassette import CassetteStore, Interaction, RecordedRequest, RecordedResponse
from foundry.cassette.models import normalise_query
from foundry.obs.redaction import (
    DEFAULT_HEADER_DENY_LIST,
    MAX_DEPTH,
    NESTING_TRUNCATION_MARKER,
    REDACTED,
    default_redactor,
)

# A value no redacted form may contain, distinctive enough that a match is a real leak.
_SENTINEL: Final[str] = "sentinel-shapes-4a91"

# The one timestamp the store test stamps its document with, so nothing reads the wall clock.
_RECORDED_AT: Final[datetime] = datetime(2024, 3, 1, 9, 30, tzinfo=UTC)
_NODEID: Final[str] = "tests/test_echo.py::test_pair_list[ticket-1]"


# --- The structural path (AAP 0.3.2) ---------------------------------------


def test_a_pair_list_under_a_secret_name_loses_its_value() -> None:
    """A ``[name, value]`` pair is masked where the engine walks a mapping, recursively."""
    payload = {
        "query": [["page", "2"], ["token", _SENTINEL]],
        "items": [["api_key", _SENTINEL]],
        "nested": {"deeper": [["X-Api-Key", _SENTINEL]]},
    }

    redacted = default_redactor().mapping(payload)

    assert redacted == {
        "query": [["page", "2"], ["token", REDACTED]],
        "items": [["api_key", REDACTED]],
        "nested": {"deeper": [["X-Api-Key", REDACTED]]},
    }
    assert _SENTINEL not in json.dumps(redacted)


def test_a_pair_list_value_never_reaches_serialised_output() -> None:
    """The serialised form -- what a dump, a cassette and an assertion message write -- is clean."""
    redacted = default_redactor().json_value({"query": (("access_token", _SENTINEL),)})

    serialised = json.dumps(redacted)

    assert redacted == {"query": [["access_token", REDACTED]]}
    assert _SENTINEL not in serialised


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (["a", "b"], ["a", "b"]),
        ([1, 2], [1, 2]),
        (["page", "2"], ["page", "2"]),
        (["note", "token"], ["note", "token"]),
        (["token"], ["token"]),
        (["token", "1", "2"], ["token", "1", "2"]),
    ],
)
def test_a_two_element_array_that_names_no_secret_is_left_intact(
    value: list[object], expected: list[object]
) -> None:
    """Only a pair whose first element is a name the engine redacts is a key and a value."""
    assert default_redactor().json_value(value) == expected


# --- The free-text pair list (AAP 0.3.2, AAP 0.14.3) -----------------------


@pytest.mark.parametrize(
    "text",
    [
        '["token", "$s"]',
        "['token', '$s']",
        '[ "token" ,  "$s" ]',
        '[\n  "token",\n  "$s"\n]',
        '{"query": [["page", "2"], ["token", "$s"]]}',
    ],
)
def test_a_quoted_pair_list_entry_loses_its_value_and_keeps_its_name(text: str) -> None:
    """Every spelling a printer gives the same pair is masked, the name staying readable."""
    scrubbed = default_redactor().text(text.replace("$s", _SENTINEL))

    assert _SENTINEL not in scrubbed
    assert "token" in scrubbed
    assert REDACTED in scrubbed


def test_a_quoted_pair_list_keeps_its_shape_where_it_was_redacted() -> None:
    """The punctuation around the value survives, so the dump still reads as the pair it was."""
    assert (
        default_redactor().text('{"query": [["token", "abc"]]}') == '{"query": [["token", "***"]]}'
    )


@pytest.mark.parametrize("text", ['["a", "b"]', '["page", "2"]', '["note", "token"]'])
def test_a_quoted_pair_that_names_no_secret_is_left_intact(text: str) -> None:
    """A two-element array of two plain values is not a pair, and nothing is replaced in it."""
    assert default_redactor().text(text) == text


# --- The multipart part (AAP 0.3.2, AAP 0.14.3) ----------------------------


@pytest.mark.parametrize(
    "text",
    [
        'name="token"\r\n\r\n$s',
        "name=token\n\n$s",
        "NAME='token'\r\n\r\n$s",
        'name="token"\r\n \r\n$s',
        'Content-Disposition: form-data; name="token"\r\n\r\n$s',
    ],
)
def test_a_multipart_part_named_by_a_secret_loses_its_body(text: str) -> None:
    """The header terminator and the body line are replaced; the parameter stays readable."""
    scrubbed = default_redactor().text(text.replace("$s", _SENTINEL))

    assert scrubbed.endswith(REDACTED)
    assert _SENTINEL not in scrubbed
    assert "name" in scrubbed.casefold()
    assert "token" in scrubbed


def test_a_full_multipart_body_keeps_its_parts_and_replaces_the_secret_one() -> None:
    """The other part, the boundaries and the framing of the redacted part survive untouched."""
    body = (
        "--X\r\n"
        'Content-Disposition: form-data; name="token"\r\n\r\n' + _SENTINEL + "\r\n"
        "--X\r\n"
        'Content-Disposition: form-data; name="page"\r\n\r\n2\r\n'
        "--X--\r\n"
    )

    scrubbed = default_redactor().text(body)

    assert scrubbed.startswith("--X\r\n")
    assert 'name="token"\r\n\r\n' + REDACTED + "\r\n--X\r\n" in scrubbed
    assert 'name="page"\r\n\r\n2\r\n--X--\r\n' in scrubbed
    assert _SENTINEL not in scrubbed


def test_a_part_that_carries_its_own_content_type_still_loses_its_body() -> None:
    """The further headers a part may carry travel with it; the body below them is still masked."""
    body = (
        '--X\r\nContent-Disposition: form-data; name="token"\r\n'
        "Content-Type: text/plain\r\n\r\n" + _SENTINEL + "\r\n--X--\r\n"
    )

    scrubbed = default_redactor().text(body)

    assert scrubbed == (
        '--X\r\nContent-Disposition: form-data; name="token"\r\n'
        "Content-Type: text/plain\r\n\r\n" + REDACTED + "\r\n--X--\r\n"
    )


def test_a_part_that_carries_several_further_headers_still_loses_its_body() -> None:
    """The bound on a part's further header lines leaves room for every header an encoder writes."""
    body = (
        '--X\r\nContent-Disposition: form-data; name="token"\r\n'
        "Content-Type: application/json\r\n"
        "Content-Transfer-Encoding: 8bit\r\n"
        "Content-ID: <part-1>\r\n\r\n" + _SENTINEL + "\r\n--X--\r\n"
    )

    scrubbed = default_redactor().text(body)

    assert scrubbed == (
        '--X\r\nContent-Disposition: form-data; name="token"\r\n'
        "Content-Type: application/json\r\n"
        "Content-Transfer-Encoding: 8bit\r\n"
        "Content-ID: <part-1>\r\n\r\n" + REDACTED + "\r\n--X--\r\n"
    )
    assert _SENTINEL not in scrubbed


@pytest.mark.parametrize(
    "text",
    ['name="page"\r\n\r\n2', 'Content-Disposition: form-data; name="page"\r\n\r\n2'],
)
def test_a_multipart_part_named_by_a_plain_field_is_left_intact(text: str) -> None:
    """A part the engine does not consider secret is not rewritten, body and framing included."""
    assert default_redactor().text(text) == text


#: A multipart body whose outer part is named by a field no deny-list carries and whose own body is
#: a second multipart body: the shape a client sends when one part is itself a form. ``$s`` is the
#: sentinel, spliced in by the test so the constant reads as the shape rather than the value.
_NESTED_PART_BODY: Final[str] = (
    "--X\r\n"
    'Content-Disposition: form-data; name="files"\r\n\r\n'
    "--Y\r\n"
    'Content-Disposition: form-data; name="token"\r\n\r\n$s\r\n'
    "--Y--\r\n"
    "--X--\r\n"
)

#: The same shape one level deeper, so the scrub has to follow two kept parts to reach the secret.
_DOUBLY_NESTED_PART_BODY: Final[str] = (
    "--X\r\n"
    'Content-Disposition: form-data; name="files"\r\n\r\n'
    "--Y\r\n"
    'Content-Disposition: form-data; name="files"\r\n\r\n'
    "--Z\r\n"
    'Content-Disposition: form-data; name="token"\r\n\r\n$s\r\n'
    "--Z--\r\n"
    "--Y--\r\n"
    "--X--\r\n"
)


@pytest.mark.parametrize("body", [_NESTED_PART_BODY, _DOUBLY_NESTED_PART_BODY])
def test_a_part_that_holds_another_multipart_body_loses_the_nested_value(body: str) -> None:
    """A part the engine keeps is scrubbed in turn, so the part nested inside it loses its body."""
    scrubbed = default_redactor().text(body.replace("$s", _SENTINEL))

    assert _SENTINEL not in scrubbed
    assert 'name="token"\r\n\r\n' + REDACTED + "\r\n" in scrubbed
    assert scrubbed.startswith("--X\r\n")
    assert scrubbed.endswith("--X--\r\n")
    assert "files" in scrubbed


@pytest.mark.parametrize(
    "body",
    [
        '--X\r\nContent-Disposition: form-data; name="files"\r\n\r\nplain body\r\n--X--\r\n',
        '--X\r\nContent-Disposition: form-data; name="files"\r\n\r\nname=page\r\n--X--\r\n',
    ],
)
def test_a_kept_part_whose_body_holds_no_nested_part_is_left_intact(body: str) -> None:
    """The descent costs a kept body nothing: with no nested part the text comes back as it was."""
    assert default_redactor().text(body) == body


#: One level past the deepest descent :data:`MAX_DEPTH` allows, so the cap is what ends the walk
#: rather than the shape of the body: each kept part costs one level, and :data:`MAX_DEPTH` is it.
_OVERNESTED_LEVELS: Final[int] = MAX_DEPTH + 2


def test_a_body_nested_past_the_depth_cap_still_terminates() -> None:
    """The descent stops at :data:`MAX_DEPTH`, so a body nested past it is masked, not followed."""
    body = 'Content-Disposition: form-data; name="token"\r\n\r\n' + _SENTINEL + "\r\n"
    for _ in range(_OVERNESTED_LEVELS):
        body = 'Content-Disposition: form-data; name="files"\r\n\r\n' + body + "--Z\r\n"
    redactor = default_redactor()

    scrubbed = redactor.text(body)

    assert _SENTINEL not in scrubbed
    assert REDACTED in scrubbed
    assert redactor.text(scrubbed) == scrubbed


def test_a_lone_line_break_is_not_a_part_header_terminator() -> None:
    """Without the blank line there is no body to redact, so the text below the header stays."""
    text = "name=token\r\nnot a part body"

    assert default_redactor().text(text) == text


# --- The multipart scan's cost (AAP 0.14.2 suite wall clock) ---------------

#: Parts in the smaller measured line, and in the larger one: ten times the text, so a scan whose
#: cost follows the length of what it reads answers in about ten times the time, and one that
#: re-reads the rest of a line at every candidate -- the shape of the pass this scan replaced -- in
#: about a hundred times the time.
_SMALL_LINE_PARTS: Final[int] = 320
_LARGE_LINE_PARTS: Final[int] = 3200

#: Timed rounds per size. Both sizes are timed inside one round, and the pair judged is the fastest
#: round's: the reading excludes time the host gave to other work, and the fastest round of the five
#: is the one least disturbed by this process's own allocations and collections.
_COST_ROUNDS: Final[int] = 5

#: The largest time ratio the larger text may cost against the smaller. Ten times the text in twenty
#: times the time leaves a linear scan a factor of two of slack, while a quadratic one needs about a
#: hundred times and cannot fit. The reading is CPU time rather than elapsed time for that slack's
#: sake: a host busy with other work takes slices away from this process, which inflates an elapsed
#: reading by however long the wait was and by a share that grows with the length of the call.
_COST_RATIO_BUDGET: Final[float] = 20.0


def _dense_part_line(parts: int) -> str:
    """Return one line holding ``parts`` ``; name="token"`` parameters and no line break."""
    return ";".join(['name="token"'] * parts)


def test_the_multipart_scan_costs_time_linear_in_the_length_of_the_text() -> None:
    """One line dense with part parameters is walked once, not once per candidate on it."""
    small = _dense_part_line(_SMALL_LINE_PARTS)
    large = _dense_part_line(_LARGE_LINE_PARTS)
    redactor = default_redactor()

    assert redactor.text(small) == small, "a line with no header terminator holds no part body"
    assert redactor.text(large) == large, "a line with no header terminator holds no part body"
    growth = len(large) / len(small)
    assert 9.5 < growth < 10.5, f"the larger line is {growth:.2f} times the smaller, not ten"

    # Untimed, so the one-off compile of the engine's patterns is not part of either reading.
    redactor.text(small)
    redactor.text(large)
    small_ms = float("inf")
    large_ms = float("inf")
    for _ in range(_COST_ROUNDS):
        started = time.process_time()
        redactor.text(small)
        small_ms = min(small_ms, (time.process_time() - started) * 1000.0)
        started = time.process_time()
        redactor.text(large)
        large_ms = min(large_ms, (time.process_time() - started) * 1000.0)

    assert small_ms > 0.0, "the smaller line measured no time at all"
    ratio = large_ms / small_ms
    assert ratio <= _COST_RATIO_BUDGET, (
        f"{len(large)} characters cost {large_ms:.2f} ms of CPU against {len(small)} characters at "
        f"{small_ms:.2f} ms -- {ratio:.1f} times the time for ten times the text, which is the "
        f"cost of a scan that reads to the end of the line at every candidate on it"
    )


# --- The header value under an unlisted name (AAP 0.3.2, AAP 0.14.3) -------


@pytest.mark.parametrize("name", ["x-echo", "x-trace", "x-note"])
@pytest.mark.parametrize("spelling", ["token=$s", "api_key=$s", "password=$s", "Bearer $s"])
def test_an_echoed_credential_under_an_unlisted_name_is_masked(name: str, spelling: str) -> None:
    """A name no deny-list carries masks nothing by name, so its value is scrubbed as free text."""
    redactor = default_redactor()
    value = spelling.replace("$s", _SENTINEL)

    masked = redactor.header(name, value)

    assert not redactor.redacts(name)
    assert _SENTINEL not in masked
    assert REDACTED in masked


@pytest.mark.parametrize("name", sorted(DEFAULT_HEADER_DENY_LIST))
@pytest.mark.parametrize("value", ["Bearer $s", "session=$s", "$s", "a b c"])
def test_a_deny_listed_header_still_returns_the_marker(name: str, value: str) -> None:
    """The name decides first: a deny-listed header loses its whole value, as it always did."""
    assert default_redactor().header(name, value.replace("$s", _SENTINEL)) == REDACTED


@pytest.mark.parametrize(
    "value",
    [
        "application/json",
        "text/html; charset=utf-8",
        "abc-123",
        "Tue, 24 Nov 2026 19:34:40 GMT",
        "gzip, deflate, br",
        "0",
        "",
    ],
)
def test_an_ordinary_header_value_is_left_exactly_as_it_was(value: str) -> None:
    """A media type, a date, an identifier or an empty value carries nothing to scrub."""
    assert default_redactor().header("x-unlisted", value) == value


def test_the_header_mapping_is_scrubbed_value_by_value() -> None:
    """``headers`` routes each value through ``header``: the echo is masked, the rest stay."""
    supplied = {
        "Content-Type": "application/json",
        "X-Echo": f"token={_SENTINEL}",
        "Authorization": f"Bearer {_SENTINEL}",
        "X-Trace": "abc-123",
    }

    masked = default_redactor().headers(supplied)

    assert masked == {
        "Content-Type": "application/json",
        "X-Echo": f"token={REDACTED}",
        "Authorization": REDACTED,
        "X-Trace": "abc-123",
    }
    assert _SENTINEL not in json.dumps(masked)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("x-echo", "token=$s"),
        ("x-echo", "Bearer $s"),
        ("x-echo", "application/json"),
        ("authorization", "Bearer $s"),
        ("authorization", "$s"),
    ],
)
def test_a_second_pass_over_a_masked_header_changes_nothing(name: str, value: str) -> None:
    """A sink that scrubs an already-scrubbed header value must get the same value back."""
    redactor = default_redactor()
    once = redactor.header(name, value.replace("$s", _SENTINEL))

    assert redactor.header(name, once) == once


# --- Idempotence (AAP 0.3.2) ----------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        '{"query": [["token", "$s"]]}',
        "['token', '$s']",
        'name="token"\r\n\r\n$s',
        'Content-Disposition: form-data; name="token"\r\n\r\n$s\r\n--X--\r\n',
        "token=$s&page=2",
        '{"token": "$s"}',
        "authorization: Bearer abcdefgh",
        _NESTED_PART_BODY,
        _DOUBLY_NESTED_PART_BODY,
    ],
)
def test_a_second_pass_over_masked_text_changes_nothing(text: str) -> None:
    """A sink that scrubs its own output again must produce the same text, marker included."""
    redactor = default_redactor()

    once = redactor.text(text.replace("$s", _SENTINEL))

    assert redactor.text(once) == once


# --- A name: text chain in a failure message (AAP 0.4.6, AAP 0.7.4) --------

#: One sentence of the long failure message, in the shape an assertion writes it: the ``name:``
#: prefix the header-line pass recognises, then a readable tail that pass keeps.
_MESSAGE_SENTENCE: Final[str] = (
    "assertion failed: the checkout total was recomputed from the item rows and "
    "matched the expected amount"
)

#: The message the finding measured: nineteen ``name: text`` prefixes on one line, 2062 characters,
#: which an engine descending one level per prefix stored and rendered as its first 885 characters
#: ending in the marker a scrubbed secret leaves.
_LONG_MESSAGE: Final[str] = "".join(f"{_MESSAGE_SENTENCE} ({number}). " for number in range(1, 20))

#: A ``name=value`` chain whose values run unbroken to the end of the text, so every pair it keeps
#: costs one descent of the whole remainder: four thousand pairs, then the one secret-bearing pair
#: the engine has to reach and the value behind it.
_UNBROKEN_CHAIN: Final[str] = "a=" * 4000


def test_a_long_message_of_name_text_fragments_survives_the_scrub_whole() -> None:
    """A recorded message reaches the document whole, whatever ``name: text`` prefixes it holds."""
    redactor = default_redactor()

    assert len(_LONG_MESSAGE) > 2000, "the message must be long enough to trip the old depth guard"
    scrubbed = redactor.text(_LONG_MESSAGE)

    assert scrubbed == _LONG_MESSAGE
    assert REDACTED not in scrubbed
    assert redactor.text(scrubbed) == scrubbed


def test_a_chain_past_the_nesting_depth_cap_is_masked_and_marked_as_dropped() -> None:
    """Past the cap the text is masked and marked, so a dropped tail never reads as a secret."""
    redactor = default_redactor()
    text = f"{_UNBROKEN_CHAIN}token={_SENTINEL}"

    scrubbed = redactor.text(text)

    assert _SENTINEL not in scrubbed
    assert scrubbed.endswith(f"{REDACTED} {NESTING_TRUNCATION_MARKER}")
    assert f"{REDACTED} {NESTING_TRUNCATION_MARKER}" in scrubbed
    assert redactor.text(scrubbed) == scrubbed


# --- The cassette's redacted_keys (AAP 0.4.6) ------------------------------


def test_a_pair_list_name_reaches_the_cassette_redacted_keys(tmp_path: Path) -> None:
    """A name the structural path masked is recorded, so the field describes the document."""
    store = CassetteStore(tmp_path / "cassettes")
    capture = Interaction(
        request=RecordedRequest(
            method="GET",
            path="/echo",
            query=normalise_query("page=2"),
            body_sha256=None,
            headers_subset={"accept": "application/json"},
        ),
        response=RecordedResponse(
            status=200,
            headers={"content-type": "application/json"},
            body=json.dumps({"query": [["page", "2"], ["token", _SENTINEL]]}),
        ),
    )

    written = store.save(_NODEID, [capture], recorded_at=_RECORDED_AT)

    assert written is not None
    text = written.read_text(encoding="utf-8")
    assert _SENTINEL not in text
    assert store.load(_NODEID).redacted_keys == ("token",)
    assert json.loads(text)["interactions"][0]["response"]["body"] == (
        '{"query": [["page", "2"], ["token", "***"]]}'
    )


def test_an_echoed_response_header_never_reaches_the_cassette(tmp_path: Path) -> None:
    """A header no deny-list names is scrubbed at capture, named in ``redacted_keys``, and kept."""
    store = CassetteStore(tmp_path / "cassettes")
    capture = Interaction(
        request=RecordedRequest(
            method="GET",
            path="/echo",
            query=normalise_query("page=2"),
            body_sha256=None,
            headers_subset={
                "accept": "application/json",
                "x-request-echo": f"token={_SENTINEL}",
            },
        ),
        response=RecordedResponse(
            status=200,
            headers={
                "content-type": "application/json",
                "x-echo": f"token={_SENTINEL}",
                "x-trace": "abc-123",
            },
            body="{}",
        ),
    )

    written = store.save(_NODEID, [capture], recorded_at=_RECORDED_AT)

    assert written is not None
    text = written.read_text(encoding="utf-8")
    assert _SENTINEL not in text
    document = json.loads(text)
    assert document["interactions"][0]["response"]["headers"] == {
        "content-type": "application/json",
        "x-echo": f"token={REDACTED}",
        "x-trace": "abc-123",
    }
    # The request's own captured headers go through the same loop, so an echo there is scrubbed too.
    assert document["interactions"][0]["request"]["headers_subset"] == {
        "accept": "application/json",
        "x-request-echo": f"token={REDACTED}",
    }
    assert document["redacted_keys"] == ["x-echo", "x-request-echo"]
    assert store.load(_NODEID).redacted_keys == ("x-echo", "x-request-echo")
