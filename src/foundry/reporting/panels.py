"""The per-failure panels of Foundry's self-contained HTML report.

A failing test's panel is the part of ``report.html`` a person actually reads (AAP 0.7.4): the
failure's message and phase, a link to the dumped request and to the dumped response, the last
captured log lines in a monospace block, and the path of the traceback artifact. This module
builds that content as data -- :class:`FailurePanel` -- and :mod:`foundry.reporting.html` renders
it through the Jinja2 template. Nothing here composes markup and nothing here writes a file:
reporting reads capture's output and never produces it (AAP 0.4.8).

One panel per :class:`foundry.reporting.record.FailureRecord`, because a test can fail in more
than one phase and each phase's failure is its own thing to read. :func:`build_panels` orders the
panels by node id, then by phase in execution order, then by message, so the rendered section is
identical across two runs of the same suite (criterion 14).

Links
    A record's artifact paths are relative to the run directory
    ``artifacts/<run_id>/<sanitized-nodeid>/`` (AAP 0.4.6), while the report is written to the
    invocation directory. A href therefore expresses the artifact path relative to the report's
    own directory (:func:`href_for`). It is always a relative filesystem path and never an
    ``http``/``https`` location, which is the property that lets the report open on a machine with
    no network (criterion 9).

Log tail
    The monospace block holds the last ``logging.capture_lines`` captured lines -- 200 by
    default, accepted from 0 to 10000. The caller passes the resolved value rather than this
    package reading configuration, which the layering contract forbids (AAP 0.4.8); implicit
    requirement I7 fixes the bound. The artifact is read as a bounded tail of at most
    :data:`MAX_LOG_TAIL_BYTES` from its end, so a multi-megabyte ``log_tail.log`` never enters
    memory whole, and a line longer than :data:`MAX_LOG_LINE_CHARS` is cut so one pathological
    record cannot bloat the page.

Redaction
    The failure message and every log line pass through
    :class:`foundry.obs.redaction.Redactor` before the panel is constructed, never in the
    template (AAP 0.3.2). A panel that reaches a renderer is therefore safe to persist.

Failure isolation and traversal safety
    An artifact a record names may be missing: ``artifacts.keep_passed`` prunes passed tests, a
    run directory may have been cleaned up, and an unwritable artifacts directory leaves every
    record with no artifact list at all (AAP 0.4.7). Reading one is therefore total -- a fault is
    reported at ``WARNING`` through :func:`foundry.obs.events.emit_plumbing_failure` and the panel
    is built without that section, because Foundry's own plumbing never changes an outcome or an
    exit code (AAP 0.14.3). Every path is resolved with :func:`foundry.core.paths.resolve_under`
    before it is read, so a results document naming ``../../../etc/passwd`` yields no link and no
    read instead of a disclosure: this module is where a file named by a result document is
    opened, and therefore where that guard belongs.

Import direction and import-time discipline (AAP 0.4.8): the standard library,
:mod:`foundry.core`, :mod:`foundry.obs` and this package's own :mod:`foundry.reporting.record`.
Importing this module performs no I/O, reads no environment variable, configures no logger and
mutates nothing.
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
"""Default size of the log tail: the default of the ``logging.capture_lines`` key (AAP 0.14.2).

Taken from :data:`foundry.core.limits.CAPTURE_LINES_DEFAULT`, the single definition site of the
number. This package may not read the configuration the number comes from (AAP 0.4.8), and a
second copy here is what would let a default drift from the published surface.
"""

MAX_LOG_TAIL_LINES: Final[int] = CAPTURE_LINES_MAX
"""Largest accepted tail size: the upper bound of ``logging.capture_lines``.

Taken from :data:`foundry.core.limits.CAPTURE_LINES_MAX`, the single definition site of the bound.
"""

MAX_LOG_TAIL_BYTES: Final[int] = 256 * 1024
"""Most bytes read from the end of ``log_tail.log``, so a huge artifact is never read whole."""

MAX_LOG_LINE_CHARS: Final[int] = 400
"""Longest log line the panel carries; a longer one is cut and terminated with ``\\u2026``.

A captured line can be a serialised payload thousands of characters wide, and the monospace block
is a reading aid rather than a data export, so the width is bounded here instead of in the
template's CSS.
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
absent: the report links the request and response dumps, the traceback and the log tail, and the
manifest is the artifact index rather than part of a failure's story.
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

The mapping is read-only. :class:`types.MappingProxyType` wraps the dict, so lookups are all a
caller has and the module-level state cannot be mutated after import (AAP 0.4.8); a ``Final``
annotation alone stops a rebinding but not a write into the dict.
"""


def _clamp_lines(value: int) -> int:
    """Clamp a requested tail size into the range ``logging.capture_lines`` accepts.

    The value arrives from a hand-editable configuration file by way of the caller, so it may be
    negative, larger than :data:`MAX_LOG_TAIL_LINES`, or not an integer at all.

    Args:
        value: The requested number of log lines.

    Returns:
        A size between ``0`` and :data:`MAX_LOG_TAIL_LINES`, inclusive. A value that is not an
        integer -- ``bool`` included, since ``True`` would silently mean one line -- falls back to
        :data:`DEFAULT_LOG_TAIL_LINES` rather than raising, because a malformed capture setting
        must never fail a run.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return DEFAULT_LOG_TAIL_LINES
    return max(0, min(MAX_LOG_TAIL_LINES, value))


def _panel_sort_key(panel: FailurePanel) -> tuple[str, int, str]:
    """Return the key that orders one panel inside its section (AAP 0.14.1, criterion 14).

    Args:
        panel: The panel to place.

    Returns:
        ``(nodeid, phase index, message)``. The phase index is the failure's position in
        :data:`PHASE_ORDER`, so a setup failure is listed before the call failure of the same
        test, and the message separates two failures of one phase. A phase outside the tuple --
        which the record's validator makes impossible, and which is still handled rather than
        raised -- sorts last.
    """
    return (
        panel.nodeid,
        _PHASE_ORDER_INDEX.get(panel.phase, len(PHASE_ORDER)),
        panel.message,
    )


@dataclass(frozen=True, slots=True)
class FailurePanel:
    """Everything one failure panel carries, ready for the template to render.

    The fields hold what a reader needs to understand a failure without opening a file: which test
    failed, how the run classified it, which phase broke, what the failure said, where the request
    and response dumps are, the tail of the captured log and where the traceback is. Every link is
    a href relative to the report's directory; every piece of free text has already been through
    the redaction engine.

    The view model is frozen because the renderer consumes it and must not be able to alter
    capture's record, and because two runs of the same suite must produce the same page.

    Attributes:
        nodeid: The test's pytest node id, unsanitised, as ``results.json`` records it.
        outcome: The test's final outcome, such as ``failed``, ``error`` or
            ``quarantined_failed``.
        phase: The pytest phase this failure belongs to.
        message: The failure message, already redacted.
        request_path: Href of the recorded request dump, or ``None`` when the record carries no
            such artifact, when artifacts were disabled, or when the recorded path would escape
            the run directory.
        response_path: Href of the recorded response dump, or ``None`` on the same conditions.
        traceback_path: Href of the traceback artifact, or ``None`` on the same conditions. The
            panel links to the file and never reads it: the traceback is an artifact a person
            opens, not text the report inlines.
        log_lines: The captured log tail, newest last, already redacted and width-truncated. Empty
            when nothing was captured or the tail could not be read.
        log_available: Whether the tail was read successfully. ``False`` tells the template to
            state that the log tail is unavailable instead of rendering an empty block, which is
            what distinguishes "a passing test captured nothing" from "the artifact is gone".
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
        omitted entirely when a run's artifacts were disabled (AAP 0.4.7) instead of rendering
        three empty placeholders.

        Returns:
            ``True`` when the request, response or traceback href is present.
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
        reads as a rendering fault, while :attr:`log_available` is what tells the template whether
        to say the tail is unavailable or to say nothing was captured.

        Returns:
            ``True`` when the tail was read and carries at least one line.
        """
        return self.log_available and bool(self.log_lines)


# --- Links to captured artifacts --------------------------------------------


def _relative_href(relative_path: str, *, run_dir: Path, report_dir: Path) -> str | None:
    """Express a run-relative artifact path as a href relative to the report's directory.

    The two directories differ: artifacts live under ``artifacts/<run_id>/`` while the report is
    written to the invocation directory (assumption A5), so the link has to cross from one to the
    other. Resolution goes through :func:`foundry.core.paths.resolve_under` first, which is the
    single containment check of the tree, so a path that escapes the run directory -- through a
    ``..`` segment a hand-edited document carries, an absolute path, or a symlinked directory
    pointing elsewhere -- yields no link rather than a readable file outside the run.

    Args:
        relative_path: The artifact path as the record holds it, relative to ``run_dir``.
        run_dir: The run directory, normally ``artifacts/<run_id>``.
        report_dir: The directory the HTML report is written to.

    Returns:
        The path relative to ``report_dir`` with ``/`` separators, or ``None`` when the path
        escapes ``run_dir`` or cannot be expressed relative to ``report_dir``. Nothing is logged
        here: the caller that attempts a read reports the fault once, so escaping a traversal
        guard costs one ``WARNING`` per artifact rather than two.
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

    Args:
        artifact: The record's artifact entry, whose ``path`` is relative to ``run_dir``.
        run_dir: The run directory, normally ``artifacts/<run_id>``.
        report_dir: The directory the HTML report is written to.

    Returns:
        The artifact's path relative to ``report_dir``, with ``/`` separators and no scheme, or
        ``None`` when the recorded path would escape ``run_dir``.

    The result is a relative filesystem path by construction, so a rendered report never carries
    an ``http`` or ``https`` reference to one of its own artifacts -- the self-contained property
    the report-render test asserts (criterion 9).
    """
    return _relative_href(artifact.path, run_dir=run_dir, report_dir=report_dir)


def _artifact_of(record: ResultRecord, kind: ArtifactKind) -> ArtifactRecord | None:
    """Return the record's first artifact of ``kind``, or ``None`` when it captured none.

    A record lists at most one artifact per kind -- the writer names one file per kind in the
    test's artifact directory -- so the first match is the artifact. Taking the first rather than
    asserting uniqueness keeps a hand-edited document renderable.

    Args:
        record: The result record to inspect.
        kind: The artifact kind wanted, one of :data:`PANEL_ARTIFACT_KINDS`.

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

    Args:
        record: The result record to inspect.
        kind: The artifact kind wanted.
        run_dir: The run directory, or ``None`` when the record's artifacts are disabled.
        report_dir: The directory the HTML report is written to.

    Returns:
        The href, or ``None`` when artifacts were disabled, the record captured no artifact of
        this kind, or its recorded path escapes the run directory.
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
    list, so the path is still linkable when a run's artifact list omits the traceback entry. The
    artifact list wins when both are present, because it is the list the manifest was built from.

    Args:
        failure: The failure record to inspect.
        run_dir: The run directory, or ``None`` when the record's artifacts are disabled.
        report_dir: The directory the HTML report is written to.

    Returns:
        The href, or ``None`` when artifacts were disabled, the failure names no traceback, or
        the named path escapes the run directory.
    """
    if run_dir is None or failure.traceback_path is None:
        return None
    return _relative_href(failure.traceback_path, run_dir=run_dir, report_dir=report_dir)


# --- The captured log tail --------------------------------------------------


def _read_tail_text(path: Path, *, max_bytes: int = MAX_LOG_TAIL_BYTES) -> str:
    """Read at most the last ``max_bytes`` of a file and decode them as text.

    The tail is what the panel renders, and a ``log_tail.log`` can be several megabytes after a
    pathological test, so the whole file is never read. When the file is longer than the budget the
    read starts in the middle of a line; that partial first line is dropped so the block does not
    begin with half a record. A file whose tail holds no line break at all is one enormous line,
    and its suffix is kept because it is still the newest content the test produced.

    Args:
        path: The file to read, already resolved and known to be a regular file.
        max_bytes: Most bytes to read from the end of the file.

    Returns:
        The decoded text, with invalid UTF-8 replaced rather than raised: a log or a capture can
        hold bytes from any source, and a decoding fault must never withhold the report.

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

    Redaction runs before truncation, as :meth:`foundry.obs.redaction.Redactor.text` does it: a
    cut taken first could leave the leading characters of a secret that straddles the boundary in
    the report.

    Args:
        line: One line of the captured tail.
        redactor: The redaction engine the run resolved from ``secrets.redact_keys``.

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

    This is total by design (AAP 0.14.3): nothing here raises into a test, changes an outcome or
    changes an exit code.

    A ``limit`` of ``0`` is a supported configuration rather than a fault. A run configured with
    ``logging.capture_lines = 0`` captures no tail at all, which
    :data:`foundry.obs.capture.MIN_CAPTURE_LINES` documents as a clean no-capture state, so a zero
    limit reads nothing, reports nothing and yields ``((), False)`` -- exactly as
    :func:`_log_tail_for` short-circuits the same case.

    The three genuine faults -- a path that escapes the run directory, a path that is not a
    regular file, and a read fault -- each yield ``((), False)`` after one ``WARNING`` through
    :func:`foundry.obs.events.emit_plumbing_failure`, because Foundry's own plumbing reports its
    faults without raising into the test. Undecodable bytes are not one of them: they are replaced
    as the tail is decoded, so a tail holding them is still shown.

    Args:
        path: The tail file. It is resolved with :func:`foundry.core.paths.resolve_under` against
            ``run_dir`` before it is opened.
        limit: How many of the newest lines to keep. It is clamped into ``0`` to
            :data:`MAX_LOG_TAIL_LINES`; ``0`` yields no lines, no read and no ``WARNING``.
        redactor: The redaction engine every line passes through before it can reach the report.
        run_dir: The directory ``path`` must stay inside, normally ``artifacts/<run_id>``. When it
            is ``None`` the path is used as given, which is what a caller outside a run -- such as
            a test -- needs; a panel always passes it.

    Returns:
        ``(lines, available)``. ``lines`` holds the kept lines, newest last, each redacted and
        width-truncated; ``available`` is ``True`` when the file was read, which distinguishes a
        tail that legitimately held no lines from one that could not be read at all.
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
    and every panel of that test carries the same lines. Reading it per failure would repeat the
    work and, when the artifact is unreadable, repeat the ``WARNING``.

    Args:
        record: The result record to read for.
        run_dir: The run directory, or ``None`` when artifacts were disabled.
        redactor: The redaction engine every line passes through.
        limit: The clamped number of lines to keep; ``0`` means the run captures no log tail.

    Returns:
        ``(lines, available)`` as :func:`read_log_tail` returns them, ``((), False)`` when the
        artifacts were disabled, the run captures no tail, or the record listed no tail artifact.
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

    Args:
        record: The result record to build panels for. Its ``failures`` tuple is non-empty.
        run_dir: The run directory, or ``None`` when artifacts were disabled.
        report_dir: The directory the HTML report is written to.
        redactor: The redaction engine every message passes through.
        limit: The clamped number of log lines to keep.

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
    the report lists it in its own Errors section and it has no phase, no message panel and no
    artifacts to link. Records that carry no failures produce no panel, so a passing run yields an
    empty tuple and the template renders no failure section at all.

    The panels are sorted by :func:`_panel_sort_key`, which makes the section byte-identical
    between two runs of the same suite (criterion 14). Every message and every log line is
    redacted before its panel is constructed, and every artifact read is isolated: a missing,
    unreadable or escaping artifact leaves that section of the panel out and logs one ``WARNING``
    instead of failing the report (AAP 0.14.3).

    Args:
        records: The run's ``results.json`` elements, in the order they were assembled.
        artifacts_root: The run directory ``artifacts/<run_id>`` that the records' artifact paths
            are relative to, or ``None`` when the artifacts directory was not writable. ``None``
            yields panels with no hrefs and no log tail, which is the state AAP 0.4.7 requires.
        report_dir: The directory the HTML report is written to, which hrefs are relative to.
            Defaults to the current working directory, the invocation directory of a run.
        redactor: The redaction engine built from ``secrets.redact_keys``. Defaults to a redactor
            carrying only the built-in deny-lists, so a caller cannot skip redaction by omitting
            the argument.
        log_lines: How many captured log lines each panel carries, normally the resolved value of
            ``logging.capture_lines``. Clamped into ``0`` to :data:`MAX_LOG_TAIL_LINES`;
            ``0`` yields an empty tuple and no read at all.

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
