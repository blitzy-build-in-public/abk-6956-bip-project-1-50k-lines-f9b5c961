"""The published defaults and bounds of the numeric configuration keys, stated once.

AAP 0.4.5 publishes a default and an accepted range for every configuration key, and
``foundry.config.keys.KEY_SPECS`` keeps each constraint text literally for the validators and the
messages that quote it. Those numbers have to be readable from outside ``foundry.config`` as well,
because five capability packages use them and may not import that package: AAP 0.4.8 lets
``foundry.obs``, ``foundry.reporting``, ``foundry.cassette``, ``foundry.http`` and
``foundry.execution`` reach the foundation rung only. This module is the one home for them, so
``logging.capture_lines``, ``cassette.dir``, ``http.timeout``, ``http.retries.max_attempts``,
``http.retries.backoff_base``, ``http.retries.backoff_max``, ``execution.retries`` and
``execution.timeout`` each have a single definition site for their default and their bounds.

The two statements are tied rather than merely intended to agree:
``tests/unit/test_config_constants.py`` parses the bounds back out of
``foundry.config.keys.constraint_for`` for each of those keys and compares them with the constants
here, and compares every ``*_RANGE`` text with the registry's constraint text byte for byte, so a
copy cannot drift from the published surface AAP 0.4.9 fixes.

:func:`range_fault_message` is the one wording of a value outside its accepted range, shared by the
configuration layer and by the two capability packages that validate a hand-built policy. AAP 0.4.5
pins the shape -- the key, the offending value and the accepted range in one sentence -- so a
person correcting ``foundry.toml``, an environment variable, a command-line option or a policy
built inside a test reads the same sentence whichever surface reported the fault.

The module sits on the foundation rung, so it imports the standard library alone and holds only
immutable constants plus one pure function: importing it reads no environment variable, touches no
file, configures no logger and mutates nothing (AAP 0.4.8 import-time discipline).
"""

from __future__ import annotations

from typing import Final

__all__: Final[tuple[str, ...]] = (
    "BACKOFF_BASE_DEFAULT",
    "BACKOFF_BASE_MAX",
    "BACKOFF_BASE_MIN",
    "BACKOFF_BASE_RANGE",
    "BACKOFF_MAX_DEFAULT",
    "BACKOFF_MAX_MAX",
    "BACKOFF_MAX_MIN",
    "BACKOFF_MAX_RANGE",
    "CAPTURE_LINES_DEFAULT",
    "CAPTURE_LINES_MAX",
    "CAPTURE_LINES_MIN",
    "CAPTURE_LINES_RANGE",
    "CASSETTE_DIR_DEFAULT",
    "EXECUTION_RETRIES_DEFAULT",
    "EXECUTION_RETRIES_MAX",
    "EXECUTION_RETRIES_MIN",
    "EXECUTION_RETRIES_RANGE",
    "HTTP_TIMEOUT_DEFAULT",
    "HTTP_TIMEOUT_MAX",
    "HTTP_TIMEOUT_MIN",
    "HTTP_TIMEOUT_RANGE",
    "MAX_ATTEMPTS_DEFAULT",
    "MAX_ATTEMPTS_MAX",
    "MAX_ATTEMPTS_MIN",
    "MAX_ATTEMPTS_RANGE",
    "RANGE_FAULT_TEMPLATE",
    "TIMEOUT_DEFAULT",
    "TIMEOUT_MAX",
    "TIMEOUT_MIN",
    "TIMEOUT_RANGE",
    "range_fault_message",
)

# --- logging.capture_lines -------------------------------------------------


CAPTURE_LINES_DEFAULT: Final[int] = 200
"""Default of ``logging.capture_lines``: the size of the per-test log ring and the log tail."""

CAPTURE_LINES_MIN: Final[int] = 0
"""Smallest accepted ``logging.capture_lines``; ``0`` is a clean no-capture state, not an error."""

CAPTURE_LINES_MAX: Final[int] = 10_000
"""Largest accepted ``logging.capture_lines``, which bounds one ring and one report log tail."""

CAPTURE_LINES_RANGE: Final[str] = f"{CAPTURE_LINES_MIN}..{CAPTURE_LINES_MAX}"
"""The accepted range of ``logging.capture_lines``, spelled as its constraint text: ``0..10000``."""


# --- cassette.dir ----------------------------------------------------------


CASSETTE_DIR_DEFAULT: Final[str] = "cassettes"
"""Default of ``cassette.dir``, the directory the cassette store reads and writes.

The only non-numeric default the capability packages need: the cassette package has to build a
path before any configuration is resolved, and it may not read the configuration model (AAP 0.4.8).
"""


# --- http.retries.max_attempts ---------------------------------------------


MAX_ATTEMPTS_DEFAULT: Final[int] = 3
"""Default of ``http.retries.max_attempts``: three attempts per logical request (AAP 0.4.5)."""

MAX_ATTEMPTS_MIN: Final[int] = 1
"""Smallest accepted ``http.retries.max_attempts``: the first attempt is always made."""

MAX_ATTEMPTS_MAX: Final[int] = 10
"""Largest accepted ``http.retries.max_attempts``: the total count, the first attempt included."""

MAX_ATTEMPTS_RANGE: Final[str] = f"{MAX_ATTEMPTS_MIN}..{MAX_ATTEMPTS_MAX}"
"""Accepted range of ``http.retries.max_attempts``, spelled as its constraint text: ``1..10``."""


# --- http.retries.backoff_base ---------------------------------------------


BACKOFF_BASE_DEFAULT: Final[float] = 0.2
"""Default of ``http.retries.backoff_base``: the wait before the second attempt, in seconds."""

BACKOFF_BASE_MIN: Final[float] = 0.0
"""Smallest accepted ``http.retries.backoff_base``, in seconds; ``0`` waits not at all."""

BACKOFF_BASE_MAX: Final[float] = 10.0
"""Largest accepted ``http.retries.backoff_base``, in seconds."""

BACKOFF_BASE_RANGE: Final[str] = f"{BACKOFF_BASE_MIN:g}..{BACKOFF_BASE_MAX:g}"
"""The accepted range of ``http.retries.backoff_base`` as its constraint text: ``0..10``."""


# --- http.retries.backoff_max ----------------------------------------------


BACKOFF_MAX_DEFAULT: Final[float] = 2.0
"""Default of ``http.retries.backoff_max``: the cap on any single wait, in seconds."""

BACKOFF_MAX_MIN: Final[float] = 0.0
"""Smallest accepted ``http.retries.backoff_max``, in seconds; ``0`` caps every wait at zero."""

BACKOFF_MAX_MAX: Final[float] = 60.0
"""Largest accepted ``http.retries.backoff_max``, in seconds."""

BACKOFF_MAX_RANGE: Final[str] = f"{BACKOFF_MAX_MIN:g}..{BACKOFF_MAX_MAX:g}"
"""The accepted range of ``http.retries.backoff_max`` as its constraint text: ``0..60``."""


# --- http.timeout ----------------------------------------------------------


HTTP_TIMEOUT_DEFAULT: Final[float] = 10.0
"""Default of ``http.timeout``: the deadline in seconds of a request that names none (AAP 0.4.5)."""

HTTP_TIMEOUT_MIN: Final[float] = 0.0
"""Exclusive lower bound of ``http.timeout``, in seconds: a zero deadline would wait for nothing."""

HTTP_TIMEOUT_MAX: Final[float] = 300.0
"""Inclusive upper bound of ``http.timeout``, in seconds: one request waits at most five minutes."""

HTTP_TIMEOUT_RANGE: Final[str] = f"{HTTP_TIMEOUT_MIN:g} < t <= {HTTP_TIMEOUT_MAX:g}"
"""The accepted range of ``http.timeout`` as its constraint text: ``0 < t <= 300``."""


# --- execution.timeout -----------------------------------------------------


TIMEOUT_DEFAULT: Final[float] = 60.0
"""Default of ``execution.timeout``, in seconds: one minute for a single test (AAP 0.4.5)."""

TIMEOUT_MIN: Final[float] = 0.0
"""Exclusive lower bound of ``execution.timeout``, in seconds: a zero limit would arm no timer."""

TIMEOUT_MAX: Final[float] = 3600.0
"""Inclusive upper bound of ``execution.timeout``, in seconds: one hour for a single test."""

TIMEOUT_RANGE: Final[str] = f"{TIMEOUT_MIN:g} < t <= {TIMEOUT_MAX:g}"
"""The accepted range of ``execution.timeout`` as its constraint text: ``0 < t <= 3600``."""


# --- execution.retries -----------------------------------------------------


EXECUTION_RETRIES_DEFAULT: Final[int] = 0
"""Default of ``execution.retries``: a failing test runs once and is not repeated (AAP 0.4.5)."""

EXECUTION_RETRIES_MIN: Final[int] = 0
"""Smallest accepted ``execution.retries``: the test runs once and is not repeated."""

EXECUTION_RETRIES_MAX: Final[int] = 10
"""Largest accepted ``execution.retries``: ten repeats after the first attempt, eleven attempts."""

EXECUTION_RETRIES_RANGE: Final[str] = f"{EXECUTION_RETRIES_MIN}..{EXECUTION_RETRIES_MAX}"
"""The accepted range of ``execution.retries`` as its constraint text: ``0..10``."""


# --- The one range-fault wording -------------------------------------------


RANGE_FAULT_TEMPLATE: Final[str] = "invalid value for {key}: {value} (expected {constraint})"
"""The one wording of a value outside its range, filled in by :func:`range_fault_message`."""


def range_fault_message(key: str, value: object, constraint: str) -> str:
    """Return the one message Foundry reports for a value outside its accepted range.

    AAP 0.4.5's worked case is ``http.timeout = -1``, which must report ``http.timeout`` and
    ``0 < t <= 300``; the sentence below carries the key, the offending value and the accepted
    range, so both stay greppable in a terminal and in a CI log.

    Args:
        key: The dotted configuration key at fault, for example
            ``http.retries.max_attempts``.
        value: The offending value. A ``str`` is used exactly as given, because the
            configuration layer renders the raw text of its file, environment variable or
            option before calling this function; any other value is rendered with
            :func:`repr`, so an ``int`` reads ``11`` and a ``float`` keeps its ``11.0``.
        constraint: The accepted range, quoted verbatim from the source of truth that
            publishes it -- the constraint text of ``foundry.config.keys.KEY_SPECS`` where the
            key is registered, or the ``*_RANGE`` constant above where a capability package
            validates a policy of its own.

    Returns:
        The message, with no trailing period, ready to be the message of a
        :class:`foundry.core.exceptions.ConfigError`.

    The function is pure and total: it reads nothing, raises nothing and returns the same text
    for the same arguments, so a fault reads alike whichever surface reported it.
    """
    rendered = value if isinstance(value, str) else repr(value)
    return RANGE_FAULT_TEMPLATE.format(key=key, value=rendered, constraint=constraint)
