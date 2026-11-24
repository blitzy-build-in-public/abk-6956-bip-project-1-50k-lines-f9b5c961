"""Structured lifecycle events: the one module Foundry writes log records through.

No other module calls a logger directly, and :func:`log_event` demotes anything above ``WARNING``.
Emitters take scalars only, and the two that take free text scrub it through
:meth:`foundry.obs.redaction.Redactor.text` with the session's engine, because pytest prints these
records in a captured-log block (AAP 0.3.2, AAP 0.13.1).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from foundry.obs.logging import get_logger, level_for
from foundry.obs.redaction import REDACTED, Redactor, default_redactor

__all__ = [
    "LEVEL_DEBUG",
    "LEVEL_INFO",
    "LEVEL_WARNING",
    "MAX_EVENT_LEVEL",
    "LifecycleEvent",
    "clamp_level",
    "emit_artifact_written",
    "emit_attempt",
    "emit_cassette_mode",
    "emit_html_fallback",
    "emit_no_config_file",
    "emit_plumbing_failure",
    "emit_report_written",
    "emit_resolution",
    "emit_run_start",
    "emit_sharding_decision",
    "event_line",
    "log_event",
]

#: The band this module emits in. ``DEBUG``: configuration resolution (the layer and the key, never
#: the value), cassette mode, sharding and per-attempt detail. ``INFO``: the one line a run emits,
#: from :func:`emit_run_start`, with its run id, profile and artifacts root. ``WARNING``: a
#: failure-isolation fault or the documented HTML-report fallback, which is the highest level here.
LEVEL_DEBUG: Final[int] = logging.DEBUG
LEVEL_INFO: Final[int] = logging.INFO
LEVEL_WARNING: Final[int] = logging.WARNING
MAX_EVENT_LEVEL: Final[int] = logging.WARNING

#: Prefix of every event line, matching :data:`foundry.obs.logging.LOGGER_NAME`, and the label used
#: when an event carries no usable name.
EVENT_PREFIX: Final[str] = "foundry"
_NAMED_FALLBACK: Final[str] = "event"

#: Characters that make a bare field value ambiguous, so such a value gets quoted, and the line
#: terminators collapsed to a space, longest first, so an event is always exactly one log line.
_QUOTING_CHARS: Final[frozenset[str]] = frozenset({" ", "\t", "=", "'", '"'})
_LINE_BREAKS: Final[tuple[tuple[str, str], ...]] = (("\r\n", " "), ("\r", " "), ("\n", " "))

#: Faults a coercion, a formatter or an attached handler can raise; ``RuntimeError`` is a handler's.
_ISOLATION_ERRORS: Final[tuple[type[BaseException], ...]] = (
    TypeError,
    ValueError,
    KeyError,
    AttributeError,
    OSError,
    RecursionError,
    RuntimeError,
)

_NO_CONFIG_FILE_EVENT: Final[str] = "no config file found"
"""Event name of :func:`emit_no_config_file`, which is the sentence AAP 0.4.7 pins.

:func:`event_line` renders ``foundry: <name>``, so this name and that prefix reproduce the state
row's line exactly; a snake_case label would put a second word between them and the pinned text
would be unreachable.
"""


def _as_text(value: object) -> str:
    """Coerce ``value`` to ``str``, and never raise while doing it."""
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    try:
        return str(value)
    except _ISOLATION_ERRORS:
        # Failure-isolation rule: a value that cannot be rendered is empty, never an exception.
        return ""


def _render_value(value: object) -> str:
    """Render one field value as one line, quoted when a space, tab or quote makes it ambiguous."""
    text = _as_text(value)
    for terminator, replacement in _LINE_BREAKS:
        text = text.replace(terminator, replacement)
    if not any(character in text for character in _QUOTING_CHARS):
        return text
    return "'" + text.replace("'", "\\'") + "'"


def _field_pairs(fields: object) -> tuple[tuple[str, str], ...]:
    """Normalise ``fields`` to ``str`` pairs sorted by key; insertion order never reaches a line."""
    pairs: list[tuple[str, str]] = []
    try:
        if isinstance(fields, Mapping):
            pairs = [(_as_text(key), _as_text(value)) for key, value in fields.items()]
        elif isinstance(fields, Sequence) and not isinstance(fields, str | bytes | bytearray):
            pairs = [
                (_as_text(item[0]), _as_text(item[1]))
                for item in fields
                if isinstance(item, tuple | list) and len(item) == 2
            ]
    except _ISOLATION_ERRORS:
        # Failure-isolation rule: a field structure that cannot be walked yields no fields.
        return ()
    return tuple(sorted(pairs))


def event_line(name: str, fields: Mapping[str, object] | Sequence[tuple[str, object]] = ()) -> str:
    """Render one event as ``foundry: <name> key=value`` with its fields sorted, never raising."""
    try:
        parts = [f"{EVENT_PREFIX}: {_as_text(name).strip() or _NAMED_FALLBACK}"]
        parts.extend(f"{key}={_render_value(value)}" for key, value in _field_pairs(fields))
        return " ".join(parts)
    except _ISOLATION_ERRORS:
        # Failure-isolation rule: rendering an event must never fail the caller that is logging.
        return f"{EVENT_PREFIX}: {_NAMED_FALLBACK}"


def _pairs(**fields: object) -> tuple[tuple[str, object], ...]:
    """Collect keyword arguments as field pairs, so an emitter's call site stays readable."""
    return tuple(fields.items())


def _resolved_level(level: object) -> int:
    """Resolve a declared level to its number; an unreadable value yields ``INFO``."""
    try:
        if isinstance(level, int) and not isinstance(level, bool):
            return level
        if isinstance(level, str):
            return level_for(level)
        value = getattr(level, "value", None)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if isinstance(value, str):
            return level_for(value)
    except _ISOLATION_ERRORS:
        # Failure-isolation rule: an unreadable level is reported as INFO.
        return LEVEL_INFO
    return LEVEL_INFO


def clamp_level(level: str | int) -> int:
    """Return ``level`` clamped to Foundry's band, never above :data:`MAX_EVENT_LEVEL`."""
    return max(LEVEL_DEBUG, min(_resolved_level(level), MAX_EVENT_LEVEL))


@dataclass(frozen=True, slots=True)
class LifecycleEvent:
    """One lifecycle event; ``fields`` is a tuple of pairs so the instance stays hashable."""

    name: str
    level: int
    fields: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        """Normalise the declared values so the instance is hashable and its line is stable."""
        object.__setattr__(self, "name", _as_text(self.name).strip() or _NAMED_FALLBACK)
        object.__setattr__(self, "level", _resolved_level(self.level))
        object.__setattr__(self, "fields", _field_pairs(self.fields))

    def as_line(self) -> str:
        """Return the event as one log line, with its fields sorted by key."""
        return event_line(self.name, self.fields)


def log_event(event: LifecycleEvent, *, logger: logging.Logger | None = None) -> None:
    """Emit ``event`` on the Foundry logger or ``logger``, passing the line as a ``%s`` argument."""
    try:
        if not isinstance(event, LifecycleEvent):
            return
        target = logger if isinstance(logger, logging.Logger) else get_logger()
        target.log(clamp_level(event.level), "%s", event.as_line())
    except _ISOLATION_ERRORS:
        # Failure-isolation rule: a logging fault is swallowed here and logs nothing further.
        return


def _emit(name: str, level: int, fields: Sequence[tuple[str, object]]) -> None:
    """Build one event from unrendered scalars and log it, isolating any fault raised doing so."""
    try:
        log_event(LifecycleEvent(name=name, level=level, fields=_field_pairs(fields)))
    except _ISOLATION_ERRORS:
        # Failure-isolation rule: constructing an event must never fail a run.
        return


def _scrubbed(value: object, redactor: Redactor | None = None) -> str:
    """Scrub free text through ``redactor`` or the deny-lists; a fault yields the marker."""
    try:
        return (default_redactor() if redactor is None else redactor).text(_as_text(value))
    except _ISOLATION_ERRORS:
        # Failure-isolation rule: replace unscrubbable text with the marker.
        return REDACTED


def _exception_name(exc: BaseException) -> str:
    """Return the class name of ``exc``, never its message, its arguments or its traceback."""
    try:
        return type(exc).__name__
    except _ISOLATION_ERRORS:
        # Failure-isolation rule: an exception that cannot be named is reported by its base type.
        return "BaseException"


def emit_run_start(run_id: str, profile: str, artifacts_root: str | Path) -> None:
    """Record the single ``INFO`` line of a run: its run id, profile and artifacts root."""
    _emit("run_start", LEVEL_INFO, _pairs(run_id=run_id, profile=profile, artifacts=artifacts_root))


def emit_resolution(key: str, layer: str, detail: str) -> None:
    """Record which layer decided a configuration key, at ``DEBUG``, never the value itself."""
    _emit("config_resolution", LEVEL_DEBUG, _pairs(key=key, layer=layer, detail=detail))


def emit_no_config_file() -> None:
    """Record that no configuration file was found, at ``DEBUG``, and emit no field.

    AAP 0.4.7 pins this state's whole line -- ``foundry: no config file found``, on the ``foundry``
    logger -- so the event *name* carries the sentence: :func:`event_line` renders the prefix and
    the name alone, whereas a snake_case label such as ``config_file`` would leave that text
    unreachable. The level is ``DEBUG`` because a run with no configuration file is not a fault:
    it proceeds from the field defaults with the profile ``default`` and exit code 0.
    """
    _emit(_NO_CONFIG_FILE_EVENT, LEVEL_DEBUG, ())


def emit_cassette_mode(mode: str, nodeid: str) -> None:
    """Record a test's cassette mode at ``DEBUG``, scrubbing the node id (``test_x[token=...]``)."""
    _emit("cassette_mode", LEVEL_DEBUG, _pairs(mode=mode, nodeid=_scrubbed(nodeid)))


def emit_sharding_decision(worker_id: str, count: int) -> None:
    """Record the number of tests a shard took, at ``DEBUG``."""
    _emit("sharding_decision", LEVEL_DEBUG, _pairs(worker_id=worker_id, count=count))


def emit_attempt(nodeid: str, attempt: int, outcome: str) -> None:
    """Record one attempt of a test and its outcome, at ``DEBUG``, with the node id scrubbed."""
    _emit(
        "attempt",
        LEVEL_DEBUG,
        _pairs(nodeid=_scrubbed(nodeid), attempt=attempt, outcome=outcome),
    )


def emit_artifact_written(nodeid: str, kind: str, path: str, size_bytes: int, sha256: str) -> None:
    """Record an artifact the writer produced, at ``DEBUG``, with its kind, size and digest."""
    _emit(
        "artifact_written",
        LEVEL_DEBUG,
        _pairs(
            nodeid=_scrubbed(nodeid),
            kind=kind,
            path=path,
            size_bytes=size_bytes,
            sha256=sha256,
        ),
    )


def emit_report_written(report: str, path: str) -> None:
    """Record a report file the run wrote, at ``DEBUG``, naming the format and the path."""
    _emit("report_written", LEVEL_DEBUG, _pairs(report=report, path=path))


def emit_plumbing_failure(
    component: str,
    message: str,
    *,
    exc: BaseException | None = None,
    redactor: Redactor | None = None,
) -> None:
    """Report a fault in Foundry's own plumbing at ``WARNING``; ``exc`` adds only its class name."""
    fields: list[tuple[str, object]] = [
        ("component", component),
        ("message", _scrubbed(message, redactor)),
    ]
    if exc is not None:
        fields.append(("error", _exception_name(exc)))
    _emit("plumbing_failure", LEVEL_WARNING, fields)


def emit_html_fallback(message: str, *, redactor: Redactor | None = None) -> None:
    """Report that ``report.html`` was skipped, at ``WARNING``, leaving the exit code unchanged."""
    _emit("html_fallback", LEVEL_WARNING, _pairs(message=_scrubbed(message, redactor)))
