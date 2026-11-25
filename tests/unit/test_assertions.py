"""Unit tests for the four public assertion helpers of :mod:`foundry.assertions`: the excerpt is the
``repr`` of the redacted body cut to ``MAX_EXCERPT_BYTES``, then marked with ``TRUNCATION_MARKER``.
"""

import ast
import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

import pytest

from foundry.assertions import assert_json_schema, assert_json_subset, assert_status, eventually
from foundry.core.exceptions import ContractError
from foundry.obs.redaction import MAX_EXCERPT_BYTES, REDACTED, TRUNCATION_MARKER, Redactor

_SECRET: Final[str] = "sentinel-credential-7c1f4b2e"  # finding it in rendered text is a leak
_EXCERPT_LABEL: Final[str] = f"body excerpt (redacted, up to {MAX_EXCERPT_BYTES} bytes): "
_LONG_BODY: Final[str] = "".join(chr(ord("a") + offset % 26) for offset in range(600))


class _Response:
    def __init__(
        self, code: int, body: str = "", source: str | None = "live", *, readable: bool = True
    ) -> None:
        self.status_code, self.body, self.source = code, body, source
        self.headers: Mapping[str, str] = {}
        self.readable = readable

    @property
    def text(self) -> str:
        if not self.readable:
            raise RuntimeError("the body must not be read once the status matches")
        return self.body


class _Timeline:
    # A fake clock that records every sleep and, unless frozen, moves itself forward by it.
    def __init__(self, *, advance: bool = True) -> None:
        self.instant, self.delays, self.advance = datetime(2025, 1, 2, tzinfo=UTC), [], advance

    def now(self) -> datetime:
        return self.instant

    def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)
        self.instant += timedelta(seconds=seconds) if self.advance else timedelta()


def _poll(action: Callable[[], object], timeline: _Timeline, timeout: float) -> object:
    return eventually(action, timeout=timeout, interval=0.5, sleep=timeline, clock=timeline)


def _status_failure(response: _Response, expected: object, **kwargs: object) -> AssertionError:
    with pytest.raises(AssertionError) as caught:
        assert_status(response, expected, **kwargs)
    return caught.value


def _subset_failure(actual: object, expected: object, **kwargs: object) -> AssertionError:
    with pytest.raises(AssertionError) as caught:
        assert_json_subset(actual, expected, **kwargs)
    return caught.value


def test_assert_status_passes_for_every_published_form_and_refuses_a_misused_one() -> None:
    for expected in (200, 201, 204, 500, 503, frozenset({200, 201}), [204, 200], range(200, 202)):
        status = expected if isinstance(expected, int) else 200
        assert assert_status(_Response(status, body="not inspected"), expected) is None
    assert assert_status(_Response(204, readable=False), 204) is None
    for misused in (True, "200", -1, 1000, []):
        failure = _status_failure(_Response(200), misused)
        assert str(failure).startswith("foundry.assertions.assert_status:")


@pytest.mark.parametrize(("source", "rendered"), [("live", "source=live"), (None, None)])
def test_assert_status_names_the_status_the_expected_set_and_the_source(
    source: str | None, rendered: str | None
) -> None:
    response = _Response(500, body='{"detail": "cart total changed"}', source=source)
    failure = _status_failure(response, {200, 201})
    message = str(failure)
    assert type(failure) is AssertionError and failure.__cause__ is None
    assert message.startswith("foundry.assertions.assert_status:") and "received 500" in message
    assert "{200, 201}" in message and "cart total changed" in message
    assert rendered in message if rendered is not None else "source=" not in message


def test_assert_status_excerpt_is_a_bounded_prefix_and_honours_a_redactor() -> None:
    message = str(_status_failure(_Response(409, body='{"detail": "insufficient stock"}'), 200))
    assert repr('{"detail": "insufficient stock"}') in message
    message = str(_status_failure(_Response(404, body=_LONG_BODY), 200))
    excerpt = ast.literal_eval(message.partition(_EXCERPT_LABEL)[2].strip())
    prefix = excerpt[: -len(TRUNCATION_MARKER)]
    assert excerpt.endswith(TRUNCATION_MARKER) and 0 < len(prefix) <= MAX_EXCERPT_BYTES
    assert len(excerpt.encode()) <= MAX_EXCERPT_BYTES + len(TRUNCATION_MARKER)
    assert prefix == _LONG_BODY[: len(prefix)]
    planted = "upstream rejected nonce=abc123nonce"
    raw = str(_status_failure(_Response(400, body=planted), 200))
    clean = str(_status_failure(_Response(400, body=planted), 200, redactor=Redactor(["nonce"])))
    assert "abc123nonce" in raw and "abc123nonce" not in clean


@pytest.mark.parametrize(
    "body",
    (
        json.dumps({"detail": "rejected", "token": _SECRET}),
        f"rejected https://api.example.test/orders/7?token={_SECRET}",
        f"rejected Authorization: Bearer {_SECRET}",
    ),
)
def test_assert_status_message_carries_no_secret_from_the_body(body: str) -> None:
    response = _Response(502, body=body)
    response.headers = {"Authorization": f"Bearer {_SECRET}"}
    failure = _status_failure(response, 200)
    message = str(failure)
    assert _SECRET not in message and failure.__cause__ is None
    assert REDACTED in message and "received 502" in message


def test_assert_json_subset_accepts_containment_and_reports_the_first_mismatch() -> None:
    actual: dict[str, object] = {"order_id": "ORD-123456", "status": "paid"}
    actual["items"] = [{"sku": "SKU-1234", "quantity": 2, "price": 9.99}]
    assert assert_json_subset(actual, {"status": "paid"}) is None
    assert_json_subset(actual, {"items": [{"sku": "SKU-1234", "price": 9.99}]})
    assert_json_subset(actual, {"items": [{"quantity": 2}], "order_id": actual["order_id"]})
    assert_json_subset(actual, actual)
    assert "mismatch at $.data.a: " in str(_subset_failure({"a": 1}, {"a": 2}, path="$.data"))
    refused = _subset_failure({"a": 1}, {"a": 1}, path="root")
    assert "path must be a str beginning with '$'" in str(refused)
    messages = [str(_subset_failure({"a": 1, "b": 2}, {"b": 9, "a": 9})) for _ in range(2)]
    assert messages[0] == messages[1] and "mismatch at $.a: " in messages[0]
    assert "$.b" not in messages[0]
    message = str(_subset_failure({"token": "actual-credential"}, {"token": "expected-credential"}))
    assert "mismatch at $.token: " in message and REDACTED in message
    assert "actual-credential" not in message and "expected-credential" not in message


@pytest.mark.parametrize(
    ("actual", "expected", "path", "detail"),
    (
        ({"a": {"b": 1}}, {"a": {"b": 2}}, "$.a.b", "expected 2, actual 1"),
        ({"a": {"c": 1}}, {"a": {"b": 1}}, "$.a.b", "expected key 'b' is absent"),
        ({"id": 7}, {"id": "7"}, "$.id", "expected string, actual number"),
        ({"items": [9.9]}, {"items": [1.0]}, "$.items[0]", "expected 1.0, actual 9.9"),
        ({"items": [1]}, {"items": [1, 2]}, "$.items", "expected list of 2 elements"),
        ({"flag": 1}, {"flag": True}, "$.flag", "expected boolean, actual number"),
        ({"flag": True}, {"flag": 1}, "$.flag", "expected number, actual boolean"),
        ([1, 2], {"a": 1}, "$", "expected object, actual list"),
    ),
)
def test_assert_json_subset_names_the_first_mismatching_path(
    actual: object, expected: object, path: str, detail: str
) -> None:
    failure = _subset_failure(actual, expected)
    message = str(failure)
    assert type(failure) is AssertionError and failure.__cause__ is None
    assert message.startswith("foundry.assertions.assert_json_subset:")
    assert f"JSON subset mismatch at {path}: " in message and detail in message


def test_assert_json_schema_reports_the_shallowest_violation(repo_root: Path) -> None:
    schema_path = repo_root / "tests" / "fixtures" / "response.schema.json"
    assert schema_path.is_file(), f"the committed schema fixture is missing: {schema_path}"
    item: dict[str, object] = {"quantity": 2, "sku": "SKU-1234"}
    instance: dict[str, object] = {"items": [item], "order_id": "ORD-123456"}
    instance.update(status="paid", totals={"currency": "EUR", "grand_total_cents": 1999})
    assert assert_json_schema(instance, schema_path) is None
    instance["order_id"], instance["status"] = "BAD", "unknown"
    for schema in (schema_path, str(schema_path), schema_path):
        with pytest.raises(ContractError) as caught:
            assert_json_schema(instance, schema)
        assert type(caught.value) is ContractError and not isinstance(caught.value, AssertionError)
        assert caught.value.message == "contract validation failed at $.order_id"
        assert "validator: pattern" in caught.value.detail
        assert caught.value.json_path == "$.order_id"
    instance["order_id"], instance["status"] = "ORD-123456", "paid"
    instance["items"] = [{"quantity": 1, "sku": "nope"}]
    with pytest.raises(ContractError) as caught:
        assert_json_schema(instance, schema_path)
    assert caught.value.json_path == "$.items[0].sku" and "nope" in caught.value.detail


@pytest.mark.parametrize(
    ("falsy", "timeout", "rendered", "delays"),
    [
        (False, 2.0, "last value: False", [0.5] * 4),
        (None, 2.0, "last value: None", [0.5] * 4),
        (False, 0.0, "and 1 poll (interval=", []),
    ],
)
def test_eventually_reports_the_last_signal_and_the_poll_count(
    falsy: object, timeout: float, rendered: str, delays: list[float]
) -> None:
    timeline = _Timeline()
    with pytest.raises(AssertionError) as caught:
        _poll(lambda: falsy, timeline, timeout)
    message = str(caught.value)
    assert type(caught.value) is AssertionError and caught.value.__cause__ is None
    assert message.startswith("foundry.assertions.eventually:")
    assert rendered in message and "1 polls" not in message
    assert f"and {len(delays) + 1} poll" in message and timeline.delays == delays


def test_eventually_survives_failing_and_frozen_polls() -> None:
    timeline = _Timeline()
    assert _poll(lambda: "order-7", timeline, 5.0) == "order-7" and timeline.delays == []
    polls: list[int] = []
    assert _poll(lambda: polls.append(1) or len(polls) == 3, timeline, 10.0) is True
    assert len(polls) == 3 and timeline.delays == [0.5, 0.5]

    def always_raises() -> bool:
        raise ValueError("boom 7")

    with pytest.raises(AssertionError) as caught:
        _poll(always_raises, timeline, 1.0)
    message = str(caught.value)
    assert "last exception ValueError(boom 7)" in message and "and 3 polls" in message
    attempts: list[int] = []
    timeline.delays.clear()

    def fail_once() -> str:
        if attempts.append(1) or len(attempts) < 2:
            raise ValueError("transient")
        return "ok"

    assert _poll(fail_once, timeline, 1.0) == "ok" and timeline.delays == [0.5]
    frozen = _Timeline(advance=False)
    with pytest.raises(AssertionError) as stuck:
        eventually(lambda: False, timeout=1.0, interval=0.5, sleep=frozen, clock=frozen)
    assert "poll budget of 4 exhausted" in str(stuck.value) and "and 4 polls" in str(stuck.value)
    assert frozen.delays == [0.5, 0.5, 0.5]
