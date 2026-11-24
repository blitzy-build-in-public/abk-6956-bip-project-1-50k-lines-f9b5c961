"""Shared primitives of the foundation package.

This module holds the vocabulary every other Foundry package shares: the pytest
phase names, the artifact kinds, the quarantine states, the per-attempt record
that travels from an xdist worker to the controller, and the small structural
protocols that let one capability package describe a collaborator owned by
another without importing it.

It obeys the foundation rule of the layering contract (AAP 0.4.8): the standard
library is the only thing it imports, it defines no exception type, and it does
no work at import time -- no I/O, no environment read, no logger configuration
and no argument parsing. Importing this module defines names and nothing else.
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
"""The pytest phases a failure can be attributed to.

``FailureRecord.phase`` is typed with this alias (AAP 0.7.2), and its three
members are the phase names pytest reports a test outcome under.
"""

PHASES: Final[tuple[Phase, ...]] = ("setup", "call", "teardown")
"""Every ``Phase`` value, in the order pytest runs the phases.

Callers validate a phase by membership of this tuple and iterate it when they
render phases, so the literals are written here once rather than retyped by
``foundry.execution`` and ``foundry.reporting``. The tuple is immutable and
nothing serialises it, which makes its order documentation rather than output:
declaration order follows execution order, not alphabetical order.
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

The mapping from a kind to a file name (``request.json``, ``stdout.log``, ...)
and the ``manifest.json`` shape belong to ``foundry.obs``; this module owns only
the kind vocabulary. ``log_tail`` is a deliberate addition to the six-file
artifact layout: the last ``logging.capture_lines`` captured log records are
written as an artifact and referenced from a result record's ``artifacts`` list.
The tuple is immutable and nothing serialises it, so its order is documentation
rather than output.
"""

# --- Quarantine states -----------------------------------------------------

QuarantineState: TypeAlias = Literal["none", "failed", "passed"]
"""The states ``QuarantineRecord.state`` can hold (AAP 0.7.2)."""

QUARANTINE_STATES: Final[tuple[QuarantineState, ...]] = ("none", "failed", "passed")
"""Every ``QuarantineState`` value.

``none`` marks a test that carries no quarantine marker, ``failed`` a marked
test that failed and ``passed`` a marked test that passed. The tuple is
immutable and nothing serialises it, so its order is documentation rather than
output.
"""


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    """One attempt at running a test, or at issuing an HTTP request.

    ``foundry.http.retries`` produces these while it retries a request and
    ``foundry.execution.coordinator`` accumulates them while it drives pytest's
    runtest protocol; ``foundry.core.exceptions.RetryExhaustedError`` carries the
    list and ``foundry.reporting`` renders it. The class is declared here because
    ``foundry.core`` is the only package that may hold the wire format shared by
    all three, and ``foundry.reporting`` re-exports it rather than declaring a
    second, incompatible copy.

    The record is frozen because it crosses the xdist worker boundary as plain
    data and must not change once the attempt has ended.

    Attributes:
        number: 1-based attempt number; the first attempt is ``1``.
        outcome: Short label for how the attempt ended, such as ``"passed"``,
            ``"failed"``, ``"timeout"``, ``"connect"``, ``"status_5xx"`` or
            ``"status_429"``. Deliberately a plain ``str``: it describes an
            attempt rather than one of the nine result-record outcome values
            that ``foundry.reporting.Outcome`` owns.
        elapsed_ms: Wall-clock duration of the attempt in milliseconds.
        retry_on: The retry predicate that describes how the attempt ended, or
            ``None`` when no predicate does: a response that is returned never
            names one, whatever its status. An attempt ended by a fault that
            classifies as a predicate keeps it even when it was the last one, so
            the number of records equals the number of attempts.
        message: Short description of the attempt, or ``None``. Callers pass
            only text that has already been through the redaction engine
            (AAP 0.3.2): the record reaches ``results.json``, the JUnit XML and
            the HTML report.
    """

    number: int
    outcome: str
    elapsed_ms: float
    retry_on: str | None = None
    message: str | None = None

    def as_dict(self) -> dict[str, object]:
        """Return the record as a JSON-serialisable mapping.

        This is the single serialisation of an attempt: the
        ``TestReport.user_properties`` wire format (AAP 0.4.8) and every report
        renderer read it, so each consumer does not invent its own key order.

        Returns:
            A mapping whose keys are ``number``, ``outcome``, ``elapsed_ms``,
            ``retry_on`` and ``message``, in field-declaration order, with every
            value a JSON scalar.
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
"""An injectable sleep: the single argument is a delay in seconds.

``foundry.assertions.eventually``, the retry backoff in ``foundry.http.retries``
and ``foundry.fixtures.polling`` all accept it (AAP 0.7.3) so a test can pass a
recording fake and assert on the delays instead of waiting for them, which is
what keeps every sleep in the suite under the 100 ms cap (AAP 0.14.3).
"""


class HttpResponseLike(Protocol):
    """The read-only part of an HTTP response that the assertion helpers read.

    The layering contract (AAP 0.4.8) keeps ``foundry.assertions`` from importing
    ``foundry.http``, so ``assert_status`` annotates its response parameter with
    this protocol and the real ``foundry.http.FoundryResponse`` satisfies it
    structurally. The members are read-only properties rather than attributes so
    that an implementation declaring a concrete mapping for ``headers``, such as
    ``dict[str, str]``, still satisfies the protocol: property types are
    covariant where attribute types are invariant.
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
            The headers, with any name in ``secrets.redact_keys`` already
            redacted by the writer that persisted them.
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

    ``foundry_clock`` (AAP 0.4.3) is injectable as ``foundry_poll``'s time
    source without ``foundry.assertions`` importing ``foundry.fixtures``, which
    the layering contract forbids, and a frozen clock makes deadline behaviour
    assertable without waiting.
    """

    def now(self) -> datetime:
        """Return the current instant.

        Returns:
            A timezone-aware ``datetime`` in UTC.
        """
        ...
