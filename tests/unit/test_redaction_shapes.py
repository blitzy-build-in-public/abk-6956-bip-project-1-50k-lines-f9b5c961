"""Cases a name-keyed rule cannot see: a decoded pair list, an RFC 7578 multipart part whose name
is a header parameter, and a credential under a name no deny-list enumerates; obs.redaction is the
one engine every sink calls, so it owns all of them here."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import pytest

from foundry.cassette import CassetteStore, Interaction, RecordedRequest, RecordedResponse
from foundry.cassette.models import normalise_query
from foundry.obs.artifacts import ArtifactWriter
from foundry.obs.redaction import (
    DEFAULT_HEADER_DENY_LIST,
    MAX_DEPTH,
    NESTING_TRUNCATION_MARKER,
    REDACTED,
    default_redactor,
)

# A value no redacted form may contain, distinctive enough that a match is a real leak.
_SENTINEL: Final[str] = "sentinel-shapes-4a91"
_RECORDED_AT: Final[datetime] = datetime(2024, 3, 1, 9, 30, tzinfo=UTC)
_NODEID: Final[str] = "tests/test_echo.py::test_pair_list[ticket-1]"

# One row per channel the engine polices; ``$s`` marks where the sentinel belongs.
_CHANNELS: Final[tuple[tuple[str, object, object], ...]] = (
    ("text", "authorization: Bearer $s", "authorization: ***"),
    ("header", ("x-echo", "token=$s"), "token=***"),
    ("headers", {"X-Echo": "token=$s", "X-Trace": "1"}, {"X-Echo": "token=***", "X-Trace": "1"}),
    ("query", {"token": "$s", "page": "2"}, {"page": "2", "token": "***"}),
    ("url", "https://user:$s@shop.example/p?page=2", "https://shop.example/p?page=2"),
    ("url", "https://shop.example/p?access_token=$s", "https://shop.example/p?access_token=***"),
    ("text", "{'token': '$s', 'page': 2}", "{'token': '***', 'page': 2}"),
    ("text", '{"token": "$s", "page": 2}', '{"token": "***", "page": 2}'),
    ("text", "Traceback:\n  authorization: Bearer $s", "Traceback:\n  authorization: ***"),
    ("text", "about to call with token $s", "about to call with token ***"),
    ("url", "http://qa:$s@[::1/x", "http://[::1/x"),
    ("text", "http://qa:$s@[::1/x", "http://[::1/x"),
    ("text", "http://qa:$s@127.0.0.1:38227/a[b] failed", "http://127.0.0.1:38227/a[b] failed"),
    ("text", "x://qa:$s@host.example", "x://host.example"),
    ("diagnostic", "token=$s", "token=***"),
    ("diagnostic", "authorization: Bearer $s", "authorization: ***"),
    ("diagnostic", "http://u:$s@h/x", "http://h/x"),
    ("diagnostic", "http://h/x?token=$s", "http://h/x?token=***"),
)


def _spliced(value: object) -> object:
    if isinstance(value, dict):
        return {key: _spliced(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return tuple(_spliced(item) for item in value)
    return str(value).replace("$s", _SENTINEL)


@pytest.mark.parametrize(("scrub", "argument", "expected"), _CHANNELS)
def test_each_channel_loses_the_secret(scrub: str, argument: object, expected: object) -> None:
    subject = _spliced(argument)
    assert _SENTINEL in json.dumps(subject), "the payload must carry the sentinel before the scrub"
    calls = subject if isinstance(subject, tuple) else (subject,)
    redacted = getattr(default_redactor(), scrub)(*calls)
    assert redacted == _spliced(expected) and _SENTINEL not in json.dumps(redacted)


def test_a_pair_list_under_a_secret_name_loses_its_value_at_any_depth() -> None:
    payload = {"query": [["page", "2"], ["token", _SENTINEL]], "items": [["api_key", _SENTINEL]]}
    payload["nested"] = {"deeper": [["X-Api-Key", _SENTINEL]]}
    expected = {"query": [["page", "2"], ["token", REDACTED]], "items": [["api_key", REDACTED]]}
    expected["nested"] = {"deeper": [["X-Api-Key", REDACTED]]}
    assert default_redactor().mapping(payload) == expected
    # The same walk over a serialised payload, which is what a dump and a cassette write.
    serialised = default_redactor().json_value({"query": (("access_token", _SENTINEL),)})
    assert serialised == {"query": [["access_token", REDACTED]]}


@pytest.mark.parametrize("value", [["a", "b"], [1, 2], ["token"], ["token", "1", "2"]])
def test_a_two_element_array_that_names_no_secret_is_left_intact(value: list[object]) -> None:
    assert default_redactor().json_value(value) == value


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('["token", "$s"]', f'["token", "{REDACTED}"]'),
        ("['token', '$s']", f"['token', '{REDACTED}']"),
        ('[ "token" ,  "$s" ]', f'[ "token" ,  "{REDACTED}" ]'),
        ('[\n  "token",\n  "$s"\n]', f'[\n  "token",\n  "{REDACTED}"\n]'),
        ('{"query": [["token", "$s"]]}', f'{{"query": [["token", "{REDACTED}"]]}}'),
        ('["a", "b"]', '["a", "b"]'),
    ],
)
def test_a_quoted_pair_list_masks_only_a_secret_value(text: str, expected: str) -> None:
    assert default_redactor().text(text.replace("$s", _SENTINEL)) == expected


@pytest.mark.parametrize(
    "body",
    [
        'name="token"\r\n\r\n$s',
        "NAME='token'\r\n\r\n$s",
        'name="token"\r\n \r\n$s',
        'Content-Disposition: form-data; name="token"\r\n\r\n$s',
        '--X\r\nContent-Disposition: form-data; name="token"\r\nContent-Type: text/plain\r\n'
        "\r\n$s\r\n--X--\r\n",
        '--X\r\nContent-Disposition: form-data; name="token"\r\nContent-Type: application/json\r\n'
        "Content-Transfer-Encoding: 8bit\r\n\r\n$s\r\n--X--\r\n",
        '--X\r\nContent-Disposition: form-data; name="token"\r\n\r\n$s\r\n'
        '--X\r\nContent-Disposition: form-data; name="page"\r\n\r\n2\r\n--X--\r\n',
        '--X\r\nContent-Disposition: form-data; name="files"\r\n\r\nname=page\r\n--X--\r\n',
        "name=token\r\nnot a part body",
    ],
)
def test_a_part_named_by_a_secret_loses_exactly_its_body_and_nothing_else(body: str) -> None:
    filled = body.replace("$s", _SENTINEL)
    assert default_redactor().text(filled) == filled.replace(_SENTINEL, REDACTED)


def _nested_body(levels: int) -> str:
    # Each level is a part the engine keeps, so the walk has to descend ``levels`` times.
    body = 'Content-Disposition: form-data; name="token"\r\n\r\n$s\r\n'
    for _ in range(levels):
        body = 'Content-Disposition: form-data; name="files"\r\n\r\n' + body + "--Z\r\n"
    return body


@pytest.mark.parametrize("levels", [1, 2, MAX_DEPTH + 2])
def test_a_part_that_holds_another_multipart_body_loses_the_nested_value(levels: int) -> None:
    scrubbed = default_redactor().text(_nested_body(levels).replace("$s", _SENTINEL))
    assert _SENTINEL not in scrubbed and REDACTED in scrubbed and "files" in scrubbed


# Ten times the text in twenty times the time leaves a linear scan a factor of two of slack.
_SMALL_PARTS, _LARGE_PARTS, _ROUNDS, _RATIO_BUDGET = 320, 3200, 5, 20.0


def test_the_multipart_scan_costs_time_linear_in_the_length_of_the_text() -> None:
    redactor = default_redactor()
    small, large = (";".join(['name="token"'] * count) for count in (_SMALL_PARTS, _LARGE_PARTS))
    assert redactor.text(small) == small and redactor.text(large) == large
    assert 9.5 < len(large) / len(small) < 10.5
    # The fastest round of CPU time, so a busy host's slices stay out of the reading.
    fastest: dict[str, float] = {}
    for name, text in (("small", small), ("large", large)):
        for _ in range(_ROUNDS):
            started = time.process_time()
            redactor.text(text)
            fastest[name] = min(fastest.get(name, float("inf")), time.process_time() - started)
    assert fastest["small"] > 0.0, "the smaller line measured no time at all"
    assert fastest["large"] / fastest["small"] <= _RATIO_BUDGET


@pytest.mark.parametrize("name", ["x-echo", "x-trace", "x-note"])
@pytest.mark.parametrize("spelling", ["token=$s", "api_key=$s", "password=$s", "Bearer $s"])
def test_an_echoed_credential_under_an_unlisted_name_is_masked(name: str, spelling: str) -> None:
    masked = default_redactor().header(name, spelling.replace("$s", _SENTINEL))
    assert not default_redactor().redacts(name) and _SENTINEL not in masked and REDACTED in masked


@pytest.mark.parametrize(
    "name", [*sorted(DEFAULT_HEADER_DENY_LIST), "Proxy-Authorization", "COOKIE", "X-API-Key"]
)
@pytest.mark.parametrize("value", ["Bearer $s", "session=$s", "$s", "application/json"])
def test_a_deny_listed_header_still_returns_the_marker(name: str, value: str) -> None:
    assert default_redactor().header(name, value.replace("$s", _SENTINEL)) == REDACTED


@pytest.mark.parametrize(
    "text",
    ['{"query": [["token", "$s"]]}', "['token', '$s']", '{"token": "$s"}', "token=$s&page=2"],
)
def test_a_second_pass_over_masked_text_changes_nothing(text: str) -> None:
    once = default_redactor().text(text.replace("$s", _SENTINEL))
    assert default_redactor().text(once) == once


# Nineteen ``name: text`` prefixes on one line, 2062 characters, which a one-descent-per-prefix
# guard truncated at character 885; a 4000-pair chain costs one descent of the remainder instead.
_MESSAGE_SENTENCE: Final[str] = (
    "assertion failed: the checkout total was recomputed from the item rows and "
    "matched the expected amount"
)
_LONG_MESSAGE: Final[str] = "".join(f"{_MESSAGE_SENTENCE} ({number}). " for number in range(1, 20))
_UNBROKEN_CHAIN: Final[str] = "a=" * 4000


def test_a_long_message_survives_whole_and_a_chain_past_the_cap_is_masked_and_marked() -> None:
    redactor = default_redactor()
    assert len(_LONG_MESSAGE) > 2000, "the message must be long enough to trip the depth guard"
    assert redactor.text(_LONG_MESSAGE) == _LONG_MESSAGE
    scrubbed = redactor.text(f"{_UNBROKEN_CHAIN}token={_SENTINEL}")
    assert _SENTINEL not in scrubbed
    assert scrubbed.endswith(f"{REDACTED} {NESTING_TRUNCATION_MARKER}")
    assert redactor.text(scrubbed) == scrubbed


def test_the_cassette_names_what_it_scrubbed_and_records_none_of_it(tmp_path: Path) -> None:
    store = CassetteStore(tmp_path / "cassettes")
    capture = Interaction(
        request=RecordedRequest(
            method="GET",
            path="/echo",
            query=normalise_query("page=2"),
            body_sha256=None,
            headers_subset={"accept": "application/json", "x-request-echo": f"token={_SENTINEL}"},
        ),
        response=RecordedResponse(
            status=200,
            headers={"content-type": "application/json", "x-echo": f"token={_SENTINEL}"},
            body=json.dumps({"query": [["page", "2"], ["token", _SENTINEL]]}),
        ),
    )
    written = store.save(_NODEID, [capture], recorded_at=_RECORDED_AT)
    assert written is not None
    text = written.read_text(encoding="utf-8")
    assert _SENTINEL not in text
    interaction = json.loads(text)["interactions"][0]
    # Both header sets and the pair list in the body pass through the one engine, which names them.
    assert interaction["request"]["headers_subset"]["x-request-echo"] == f"token={REDACTED}"
    assert interaction["response"]["headers"]["x-echo"] == f"token={REDACTED}"
    assert json.loads(interaction["response"]["body"])["query"][1] == ["token", REDACTED]
    assert store.load(_NODEID).redacted_keys == ("token", "x-echo", "x-request-echo")


def test_url_userinfo_never_survives_free_text() -> None:
    text = "foundry_config = HttpConfig(base_url='http://qa:$s@127.0.0.1:38227')"
    scrubbed = default_redactor().text(text.replace("$s", _SENTINEL))
    assert _SENTINEL not in scrubbed and "qa:" not in scrubbed


@pytest.mark.parametrize(
    "text",
    [
        "x-echo token=$s",
        "caller sent x-trace-id=abc authorization=$s",
        "token: $s",
        "\ttoken: $s",
        'redact_keys=["token", "$s"]',
        "token=$s&page=2",
    ],
)
def test_every_shape_a_credential_assignment_takes_loses_its_value(text: str) -> None:
    scrubbed = default_redactor().text(text.replace("$s", _SENTINEL))
    assert _SENTINEL not in scrubbed and REDACTED in scrubbed
    assert default_redactor().text(scrubbed) == scrubbed


@pytest.mark.parametrize(
    ("scrub", "text"),
    [
        ("text", "redact_keys=['authorization', 'set-cookie', 'cookie', 'x-api-key']"),
        ("text", "test_keys.py:3: AssertionError"),
        ("text", '{"nodeid": "tests/test_token_refresh.py::test_x"}'),
        ("text", "token bucket"),
        ("text", "password reset required for the admin account"),
        ("text", "secret rotation is scheduled"),
        ("diagnostic", "unknown configuration key: zzzz_misspelled"),
    ],
)
def test_prose_that_names_a_secret_word_keeps_its_text(scrub: str, text: str) -> None:
    assert getattr(default_redactor(), scrub)(text) == text


def test_a_node_id_that_names_a_secret_word_keeps_its_artifact_directory(tmp_path: Path) -> None:
    writer = ArtifactWriter(
        tmp_path, run_id="20250102T030405-0abc12", profile="ci", redactor=default_redactor()
    )
    auth = writer.dir_for("tests/test_auth_login.py::test_alpha")
    token = writer.dir_for("tests/test_token_refresh.py::test_x")
    assert auth != token and "test_auth_login.py" in auth.name and REDACTED not in auth.name
