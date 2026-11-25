"""The typed stash keys, and the wire format that carries per-test state across a worker boundary.

Every piece of per-test state Foundry keeps lives in a ``pytest.Stash`` entry keyed by a
:class:`pytest.StashKey` declared here, and everything that must survive a ``pytest-xdist`` worker
travels as one :class:`TestPayload` stamped onto ``TestReport.user_properties``. This is the single
declaration site for both, because two declarations of one key would be two different keys
(AAP 0.4.8, requirement I3). A value in ``user_properties`` must be JSON-serialisable, so nested
structures travel as the ``json.dumps(..., sort_keys=True)`` text of :func:`dumps`;
:func:`write_properties` replaces Foundry's entries and :func:`read_properties` is the tolerant
inverse, because a fault in Foundry's plumbing may never change a test's outcome.
"""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, fields
from datetime import datetime
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Final, Literal, Protocol, Self, TypeAlias, TypeVar

import pytest

from foundry.core.exceptions import FieldValueError
from foundry.core.ids import is_valid_run_id
from foundry.core.types import (
    ARTIFACT_KINDS,
    PHASES,
    QUARANTINE_STATES,
    ArtifactKind,
    AttemptRecord,
    Phase,
    QuarantineState,
)
from foundry.obs.capture import (
    DEFAULT_CAPTURE_LINES,
    MAX_CAPTURE_LINES,
    MAX_TAIL_LINE_LENGTH,
    MIN_CAPTURE_LINES,
    TRUNCATION_MARKER,
    LogRing,
)

if TYPE_CHECKING:
    from foundry.config.models import FoundryConfig
    from foundry.execution.quarantine import QuarantineDecision
    from foundry.execution.session import SessionState
    from foundry.execution.sharding import NodeLedger, RunErrorLedger
    from foundry.obs.artifacts import ArtifactWriter

PROPERTY_PREFIX: Final[str] = "foundry."
"""Prefix of every ``user_properties`` name Foundry writes; each ``PROPERTY_*`` constant adds one.

A decoder strips the prefix instead of keeping a second table in step, and the names are additive
contract (AAP 0.4.9).
"""

PROPERTY_ARTIFACTS: Final[str] = PROPERTY_PREFIX + "artifacts"
PROPERTY_ARTIFACTS_DISABLED: Final[str] = PROPERTY_PREFIX + "artifacts_disabled"
PROPERTY_ATTEMPTS: Final[str] = PROPERTY_PREFIX + "attempts"
PROPERTY_DURATION_MS: Final[str] = PROPERTY_PREFIX + "duration_ms"
PROPERTY_FAILURES: Final[str] = PROPERTY_PREFIX + "failures"
PROPERTY_FINISHED_AT: Final[str] = PROPERTY_PREFIX + "finished_at"
PROPERTY_LOG_TAIL: Final[str] = PROPERTY_PREFIX + "log_tail"
PROPERTY_MARKERS: Final[str] = PROPERTY_PREFIX + "markers"
PROPERTY_NODEID: Final[str] = PROPERTY_PREFIX + "nodeid"
PROPERTY_OUTCOME: Final[str] = PROPERTY_PREFIX + "outcome"
PROPERTY_PROFILE: Final[str] = PROPERTY_PREFIX + "profile"
PROPERTY_QUARANTINE: Final[str] = PROPERTY_PREFIX + "quarantine"
PROPERTY_RUN_ID: Final[str] = PROPERTY_PREFIX + "run_id"
PROPERTY_STARTED_AT: Final[str] = PROPERTY_PREFIX + "started_at"
"""The last of the fourteen stamped names, one per payload field, in AAP 0.4.6's order."""

_LiteralValue = TypeVar("_LiteralValue", bound=str)

OutcomeValue: TypeAlias = Literal[
    "passed",
    "failed",
    "skipped",
    "xfailed",
    "xpassed",
    "error",
    "quarantined_failed",
    "quarantine_passed",
    "not_run",
]
"""The nine outcome values a record carries (AAP 0.4.6, AAP 0.4.9), in the enum's own order."""

OUTCOME_VALUES: Final[tuple[OutcomeValue, ...]] = (
    "passed",
    "failed",
    "skipped",
    "xfailed",
    "xpassed",
    "error",
    "quarantined_failed",
    "quarantine_passed",
    "not_run",
)


class _WireRecord(Protocol):
    """A payload a decoded mapping turns back into, so one reader can decode a list of either."""

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> Self | None:
        """Decode ``payload``, or return ``None`` when it is malformed."""
        ...


_RecordT = TypeVar("_RecordT", bound=_WireRecord)


def _malformed(key: str, expectation: str) -> FieldValueError:
    return FieldValueError(f"{key} must be {expectation}")


def _raw(payload: Mapping[str, object], key: str, expectation: str) -> object:
    if key not in payload:
        raise _malformed(key, expectation)
    return payload[key]


def _text(payload: Mapping[str, object], key: str) -> str:
    value = _raw(payload, key, "text")
    if not isinstance(value, str):
        raise _malformed(key, "text")
    return value


def _optional_text(payload: Mapping[str, object], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise _malformed(key, "text or null")
    return value


def _inside_run_dir(key: str, value: str) -> str:
    """Return ``value`` as a forward-slash path inside the run directory, or raise on escape."""
    candidate = value.replace("\\", "/")
    path = PurePosixPath(candidate)
    if not candidate.strip() or path.is_absolute() or any(part == ".." for part in path.parts):
        raise _malformed(key, "a path relative to the run directory")
    return candidate


def _integer(payload: Mapping[str, object], key: str, *, minimum: int | None = None) -> int:
    value = _raw(payload, key, "an integer")
    number = value if isinstance(value, int) and not isinstance(value, bool) else None
    if number is None and isinstance(value, str):
        try:
            number = int(value.strip())
        except ValueError:
            number = None
    if number is None or (minimum is not None and number < minimum):
        raise _malformed(key, "an integer" if minimum is None else f"an integer >= {minimum}")
    return number


def _number(payload: Mapping[str, object], key: str, *, minimum: float | None = None) -> float:
    value = _raw(payload, key, "a number")
    number: float | None = None
    if isinstance(value, (int, float)):
        number = None if isinstance(value, bool) else float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            number = None
    if number is None or (minimum is not None and number < minimum):
        raise _malformed(key, "a number" if minimum is None else f"a number >= {minimum:g}")
    return number


def _boolean(payload: Mapping[str, object], key: str) -> bool:
    value = _raw(payload, key, "a boolean")
    if not isinstance(value, bool):
        raise _malformed(key, "a boolean")
    return value


def _one_of(
    payload: Mapping[str, object], key: str, allowed: tuple[_LiteralValue, ...]
) -> _LiteralValue:
    value = _raw(payload, key, " / ".join(allowed))
    for candidate in allowed:
        if candidate == value:
            return candidate
    raise _malformed(key, " / ".join(allowed))


def _sequence(payload: Mapping[str, object], key: str) -> Sequence[object]:
    value = _raw(payload, key, "a list")
    if isinstance(value, (list, tuple)):
        return value
    raise _malformed(key, "a list")


def _text_sequence(payload: Mapping[str, object], key: str) -> tuple[str, ...]:
    items: list[str] = []
    for item in _sequence(payload, key):
        if not isinstance(item, str):
            raise _malformed(key, "a list of strings")
        items.append(item)
    return tuple(items)


def _records(
    payload: Mapping[str, object], key: str, record_type: type[_RecordT]
) -> tuple[_RecordT, ...]:
    decoded: list[_RecordT] = []
    for item in _sequence(payload, key):
        if not isinstance(item, dict):
            raise _malformed(key, "a list of objects")
        record = record_type.from_dict(item)
        if record is None:
            raise _malformed(key, "a list of valid payloads")
        decoded.append(record)
    return tuple(decoded)


def _optional_path(payload: Mapping[str, object], key: str) -> str | None:
    """Return an optional path that stays inside the run directory, or ``None``."""
    value = _optional_text(payload, key)
    return None if value is None else _inside_run_dir(key, value)


def _nested(payload: Mapping[str, object], key: str, record_type: type[_RecordT]) -> _RecordT:
    value = _raw(payload, key, "an object")
    if not isinstance(value, dict):
        raise _malformed(key, "an object")
    record = record_type.from_dict(value)
    if record is None:
        raise _malformed(key, "a valid payload")
    return record


def _run_id(payload: Mapping[str, object], key: str) -> str:
    value = _text(payload, key)
    if not is_valid_run_id(value):
        raise _malformed(key, "a run id of the form YYYYmmddTHHMMSS-<6 hex>")
    return value


@dataclass(frozen=True, slots=True)
class AttemptPayload:
    """One attempt at a test: ``foundry.core.types.AttemptRecord``'s fields, on the wire."""

    number: int
    outcome: str
    elapsed_ms: float
    retry_on: str | None = None
    message: str | None = None

    @classmethod
    def from_record(cls, record: AttemptRecord) -> Self:
        """Return the wire form of ``record``."""
        return cls(
            record.number, record.outcome, record.elapsed_ms, record.retry_on, record.message
        )

    def to_record(self) -> AttemptRecord:
        """Return the attempt as the frozen record ``RetryExhaustedError`` carries."""
        return AttemptRecord(
            self.number, self.outcome, self.elapsed_ms, self.retry_on, self.message
        )

    def as_dict(self) -> dict[str, object]:
        """Return the attempt as the mapping ``AttemptRecord`` serialises."""
        return self.to_record().as_dict()

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> Self | None:
        """Decode one attempt, or return ``None`` when a field is missing or mistyped."""
        try:
            return cls(
                number=_integer(payload, "number", minimum=1),
                outcome=_text(payload, "outcome"),
                elapsed_ms=_number(payload, "elapsed_ms"),
                retry_on=_optional_text(payload, "retry_on"),
                message=_optional_text(payload, "message"),
            )
        except FieldValueError:
            return None


@dataclass(frozen=True, slots=True)
class FailurePayload:
    """One failing phase of one test, with its run-relative traceback path (AAP 0.4.6)."""

    phase: Phase
    message: str
    traceback_path: str | None = None

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> Self | None:
        """Decode one failure, or return ``None`` when a field is missing or mistyped."""
        try:
            return cls(
                phase=_one_of(payload, "phase", PHASES),
                message=_text(payload, "message"),
                traceback_path=_optional_path(payload, "traceback_path"),
            )
        except FieldValueError:
            return None

    def as_dict(self) -> dict[str, object]:
        """Return the failure as the mapping ``results.json`` carries."""
        return {
            "phase": self.phase,
            "message": self.message,
            "traceback_path": self.traceback_path,
        }


@dataclass(frozen=True, slots=True)
class ArtifactPayload:
    """One captured file: its kind, its run-relative path and its size in bytes (AAP 0.4.6)."""

    kind: ArtifactKind
    path: str
    bytes: int

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> Self | None:
        """Decode one artifact reference, or return ``None`` when a field is malformed."""
        try:
            return cls(
                kind=_one_of(payload, "kind", ARTIFACT_KINDS),
                path=_inside_run_dir("path", _text(payload, "path")),
                bytes=_integer(payload, "bytes", minimum=0),
            )
        except FieldValueError:
            return None

    def as_dict(self) -> dict[str, object]:
        """Return the reference as the mapping ``results.json`` carries."""
        return {"kind": self.kind, "path": self.path, "bytes": self.bytes}


@dataclass(frozen=True, slots=True)
class QuarantinePayload:
    """The quarantine verdict a record carries (AAP 0.7.2): ``none``, ``failed`` or ``passed``."""

    state: QuarantineState = "none"

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> Self | None:
        """Decode a verdict, or return ``None`` when the state is not one of the three."""
        try:
            return cls(state=_one_of(payload, "state", QUARANTINE_STATES))
        except FieldValueError:
            return None

    def as_dict(self) -> dict[str, object]:
        """Return the verdict as the ``{"state": ...}`` object ``results.json`` carries."""
        return {"state": self.state}


@dataclass(frozen=True, slots=True)
class RunErrorPayload:
    """A failure of the run itself, such as a lost xdist worker (AAP 0.4.6), and no test's."""

    run_id: str
    recorded_at: str
    worker_id: str
    message: str
    record_type: Literal["run_error"] = "run_error"

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> Self | None:
        """Decode one run error, or return ``None`` when a field is missing or mistyped."""
        if "record_type" in payload and payload["record_type"] != "run_error":
            return None
        try:
            return cls(
                run_id=_run_id(payload, "run_id"),
                recorded_at=_text(payload, "recorded_at"),
                worker_id=_text(payload, "worker_id"),
                message=_text(payload, "message"),
            )
        except FieldValueError:
            return None

    def as_dict(self) -> dict[str, object]:
        """Return the error as the JSON-ready object ``results.json`` holds."""
        return {
            "record_type": self.record_type,
            "run_id": self.run_id,
            "recorded_at": self.recorded_at,
            "worker_id": self.worker_id,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class TestPayload:
    """One collected test's record, in the shape that crosses the worker boundary.

    The fields are AAP 0.4.6's thirteen result-record fields plus ``artifacts_disabled``, the
    additive field AAP 0.4.7 requires on every record of a run whose artifact directory could not
    be written (AAP 0.4.9 permits it). ``log_tail`` is the one transport-only field: it rides the
    wire so the HTML panel can render it, and the persisted record does not carry it.
    """

    run_id: str
    started_at: str
    finished_at: str
    profile: str
    nodeid: str
    outcome: OutcomeValue
    attempts: int
    duration_ms: float
    markers: tuple[str, ...] = ()
    failures: tuple[FailurePayload, ...] = ()
    artifacts: tuple[ArtifactPayload, ...] = ()
    quarantine: QuarantinePayload = QuarantinePayload()
    log_tail: tuple[str, ...] = ()
    artifacts_disabled: bool = False
    record_type: Literal["result"] = "result"

    def __post_init__(self) -> None:
        """Canonicalise the three collections, so two runs and two workers serialise one shape."""
        failures = sorted(self.failures, key=lambda item: (item.phase, item.message))
        artifacts = sorted(self.artifacts, key=lambda item: (item.kind, item.path))
        object.__setattr__(self, "markers", tuple(sorted(dict.fromkeys(self.markers))))
        object.__setattr__(self, "failures", tuple(failures))
        object.__setattr__(self, "artifacts", tuple(artifacts))

    def as_result_fields(self) -> dict[str, object]:
        """Return the fields ``ResultRecord`` is built from, the one transport-only field excluded.

        Everything a persisted record carries is here, ``artifacts_disabled`` included, so the
        AAP 0.4.7 flag reaches ``results.json`` instead of stopping at the wire.
        """
        return {
            "record_type": self.record_type,
            "run_id": self.run_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "profile": self.profile,
            "nodeid": self.nodeid,
            "outcome": self.outcome,
            "attempts": self.attempts,
            "duration_ms": self.duration_ms,
            "markers": self.markers,
            "failures": [failure.as_dict() for failure in self.failures],
            "artifacts": [artifact.as_dict() for artifact in self.artifacts],
            "quarantine": self.quarantine.as_dict(),
            "artifacts_disabled": self.artifacts_disabled,
        }

    def as_dict(self) -> dict[str, object]:
        """Return the payload as the mapping :meth:`from_dict` decodes, ``log_tail`` added."""
        wire = self.as_result_fields()
        wire["log_tail"] = list(self.log_tail)
        return wire

    def as_properties(self) -> list[tuple[str, object]]:
        """Return the payload as the sorted ``(name, value)`` pairs ``user_properties`` carries."""
        properties: dict[str, object] = {}
        for name, value in self.as_dict().items():
            key = PROPERTY_PREFIX + name
            if key in FOUNDRY_PROPERTY_NAMES:
                properties[key] = dumps(value) if name in _JSON_FIELDS else value
        return [(key, properties[key]) for key in sorted(properties)]

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> Self | None:
        """Decode one test record, or return ``None`` when a field is missing or mistyped."""
        if "record_type" in payload and payload["record_type"] != "result":
            return None
        try:
            return cls(
                run_id=_run_id(payload, "run_id"),
                started_at=_text(payload, "started_at"),
                finished_at=_text(payload, "finished_at"),
                profile=_text(payload, "profile"),
                nodeid=_text(payload, "nodeid"),
                outcome=_one_of(payload, "outcome", OUTCOME_VALUES),
                attempts=_integer(payload, "attempts", minimum=1),
                duration_ms=_number(payload, "duration_ms"),
                markers=_text_sequence(payload, "markers"),
                failures=_records(payload, "failures", FailurePayload),
                artifacts=_records(payload, "artifacts", ArtifactPayload),
                quarantine=_nested(payload, "quarantine", QuarantinePayload),
                log_tail=_text_sequence(payload, "log_tail"),
                artifacts_disabled=_boolean(payload, "artifacts_disabled"),
            )
        except FieldValueError:
            return None


FOUNDRY_PROPERTY_NAMES: Final[tuple[str, ...]] = tuple(
    sorted(
        PROPERTY_PREFIX + field.name for field in fields(TestPayload) if field.name != "record_type"
    )
)

_JSON_FIELDS: Final[frozenset[str]] = frozenset(
    {"artifacts", "failures", "log_tail", "markers", "quarantine"}
)
"""The payload fields whose wire value is JSON text rather than a scalar."""


def dumps(value: object) -> str:
    """Return ``value`` as the canonical JSON text the wire format carries."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def loads(text: str) -> object | None:
    """Decode JSON text, returning ``None`` when it is not JSON (``null`` reads as ``None`` too)."""
    try:
        decoded: object = json.loads(text)
    except (TypeError, ValueError):
        return None
    return decoded


def is_foundry_property(name: str) -> bool:
    """Report whether ``name`` belongs to Foundry's ``user_properties`` namespace."""
    return name.startswith(PROPERTY_PREFIX)


def _is_foundry_entry(entry: object) -> bool:
    """Report whether ``entry`` is a Foundry property pair.

    A pair is a tuple where it was stamped and a two-element list once an xdist report is decoded.
    """
    if isinstance(entry, (tuple, list)) and len(entry) == 2 and isinstance(entry[0], str):
        return is_foundry_property(entry[0])
    return False


def write_properties(report: pytest.TestReport, payload: TestPayload) -> None:
    """Stamp ``payload`` onto a report's ``user_properties``, replacing Foundry's entries.

    The list is mutated in place, because that object is what pytest serialises for the controller;
    a fault is swallowed, since capture may never change a test's outcome (AAP 0.1.2).
    """
    try:
        properties = payload.as_properties()
        report.user_properties[:] = [
            entry for entry in report.user_properties if not _is_foundry_entry(entry)
        ]
        report.user_properties.extend(properties)
    except (AttributeError, TypeError, ValueError):
        return


def read_properties(report: pytest.TestReport) -> TestPayload | None:
    """Decode the payload a report carries, or return ``None`` when it carries none.

    The tolerant inverse of :func:`write_properties`: a missing, partial or malformed set is
    ``None``.
    """
    try:
        entries = report.user_properties
    except AttributeError:
        return None
    if not isinstance(entries, list):
        return None
    payload: dict[str, object] = {}
    for entry in entries:
        if not _is_foundry_entry(entry):
            continue
        name = entry[0]
        field = name[len(PROPERTY_PREFIX) :]
        value = entry[1]
        if field not in _JSON_FIELDS:
            payload[field] = value
        elif isinstance(value, str) and (parsed := loads(value)) is not None:
            payload[field] = parsed
        else:
            return None
    return TestPayload.from_dict(payload)


# One key per piece of state, named for its owning module, so no two modules can claim one slot.

SESSION_STATE: Final[pytest.StashKey[SessionState]] = pytest.StashKey["SessionState"]()
SESSION_CONFIG: Final[pytest.StashKey[FoundryConfig]] = pytest.StashKey["FoundryConfig"]()
SESSION_NODES: Final[pytest.StashKey[NodeLedger]] = pytest.StashKey["NodeLedger"]()
SESSION_RUN_ERRORS: Final[pytest.StashKey[RunErrorLedger]] = pytest.StashKey["RunErrorLedger"]()
SESSION_STASH_KEYS: Final = (SESSION_STATE, SESSION_CONFIG, SESSION_NODES, SESSION_RUN_ERRORS)

# The per-item ledger holds the accumulated attempt records (AAP 0.7.1), declared against state's
# own record type so the key does not depend on the driver module that fills it.
ITEM_ATTEMPTS: Final[pytest.StashKey[list[AttemptRecord]]] = pytest.StashKey[list[AttemptRecord]]()
ITEM_QUARANTINE: Final[pytest.StashKey[QuarantineDecision]] = pytest.StashKey[
    "QuarantineDecision"
]()
ITEM_LOG_RING: Final[pytest.StashKey[LogRing]] = pytest.StashKey[LogRing]()
ITEM_ARTIFACTS: Final[pytest.StashKey[ArtifactWriter]] = pytest.StashKey["ArtifactWriter"]()
# Two clocks per item: when the item began, and when the attempt in flight began. The second is
# what makes every attempt record's span its own, so summing the ledger yields the item's wall clock
# instead of a total that counts each earlier attempt again (AAP 0.4.6's ``duration_ms``).
ITEM_STARTED_MONOTONIC: Final[pytest.StashKey[float]] = pytest.StashKey[float]()
ITEM_STARTED_AT: Final[pytest.StashKey[datetime]] = pytest.StashKey[datetime]()
ITEM_ATTEMPT_STARTED_MONOTONIC: Final[pytest.StashKey[float]] = pytest.StashKey[float]()
ITEM_STASH_KEYS: Final = (
    ITEM_ATTEMPTS,
    ITEM_QUARANTINE,
    ITEM_LOG_RING,
    ITEM_ARTIFACTS,
    ITEM_STARTED_MONOTONIC,
    ITEM_STARTED_AT,
    ITEM_ATTEMPT_STARTED_MONOTONIC,
)


def bounded_tail(lines: Iterable[str], limit: int) -> tuple[str, ...]:
    """Return the newest ``limit`` lines, each cut at ``foundry.obs.capture``'s line cap.

    ``limit`` is the resolved ``logging.capture_lines``, clamped into its published bounds, so the
    tail riding ``user_properties`` stays bounded in both directions (requirement I7).
    """
    usable = not isinstance(limit, bool) and isinstance(limit, int)
    requested = limit if usable else DEFAULT_CAPTURE_LINES
    capacity = max(MIN_CAPTURE_LINES, min(MAX_CAPTURE_LINES, requested))
    if capacity <= 0:
        return ()
    keep = MAX_TAIL_LINE_LENGTH - len(TRUNCATION_MARKER)
    tail: deque[str] = deque(maxlen=capacity)
    for line in lines:
        tail.append(line if len(line) <= MAX_TAIL_LINE_LENGTH else line[:keep] + TRUNCATION_MARKER)
    return tuple(tail)


__all__: Final[tuple[str, ...]] = (
    "ArtifactPayload",
    "AttemptPayload",
    "FOUNDRY_PROPERTY_NAMES",
    "FailurePayload",
    "ITEM_ARTIFACTS",
    "ITEM_ATTEMPTS",
    "ITEM_ATTEMPT_STARTED_MONOTONIC",
    "ITEM_LOG_RING",
    "ITEM_QUARANTINE",
    "ITEM_STARTED_AT",
    "ITEM_STARTED_MONOTONIC",
    "ITEM_STASH_KEYS",
    "OUTCOME_VALUES",
    "OutcomeValue",
    "PHASES",
    "PROPERTY_ARTIFACTS",
    "PROPERTY_ARTIFACTS_DISABLED",
    "PROPERTY_ATTEMPTS",
    "PROPERTY_DURATION_MS",
    "PROPERTY_FAILURES",
    "PROPERTY_FINISHED_AT",
    "PROPERTY_LOG_TAIL",
    "PROPERTY_MARKERS",
    "PROPERTY_NODEID",
    "PROPERTY_OUTCOME",
    "PROPERTY_PREFIX",
    "PROPERTY_PROFILE",
    "PROPERTY_QUARANTINE",
    "PROPERTY_RUN_ID",
    "PROPERTY_STARTED_AT",
    "QuarantinePayload",
    "RunErrorPayload",
    "SESSION_CONFIG",
    "SESSION_NODES",
    "SESSION_RUN_ERRORS",
    "SESSION_STASH_KEYS",
    "SESSION_STATE",
    "TestPayload",
    "bounded_tail",
    "dumps",
    "is_foundry_property",
    "loads",
    "read_properties",
    "write_properties",
)
