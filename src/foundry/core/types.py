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

Phase: TypeAlias = Literal["setup", "call", "teardown"]
"""The pytest phases a failure can be attributed to (AAP 0.7.2)."""

PHASES: Final[tuple[Phase, ...]] = ("setup", "call", "teardown")
"""Every ``Phase`` value, in the order pytest runs the phases."""

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
"""Every ``ArtifactKind`` value, in the order AAP 0.4.6 lists the artifacts; ``log_tail`` is a
deliberate addition to the six-file layout, and ``foundry.obs`` owns the filename map."""

QuarantineState: TypeAlias = Literal["none", "failed", "passed"]
"""The states ``QuarantineRecord.state`` can hold (AAP 0.7.2)."""

QUARANTINE_STATES: Final[tuple[QuarantineState, ...]] = ("none", "failed", "passed")
"""Every ``QuarantineState`` value: ``none`` no marker, ``failed`` a marked failure, ``passed`` a
marked test that passed (AAP 0.7.2)."""


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    """One attempt at running a test, or at issuing an HTTP request (AAP 0.7.2).

    Frozen because it crosses the xdist worker boundary as plain data. ``number`` is 1-based,
    ``outcome`` a short token such as ``"timeout"``, ``elapsed_ms`` the duration, ``retry_on`` the
    predicate describing the ending (``None`` for a returned response, so the record count equals
    the attempt count) and ``message`` a description already through the redaction engine.
    """

    number: int
    outcome: str
    elapsed_ms: float
    retry_on: str | None = None
    message: str | None = None

    def as_dict(self) -> dict[str, object]:
        """Return the five fields in declaration order, every value a JSON scalar (AAP 0.4.8)."""
        return {
            "number": self.number,
            "outcome": self.outcome,
            "elapsed_ms": self.elapsed_ms,
            "retry_on": self.retry_on,
            "message": self.message,
        }


Sleep: TypeAlias = Callable[[float], None]
"""An injectable sleep: the single argument is a delay in seconds (AAP 0.7.3)."""


class HttpResponseLike(Protocol):
    """The read-only part of an HTTP response that the assertion helpers read.

    ``foundry.assertions`` may not import ``foundry.http`` (AAP 0.4.8), so the helpers annotate this
    protocol. The members are read-only properties, which a concrete ``headers`` mapping satisfies.
    """

    @property
    def status_code(self) -> int:
        """Return the HTTP status code, including the non-2xx codes Foundry returns."""
        ...

    @property
    def headers(self) -> Mapping[str, str]:
        """Return the response headers, any name in ``secrets.redact_keys`` already redacted."""
        ...

    @property
    def text(self) -> str:
        """Return the response body as text, suitable for an excerpt in an assertion message."""
        ...


class ClockLike(Protocol):
    """A time source the poller can be given instead of the wall clock.

    ``foundry_clock`` (AAP 0.4.3) is injectable as the poller's time source.
    """

    def now(self) -> datetime:
        """Return the current instant as a timezone-aware ``datetime`` in UTC."""
        ...
