"""The tie between the single source of Foundry's configuration numbers and their registry: the
bounds and range texts of ``foundry.core.limits``, the constraint texts in ``foundry.config.keys``
and every constant a capability package re-publishes must agree, number for number and text too."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Final

import pytest

from foundry.cassette import paths
from foundry.config import errors, keys
from foundry.core import limits
from foundry.core.exceptions import ConfigError
from foundry.execution import retry, timeout
from foundry.http import retries, session
from foundry.obs import capture
from foundry.reporting import panels

_RANGE_PATTERN = re.compile(r"^(?P<low>-?\d+(?:\.\d+)?)\.\.(?P<high>-?\d+(?:\.\d+)?)$")
_TIMEOUT_PATTERN = re.compile(r"^(?P<low>-?\d+(?:\.\d+)?) < t <= (?P<high>-?\d+(?:\.\d+)?)$")
_STEMS: Final[tuple[tuple[str, str], ...]] = (
    ("logging.capture_lines", "CAPTURE_LINES"),
    ("http.timeout", "HTTP_TIMEOUT"),
    ("http.retries.max_attempts", "MAX_ATTEMPTS"),
    ("http.retries.backoff_base", "BACKOFF_BASE"),
    ("http.retries.backoff_max", "BACKOFF_MAX"),
    ("execution.retries", "EXECUTION_RETRIES"),
    ("execution.timeout", "TIMEOUT"),
)
_FAULT_CASES: Final[tuple[tuple[str, Callable[[], object], str, str], ...]] = (
    ("http.retries.max_attempts", lambda: retries.RetryPolicy(max_attempts=11), "11", "1..10"),
    ("http.retries.backoff_base", lambda: retries.RetryPolicy(backoff_base=11.0), "11.0", "0..10"),
    ("execution.timeout", lambda: timeout.TimeoutPolicy(seconds=0.0), "0.0", "0 < t <= 3600"),
    ("execution.retries", lambda: retry.ExecutionRetries(repeats=11), "11", "0..10"),
)


def _bounds_of(text: str) -> tuple[float, float]:
    match = _RANGE_PATTERN.match(text) or _TIMEOUT_PATTERN.match(text)
    assert match is not None, f"{text!r} publishes no numeric range"
    return float(match.group("low")), float(match.group("high"))


@pytest.mark.parametrize(("dotted", "stem"), _STEMS)
def test_registry_bounds_and_range_text_are_the_single_source(dotted: str, stem: str) -> None:
    constraint = keys.constraint_for(dotted)
    assert constraint is not None
    low, high = getattr(limits, f"{stem}_MIN"), getattr(limits, f"{stem}_MAX")
    assert _bounds_of(constraint) == (float(low), float(high))
    assert constraint == getattr(limits, f"{stem}_RANGE")


def test_capability_constants_are_the_single_source_values() -> None:
    tail = limits.CAPTURE_LINES_DEFAULT
    assert capture.DEFAULT_CAPTURE_LINES == panels.DEFAULT_LOG_TAIL_LINES == tail
    assert capture.MIN_CAPTURE_LINES == limits.CAPTURE_LINES_MIN
    assert capture.MAX_CAPTURE_LINES == panels.MAX_LOG_TAIL_LINES == limits.CAPTURE_LINES_MAX
    assert paths.DEFAULT_CASSETTE_DIR == "cassettes" == limits.CASSETTE_DIR_DEFAULT
    assert session.DEFAULT_TIMEOUT == limits.HTTP_TIMEOUT_DEFAULT
    assert session.MAX_TIMEOUT == limits.HTTP_TIMEOUT_MAX
    assert retries.MIN_ATTEMPTS == limits.MAX_ATTEMPTS_MIN
    assert retries.MAX_ATTEMPTS == limits.MAX_ATTEMPTS_MAX
    assert retries.MIN_BACKOFF_BASE == limits.BACKOFF_BASE_MIN
    assert retries.MAX_BACKOFF_BASE == limits.BACKOFF_BASE_MAX
    assert retries.MIN_BACKOFF_MAX == limits.BACKOFF_MAX_MIN
    assert retries.MAX_BACKOFF_MAX == limits.BACKOFF_MAX_MAX
    assert timeout.MIN_TIMEOUT_SECONDS == limits.TIMEOUT_MIN
    assert timeout.MAX_TIMEOUT_SECONDS == limits.TIMEOUT_MAX
    assert timeout.TIMEOUT_RANGE_TEXT == limits.TIMEOUT_RANGE
    assert retry.MIN_RETRIES == limits.EXECUTION_RETRIES_MIN
    assert retry.MAX_RETRIES == limits.EXECUTION_RETRIES_MAX
    assert retry.RETRIES_RANGE_TEXT == limits.EXECUTION_RETRIES_RANGE


def test_the_policy_defaults_and_the_range_template_are_the_single_source() -> None:
    policy = retries.RetryPolicy()
    assert policy.max_attempts == limits.MAX_ATTEMPTS_DEFAULT
    assert policy.backoff_base == limits.BACKOFF_BASE_DEFAULT
    assert policy.backoff_max == limits.BACKOFF_MAX_DEFAULT
    assert session.SessionSettings().timeout == limits.HTTP_TIMEOUT_DEFAULT
    assert retry.ExecutionRetries().repeats == limits.EXECUTION_RETRIES_DEFAULT
    assert errors.RANGE_TEMPLATE is limits.RANGE_FAULT_TEMPLATE


@pytest.mark.parametrize(
    ("value", "expected"),
    ((11, "11"), (11.0, "11.0"), (None, "None"), ("-1", "-1"), ("abc", "abc")),
)
def test_range_fault_message_renders_the_value(value: object, expected: str) -> None:
    assert limits.range_fault_message("http.timeout", value, "0 < t <= 300") == (
        f"invalid value for http.timeout: {expected} (expected 0 < t <= 300)"
    )


@pytest.mark.parametrize(
    ("dotted", "factory", "literal", "constraint"),
    _FAULT_CASES,
    ids=[case[0] for case in _FAULT_CASES],
)
def test_out_of_range_policy_reports_the_one_wording(
    dotted: str, factory: Callable[[], object], literal: str, constraint: str
) -> None:
    with pytest.raises(ConfigError) as excinfo:
        factory()
    assert excinfo.value.message == f"invalid value for {dotted}: {literal} (expected {constraint})"
    assert excinfo.value.key == dotted
