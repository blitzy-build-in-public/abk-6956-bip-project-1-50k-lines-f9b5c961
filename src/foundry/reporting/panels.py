"""The per-failure panels of Foundry's self-contained HTML report.

:class:`FailurePanel` is one failing phase of one test as data -- message, artifact hrefs, the
bounded and redacted log tail -- and :func:`build_panels` orders the panels by node id, phase and
message so two runs render identically. AAP 0.3.2, 0.4.6, 0.4.8, 0.7.4, 0.14.3.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Final

from foundry.core.exceptions import ArtifactError
from foundry.core.limits import CAPTURE_LINES_DEFAULT, CAPTURE_LINES_MAX
from foundry.core.paths import resolve_under
from foundry.core.types import PHASES, ArtifactKind, Phase
from foundry.obs.events import emit_plumbing_failure
from foundry.obs.redaction import Redactor, default_redactor
from foundry.reporting.record import (
    ArtifactRecord,
    FailureRecord,
    Outcome,
    ResultElement,
    ResultRecord,
    RunError,
)

__all__: Final[tuple[str, ...]] = (
    "DEFAULT_LOG_TAIL_LINES",
    "MAX_LOG_LINE_CHARS",
    "MAX_LOG_TAIL_BYTES",
    "MAX_LOG_TAIL_LINES",
    "PANEL_ARTIFACT_KINDS",
    "PHASE_ORDER",
    "FailurePanel",
    "build_panels",
    "href_for",
    "read_log_tail",
)

DEFAULT_LOG_TAIL_LINES: Final[int] = CAPTURE_LINES_DEFAULT
"""Default size of the log tail: the default of the ``logging.capture_lines`` key.

Taken from :data:`foundry.core.limits.CAPTURE_LINES_DEFAULT`, the single definition site of the
number, which this package may not read from configuration (AAP 0.4.8, 0.14.2).
"""

MAX_LOG_TAIL_LINES: Final[int] = CAPTURE_LINES_MAX
"""Largest accepted tail size: the upper bound of ``logging.capture_lines``."""

MAX_LOG_TAIL_BYTES: Final[int] = 256 * 1024
"""Most bytes read from the end of ``log_tail.log``, so a huge artifact is never read whole."""

MAX_LOG_LINE_CHARS: Final[int] = 400
"""Longest log line the panel carries; a longer one is cut and terminated with ``\\u2026``.

A captured line can be a serialised payload thousands of characters wide, and the block is a
reading aid rather than a data export, so the width is bounded here rather than in CSS.
"""

PHASE_ORDER: Final[tuple[Phase, ...]] = PHASES
"""The phases in the order pytest runs them, which is the order the panels are sorted by."""

PANEL_ARTIFACT_KINDS: Final[tuple[ArtifactKind, ...]] = (
    "request",
    "response",
    "traceback",
    "log_tail",
)
"""The artifact kinds a panel links to or reads, in the order AAP 0.7.4 lists them.

``stdout``, ``stderr`` and ``manifest`` are captured and listed in the record but deliberately
absent: the manifest is the artifact index, not part of a failure's story.
"""

_COMPONENT: Final[str] = "reporting.panels"
"""Component name ``WARNING`` lines carry, so a plumbing fault names its source."""

_TRUNCATION_SUFFIX: Final[str] = "\u2026"
"""Marker appended to a line cut at :data:`MAX_LOG_LINE_CHARS`, one character wide."""

_REQUEST_KIND: Final[ArtifactKind] = "request"
"""Kind of the recorded request dump a panel links to."""

_RESPONSE_KIND: Final[ArtifactKind] = "response"
"""Kind of the recorded response dump a panel links to."""

_TRACEBACK_KIND: Final[ArtifactKind] = "traceback"
"""Kind of the traceback artifact a panel links to."""

_LOG_TAIL_KIND: Final[ArtifactKind] = "log_tail"
"""Kind of the captured log tail a panel reads into its monospace block."""

_PHASE_ORDER_INDEX: Final[Mapping[Phase, int]] = MappingProxyType(
    {phase: index for index, phase in enumerate(PHASE_ORDER)}
)
"""Position of each phase in :data:`PHASE_ORDER`, for the deterministic panel order.

:class:`types.MappingProxyType` keeps the module-level state unmutated after import (AAP 0.4.8).
"""


def _clamp_lines(value: int) -> int:
    """Clamp a requested tail size into the range ``logging.capture_lines`` accepts.

    The value arrives from a hand-editable configuration file, so it may be negative, larger than
    :data:`MAX_LOG_TAIL_LINES`, or not an integer at all.

    Returns:
        A size between ``0`` and :data:`MAX_LOG_TAIL_LINES`. A non-integer -- ``bool`` included,
        since ``True`` would mean one line -- falls back to :data:`DEFAULT_LOG_TAIL_LINES` rather
        than raising, because a malformed capture setting must never fail a run.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return DEFAULT_LOG_TAIL_LINES
    return max(0, min(MAX_LOG_TAIL_LINES, value))


def _panel_sort_key(panel: FailurePanel) -> tuple[str, int, str]:
    """Return the key that orders one panel inside its section (AAP 0.14.1, criterion 14).

    Returns:
        ``(nodeid, phase index, message)``: a setup failure is listed before the call failure of
        the same test, and the message separates two failures of one phase. A phase outside
        :data:`PHASE_ORDER` -- which the record's validator makes impossible -- sorts last.
    """
    return (
        panel.nodeid,
        _PHASE_ORDER_INDEX.get(panel.phase, len(PHASE_ORDER)),
        panel.message,
    )


@dataclass(frozen=True, slots=True)
class FailurePanel:
    """Everything one failure panel carries, ready for the template to render.

    Every link is a href relative to the report's directory and every piece of free text has already
    been through the redaction engine; the frozen view model keeps two runs rendering one page.

    Attributes:
        nodeid: The test's pytest node id, unsanitised, as ``results.json`` records it.
        outcome: The test's final outcome, such as ``failed``, ``error`` or ``quarantined_failed``.
        phase: The pytest phase this failure belongs to.
        message: The failure message, already redacted.
        request_path: Href of the recorded request dump, or ``None`` when the record carries no
            such artifact, artifacts were disabled, or the path would escape the run directory.
        response_path: Href of the recorded response dump, or ``None`` on the same conditions.
        traceback_path: Href of the traceback artifact, or ``None`` on the same conditions. The
            panel links to the file and never inlines it.
        log_lines: The captured log tail, newest last, redacted and width-truncated. Empty when
            nothing was captured or the tail could not be read.
        log_available: Whether the tail was read successfully. ``False`` tells the template to say
            the tail is unavailable instead of rendering an empty block.
    """

    nodeid: str
    outcome: Outcome
    phase: Phase
    message: str
    request_path: str | None = None
    response_path: str | None = None
    traceback_path: str | None = None
    log_lines: tuple[str, ...] = ()
    log_available: bool = False

    @property
    def has_links(self) -> bool:
        """Return whether the panel has at least one artifact link to render.

        The template asks this rather than testing three fields itself, so a link section is
        omitted entirely when a run's artifacts were disabled (AAP 0.4.7).
        """
        return (
            self.request_path is not None
            or self.response_path is not None
            or self.traceback_path is not None
        )

    @property
    def has_log(self) -> bool:
        """Return whether there is a log tail to render in the monospace block.

        A tail that was read but held no lines yields ``False`` as well: an empty ``<pre>`` block
        reads as a rendering fault, while :attr:`log_available` distinguishes the two states.
        """
        return self.log_available and bool(self.log_lines)


# --- Links to captured artifacts --------------------------------------------


def _relative_href(relative_path: str, *, run_dir: Path, report_dir: Path) -> str | None:
    """Express a run-relative artifact path as a href relative to the report's directory.

    Resolution goes through :func:`foundry.core.paths.resolve_under` first, so a path that escapes
    the run directory -- through a ``..`` segment, an absolute path or a symlinked directory --
    yields no link rather than a readable file outside the run.

    Returns:
        The path relative to ``report_dir`` with ``/`` separators, or ``None`` when the path escapes
        ``run_dir`` or cannot be expressed relative to ``report_dir``. Nothing is logged here: the
        caller that attempts a read reports the fault once.
    """
    try:
        resolved = resolve_under(run_dir, relative_path)
    except ArtifactError:
        # Failure-isolation rule (AAP 0.14.3): a path outside the run directory gets no href and
        # is logged by the read path, never raised -- a report must still render.
        return None
    try:
        relative = os.path.relpath(resolved, Path(report_dir).resolve())
    except (OSError, ValueError):
        # Failure-isolation rule: two paths with no common ancestor cannot be linked, which must
        # not stop the report being written.
        return None
    return PurePosixPath(relative).as_posix()


def href_for(artifact: ArtifactRecord, *, run_dir: Path, report_dir: Path) -> str | None:
    """Return the href a report link uses for one recorded artifact.

    Returns:
        The artifact's path relative to ``report_dir``, with ``/`` separators and no scheme, or
        ``None`` when the recorded path would escape ``run_dir``.

    The result is a relative filesystem path by construction, so a rendered report never carries an
    ``http`` or ``https`` reference to one of its own artifacts (criterion 9).
    """
    return _relative_href(artifact.path, run_dir=run_dir, report_dir=report_dir)


def _artifact_of(record: ResultRecord, kind: ArtifactKind) -> ArtifactRecord | None:
    """Return the record's first artifact of ``kind``, or ``None`` when it captured none.

    A record lists at most one artifact per kind, and taking the first rather than asserting
    uniqueness keeps a hand-edited document renderable.

    Returns:
        The matching artifact entry, or ``None``.
    """
    return next((artifact for artifact in record.artifacts if artifact.kind == kind), None)


def _link_for(
    record: ResultRecord,
    kind: ArtifactKind,
    *,
    run_dir: Path | None,
    report_dir: Path,
) -> str | None:
    """Return the href of the record's artifact of ``kind``, or ``None`` when there is none.

    Returns:
        The href, or ``None`` when artifacts were disabled, the record captured no artifact of this
        kind, or its recorded path escapes the run directory.
    """
    if run_dir is None:
        return None
    artifact = _artifact_of(record, kind)
    if artifact is None:
        return None
    return href_for(artifact, run_dir=run_dir, report_dir=report_dir)


def _failure_traceback_link(
    failure: FailureRecord, *, run_dir: Path | None, report_dir: Path
) -> str | None:
    """Return the href of the traceback a failure names, when no traceback artifact was listed.

    A failure record carries its own ``traceback_path`` (AAP 0.4.6) beside the record's artifact
    list; the artifact list wins when both are present, because it is the list the manifest was
    built from.

    Returns:
        The href, or ``None`` when artifacts were disabled, the failure names no traceback, or the
        named path escapes the run directory.
    """
    if run_dir is None or failure.traceback_path is None:
        return None
    return _relative_href(failure.traceback_path, run_dir=run_dir, report_dir=report_dir)


# --- The captured log tail --------------------------------------------------


def _read_tail_text(path: Path, *, max_bytes: int = MAX_LOG_TAIL_BYTES) -> str:
    """Read at most the last ``max_bytes`` of a file and decode them as text.

    A ``log_tail.log`` can be several megabytes, so the whole file is never read; a partial first
    line is dropped so the block does not begin with half a record, and a file whose tail holds no
    line break at all keeps its suffix, which is still the newest content.

    Returns:
        The decoded text, with invalid UTF-8 replaced rather than raised: a decoding fault must
        never withhold the report.

    Raises:
        OSError: The file cannot be opened, sought or read. The caller isolates it.
    """
    size = path.stat().st_size
    with path.open("rb") as handle:
        if size > max_bytes:
            handle.seek(size - max_bytes)
            data = handle.read(max_bytes)
            first_break = data.find(b"\n")
            if first_break >= 0:
                data = data[first_break + 1 :]
        else:
            data = handle.read()
    return data.decode("utf-8", errors="replace")


def _prepare_line(line: str, redactor: Redactor) -> str:
    """Redact one captured log line and cut it to :data:`MAX_LOG_LINE_CHARS`.

    Redaction runs before truncation: a cut taken first could leave the leading characters of a
    secret that straddles the boundary in the report.

    Returns:
        The line, redacted and at most :data:`MAX_LOG_LINE_CHARS` characters wide, terminated with
        :data:`_TRUNCATION_SUFFIX` when it was cut.
    """
    scrubbed = redactor.text(line)
    if len(scrubbed) <= MAX_LOG_LINE_CHARS:
        return scrubbed
    return scrubbed[: MAX_LOG_LINE_CHARS - 1] + _TRUNCATION_SUFFIX


def read_log_tail(
    path: Path,
    *,
    limit: int,
    redactor: Redactor,
    run_dir: Path | None = None,
) -> tuple[tuple[str, ...], bool]:
    """Read, redact and truncate the last ``limit`` lines of a captured log tail.

    Total by design (AAP 0.14.3): nothing here raises into a test, changes an outcome or changes an
    exit code. A ``limit`` of ``0`` is a supported configuration rather than a fault -- it reads
    nothing, reports nothing and yields ``((), False)`` -- and the three genuine faults, a path that
    escapes the run directory, a path that is not a regular file and a read fault, each yield
    ``((), False)`` after one ``WARNING``. Undecodable bytes are not a fault: they are replaced as
    the tail is decoded, so a tail holding them is still shown.

    Args:
        path: The tail file, resolved against ``run_dir`` before it is opened.
        limit: How many of the newest lines to keep; ``0`` reads nothing and logs nothing.
        redactor: The redaction engine every line passes through.
        run_dir: The directory ``path`` must stay inside; ``None`` uses the path as given.

    Returns:
        ``(lines, available)``, ``available`` being ``True`` when the file was read, which
        distinguishes a tail that legitimately held no lines from one that could not be read.
    """
    wanted = _clamp_lines(limit)
    if wanted == 0:
        return (), False

    try:
        source = resolve_under(run_dir, path) if run_dir is not None else Path(path)
    except ArtifactError as exc:
        emit_plumbing_failure(
            _COMPONENT, f"log tail path escapes the run directory: {path}", exc=exc
        )
        return (), False

    try:
        if not source.is_file():
            emit_plumbing_failure(_COMPONENT, f"log tail artifact is not a regular file: {source}")
            return (), False
        text = _read_tail_text(source)
    except OSError as exc:
        emit_plumbing_failure(_COMPONENT, f"log tail artifact could not be read: {source}", exc=exc)
        return (), False

    lines = text.splitlines()[-wanted:]
    return tuple(_prepare_line(line, redactor) for line in lines), True


def _log_tail_for(
    record: ResultRecord,
    *,
    run_dir: Path | None,
    redactor: Redactor,
    limit: int,
) -> tuple[tuple[str, ...], bool]:
    """Return the log tail one record's panels share.

    The tail belongs to the test, not to one of its failing phases, so it is read once per record
    and every panel of that test carries the same lines; reading it per failure would repeat the
    work and, when the artifact is unreadable, repeat the ``WARNING``.

    Returns:
        ``(lines, available)`` as :func:`read_log_tail` returns them, ``((), False)`` when artifacts
        were disabled, the run captures no tail, or the record listed no tail artifact.
    """
    if run_dir is None or limit == 0:
        return (), False
    artifact = _artifact_of(record, _LOG_TAIL_KIND)
    if artifact is None:
        return (), False
    return read_log_tail(
        Path(run_dir) / artifact.path, limit=limit, redactor=redactor, run_dir=Path(run_dir)
    )


# --- Building the panels ----------------------------------------------------


def _panels_for_record(
    record: ResultRecord,
    *,
    run_dir: Path | None,
    report_dir: Path,
    redactor: Redactor,
    limit: int,
) -> tuple[FailurePanel, ...]:
    """Build one panel per failing phase of ``record``, all sharing that test's artifacts.

    Returns:
        One :class:`FailurePanel` per :class:`foundry.reporting.record.FailureRecord`, in the
        record's failure order.
    """
    request_path = _link_for(record, _REQUEST_KIND, run_dir=run_dir, report_dir=report_dir)
    response_path = _link_for(record, _RESPONSE_KIND, run_dir=run_dir, report_dir=report_dir)
    traceback_path = _link_for(record, _TRACEBACK_KIND, run_dir=run_dir, report_dir=report_dir)
    log_lines, log_available = _log_tail_for(
        record, run_dir=run_dir, redactor=redactor, limit=limit
    )

    panels: list[FailurePanel] = []
    for failure in record.failures:
        panels.append(
            FailurePanel(
                nodeid=record.nodeid,
                outcome=record.outcome,
                phase=failure.phase,
                message=redactor.text(failure.message),
                request_path=request_path,
                response_path=response_path,
                traceback_path=(
                    traceback_path
                    if traceback_path is not None
                    else _failure_traceback_link(failure, run_dir=run_dir, report_dir=report_dir)
                ),
                log_lines=log_lines,
                log_available=log_available,
            )
        )
    return tuple(panels)


def build_panels(
    records: Sequence[ResultElement],
    *,
    artifacts_root: str | Path | None = None,
    report_dir: str | Path | None = None,
    redactor: Redactor | None = None,
    log_lines: int = DEFAULT_LOG_TAIL_LINES,
) -> tuple[FailurePanel, ...]:
    """Build the failure panels of a run from its result elements.

    One panel is produced per failing phase of every result record, and run-level errors are
    skipped: a :class:`foundry.reporting.record.RunError` describes the run rather than a test, so
    the report lists it in its own Errors section. Records that carry no failures produce no panel.

    The panels are sorted by :func:`_panel_sort_key`, which makes the section byte-identical between
    two runs of the same suite (criterion 14). Every message and log line is redacted before its
    panel is constructed, and every artifact read is isolated, so a missing or escaping artifact
    leaves that section out and logs one ``WARNING`` instead of failing the report (AAP 0.14.3).

    Returns:
        The failure panels, ordered by node id, then phase in execution order, then message. An
        empty tuple when nothing failed.
    """
    scrubber = redactor if redactor is not None else default_redactor()
    target = Path.cwd() if report_dir is None else Path(report_dir)
    run_dir = None if artifacts_root is None else Path(artifacts_root)
    limit = _clamp_lines(log_lines)

    panels: list[FailurePanel] = []
    for element in records:
        if isinstance(element, RunError) or not element.failures:
            continue
        panels.extend(
            _panels_for_record(
                element, run_dir=run_dir, report_dir=target, redactor=scrubber, limit=limit
            )
        )
    panels.sort(key=_panel_sort_key)
    return tuple(panels)
