"""Structured lifecycle events: the one module Foundry writes log records through.

No other module calls a logger directly. ``foundry.execution`` reports attempts, sharding
decisions and plumbing faults here, ``foundry.reporting`` reports the files it wrote, and
``foundry.cli`` reports what a subcommand did, so the level policy lives in one checkable place
instead of being spread across fifteen packages.

Level policy
    ``DEBUG`` carries configuration resolution (the layer and the key that decided it, never the
    value), cassette-mode selection, sharding decisions and per-attempt detail. ``INFO`` carries
    exactly one line per run -- :func:`emit_run_start`, with the run id, the profile and the
    artifacts root. ``WARNING`` is reserved for the failure-isolation cases
    (:func:`emit_plumbing_failure`) and the documented HTML fallback (:func:`emit_html_fallback`).
    Nothing is emitted above ``WARNING``: :func:`log_event` demotes an over-level event rather
    than trusting its caller, and no emitter asks for ``ERROR`` or ``CRITICAL``.

Secret safety by construction
    Every emitter takes scalars only -- a ``str``, an ``int``, a ``Path``, ``None`` -- never a
    headers mapping, never a response body, never a raw URL carrying a query string, so a
    configured secret has no route into a log line through this module. The two emitters that do
    accept free text -- :func:`emit_plumbing_failure` and :func:`emit_html_fallback` -- scrub it
    through :meth:`foundry.obs.redaction.Redactor.text`, using the redactor the caller supplies:
    the session passes the engine it built from ``secrets.redact_keys``, so a key that only that
    configuration adds is scrubbed out of the record, and a caller that supplies ``None`` gets the
    built-in deny-lists, the documented fallback for a fault logged before configuration has
    resolved. :func:`emit_plumbing_failure` records only an exception's class name: a transport
    error's ``args`` can carry a full URL with a token, and a traceback is an artifact, not a log
    line. pytest prints these records inside the captured-log block of a failing test, which is a
    known leak channel.

Failure isolation and determinism
    Foundry's own plumbing never changes a test's outcome or an exit code, so every public
    function here is total: a ``None`` where a ``str`` was declared, a coercion that throws, a
    closed logger or a handler that raises inside ``emit`` all end in a quiet return. Fields are
    sorted by key and rendered through one quoting rule, and no timestamp, object address or
    environment detail is added -- the run id already carries the run's start time -- so two runs
    of the same suite log byte-identical lines.

Import direction
    The standard library plus :mod:`foundry.obs.logging` and :mod:`foundry.obs.redaction`, the two
    modules of this package that hold no Foundry dependency of their own. Importing this module
    performs no I/O, reads no environment variable and mutates nothing.
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
    "emit_plumbing_failure",
    "emit_report_written",
    "emit_resolution",
    "emit_run_start",
    "emit_sharding_decision",
    "event_line",
    "log_event",
]

LEVEL_DEBUG: Final[int] = logging.DEBUG
"""``DEBUG``: configuration resolution, cassette mode, sharding and per-attempt detail."""

LEVEL_INFO: Final[int] = logging.INFO
"""``INFO``: the single line a run emits, carrying its run id, profile and artifacts root."""

LEVEL_WARNING: Final[int] = logging.WARNING
"""``WARNING``: a failure-isolation fault, or the documented HTML-report fallback."""

MAX_EVENT_LEVEL: Final[int] = logging.WARNING
"""Highest level this module emits at; an event above it is demoted to ``WARNING``."""

EVENT_PREFIX: Final[str] = "foundry"
"""Prefix of every event line, matching :data:`foundry.obs.logging.LOGGER_NAME`."""

_NAMED_FALLBACK: Final[str] = "event"
"""Label used when an event carries no usable name."""

_QUOTING_CHARS: Final[frozenset[str]] = frozenset({" ", "\t", "=", "'", '"'})
"""Characters that make a bare field value ambiguous, so such a value gets quoted."""

_LINE_BREAKS: Final[tuple[tuple[str, str], ...]] = (("\r\n", " "), ("\r", " "), ("\n", " "))
"""Line terminators collapsed to a space, longest first, so an event is one log line."""

_ISOLATION_ERRORS: Final[tuple[type[BaseException], ...]] = (
    TypeError,
    ValueError,
    KeyError,
    AttributeError,
    OSError,
    RecursionError,
    RuntimeError,
)
"""Faults a coercion, a formatter or an attached handler can raise while a record is emitted.

``RuntimeError`` is included because a handler's ``emit`` may raise it and pytest attaches
handlers of its own to the log tree; ``RecursionError`` is named for the reader even though it
subclasses ``RuntimeError``.
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
        # Failure-isolation rule: a value that cannot be rendered is reported as empty, because
        # the caller is only trying to log and must not fail over it.
        return ""


def _render_value(value: object) -> str:
    """Render one field value as a single line, quoted when it would otherwise be ambiguous.

    Any line break becomes a space, so an event is always exactly one log line. A value
    containing a space, a tab, ``=`` or a quote is wrapped in single quotes with inner single
    quotes escaped, so a reader can tell where the value ends.
    """
    text = _as_text(value)
    for terminator, replacement in _LINE_BREAKS:
        text = text.replace(terminator, replacement)
    if not any(character in text for character in _QUOTING_CHARS):
        return text
    return "'" + text.replace("'", "\\'") + "'"


def _field_pairs(fields: object) -> tuple[tuple[str, str], ...]:
    """Normalise ``fields`` to ``str`` pairs, sorted by key and then by value.

    A mapping or a sequence of two-item pairs is accepted; anything else, and any pair that
    cannot be read, is ignored rather than raised. Sorting happens here so that insertion order --
    and therefore a ``dict``'s iteration order -- can never reach a log line, which is what makes
    the rendered output deterministic.
    """
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
        # Failure-isolation rule: a field structure that cannot be walked yields no fields
        # rather than failing the caller that is only trying to log.
        return ()
    return tuple(sorted(pairs))


def event_line(name: str, fields: Mapping[str, object] | Sequence[tuple[str, object]] = ()) -> str:
    """Render one lifecycle event as a single deterministic log line.

    The line reads ``foundry: <name> key=value key2=value2``, with the fields sorted by key, so
    the same event always produces the same bytes however the caller built the mapping. Values
    are coerced with ``str``; a value carrying a space, a tab, ``=`` or a quote is wrapped in
    single quotes, and a line break inside a value collapses to a space, so one event is always
    exactly one log line.

    Args:
        name: The event name, such as ``"run_start"``. A blank or missing name renders as
            ``event``.
        fields: The event's fields, as a mapping or a sequence of ``(key, value)`` pairs. An
            entry that is not a pair, and a ``fields`` of an unexpected type, are ignored.

    Returns:
        The rendered line. It never contains a line break and never raises: a fault while
        rendering yields the line a nameless event would.

    Example:
        >>> event_line("run", {"profile": "default", "artifacts": "artifacts/x"})
        'foundry: run artifacts=artifacts/x profile=default'
    """
    try:
        parts = [f"{EVENT_PREFIX}: {_as_text(name).strip() or _NAMED_FALLBACK}"]
        parts.extend(f"{key}={_render_value(value)}" for key, value in _field_pairs(fields))
        return " ".join(parts)
    except _ISOLATION_ERRORS:
        # Failure-isolation rule: rendering an event must never fail the caller that is only
        # trying to log, so a fault yields a minimal line instead of an exception.
        return f"{EVENT_PREFIX}: {_NAMED_FALLBACK}"


def _pairs(**fields: object) -> tuple[tuple[str, object], ...]:
    """Collect keyword arguments as field pairs, so an emitter's call site stays readable."""
    return tuple(fields.items())


def _resolved_level(level: object) -> int:
    """Resolve a declared level to its number, without applying the ceiling.

    A numeric level passes through, a level name is mapped through
    :func:`foundry.obs.logging.level_for`, and a level-valued enum member is resolved by its
    value. Anything that cannot be understood yields ``INFO``, because a level that cannot be
    read must never fail a run.
    """
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
    """Return ``level`` restricted to the band Foundry is allowed to emit in.

    The result is never above :data:`MAX_EVENT_LEVEL` and never below :data:`LEVEL_DEBUG`, so a
    record always carries a real level name and library code can never log above ``WARNING``,
    whatever a caller passes.
    """
    return max(LEVEL_DEBUG, min(_resolved_level(level), MAX_EVENT_LEVEL))


@dataclass(frozen=True, slots=True)
class LifecycleEvent:
    """One structured lifecycle event, ready to be emitted as a single log line.

    ``fields`` is a tuple of ``(key, value)`` pairs rather than a mapping: a frozen dataclass
    holding a ``dict`` is not hashable, and a tuple makes the sorted rendering order impossible to
    subvert with insertion order.

    Attributes:
        name: The event name, such as ``"run_start"``; a blank name renders as ``event``.
        level: The declared level. It is stored as given and clamped when the event is emitted,
            so an over-level event stays inspectable as what the caller asked for.
        fields: The event's fields, sorted by key and coerced to ``str`` on construction.
    """

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
    """Emit ``event`` on the Foundry logger, or on ``logger`` when one is supplied.

    The pre-built line is passed as a ``%s`` argument rather than as the format string, so a
    ``%`` inside a node id or a message can never corrupt the record. The level is clamped with
    :func:`clamp_level`, which is what keeps library code from emitting above ``WARNING``.

    Nothing here raises: a coercion fault, a closed logger, or a handler that raises inside its
    ``emit`` ends in a quiet return, because Foundry's own logging never changes a test's outcome
    or an exit code. A value that is not a :class:`LifecycleEvent` is ignored, and a ``logger``
    that is not a :class:`logging.Logger` falls back to the Foundry logger.
    """
    try:
        if not isinstance(event, LifecycleEvent):
            return
        target = logger if isinstance(logger, logging.Logger) else get_logger()
        target.log(clamp_level(event.level), "%s", event.as_line())
    except _ISOLATION_ERRORS:
        # Failure-isolation rule: a logging fault is swallowed here rather than propagated into
        # the test or the run, and deliberately logs nothing further.
        return


def _emit(name: str, level: int, fields: Sequence[tuple[str, object]]) -> None:
    """Build one event from unrendered scalars and log it, isolating any fault raised doing so."""
    try:
        log_event(LifecycleEvent(name=name, level=level, fields=_field_pairs(fields)))
    except _ISOLATION_ERRORS:
        # Failure-isolation rule: constructing an event must never fail a run.
        return


def _scrubbed(value: object, redactor: Redactor | None = None) -> str:
    """Scrub secrets out of free text before it can reach a log record.

    Header lines, ``Bearer``/``Basic`` credentials, embedded URLs and secret-named key/value
    pairs are replaced with ``***`` by :class:`foundry.obs.redaction.Redactor`. Text that cannot
    be scrubbed at all is reported as the marker itself, since logging it raw would be the leak
    this avoids.

    Args:
        value: The free text to scrub, coerced to ``str`` first.
        redactor: The run's redaction engine, or ``None`` to build one carrying only the built-in
            deny-lists, which is the fallback for a fault logged before configuration resolved.

    Returns:
        The scrubbed text, or the redaction marker when it could not be scrubbed at all.
    """
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
    """Record which layer decided a configuration key, at ``DEBUG``.

    The resolved value is deliberately absent from this event: a per-key source answers where a
    value came from, and the value itself can be secret-adjacent. ``detail`` is the file path,
    environment-variable spelling or CLI option spelling that ``foundry.config`` already computed.
    """
    _emit("config_resolution", LEVEL_DEBUG, _pairs(key=key, layer=layer, detail=detail))


def emit_cassette_mode(mode: str, nodeid: str) -> None:
    """Record the cassette mode a test's session runs in, at ``DEBUG``."""
    _emit("cassette_mode", LEVEL_DEBUG, _pairs(mode=mode, nodeid=nodeid))


def emit_sharding_decision(worker_id: str, count: int) -> None:
    """Record the number of tests a shard took, at ``DEBUG``."""
    _emit("sharding_decision", LEVEL_DEBUG, _pairs(worker_id=worker_id, count=count))


def emit_attempt(nodeid: str, attempt: int, outcome: str) -> None:
    """Record one attempt of a test and the outcome it reached, at ``DEBUG``."""
    _emit("attempt", LEVEL_DEBUG, _pairs(nodeid=nodeid, attempt=attempt, outcome=outcome))


def emit_artifact_written(nodeid: str, kind: str, path: str, size_bytes: int, sha256: str) -> None:
    """Record an artifact the writer produced, at ``DEBUG``, with its kind, size and digest.

    ``path`` is the run-relative artifact path, which is already sanitised, so nothing here has to
    strip a directory or encode a separator.
    """
    _emit(
        "artifact_written",
        LEVEL_DEBUG,
        _pairs(nodeid=nodeid, kind=kind, path=path, size_bytes=size_bytes, sha256=sha256),
    )


def emit_report_written(report: str, path: str) -> None:
    """Record a report file the run wrote, at ``DEBUG``, naming the format and the path.

    Deliberately not ``INFO``: a run writes three report files, and the level policy allows
    exactly one ``INFO`` line per run, which :func:`emit_run_start` owns.
    """
    _emit("report_written", LEVEL_DEBUG, _pairs(report=report, path=path))


def emit_plumbing_failure(
    component: str,
    message: str,
    *,
    exc: BaseException | None = None,
    redactor: Redactor | None = None,
) -> None:
    """Report a fault in Foundry's own plumbing at ``WARNING``; the failure-isolation entry point.

    ``message`` is scrubbed through :meth:`foundry.obs.redaction.Redactor.text` before it is
    logged, using ``redactor`` -- the engine the session built from ``secrets.redact_keys``, so a
    configured key that the built-in :data:`~foundry.obs.redaction.SECRET_NAME_PATTERN` does not
    match is still replaced -- and falling back to the built-in deny-lists when no engine is
    supplied. ``exc`` contributes only its class name, never its message, its arguments or its
    traceback: a transport error's ``args`` can carry a full URL with a token, and a traceback
    belongs in an artifact, not in a log line.
    """
    fields: list[tuple[str, object]] = [
        ("component", component),
        ("message", _scrubbed(message, redactor)),
    ]
    if exc is not None:
        fields.append(("error", _exception_name(exc)))
    _emit("plumbing_failure", LEVEL_WARNING, fields)


def emit_html_fallback(message: str, *, redactor: Redactor | None = None) -> None:
    """Report that ``report.html`` was skipped, at ``WARNING``, leaving the exit code unchanged.

    ``message`` is scrubbed through ``redactor``, the engine the session built from
    ``secrets.redact_keys``, and through the built-in deny-lists when no engine is supplied.
    """
    _emit("html_fallback", LEVEL_WARNING, _pairs(message=_scrubbed(message, redactor)))
