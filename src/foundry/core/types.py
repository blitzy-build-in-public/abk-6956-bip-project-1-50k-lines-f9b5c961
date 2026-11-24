"""Shared primitives of the foundation package.

The phase, artifact-kind and quarantine vocabularies, the per-attempt record and the protocols.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal, Protocol, TypeAlias

__all__: Final[tuple[str, ...]] = (
    "ARTIFACT_KINDS",
    "ArtifactKind",
    "AttemptRecord",
    "ClockLike",
    "HttpResponseLike",
    "PHASES",
    "Phase",
    "QUARANTINE_STATES",
    "QuarantineState",
    "Sleep",
)

# --- Phases ----------------------------------------------------------------

Phase: TypeAlias = Literal["setup", "call", "teardown"]
"""The pytest phases a failure can be attributed to (AAP 0.7.2)."""

PHASES: Final[tuple[Phase, ...]] = ("setup", "call", "teardown")
"""Every ``Phase`` value, in the order pytest runs the phases.

Declaration order follows execution order, not alphabetical order; nothing serialises it.
"""

# --- Artifact kinds --------------------------------------------------------

ArtifactKind: TypeAlias = Literal[
    "request",
    "response",
    "stdout",
    "stderr",
    "traceback",
    "log_tail",
    "manifest",
]
"""The kinds of per-test artifact Foundry captures (AAP 0.4.6, AAP 0.7.2)."""

ARTIFACT_KINDS: Final[tuple[ArtifactKind, ...]] = (
    "request",
    "response",
    "stdout",
    "stderr",
    "traceback",
    "log_tail",
    "manifest",
)
"""Every ``ArtifactKind`` value, in the order AAP 0.4.6 lists the artifacts.

``foundry.obs`` owns the kind-to-filename map and the ``manifest.json`` shape. ``log_tail``
is a deliberate addition to the six-file layout.
"""

# --- Quarantine states -----------------------------------------------------

QuarantineState: TypeAlias = Literal["none", "failed", "passed"]
"""The states ``QuarantineRecord.state`` can hold (AAP 0.7.2)."""

QUARANTINE_STATES: Final[tuple[QuarantineState, ...]] = ("none", "failed", "passed")
"""Every ``QuarantineState`` value (AAP 0.7.2).

``none`` marks no marker, ``failed`` a marked failure, ``passed`` a marked test that passed.
"""


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    """One attempt at running a test, or at issuing an HTTP request.

    Produced by ``foundry.http.retries`` and ``foundry.execution.coordinator``, carried by
    ``RetryExhaustedError`` and rendered by ``foundry.reporting``. Frozen because it crosses the
    xdist worker boundary as plain data and must not change once the attempt has ended.

    Attributes:
        number: 1-based attempt number; the first attempt is ``1``.
        outcome: Short label for how the attempt ended, such as ``"timeout"`` or ``"status_5xx"``.
        elapsed_ms: Wall-clock duration of the attempt in milliseconds.
        retry_on: The retry predicate describing how the attempt ended, or ``None``: a returned
            response never names one, and a predicate names the ending even on the last attempt, so
            the number of records equals the number of attempts.
        message: Short description, already through the redaction engine (AAP 0.3.2), or ``None``;
            it reaches ``results.json``, the JUnit XML and the HTML report.
    """

    number: int
    outcome: str
    elapsed_ms: float
    retry_on: str | None = None
    message: str | None = None

    def as_dict(self) -> dict[str, object]:
        """Return the record as a JSON-serialisable mapping, the single serialisation of an attempt.

        Returns:
            The five fields in declaration order, every value a JSON scalar, for the
            ``user_properties`` wire format (AAP 0.4.8) and every renderer.
        """
        return {
            "number": self.number,
            "outcome": self.outcome,
            "elapsed_ms": self.elapsed_ms,
            "retry_on": self.retry_on,
            "message": self.message,
        }


# --- Injectable collaborator shapes ----------------------------------------

Sleep: TypeAlias = Callable[[float], None]
"""An injectable sleep: the single argument is a delay in seconds (AAP 0.7.3).

A recording fake keeps every sleep in the suite under the 100 ms cap (AAP 0.14.3).
"""


class HttpResponseLike(Protocol):
    """The read-only part of an HTTP response that the assertion helpers read.

    ``foundry.assertions`` may not import ``foundry.http`` (AAP 0.4.8), so the helpers annotate this
    protocol instead. The members are read-only properties, not attributes, so an implementation
    declaring a concrete ``headers`` mapping still satisfies it: property types are covariant where
    attribute types are invariant.
    """

    @property
    def status_code(self) -> int:
        """Return the HTTP status code, including the non-2xx codes Foundry returns.

        Returns:
            The numeric status code of the response.
        """
        ...

    @property
    def headers(self) -> Mapping[str, str]:
        """Return the response headers.

        Returns:
            The headers, with any name in ``secrets.redact_keys`` already redacted.
        """
        ...

    @property
    def text(self) -> str:
        """Return the response body decoded as text.

        Returns:
            The body as text, suitable for an excerpt in an assertion message.
        """
        ...


class ClockLike(Protocol):
    """A time source the poller can be given instead of the wall clock.

    ``foundry_clock`` (AAP 0.4.3) is injectable as the poller's time source.
    """

    def now(self) -> datetime:
        """Return the current instant.

        Returns:
            A timezone-aware ``datetime`` in UTC.
        """
        ...
