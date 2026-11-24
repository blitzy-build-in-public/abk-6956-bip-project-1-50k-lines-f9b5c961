"""The tie between the single source of Foundry's configuration numbers and their registry.

AAP 0.4.5 publishes a default and an accepted range for every configuration key, and AAP 0.4.9
makes those values, the constraint texts included, a published surface that may not change. The
numbers live in two places by necessity: ``foundry.config.keys.KEY_SPECS`` states the constraint
texts for the validators and the messages that quote them, and
:mod:`foundry.core.limits` states the same numbers for the five capability packages that may not
import ``foundry.config`` (AAP 0.4.8).

This module is the proof that the two statements agree. It reads the bounds back out of the
registry, compares them with the constants on the foundation rung, compares each ``*_RANGE`` text
with the registry's constraint text byte for byte, and checks that every capability package
publishes the single source's value rather than a copy of its own. A drifted number therefore fails
here before it reaches a policy, a message or a report.

The one value the registry cannot supply is ``cassette.dir``'s default -- ``KEY_SPECS`` records no
defaults at all -- so that value is pinned against AAP 0.4.5 directly.
"""

from __future__ import annotations

import re
from collections.abc import Callable

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
"""A closed range as the registry spells one, for example ``1..10``."""

_TIMEOUT_PATTERN = re.compile(r"^(?P<low>-?\d+(?:\.\d+)?) < t <= (?P<high>-?\d+(?:\.\d+)?)$")
"""A range with an exclusive lower bound, the other spelling the registry uses: ``0 < t <= 300``."""

_BOUND_CASES: tuple[tuple[str, float, float], ...] = (
    ("logging.capture_lines", limits.CAPTURE_LINES_MIN, limits.CAPTURE_LINES_MAX),
    ("http.timeout", limits.HTTP_TIMEOUT_MIN, limits.HTTP_TIMEOUT_MAX),
    ("http.retries.max_attempts", limits.MAX_ATTEMPTS_MIN, limits.MAX_ATTEMPTS_MAX),
    ("http.retries.backoff_base", limits.BACKOFF_BASE_MIN, limits.BACKOFF_BASE_MAX),
    ("http.retries.backoff_max", limits.BACKOFF_MAX_MIN, limits.BACKOFF_MAX_MAX),
    ("execution.retries", limits.EXECUTION_RETRIES_MIN, limits.EXECUTION_RETRIES_MAX),
    ("execution.timeout", limits.TIMEOUT_MIN, limits.TIMEOUT_MAX),
)
"""Every numeric range the single source covers, with the bounds it states for it."""

_RANGE_TEXT_CASES: tuple[tuple[str, str], ...] = (
    ("logging.capture_lines", limits.CAPTURE_LINES_RANGE),
    ("http.timeout", limits.HTTP_TIMEOUT_RANGE),
    ("http.retries.max_attempts", limits.MAX_ATTEMPTS_RANGE),
    ("http.retries.backoff_base", limits.BACKOFF_BASE_RANGE),
    ("http.retries.backoff_max", limits.BACKOFF_MAX_RANGE),
    ("execution.retries", limits.EXECUTION_RETRIES_RANGE),
    ("execution.timeout", limits.TIMEOUT_RANGE),
)
"""Every ``*_RANGE`` text beside the key whose published constraint it must equal."""

_CAPABILITY_CASES: tuple[tuple[str, object, object], ...] = (
    ("capture.DEFAULT_CAPTURE_LINES", capture.DEFAULT_CAPTURE_LINES, limits.CAPTURE_LINES_DEFAULT),
    ("capture.MIN_CAPTURE_LINES", capture.MIN_CAPTURE_LINES, limits.CAPTURE_LINES_MIN),
    ("capture.MAX_CAPTURE_LINES", capture.MAX_CAPTURE_LINES, limits.CAPTURE_LINES_MAX),
    ("panels.DEFAULT_LOG_TAIL_LINES", panels.DEFAULT_LOG_TAIL_LINES, limits.CAPTURE_LINES_DEFAULT),
    ("panels.MAX_LOG_TAIL_LINES", panels.MAX_LOG_TAIL_LINES, limits.CAPTURE_LINES_MAX),
    ("paths.DEFAULT_CASSETTE_DIR", paths.DEFAULT_CASSETTE_DIR, limits.CASSETTE_DIR_DEFAULT),
    ("session.DEFAULT_TIMEOUT", session.DEFAULT_TIMEOUT, limits.HTTP_TIMEOUT_DEFAULT),
    ("session.MAX_TIMEOUT", session.MAX_TIMEOUT, limits.HTTP_TIMEOUT_MAX),
    ("retries.MIN_ATTEMPTS", retries.MIN_ATTEMPTS, limits.MAX_ATTEMPTS_MIN),
    ("retries.MAX_ATTEMPTS", retries.MAX_ATTEMPTS, limits.MAX_ATTEMPTS_MAX),
    ("retries.MIN_BACKOFF_BASE", retries.MIN_BACKOFF_BASE, limits.BACKOFF_BASE_MIN),
    ("retries.MAX_BACKOFF_BASE", retries.MAX_BACKOFF_BASE, limits.BACKOFF_BASE_MAX),
    ("retries.MIN_BACKOFF_MAX", retries.MIN_BACKOFF_MAX, limits.BACKOFF_MAX_MIN),
    ("retries.MAX_BACKOFF_MAX", retries.MAX_BACKOFF_MAX, limits.BACKOFF_MAX_MAX),
    ("timeout.MIN_TIMEOUT_SECONDS", timeout.MIN_TIMEOUT_SECONDS, limits.TIMEOUT_MIN),
    ("timeout.MAX_TIMEOUT_SECONDS", timeout.MAX_TIMEOUT_SECONDS, limits.TIMEOUT_MAX),
    ("timeout.TIMEOUT_RANGE_TEXT", timeout.TIMEOUT_RANGE_TEXT, limits.TIMEOUT_RANGE),
    ("retry.MIN_RETRIES", retry.MIN_RETRIES, limits.EXECUTION_RETRIES_MIN),
    ("retry.MAX_RETRIES", retry.MAX_RETRIES, limits.EXECUTION_RETRIES_MAX),
    ("retry.RETRIES_RANGE_TEXT", retry.RETRIES_RANGE_TEXT, limits.EXECUTION_RETRIES_RANGE),
)
"""Every public constant a capability package re-publishes, beside the single source's value."""

_POLICY_DEFAULT_CASES: tuple[tuple[str, object], ...] = (
    ("max_attempts", limits.MAX_ATTEMPTS_DEFAULT),
    ("backoff_base", limits.BACKOFF_BASE_DEFAULT),
    ("backoff_max", limits.BACKOFF_MAX_DEFAULT),
)
"""The three ``RetryPolicy`` field defaults, beside the single source's values."""

_FAULT_CASES: tuple[tuple[str, Callable[[], object], str], ...] = (
    (
        "http.retries.max_attempts",
        lambda: retries.RetryPolicy(max_attempts=11),
        "invalid value for http.retries.max_attempts: 11 (expected 1..10)",
    ),
    (
        "http.retries.backoff_base",
        lambda: retries.RetryPolicy(backoff_base=11.0),
        "invalid value for http.retries.backoff_base: 11.0 (expected 0..10)",
    ),
    (
        "execution.timeout",
        lambda: timeout.TimeoutPolicy(seconds=0.0),
        "invalid value for execution.timeout: 0.0 (expected 0 < t <= 3600)",
    ),
    (
        "execution.retries",
        lambda: retry.ExecutionRetries(repeats=11),
        "invalid value for execution.retries: 11 (expected 0..10)",
    ),
)
"""One out-of-range value per capability validator, with the message AAP 0.4.5 fixes for it."""


def _bounds_of(dotted: str) -> tuple[float, float]:
    """Return the low and high bound the registry publishes for ``dotted``.

    Args:
        dotted: The dotted configuration key to read from
            :func:`foundry.config.keys.constraint_for`.

    Returns:
        The two bounds the key's constraint text states, as floats.

    Raises:
        AssertionError: When the key is unregistered or its constraint is not one of the two
            numeric range spellings this module parses, which would leave the comparison below
            unable to prove anything.
    """
    constraint = keys.constraint_for(dotted)
    assert constraint is not None, f"{dotted} is not registered in KEY_SPECS"
    match = _RANGE_PATTERN.match(constraint) or _TIMEOUT_PATTERN.match(constraint)
    assert match is not None, f"{dotted} publishes no numeric range: {constraint!r}"
    return float(match.group("low")), float(match.group("high"))


@pytest.mark.parametrize(("dotted", "low", "high"), _BOUND_CASES)
def test_registry_bounds_equal_the_single_source(dotted: str, low: float, high: float) -> None:
    """The registry's published bounds and ``foundry.core.limits`` state the same numbers."""
    assert _bounds_of(dotted) == (float(low), float(high))


@pytest.mark.parametrize(("dotted", "text"), _RANGE_TEXT_CASES)
def test_range_text_is_the_registry_constraint_byte_for_byte(dotted: str, text: str) -> None:
    """Each ``*_RANGE`` text is the constraint a range fault quotes, character for character."""
    assert text == keys.constraint_for(dotted)


@pytest.mark.parametrize(
    ("label", "published", "expected"),
    _CAPABILITY_CASES,
    ids=[case[0] for case in _CAPABILITY_CASES],
)
def test_capability_constant_is_the_single_source_value(
    label: str, published: object, expected: object
) -> None:
    """Every re-published capability constant carries the single source's value, not a copy."""
    assert published == expected, label


@pytest.mark.parametrize(("field", "expected"), _POLICY_DEFAULT_CASES)
def test_retry_policy_default_is_the_single_source_value(field: str, expected: object) -> None:
    """``RetryPolicy()``'s field defaults are the ``http.retries.*`` defaults of AAP 0.4.5."""
    assert getattr(retries.RetryPolicy(), field) == expected


def test_session_settings_default_timeout_is_the_single_source_value() -> None:
    """A hand-built ``SessionSettings`` carries the published ``http.timeout`` default.

    ``foundry.http`` may not read the configuration model (AAP 0.4.8), so the session's own default
    is the one copy of ``http.timeout`` a suite sees before any fixture runs: a changed published
    default must move it, and this assertion is what fails when it does not.
    """
    assert session.SessionSettings().timeout == limits.HTTP_TIMEOUT_DEFAULT


def test_execution_retries_default_is_the_single_source_value() -> None:
    """The retry loop's own default is the published ``execution.retries`` default."""
    assert retry.ExecutionRetries().repeats == limits.EXECUTION_RETRIES_DEFAULT


def test_range_template_is_the_single_source_template() -> None:
    """``errors.RANGE_TEMPLATE`` is the shared template object, not an equal copy of its text."""
    assert errors.RANGE_TEMPLATE is limits.RANGE_FAULT_TEMPLATE


@pytest.mark.parametrize(
    ("value", "expected"),
    (
        (11, "11"),
        (11.0, "11.0"),
        (None, "None"),
        ("-1", "-1"),
        ("abc", "abc"),
    ),
)
def test_range_fault_message_renders_the_value(value: object, expected: str) -> None:
    """An already-rendered ``str`` is used as it stands; any other value is rendered by ``repr``.

    The configuration layer hands over the raw text of a file, an environment variable or an
    option, so quoting it here would change the wording AAP 0.4.5 pins (``http.timeout = -1``
    reports ``-1``); a capability package hands over the value it was given, which ``repr``
    renders so an ``int`` reads ``11`` and a ``float`` keeps its ``.0``.
    """
    assert (
        limits.range_fault_message("http.timeout", value, "0 < t <= 300")
        == f"invalid value for http.timeout: {expected} (expected 0 < t <= 300)"
    )


@pytest.mark.parametrize(
    ("dotted", "factory", "message"), _FAULT_CASES, ids=[c[0] for c in _FAULT_CASES]
)
def test_out_of_range_policy_reports_the_one_wording(
    dotted: str, factory: Callable[[], object], message: str
) -> None:
    """A policy built out of range is rejected with the configuration layer's one sentence."""
    with pytest.raises(ConfigError) as excinfo:
        factory()
    assert excinfo.value.message == message
    assert excinfo.value.key == dotted


def test_cassette_dir_default_is_the_published_value() -> None:
    """``cassette.dir`` defaults to ``cassettes`` (AAP 0.4.5).

    The registry carries no defaults, so this is the one value this module pins directly rather
    than reading back out of :data:`foundry.config.keys.KEY_SPECS`.
    """
    assert paths.DEFAULT_CASSETTE_DIR == "cassettes"
