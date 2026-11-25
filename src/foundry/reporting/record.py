"""The immutable result records and the outcome vocabulary of Foundry's reports.

:class:`ResultRecord`, :class:`RunError` and the nine values of :class:`Outcome` are what every
persisted format consumes, so their field names and the ``record_type`` discriminator are the
compatibility promise of AAP 0.4.9; ``to_dict`` builds JSON-ready mappings in the order AAP 0.4.6
tabulates and :func:`parse_element` raises ``DataError`` for a document it cannot read (AAP 0.4.4).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Annotated, Final, Literal, Self, TypeAlias, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from foundry.core.exceptions import DataError, FieldValueError, FoundryError
from foundry.core.ids import is_valid_run_id, parse_iso8601, to_iso8601
from foundry.core.types import ArtifactKind, Phase, QuarantineState

__all__: Final[tuple[str, ...]] = (
    "FAILING_OUTCOMES",
    "OUTCOMES",
    "PROFILE_NAMES",
    "QUARANTINE_OUTCOMES",
    "RESULT_ELEMENT_ADAPTER",
    "ArtifactRecord",
    "FailureRecord",
    "Outcome",
    "QuarantineRecord",
    "ResultElement",
    "ResultRecord",
    "RunError",
    "element_to_dict",
    "parse_element",
)

# --- Outcomes --------------------------------------------------------------


class Outcome(StrEnum):
    """A collected test's final outcome: the nine values AAP 0.4.6 and AAP 0.4.9 fix.

    The member values, not the member names, are what ``results.json``, the JUnit properties and
    the report's badges carry, and :meth:`__str__` returns the bare value so an f-string renders
    ``passed`` rather than ``Outcome.PASSED``.

    Attributes:
        PASSED: The test passed.
        FAILED: The test failed, which fails the run.
        SKIPPED: pytest skipped the test, through a marker or a ``skip`` call.
        XFAILED: A failure pytest reported as expected (``xfail``).
        XPASSED: An unexpected pass of a test marked ``xfail``.
        ERROR: A setup or teardown error, which pytest reports apart from a failure.
        QUARANTINED_FAILED: A marked test failed; ``report`` mode keeps the run succeeding and
            ``strict`` mode fails it.
        QUARANTINE_PASSED: A marked test passed, so the marker can be removed.
        NOT_RUN: The item never executed because the session stopped or a worker was lost.
    """

    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    XFAILED = "xfailed"
    XPASSED = "xpassed"
    ERROR = "error"
    QUARANTINED_FAILED = "quarantined_failed"
    QUARANTINE_PASSED = "quarantine_passed"
    NOT_RUN = "not_run"

    def __str__(self) -> str:
        """Return the bare value, so ``str(Outcome.PASSED)`` is ``"passed"``."""
        return self.value


OUTCOMES: Final[tuple[Outcome, ...]] = tuple(Outcome)
"""All nine :class:`Outcome` values in declaration order, which is the summary strip's order.

Derived from the enum rather than restated, so a member and the strip that renders it can never
drift apart.
"""

FAILING_OUTCOMES: Final[frozenset[Outcome]] = frozenset({Outcome.FAILED, Outcome.ERROR})
"""The outcomes that count against a run: the single definition the comparator, the JUnit
writer and the exit code share.

The quarantine outcomes are deliberately excluded, so a known-flaky failure does not fail the run
in ``report`` mode (AAP 0.4.7) or read as a regression in ``foundry report compare``.
"""

QUARANTINE_OUTCOMES: Final[frozenset[Outcome]] = frozenset(
    {Outcome.QUARANTINED_FAILED, Outcome.QUARANTINE_PASSED}
)
"""The outcomes the report's Quarantined section lists, failed or passed."""

PROFILE_NAMES: Final[tuple[str, ...]] = ("default", "local", "ci", "staging")
"""The four profile names a result record can carry, in cascade order.

These mirror ``foundry.config.enums.ProfileName`` and are restated because the layering contract
(AAP 0.4.8) forbids ``foundry.reporting`` from importing ``foundry.config``. A test cross-checks
the two lists, so a profile added to the enum but not here fails the build.
"""

# --- Record base and shared validation -------------------------------------


class _FrozenRecord(BaseModel):
    """Base of every record here: immutable, hashable and closed to unknown fields.

    ``extra="forbid"`` makes a document from a newer release fail loudly instead of losing fields.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


def _require_run_id(value: str) -> str:
    """Return ``value`` when it has the shape of a run id, else reject it.

    Raises:
        FieldValueError: The value is not ``YYYYmmddTHHMMSS-<6 hex>``. A validator raises this
            ``ValueError`` subclass rather than a bare builtin, so pydantic still wraps it into one
            ``ValidationError`` (AAP 0.14.3).
    """
    if not is_valid_run_id(value):
        raise FieldValueError(f"run_id must be YYYYmmddTHHMMSS-<6 hex>, got {value!r}")
    return value


def _normalise_relative_path(value: str, *, field: str) -> str:
    """Check that a recorded path stays inside the run directory, and normalise it.

    A record's paths are what the report links to, so an absolute path or a ``..`` segment would
    turn a link into a read outside the run (AAP 0.7.2); backslashes become forward slashes.

    Returns:
        The path with every backslash replaced by a forward slash, otherwise unchanged.

    Raises:
        FieldValueError: The path is empty, absolute, or contains a ``..`` segment.
    """
    candidate = value.replace("\\", "/")
    if not candidate.strip():
        raise FieldValueError(f"{field} must not be empty")
    if PurePosixPath(candidate).is_absolute():
        raise FieldValueError(f"{field} must be relative to the run directory, got {value!r}")
    if any(part == ".." for part in PurePosixPath(candidate).parts):
        raise FieldValueError(f"{field} must not contain '..', got {value!r}")
    return candidate


def _normalise_timestamp(value: object) -> datetime:
    """Return a timestamp field's value as a timezone-aware UTC datetime.

    A record is compared and rendered, so its timestamps must be absolute: a naive value is read as
    UTC and any other offset is converted, the rule ``to_iso8601`` applies when it renders one.

    Returns:
        The instant, timezone-aware and in UTC.

    Raises:
        FieldValueError: The value is neither a datetime nor parseable ISO 8601 text.
    """
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str):
        try:
            moment = parse_iso8601(value)
        except FoundryError as exc:
            raise FieldValueError(str(exc)) from exc
    else:
        raise FieldValueError("expected a datetime or an ISO 8601 timestamp string")
    if moment.utcoffset() is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


# --- Supporting records ----------------------------------------------------


class FailureRecord(_FrozenRecord):
    """One failing phase of one test, as ``results.json`` reports it (AAP 0.4.6).

    A test can fail in more than one phase -- a broken fixture in ``setup`` and a leaking resource
    in ``teardown``, for instance -- so a record holds one entry per failing phase, and the report
    renders one panel per entry.

    Attributes:
        phase: The pytest phase the failure belongs to.
        message: The failure text, already redacted (AAP 0.3.2), so it is safe to persist.
        traceback_path: The traceback artifact's path relative to the run directory, or ``None``
            when no traceback file was written.
    """

    phase: Phase
    message: str
    traceback_path: str | None = None

    @field_validator("traceback_path")
    @classmethod
    def _check_traceback_path(cls, value: str | None) -> str | None:
        """Reject a traceback path that escapes the run directory."""
        if value is None:
            return None
        return _normalise_relative_path(value, field="traceback_path")

    def to_dict(self) -> dict[str, object]:
        """Return the failure as a JSON-ready mapping of its three fields."""
        return {
            "phase": self.phase,
            "message": self.message,
            "traceback_path": self.traceback_path,
        }


class ArtifactRecord(_FrozenRecord):
    """One captured file belonging to a test, as ``results.json`` lists it.

    The path is what a report link points at and the size is what the manifest cross-checks, so both
    are recorded rather than read from the filesystem at render time (AAP 0.4.6).

    Attributes:
        kind: Which artifact this is -- request, response, stdout, stderr, traceback, log_tail
            or manifest.
        path: The file's path relative to the run directory, with forward slashes.
        bytes: The file's size in bytes, never negative.
    """

    kind: ArtifactKind
    path: str
    bytes: int = Field(ge=0)

    @field_validator("path")
    @classmethod
    def _check_path(cls, value: str) -> str:
        """Reject an artifact path that escapes the run directory."""
        return _normalise_relative_path(value, field="artifact path")

    def to_dict(self) -> dict[str, object]:
        """Return the artifact reference as a JSON-ready mapping."""
        return {"kind": self.kind, "path": self.path, "bytes": self.bytes}


class QuarantineRecord(_FrozenRecord):
    """The quarantine verdict a record carries (AAP 0.7.2).

    An object rather than a bare string field, so a later release can add quarantine detail without
    changing the meaning of the field an existing consumer reads.

    Attributes:
        state: ``none`` for an unmarked test, ``failed`` for a marked test that failed and
            ``passed`` for a marked test that passed.
    """

    state: QuarantineState = "none"

    def to_dict(self) -> dict[str, object]:
        """Return the verdict as the ``{"state": ...}`` object ``results.json`` carries."""
        return {"state": self.state}


# --- The per-test result record --------------------------------------------


class ResultRecord(_FrozenRecord):
    """One collected test's result: the element ``results.json`` holds per test.

    Field names, order and meaning are the frozen schema of AAP 0.4.6 and the compatibility
    promise of AAP 0.4.9. Every renderer reads this record rather than the run's live state, which
    is what lets JSON, JUnit, HTML and the comparator agree.

    Attributes:
        record_type: The discriminator marking this element a result, not a :class:`RunError`.
        run_id: The run's identifier, ``YYYYmmddTHHMMSS-<6 hex>``.
        started_at: When the first attempt began, timezone-aware UTC.
        finished_at: When the last attempt ended, UTC, never before ``started_at``.
        profile: The active profile, one of :data:`PROFILE_NAMES`.
        nodeid: The pytest node id, unsanitised -- sanitising is the writer's job.
        outcome: The test's final outcome.
        attempts: Total attempts including the first, so ``--foundry-retries 2`` yields ``3``
            (assumption A2).
        duration_ms: Wall-clock milliseconds across all attempts.
        markers: The item's marker names, sorted and de-duplicated.
        failures: One entry per failing phase.
        artifacts: One entry per artifact file actually written.
        quarantine: The quarantine verdict; its state must agree with ``outcome``.
    """

    record_type: Literal["result"] = "result"
    run_id: str
    started_at: datetime
    finished_at: datetime
    profile: str
    nodeid: str
    outcome: Outcome
    attempts: int = Field(ge=1)
    duration_ms: float = Field(ge=0)
    markers: tuple[str, ...] = ()
    failures: tuple[FailureRecord, ...] = ()
    artifacts: tuple[ArtifactRecord, ...] = ()
    quarantine: QuarantineRecord = QuarantineRecord()

    @field_validator("run_id")
    @classmethod
    def _check_run_id(cls, value: str) -> str:
        """Reject a run id of the wrong shape."""
        return _require_run_id(value)

    @field_validator("started_at", "finished_at", mode="before")
    @classmethod
    def _as_aware_utc(cls, value: object) -> datetime:
        """Read a timestamp as a timezone-aware UTC datetime."""
        return _normalise_timestamp(value)

    @field_validator("profile")
    @classmethod
    def _check_profile(cls, value: str) -> str:
        """Reject a profile name no configuration can produce."""
        if value not in PROFILE_NAMES:
            raise FieldValueError(
                f"unknown profile {value!r}; expected one of: {', '.join(PROFILE_NAMES)}"
            )
        return value

    @field_validator("nodeid")
    @classmethod
    def _check_nodeid(cls, value: str) -> str:
        """Reject a blank node id, which no renderer could label."""
        if not value.strip():
            raise FieldValueError("nodeid must not be empty")
        return value

    @field_validator("markers", mode="after")
    @classmethod
    def _canonical_markers(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Sort and de-duplicate the markers rather than reject a repeat (AAP 0.13.1).

        ``dict.fromkeys`` de-duplicates, and a set is never iterated on the way to a file.
        """
        return tuple(sorted(dict.fromkeys(value)))

    @model_validator(mode="after")
    def _check_temporal_order(self) -> Self:
        """Reject a record that finished before it started, which no run can produce."""
        if self.finished_at < self.started_at:
            raise FieldValueError(
                f"finished_at {to_iso8601(self.finished_at)} precedes "
                f"started_at {to_iso8601(self.started_at)}"
            )
        return self

    @model_validator(mode="after")
    def _check_quarantine_consistency(self) -> Self:
        """Reject a quarantine state that contradicts the outcome (AAP 0.7.2).

        Only a quarantined outcome carries a quarantine state, and each carries its own: a
        mismatch would let the report's Quarantined section and the exit code disagree.
        """
        expected: QuarantineState = "none"
        if self.outcome is Outcome.QUARANTINED_FAILED:
            expected = "failed"
        elif self.outcome is Outcome.QUARANTINE_PASSED:
            expected = "passed"
        if self.quarantine.state != expected:
            raise FieldValueError(
                f"quarantine.state must be {expected!r} for outcome "
                f"{self.outcome.value!r}, got {self.quarantine.state!r}"
            )
        return self

    @property
    def has_failures(self) -> bool:
        """Return whether the record carries a failure entry to render.

        A quarantined failure carries one too: it is a failure a person has to see, even when it
        does not fail the run.
        """
        return bool(self.failures)

    @property
    def is_failing(self) -> bool:
        """Return whether this record counts against the run.

        ``True`` when the outcome is in :data:`FAILING_OUTCOMES`, the definition the comparator,
        the JUnit writer and the exit code share.
        """
        return self.outcome in FAILING_OUTCOMES

    def to_dict(self) -> dict[str, object]:
        """Return the record as the JSON-ready object ``results.json`` holds.

        Built field by field in the order AAP 0.4.6 tabulates, because ``model_dump`` would emit a
        datetime where the document carries the text ``to_iso8601`` writes.
        """
        return {
            "record_type": self.record_type,
            "run_id": self.run_id,
            "started_at": to_iso8601(self.started_at),
            "finished_at": to_iso8601(self.finished_at),
            "profile": self.profile,
            "nodeid": self.nodeid,
            "outcome": self.outcome.value,
            "attempts": self.attempts,
            "duration_ms": self.duration_ms,
            "markers": list(self.markers),
            "failures": [failure.to_dict() for failure in self.failures],
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
            "quarantine": self.quarantine.to_dict(),
        }


# --- The run-level error element -------------------------------------------


class RunError(_FrozenRecord):
    """A failure of the run itself, such as a lost xdist worker (AAP 0.4.6).

    It is the second element kind of ``results.json`` and the only one describing the run rather
    than a test, so it deliberately carries no ``nodeid``, no ``outcome`` and no artifacts: a
    reader tells the two kinds apart from ``record_type`` alone.

    Attributes:
        record_type: The discriminator; always ``"run_error"``.
        run_id: The run's identifier, ``YYYYmmddTHHMMSS-<6 hex>``.
        recorded_at: When the error was recorded, timezone-aware UTC.
        worker_id: The xdist worker the error came from.
        message: The redacted text describing the error.
    """

    record_type: Literal["run_error"] = "run_error"
    run_id: str
    recorded_at: datetime
    worker_id: str
    message: str

    @field_validator("run_id")
    @classmethod
    def _check_run_id(cls, value: str) -> str:
        """Reject a run id of the wrong shape."""
        return _require_run_id(value)

    @field_validator("recorded_at", mode="before")
    @classmethod
    def _as_aware_utc(cls, value: object) -> datetime:
        """Read the timestamp as a timezone-aware UTC datetime."""
        return _normalise_timestamp(value)

    def to_dict(self) -> dict[str, object]:
        """Return the error as the JSON-ready object ``results.json`` holds."""
        return {
            "record_type": self.record_type,
            "run_id": self.run_id,
            "recorded_at": to_iso8601(self.recorded_at),
            "worker_id": self.worker_id,
            "message": self.message,
        }


# --- Reading a stored document ---------------------------------------------

ResultElement: TypeAlias = Annotated[ResultRecord | RunError, Field(discriminator="record_type")]
"""One element of a ``results.json`` array: a result record or a run error.

Discriminated on ``record_type`` rather than resolved by shape, so an element missing the key, or
carrying one this release does not know, is rejected outright.
"""

RESULT_ELEMENT_ADAPTER: Final[TypeAdapter[ResultElement]] = TypeAdapter(
    # pydantic's ``TypeForm`` accepts the Annotated union at runtime; mypy expands the parameter
    # to ``type[ResultRecord] | type[RunError]`` and rejects a union object, so the cast states
    # what the runtime already guarantees and keeps the check stable across mypy's cache.
    cast("type[ResultRecord | RunError]", ResultElement)
)
"""The one validator of a stored element, built once at import.

Constructing it compiles the union's schema, so parsing a five-hundred-record document reuses
this validator rather than rebuilding a validator per element.
"""


def _first_error(exc: ValidationError) -> tuple[str, str]:
    """Return the location and message of a validation failure's first error.

    Returns:
        The dotted location of the reported error, or ``"<document>"`` when it concerns the whole
        document, followed by pydantic's own message.
    """
    errors = exc.errors()
    if not errors:
        return "<document>", "no detail available"
    first = errors[0]
    location = ".".join(str(part) for part in first["loc"]) or "<document>"
    return location, first["msg"]


def parse_element(payload: Mapping[str, object], *, source: Path) -> ResultElement:
    """Validate one decoded element of a result document.

    The only place a stored element is decoded, so every reader accepts and rejects exactly the
    same documents, and a document that is not a Foundry result document surfaces as
    :class:`foundry.core.exceptions.DataError` naming the file and the first error location --
    which is what the command line turns into exit code ``2`` (AAP 0.4.4).

    Raises:
        DataError: The payload is not a valid element. Both the message and ``file`` name the
            document, and the message names the first error's location.
    """
    try:
        return RESULT_ELEMENT_ADAPTER.validate_python(payload)
    except ValidationError as exc:
        location, detail = _first_error(exc)
        raise DataError(
            f"not a Foundry result document: {source}: {location}: {detail}",
            file=source,
            hint=(
                "results.json holds an array of objects with record_type 'result' or "
                "'run_error'; regenerate it with a Foundry run"
            ),
        ) from exc


def element_to_dict(element: ResultElement) -> dict[str, object]:
    """Serialise either element kind back to its JSON-ready object, without narrowing the union."""
    return element.to_dict()
