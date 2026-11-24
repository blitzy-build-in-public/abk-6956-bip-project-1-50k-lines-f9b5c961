"""Unit tests for the four public assertion helpers of :mod:`foundry.assertions`.

The helpers are the whole of Foundry's assertion surface (AAP 0.4.3, AAP 0.7.3), and this module is
their unit contract. Nothing here drives pytest, the CLI or a report renderer, which AAP 0.14.5
places in ``tests/plugin`` and ``tests/e2e``; every payload is built in this module, and the only
file read is the committed ``tests/fixtures/response.schema.json`` reached through ``repo_root``.

* ``status`` reports the received status, the expected set and a redacted body excerpt that is a
  prefix of the body and no longer than
  :data:`~foundry.obs.redaction.MAX_EXCERPT_BYTES`, quotes no header at all, and reads no body on
  the passing path.
* ``json_subset`` reports the first mismatching JSON path from ``$``, refuses the ``True == 1``
  false pass, and withholds every value under a secret-bearing key.
* ``json_schema`` raises :class:`~foundry.core.exceptions.ContractError` -- never an
  ``AssertionError`` -- carrying the deterministic shallowest ``json_path`` and a ``detail`` that
  names the validator.
* ``eventually`` returns the first truthy value and quotes the last value or exception together
  with the poll count once the deadline passes, and it never spends real time: the sleep is a
  recording fake and the time source is a fake :class:`foundry.core.types.ClockLike`.

Determinism (AAP 0.13.1, AAP 0.14.3): ``time.sleep`` appears nowhere in this file, no wall-clock
value is read or asserted, poll counts and recorded intervals stand in for elapsed time, and every
collection compared in an assertion is built in a fixed order.

Five points where the behaviour on disk differs from the brief this file was written against, the
modules being authoritative:

1. ``eventually`` carries two additive keyword-only arguments beyond the AAP 0.4.3 signature --
   ``clock`` and ``redactor``, which AAP 0.4.9 permits -- and these tests need ``clock``: a deadline
   observed through a supplied clock is the only way to assert timeout behaviour without waiting.
2. ``assert_json_subset`` walks the expectation in **sorted** key order rather than in the
   expectation's document order, so when two keys disagree the reported path is the alphabetically
   first one. That is the property pinned here, because it is what makes the message reproducible.
3. "A ``timeout`` shorter than the first interval yields exactly one poll" holds exactly for
   ``timeout == 0``: with a positive timeout the poll precedes the deadline check, so the observable
   facts are two polls and a single wait clamped to the remaining time.
4. The excerpt inside an ``assert_status`` message is the ``repr`` of the redacted excerpt, so these
   tests parse it back with :func:`ast.literal_eval` before asserting the byte bound and the prefix
   property; the assertion is about the rendered string, never about an object attribute.
5. The redaction engine scrubs a single-line JSON body, a bare URL carrying a secret query parameter
   and a header line cleanly, so the sentinel payloads take those shapes.
"""

from __future__ import annotations

import ast
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final, cast

import pytest

from foundry.assertions import (
    assert_json_schema,
    assert_json_subset,
    assert_status,
    eventually,
)
from foundry.core.exceptions import ContractError
from foundry.obs.redaction import (
    MAX_EXCERPT_BYTES,
    REDACTED,
    TRUNCATION_MARKER,
    Redactor,
)

# A sentinel distinctive enough that finding it in any rendered text is a real leak rather than a
# coincidence, and shaped so that it cannot match a real provider's credential pattern.
_SECRET: Final[str] = "sentinel-credential-7c1f4b2e"

# The label ``assert_status`` puts in front of the excerpt, rebuilt from the published byte budget
# so a change to either the label or the budget fails here rather than passing silently.
_EXCERPT_LABEL: Final[str] = f"body excerpt (redacted, up to {MAX_EXCERPT_BYTES} bytes): "

# An ASCII body several times the excerpt budget, so truncation is observable and the surviving
# prefix is unambiguous.
_LONG_BODY: Final[str] = "".join(chr(ord("a") + offset % 26) for offset in range(600))


# --- Stand-ins -------------------------------------------------------------
# ``foundry.assertions`` may not import ``foundry.http`` (AAP 0.4.8), so the helpers annotate the
# ``HttpResponseLike`` protocol; a plain object carrying the three protocol members is enough here
# and keeps this module free of the HTTP layer.


@dataclass(frozen=True)
class _Response:
    """A minimal response satisfying :class:`foundry.core.types.HttpResponseLike`."""

    status_code: int
    body: str = ""
    headers: Mapping[str, str] = field(default_factory=dict)
    source: str | None = "live"

    @property
    def text(self) -> str:
        """Return the response body."""
        return self.body


@dataclass(frozen=True)
class _UnreadableResponse:
    """A response whose body raises when read, so a body read on the passing path fails loudly."""

    status_code: int

    @property
    def headers(self) -> Mapping[str, str]:
        """Return no headers; the helper never reads them."""
        return {}

    @property
    def text(self) -> str:
        """Raise: ``assert_status`` must not read the body once the status matches."""
        raise RuntimeError("the body must not be read when the status is one of the expected codes")


class _FakeClock:
    """A :class:`foundry.core.types.ClockLike` reading a fixed origin plus what it was advanced."""

    def __init__(self) -> None:
        self._origin = datetime(2025, 1, 2, 3, 4, 5, tzinfo=UTC)
        self._elapsed = 0.0

    def now(self) -> datetime:
        """Return the current instant of this fake timeline."""
        return self._origin + timedelta(seconds=self._elapsed)

    def advance(self, seconds: float) -> None:
        """Move this fake timeline forward by ``seconds``."""
        self._elapsed += seconds


class _SleepRecorder:
    """A :class:`foundry.core.types.Sleep` that records every delay and returns immediately.

    When a clock is bound, the recorded delay also advances it, which is what makes a poll interval
    cost nothing in real time while still moving the poller's deadline forward.
    """

    def __init__(self, clock: _FakeClock | None = None) -> None:
        self.delays: list[float] = []
        self._clock = clock

    def __call__(self, seconds: float) -> None:
        """Record ``seconds`` and advance the bound clock."""
        self.delays.append(seconds)
        if self._clock is not None:
            self._clock.advance(seconds)

    @property
    def total(self) -> float:
        """Return the sum of every recorded delay."""
        return sum(self.delays)


@pytest.fixture
def clock() -> _FakeClock:
    """Return a fake time source starting at a fixed instant."""
    return _FakeClock()


@pytest.fixture
def sleeping(clock: _FakeClock) -> _SleepRecorder:
    """Return a recording sleep bound to the fake clock, so a poll costs no real time."""
    return _SleepRecorder(clock)


# --- Helpers ---------------------------------------------------------------


def _rendered_excerpt(message: str) -> str:
    """Return the excerpt an ``assert_status`` message renders, parsed back to text."""
    _, separator, rendered = message.partition(_EXCERPT_LABEL)
    assert separator, f"the message carries no excerpt section: {message!r}"
    parsed = ast.literal_eval(rendered.strip())
    assert isinstance(parsed, str), f"the rendered excerpt is not text: {parsed!r}"
    return parsed


def _chain_text(error: BaseException) -> str:
    """Return the text of ``error`` and of every exception it chains to."""
    parts: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        parts.append(f"{type(current).__name__}: {current}")
        current = current.__cause__ if current.__cause__ is not None else current.__context__
    return "\n".join(parts)


def _valid_instance() -> dict[str, object]:
    """Return a response body that conforms to ``response.schema.json``."""
    return {
        "items": [{"quantity": 2, "sku": "SKU-1234"}],
        "order_id": "ORD-123456",
        "status": "paid",
        "totals": {"currency": "EUR", "grand_total_cents": 1999},
    }


@pytest.fixture(scope="module")
def response_schema(repo_root: Path) -> Path:
    """Return the committed response schema the contract assertions are proven against."""
    path = repo_root / "tests" / "fixtures" / "response.schema.json"
    assert path.is_file(), f"the committed schema fixture is missing: {path}"
    return path


# --- assert_status (AAP 0.4.3, AAP 0.7.3) ----------------------------------


@pytest.mark.parametrize("status", [200, 201, 204, 500, 503])
def test_assert_status_passes_for_a_single_expected_code(status: int) -> None:
    assert assert_status(_Response(status, body="not inspected"), status) is None


@pytest.mark.parametrize(
    "expected",
    [
        pytest.param(frozenset({200, 201}), id="frozenset"),
        pytest.param([204, 200], id="list"),
        pytest.param((200, 202), id="tuple"),
        pytest.param(range(200, 202), id="range"),
    ],
)
def test_assert_status_passes_for_a_member_of_an_iterable(expected: Iterable[int]) -> None:
    assert assert_status(_Response(200, body="not inspected"), expected) is None


def test_assert_status_reads_no_body_when_the_status_matches() -> None:
    assert_status(_UnreadableResponse(204), 204)


@pytest.mark.parametrize("source", ["live", "cassette"])
def test_assert_status_failure_names_status_expected_set_and_source(source: str) -> None:
    response = _Response(500, body='{"detail": "cart total changed"}', source=source)
    with pytest.raises(AssertionError) as caught:
        assert_status(response, {200, 201})
    message = str(caught.value)
    assert message.startswith("foundry.assertions.assert_status:")
    assert "received 500" in message
    assert "{200, 201}" in message
    assert f"source={source}" in message
    assert "cart total changed" in message


def test_assert_status_omits_the_source_when_the_response_reports_none() -> None:
    with pytest.raises(AssertionError) as caught:
        assert_status(_Response(404, body="missing", source=None), 200)
    assert "source=" not in str(caught.value)


def test_assert_status_excerpt_is_the_whole_body_when_it_fits() -> None:
    body = '{"detail": "insufficient stock"}'
    with pytest.raises(AssertionError) as caught:
        assert_status(_Response(409, body=body), 200)
    assert _rendered_excerpt(str(caught.value)) == body


def test_assert_status_excerpt_is_a_bounded_prefix_of_the_body() -> None:
    with pytest.raises(AssertionError) as caught:
        assert_status(_Response(404, body=_LONG_BODY), 200)
    message = str(caught.value)
    excerpt = _rendered_excerpt(message)
    assert len(excerpt.encode()) <= MAX_EXCERPT_BYTES + len(TRUNCATION_MARKER)
    assert excerpt.endswith(TRUNCATION_MARKER)
    prefix = excerpt[: -len(TRUNCATION_MARKER)]
    assert 0 < len(prefix) <= MAX_EXCERPT_BYTES
    assert prefix == _LONG_BODY[: len(prefix)]
    assert "\n" not in message


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(
            json.dumps({"detail": "upstream rejected the cart", "token": _SECRET}), id="json-pair"
        ),
        pytest.param(
            f"upstream rejected https://api.example.test/orders/7?token={_SECRET}", id="url-query"
        ),
        pytest.param(f"upstream rejected Authorization: Bearer {_SECRET}", id="header-line"),
    ],
)
def test_assert_status_message_carries_no_secret_from_the_body(body: str) -> None:
    response = _Response(502, body=body, headers={"Authorization": f"Bearer {_SECRET}"})
    with pytest.raises(AssertionError) as caught:
        assert_status(response, 200)
    message = str(caught.value)
    assert _SECRET not in message
    assert _SECRET not in _chain_text(caught.value)
    assert REDACTED in message
    assert "received 502" in message


def test_assert_status_header_values_never_reach_the_message() -> None:
    headers = {
        "Authorization": f"Bearer {_SECRET}",
        "X-Api-Key": _SECRET,
        "Set-Cookie": f"sid={_SECRET}",
    }
    with pytest.raises(AssertionError) as caught:
        assert_status(_Response(401, body="unauthorized", headers=headers), 200)
    assert _SECRET not in str(caught.value)


def test_assert_status_applies_a_supplied_redactor_to_the_excerpt() -> None:
    body = "upstream rejected nonce=abc123nonce"
    with pytest.raises(AssertionError) as caught:
        assert_status(_Response(400, body=body), 200)
    assert "abc123nonce" in str(caught.value)
    with pytest.raises(AssertionError) as caught_again:
        assert_status(_Response(400, body=body), 200, redactor=Redactor(["nonce"]))
    assert "abc123nonce" not in str(caught_again.value)


@pytest.mark.parametrize(
    "expected",
    [
        pytest.param(True, id="bool"),
        pytest.param("200", id="string"),
        pytest.param(-1, id="below-range"),
        pytest.param(1000, id="above-range"),
        pytest.param([], id="empty"),
    ],
)
def test_assert_status_rejects_a_misused_expected_argument(expected: object) -> None:
    with pytest.raises(AssertionError) as caught:
        assert_status(_Response(200), cast("int | Iterable[int]", expected))
    assert str(caught.value).startswith("foundry.assertions.assert_status:")


def test_assert_status_raises_the_builtin_assertion_error() -> None:
    with pytest.raises(AssertionError) as caught:
        assert_status(_Response(500, body="boom"), 200)
    assert type(caught.value) is AssertionError
    assert caught.value.__cause__ is None


# --- assert_json_subset (AAP 0.4.3) ----------------------------------------


def test_assert_json_subset_accepts_containment() -> None:
    actual: dict[str, object] = {
        "order_id": "ORD-123456",
        "status": "paid",
        "items": [{"sku": "SKU-1234", "quantity": 2, "price": 9.99}],
        "trace": {"request_id": "req-7"},
    }
    assert assert_json_subset(actual, {"status": "paid"}) is None
    assert_json_subset(actual, {"items": [{"sku": "SKU-1234", "price": 9.99}]})
    assert_json_subset(actual, {"items": [{"quantity": 2}], "trace": {}})
    assert_json_subset(actual, actual)


@pytest.mark.parametrize(
    ("actual", "expected", "path", "detail"),
    [
        pytest.param(
            {"a": {"b": 1}}, {"a": {"b": 2}}, "$.a.b", "expected 2, actual 1", id="nested"
        ),
        pytest.param(
            {"a": {"c": 1}},
            {"a": {"b": 1}},
            "$.a.b",
            "expected key 'b' is absent",
            id="missing-key",
        ),
        pytest.param({"id": 7}, {"id": "7"}, "$.id", "expected string, actual number", id="type"),
        pytest.param(
            {"items": [{"price": 9.99}]},
            {"items": [{"price": 1.0}]},
            "$.items[0].price",
            "expected 1.0, actual 9.99",
            id="list-element",
        ),
        pytest.param(
            {"items": [1]},
            {"items": [1, 2]},
            "$.items",
            "expected list of 2 elements",
            id="list-length",
        ),
        pytest.param([1, 2], {"a": 1}, "$", "expected object, actual list", id="object-expected"),
    ],
)
def test_assert_json_subset_names_the_first_mismatching_path(
    actual: object, expected: object, path: str, detail: str
) -> None:
    with pytest.raises(AssertionError) as caught:
        assert_json_subset(actual, expected)
    message = str(caught.value)
    assert message.startswith("foundry.assertions.assert_json_subset:")
    assert f"JSON subset mismatch at {path}: " in message
    assert detail in message


@pytest.mark.parametrize(
    ("expected", "actual", "detail"),
    [
        pytest.param(True, 1, "expected boolean, actual number", id="true-against-number"),
        pytest.param(1, True, "expected number, actual boolean", id="number-against-true"),
    ],
)
def test_assert_json_subset_refuses_boolean_number_equality(
    expected: object, actual: object, detail: str
) -> None:
    with pytest.raises(AssertionError) as caught:
        assert_json_subset({"flag": actual}, {"flag": expected})
    message = str(caught.value)
    assert "mismatch at $.flag: " in message
    assert detail in message


def test_assert_json_subset_reports_the_sorted_first_key_deterministically() -> None:
    actual = {"a": 1, "b": 2}
    expected = {"b": 9, "a": 9}
    messages = []
    for _ in range(2):
        with pytest.raises(AssertionError) as caught:
            assert_json_subset(actual, expected)
        messages.append(str(caught.value))
    assert messages[0] == messages[1]
    assert "mismatch at $.a: " in messages[0]
    assert "$.b" not in messages[0]


def test_assert_json_subset_withholds_values_under_secret_keys() -> None:
    with pytest.raises(AssertionError) as caught:
        assert_json_subset({"token": "actual-credential"}, {"token": "expected-credential"})
    message = str(caught.value)
    assert "mismatch at $.token: " in message
    assert REDACTED in message
    assert "actual-credential" not in message
    assert "expected-credential" not in message


def test_assert_json_subset_honours_a_nested_path_prefix() -> None:
    with pytest.raises(AssertionError) as caught:
        assert_json_subset({"a": 1}, {"a": 2}, path="$.data")
    assert "mismatch at $.data.a: " in str(caught.value)


def test_assert_json_subset_rejects_a_path_outside_the_document_root() -> None:
    with pytest.raises(AssertionError) as caught:
        assert_json_subset({"a": 1}, {"a": 1}, path="root")
    assert "path must be a str beginning with '$'" in str(caught.value)


def test_assert_json_subset_raises_the_builtin_assertion_error() -> None:
    with pytest.raises(AssertionError) as caught:
        assert_json_subset({"a": 1}, {"a": 2})
    assert type(caught.value) is AssertionError
    assert caught.value.__cause__ is None


# --- assert_json_schema (AAP 0.4.3, AAP 0.4.6) -----------------------------


def test_assert_json_schema_accepts_a_conforming_instance(response_schema: Path) -> None:
    assert assert_json_schema(_valid_instance(), response_schema) is None


@pytest.mark.parametrize("shape", ["path", "text"])
def test_assert_json_schema_reports_the_deterministic_shallowest_violation(
    response_schema: Path, shape: str
) -> None:
    instance = _valid_instance()
    instance["order_id"] = "BAD"
    instance["status"] = "unknown"
    schema = response_schema if shape == "path" else str(response_schema)
    reported: list[str] = []
    for _ in range(2):
        with pytest.raises(ContractError) as caught:
            assert_json_schema(instance, schema)
        error = caught.value
        assert type(error) is ContractError
        assert error.message == "contract validation failed at $.order_id"
        assert "validator: pattern" in error.detail
        reported.append(error.json_path)
    assert reported == ["$.order_id", "$.order_id"]


def test_assert_json_schema_names_a_violation_inside_a_list_element(response_schema: Path) -> None:
    instance = _valid_instance()
    instance["items"] = [{"quantity": 1, "sku": "nope"}]
    with pytest.raises(ContractError) as caught:
        assert_json_schema(instance, response_schema)
    assert caught.value.json_path == "$.items[0].sku"
    assert "validator: pattern" in caught.value.detail
    assert "nope" in caught.value.detail


def test_assert_json_schema_raises_contract_error_and_not_an_assertion(
    response_schema: Path,
) -> None:
    instance = _valid_instance()
    instance["status"] = "unknown"
    with pytest.raises(ContractError) as caught:
        assert_json_schema(instance, response_schema)
    assert not isinstance(caught.value, AssertionError)
    assert caught.value.json_path == "$.status"
    assert "validator: enum" in caught.value.detail


# --- eventually (AAP 0.4.3, AAP 0.7.3) -------------------------------------


def test_eventually_returns_the_first_truthy_value(
    clock: _FakeClock, sleeping: _SleepRecorder
) -> None:
    polls: list[int] = []

    def third_time() -> bool:
        polls.append(len(polls) + 1)
        return len(polls) == 3

    assert eventually(third_time, timeout=10.0, interval=0.5, sleep=sleeping, clock=clock) is True
    assert len(polls) == 3
    assert sleeping.delays == [0.5, 0.5]


def test_eventually_returns_without_waiting_when_the_first_poll_succeeds(
    clock: _FakeClock, sleeping: _SleepRecorder
) -> None:
    assert (
        eventually(lambda: "order-7", timeout=5.0, interval=1.0, sleep=sleeping, clock=clock)
        == "order-7"
    )
    assert sleeping.delays == []


def test_eventually_reports_the_last_value_and_the_poll_count(
    clock: _FakeClock, sleeping: _SleepRecorder
) -> None:
    with pytest.raises(AssertionError) as caught:
        eventually(lambda: False, timeout=2.0, interval=0.5, sleep=sleeping, clock=clock)
    message = str(caught.value)
    assert message.startswith("foundry.assertions.eventually:")
    assert "last value: False" in message
    assert "and 5 polls" in message
    assert sleeping.delays == [0.5, 0.5, 0.5, 0.5]
    assert sleeping.total == 2.0


def test_eventually_never_accepts_none(clock: _FakeClock, sleeping: _SleepRecorder) -> None:
    with pytest.raises(AssertionError) as caught:
        eventually(lambda: None, timeout=1.0, interval=0.5, sleep=sleeping, clock=clock)
    assert "last value: None" in str(caught.value)


def test_eventually_reports_the_last_exception(clock: _FakeClock, sleeping: _SleepRecorder) -> None:
    def always_raises() -> bool:
        raise ValueError("boom 7")

    with pytest.raises(AssertionError) as caught:
        eventually(always_raises, timeout=1.0, interval=0.5, sleep=sleeping, clock=clock)
    message = str(caught.value)
    assert "last exception ValueError(boom 7)" in message
    assert "and 3 polls" in message
    assert caught.value.__cause__ is None


def test_eventually_recovers_when_a_poll_raises_once(
    clock: _FakeClock, sleeping: _SleepRecorder
) -> None:
    attempts: list[int] = []

    def fail_once() -> str:
        attempts.append(len(attempts) + 1)
        if len(attempts) == 1:
            raise ValueError("transient")
        return "ok"

    assert eventually(fail_once, timeout=1.0, interval=0.5, sleep=sleeping, clock=clock) == "ok"
    assert sleeping.delays == [0.5]


def test_eventually_with_a_zero_timeout_polls_exactly_once(
    clock: _FakeClock, sleeping: _SleepRecorder
) -> None:
    polls: list[int] = []

    def counted() -> bool:
        polls.append(len(polls) + 1)
        return False

    with pytest.raises(AssertionError) as caught:
        eventually(counted, timeout=0.0, interval=0.5, sleep=sleeping, clock=clock)
    message = str(caught.value)
    assert len(polls) == 1
    assert sleeping.delays == []
    assert "and 1 poll (interval=" in message
    assert "1 polls" not in message


def test_eventually_clamps_the_wait_to_the_remaining_time(
    clock: _FakeClock, sleeping: _SleepRecorder
) -> None:
    with pytest.raises(AssertionError) as caught:
        eventually(lambda: False, timeout=0.25, interval=1.0, sleep=sleeping, clock=clock)
    assert sleeping.delays == [0.25]
    assert "and 2 polls" in str(caught.value)


def test_eventually_stops_on_a_clock_that_never_advances(clock: _FakeClock) -> None:
    frozen = _SleepRecorder()
    with pytest.raises(AssertionError) as caught:
        eventually(lambda: False, timeout=1.0, interval=0.5, sleep=frozen, clock=clock)
    message = str(caught.value)
    assert "poll budget of 4 exhausted" in message
    assert "and 4 polls" in message
    assert frozen.delays == [0.5, 0.5, 0.5]


def test_eventually_raises_the_builtin_assertion_error(
    clock: _FakeClock, sleeping: _SleepRecorder
) -> None:
    with pytest.raises(AssertionError) as caught:
        eventually(lambda: False, timeout=0.0, interval=0.5, sleep=sleeping, clock=clock)
    assert type(caught.value) is AssertionError
    assert caught.value.__cause__ is None
