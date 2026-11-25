"""The renderer of Foundry's self-contained, offline-openable ``report.html``.

:func:`build_context` assembles the sorted, immutable :class:`ReportContext` the template reads and
:func:`render` renders it through Jinja2 with ``autoescape=True``, returning text and writing no
file. AAP 0.4.7 (the fallback), 0.4.8, 0.7.4, 0.14.1, 0.14.2.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from foundry.core.exceptions import ElementTypeError
from foundry.core.ids import to_iso8601
from foundry.obs.events import emit_html_fallback
from foundry.obs.redaction import Redactor, default_redactor
from foundry.reporting.assets import (
    DEFAULT_ASSETS,
    AssetsBundle,
    assert_self_contained,
    contains_external_reference,
)
from foundry.reporting.panels import DEFAULT_LOG_TAIL_LINES, FailurePanel, build_panels
from foundry.reporting.record import (
    OUTCOMES,
    QUARANTINE_OUTCOMES,
    Outcome,
    ResultElement,
    ResultRecord,
    RunError,
)

__all__: Final[tuple[str, ...]] = (
    "DEFAULT_LOG_LINES",
    "QUARANTINE_REMOVAL_NOTE",
    "REPORT_TITLE",
    "TEMPLATE_DIR",
    "TEMPLATE_NAME",
    "OutcomeCount",
    "QuarantinedRow",
    "ReportContext",
    "ReportRow",
    "RunErrorRow",
    "build_context",
    "render",
    "template_path",
)

# --- Template location and report vocabulary --------------------------------

TEMPLATE_DIR: Final[Path] = Path(__file__).resolve().parent / "templates"
"""Directory holding the report template, shipped as package data (AAP 0.4.1).

Resolved from this module's own location, so one path serves a wheel and a source checkout.
"""

TEMPLATE_NAME: Final[str] = "report.html.j2"
"""File name of the single report template inside :data:`TEMPLATE_DIR`."""

REPORT_TITLE: Final[str] = "foundry report"
"""The single source of the document's title text, rendered in ``<title>`` and ``<h1>``.

The template reads it as :attr:`ReportContext.title`, so the page title has one producer:
English-only, light theme, no run id and no timestamp (assumption A14).
"""

DEFAULT_LOG_LINES: Final[int] = DEFAULT_LOG_TAIL_LINES
"""Captured log lines each failure panel carries: the default of the ``logging.capture_lines`` key,
taken from :data:`foundry.reporting.panels.DEFAULT_LOG_TAIL_LINES` because this package may not read
that key itself (AAP 0.4.8).
"""

QUARANTINE_REMOVAL_NOTE: Final[str] = (
    "foundry: quarantined test passed; remove the foundry_quarantine marker"
)
"""The recommendation rendered beside a ``quarantine_passed`` entry (assumption A11).

Byte-identical to :data:`foundry.reporting.junit.QUARANTINE_PASSED_NOTE`, so both documents carry
one recommendation, and a test pins both reporting-side copies equal.
"""

_UNKNOWN: Final[str] = "unknown"
"""The header's last resort when nothing supplies a ``run_id``, ``profile`` or ``finished_at``.

A fabricated run id or timestamp could collide with a real one.
"""

_MILLISECONDS_PER_SECOND: Final[float] = 1000.0
"""Milliseconds in a second, the unit boundary :func:`_format_duration` switches at."""

# --- The view model the template renders ------------------------------------


@dataclass(frozen=True, slots=True)
class OutcomeCount:
    """One outcome's tally in the report's summary strip.

    All nine outcomes are always present, zero counts included, so the strip keeps one width
    (criterion 14).

    Attributes:
        outcome: The outcome this chip counts.
        count: How many result records carry it; never negative.
        css_class: The class the stylesheet colours the chip by, such as ``summary-failed``.
    """

    outcome: Outcome
    count: int
    css_class: str


@dataclass(frozen=True, slots=True)
class ReportRow:
    """One test's row in the report's results table.

    The row carries what the summary view of a test shows -- which test, how it ended, how long it
    took, which profile it ran under and which markers it held.

    Attributes:
        nodeid: The test's pytest node id, unsanitised, exactly as ``results.json`` records it.
        outcome_value: The outcome's bare value, so a template can render it without the enum.
        css_class: The class the stylesheet styles the outcome badge by, such as ``outcome-passed``.
        duration_display: The duration formatted for a reader, such as ``12.4 ms``.
        profile: The profile the test ran under.
        markers: The item's marker names, sorted and de-duplicated by the record.
    """

    nodeid: str
    outcome_value: str
    css_class: str
    duration_display: str
    profile: str
    markers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class QuarantinedRow:
    """One entry of the report's Quarantined section.

    A quarantined test is a known-flaky one: its failure is listed while the run still succeeds in
    ``report`` mode (AAP 0.4.7); a ``quarantine_passed`` entry carries the removal recommendation
    instead (assumption A11).

    Attributes:
        nodeid: The test's pytest node id, unsanitised.
        outcome: ``quarantined_failed`` or ``quarantine_passed``.
        css_class: The badge's ``.outcome-<value>`` class, such as ``outcome-quarantine_passed``.
        note: :data:`QUARANTINE_REMOVAL_NOTE` for a marked test that passed, else an empty string.
    """

    nodeid: str
    outcome: Outcome
    css_class: str
    note: str = ""


@dataclass(frozen=True, slots=True)
class RunErrorRow:
    """One entry of the report's Errors section: a failure of the run itself.

    It carries the worker and its message, and deliberately no timestamp (AAP 0.7.4).

    Attributes:
        worker_id: The xdist worker the error came from, as the record holds it.
        message: The error text, already redacted by :func:`build_context`.
    """

    worker_id: str
    message: str


@dataclass(frozen=True, slots=True)
class ReportContext:
    """Everything one rendered report reads: the frozen view model of a run.

    The context is the whole contract between this module and the template: built once per run,
    immutable, and with every collection sorted, so the template iterates straight into markup and
    the result is byte-identical across two runs of the same suite.

    Attributes:
        run_id: The run's identifier, ``YYYYmmddTHHMMSS-<6 hex>``.
        profile: The active profile name.
        finished_at: The run's finish time, ISO 8601 UTC: the value ``plugin.py`` supplies when it
            knows the session's own, else the latest result record's, else :data:`_UNKNOWN`.
        title: The document title text, which the template renders as the page title and the
            page heading; :data:`REPORT_TITLE`, as :func:`build_context` fills it.
        total_tests: How many tests the report holds a result record for.
        no_tests: Whether the run collected nothing, rendered as an explicit statement.
        artifacts_disabled: Whether capture was disabled, so the template shows one banner
            instead of artifact links (AAP 0.4.7).
        counts: The nine per-outcome tallies, in :data:`foundry.reporting.record.OUTCOMES` order.
        rows: One row per test, sorted by node id.
        quarantined: The Quarantined section's entries, sorted by node id.
        panels: One failure panel per failing phase, ordered as ``build_panels`` orders them.
        errors: The run-level errors, sorted by worker id and message.
        assets: The inlined stylesheet and script.
    """

    run_id: str
    profile: str
    finished_at: str
    total_tests: int
    no_tests: bool
    artifacts_disabled: bool
    counts: tuple[OutcomeCount, ...]
    rows: tuple[ReportRow, ...]
    quarantined: tuple[QuarantinedRow, ...]
    panels: tuple[FailurePanel, ...]
    errors: tuple[RunErrorRow, ...]
    assets: AssetsBundle
    title: str = REPORT_TITLE


# --- Template discovery -----------------------------------------------------


def template_path() -> Path:
    """Return the path of the shipped report template.

    The single place the package-data path is computed, so ``pyproject.toml``'s package data, the
    wheel-listing test and the renderer all point at one file name.

    Returns:
        :data:`TEMPLATE_DIR` joined with :data:`TEMPLATE_NAME`.
    """
    return TEMPLATE_DIR / TEMPLATE_NAME


# --- Deriving the view model ------------------------------------------------


def _record_sort_key(record: ResultRecord) -> str:
    """Return the node id a result record is sorted by; total and worker-independent."""
    return record.nodeid


def _run_error_sort_key(error: RunError) -> tuple[str, str]:
    """Return the pair a run error is sorted by; ``recorded_at`` is not normalised, so not used."""
    return (error.worker_id, error.message)


def _outcome_css_class(outcome: Outcome) -> str:
    """Return the outcome badge's class, matching the stylesheet's ``.outcome-<value>`` rule."""
    return f"outcome-{outcome.value}"


def _count_css_class(outcome: Outcome) -> str:
    """Return the summary chip's class, matching the stylesheet's ``.summary-<value> .count``."""
    return f"summary-{outcome.value}"


def _format_duration(milliseconds: float) -> str:
    """Format a millisecond duration for a reader.

    Sub-second durations stay in milliseconds; anything longer switches to seconds so a slow test
    does not render as ``12500.0 ms``.
    """
    if milliseconds < _MILLISECONDS_PER_SECOND:
        return f"{milliseconds:.1f} ms"
    return f"{milliseconds / _MILLISECONDS_PER_SECOND:.2f} s"


def _row_for(record: ResultRecord) -> ReportRow:
    """Build one result record's row of the results table.

    Returns:
        The row, every value copied from the record and nothing derived from the clock or the host.
    """
    return ReportRow(
        nodeid=record.nodeid,
        outcome_value=record.outcome.value,
        css_class=_outcome_css_class(record.outcome),
        duration_display=_format_duration(record.duration_ms),
        profile=record.profile,
        markers=record.markers,
    )


def _counts(results: Sequence[ResultRecord]) -> tuple[OutcomeCount, ...]:
    """Tally the run's outcomes into the nine summary chips, in outcome declaration order.

    Every outcome is present, zero counts included, counted from the records rather than from a
    mutable accumulator.

    Returns:
        Nine :class:`OutcomeCount` entries in :data:`foundry.reporting.record.OUTCOMES` order.
    """
    return tuple(
        OutcomeCount(
            outcome=outcome,
            count=sum(1 for record in results if record.outcome is outcome),
            css_class=_count_css_class(outcome),
        )
        for outcome in OUTCOMES
    )


def _removal_note(outcome: Outcome) -> str:
    """Return the removal recommendation for a marked test that passed, else nothing (A11)."""
    return QUARANTINE_REMOVAL_NOTE if outcome is Outcome.QUARANTINE_PASSED else ""


def _quarantined_rows(results: Sequence[ResultRecord]) -> tuple[QuarantinedRow, ...]:
    """Collect the Quarantined section from the run's result records.

    The records are already sorted by node id, so the section inherits that order, and only the two
    outcomes of :data:`foundry.reporting.record.QUARANTINE_OUTCOMES` are selected. The badge's class
    comes from :func:`_outcome_css_class`, so the results table and the Quarantined section cannot
    disagree.

    Returns:
        One :class:`QuarantinedRow` per quarantined result, in node-id order; an empty tuple when
        nothing was quarantined, which makes the template omit the section.
    """
    return tuple(
        QuarantinedRow(
            nodeid=record.nodeid,
            outcome=record.outcome,
            css_class=_outcome_css_class(record.outcome),
            note=_removal_note(record.outcome),
        )
        for record in results
        if record.outcome in QUARANTINE_OUTCOMES
    )


def _run_id_for(results: Sequence[ResultRecord], override: str | None) -> str:
    """Return the run id the report's header carries.

    ``plugin.py`` knows the run id even when no record exists, which is the zero-tests case.

    Returns:
        The override when it is set, else the first record's run id, else :data:`_UNKNOWN`.
    """
    if override:
        return override
    if results:
        return results[0].run_id
    return _UNKNOWN


def _profile_for(results: Sequence[ResultRecord], override: str | None) -> str:
    """Return the profile name the report's header carries.

    ``override`` is what labels a zero-tests run, which has no record to read the profile from; an
    empty string is treated as absent.

    Returns:
        The override when it is set, else the first record's profile, else :data:`_UNKNOWN`.
    """
    if override:
        return override
    if results:
        return results[0].profile
    return _UNKNOWN


def _finished_at(results: Sequence[ResultRecord], override: str | None) -> str:
    """Return the run's finish time, the report's only date (AAP 0.4.6, 0.7.4).

    ``override`` is what ``plugin.py`` supplies when it knows the session's finish time, which is
    the zero-tests and run-error-only cases where no record carries one; an empty string is treated
    as absent. The clock is never read, so two renders of the same records stay byte-identical.

    Returns:
        The override when it is set, else the latest record ``finished_at`` as ISO 8601, else
        :data:`_UNKNOWN`.
    """
    if override:
        return override
    if results:
        return to_iso8601(max(record.finished_at for record in results))
    return _UNKNOWN


def _artifacts_disabled(results: Sequence[ResultRecord], explicit: bool | None) -> bool:
    """Return whether the report must state that artifact capture was disabled (AAP 0.4.7).

    ``explicit`` is the artifact writer's own ``disabled`` flag, which the caller holding it --
    ``plugin.py`` -- passes down rather than this package reading it out of the writer's state
    (AAP 0.4.8, 0.13.1).

    Without that signal the banner falls back to the records: a run that captured no artifact for
    any of its tests. The inference cannot tell a disabled writer from an all-skipped run, so it is
    only the fallback for a caller that supplies nothing, not the trigger AAP 0.4.7 names.
    """
    if explicit is not None:
        return explicit
    if not results:
        return False
    return not any(record.artifacts for record in results)


# --- Building the context ---------------------------------------------------


def _partition(records: Sequence[ResultElement]) -> tuple[list[ResultRecord], list[RunError]]:
    """Split the run's elements into its result records and its run errors, each sorted.

    The sequence is validated once, here, before any panel is built, so an element of neither kind
    is the published exception rather than a builtin escaping into the session (AAP 0.14.3).

    Args:
        records: The run's elements in any order.

    Returns:
        The result records sorted by node id, and the run errors sorted by worker id and message.

    Raises:
        ElementTypeError: An element is neither a :class:`ResultRecord` nor a :class:`RunError`.
            It subclasses ``TypeError`` and :class:`foundry.core.exceptions.FoundryError`, so a
            caller defect surfaces instead of being absorbed into a report.
    """
    results: list[ResultRecord] = []
    errors: list[RunError] = []
    for element in records:
        if isinstance(element, ResultRecord):
            results.append(element)
        elif isinstance(element, RunError):
            errors.append(element)
        else:
            raise ElementTypeError(
                "report.html holds only ResultRecord and RunError elements, "
                f"got {type(element).__name__}",
                hint="pass ResultRecord and RunError elements, as the run's recorder produces them",
            )
    results.sort(key=_record_sort_key)
    errors.sort(key=_run_error_sort_key)
    return results, errors


def build_context(
    records: Sequence[ResultElement],
    *,
    assets: AssetsBundle | None = None,
    artifacts_root: str | Path | None = None,
    report_dir: str | Path | None = None,
    redactor: Redactor | None = None,
    log_lines: int = DEFAULT_LOG_LINES,
    run_id: str | None = None,
    profile: str | None = None,
    finished_at: str | None = None,
    artifacts_disabled: bool | None = None,
) -> ReportContext:
    """Assemble the immutable view model one report is rendered from.

    Public so a test can assert what the template will see without rendering anything; the builder
    does no I/O of its own. The element sequence is validated once, here, so only a result record
    and a run error reach the document (AAP 0.14.3).

    ``run_id``, ``profile``, ``finished_at`` and ``artifacts_disabled`` are the values ``plugin.py``
    holds even when no record does: the run id and profile of a zero-tests run, the session's finish
    time, and the artifact writer's own disabled flag, the signal AAP 0.4.7 names for the report's
    single banner. Each is used when given, else derived from the records, with :data:`_UNKNOWN` as
    the last resort. ``report_dir`` is the directory the document will be written into, which every
    artifact href is relative to (assumption A5).

    Determinism (criterion 14): records are sorted by node id, the run errors by worker id and
    message, the counts follow the outcome declaration order. Redaction (AAP 0.3.2): a run error's
    message is scrubbed here and failure messages and log lines where the panels are built, while
    contract values are left alone, because redacting one would break ``foundry report compare``.

    Returns:
        The context, with every collection sorted and every free-text value redacted.

    Raises:
        ElementTypeError: An element is neither a result record nor a run error, as
            :func:`_partition` reports.
    """
    scrubber = default_redactor() if redactor is None else redactor
    bundle = DEFAULT_ASSETS if assets is None else assets
    results, run_errors = _partition(records)
    rows = tuple(_row_for(record) for record in results)
    return ReportContext(
        run_id=_run_id_for(results, run_id),
        profile=_profile_for(results, profile),
        finished_at=_finished_at(results, finished_at),
        title=REPORT_TITLE,
        total_tests=len(rows),
        no_tests=not rows,
        artifacts_disabled=_artifacts_disabled(results, artifacts_disabled),
        counts=_counts(results),
        rows=rows,
        quarantined=_quarantined_rows(results),
        panels=build_panels(
            results,
            artifacts_root=artifacts_root,
            report_dir=report_dir,
            redactor=scrubber,
            log_lines=log_lines,
        ),
        errors=tuple(
            RunErrorRow(worker_id=error.worker_id, message=scrubber.text(error.message))
            for error in run_errors
        ),
        assets=bundle,
    )


# --- Rendering --------------------------------------------------------------

_ISOLATION_ERRORS: Final[tuple[type[BaseException], ...]] = (
    TypeError,
    ValueError,
    KeyError,
    AttributeError,
    OSError,
    RecursionError,
    RuntimeError,
)
"""The families a rendering fault raises outside :class:`jinja2.TemplateError`.

AAP 0.14.3 makes a reporting fault one ``WARNING`` that changes no outcome and no exit code, so
:func:`_render_document` isolates exactly these -- not every exception: an interrupt or a
``SystemExit`` is a caller's control flow and still propagates.
"""

_ATTRIBUTE_REFERENCE: Final[re.Pattern[str]] = re.compile(
    r"""\s(?:href|src)\s*=\s*(?:"(?P<double>[^"]*)"|'(?P<single>[^']*)')""",
    re.IGNORECASE,
)
"""One quoted ``href`` or ``src`` attribute of the rendered document, in either quote style.

The scan reads attribute values rather than the whole text, so an escaped URL in a message cannot
match. The leading whitespace keeps ``data-href`` out.
"""

_PROTOCOL_RELATIVE_PREFIX: Final[str] = "//"
"""Prefix of a protocol-relative URL, which fetches from another host under the page's own scheme.

``//host/path`` names no scheme for the asset scan to recognise, yet it fetches elsewhere.
"""


def _search_dirs(source: Path) -> list[str]:
    """Return the template search path: ``source``'s directory first, then the package's.

    The package directory stays on the path so a template can include a partial shipped beside it.
    """
    directory = source.parent.resolve()
    if directory == TEMPLATE_DIR:
        return [str(TEMPLATE_DIR)]
    return [str(directory), str(TEMPLATE_DIR)]


def _first_external_attribute(document: str) -> str | None:
    """Return the first ``href`` or ``src`` in ``document`` that points outside it, or ``None``.

    Only the attributes the template controls are inspected, which keeps a failure message that
    legitimately quotes a URL from reading as an external reference.

    Returns:
        The offending value stripped of surrounding whitespace, or ``None`` when every attribute
        stays inside the document.
    """
    for match in _ATTRIBUTE_REFERENCE.finditer(document):
        value = (match.group("double") or match.group("single") or "").strip()
        if value.startswith(_PROTOCOL_RELATIVE_PREFIX) or contains_external_reference(value):
            return value
    return None


def _render_document(context: ReportContext, source: Path, redactor: Redactor) -> str | None:
    """Render ``context`` through ``source``, isolating every rendering fault into one warning.

    Jinja2 is imported here rather than at module scope, because the library's import cost must not
    reach the plugin's startup budget (implicit requirement I5). Marking a value safe is the
    template's job: its ``safe`` filter keeps the two module-owned asset constants unescaped, while
    every record value stays escaped by ``autoescape=True``. The finished document is scanned for a
    ``href`` or ``src`` value that points outside it, so a template regression that reintroduces an
    external asset takes the fallback instead of shipping a report that fetches something.

    Returns:
        The rendered document, or ``None`` when the template is missing, cannot be rendered,
        or pulls in a reference from outside itself; the caller then skips ``report.html``,
        keeps ``results.json``, writes no ``junit.xml`` and leaves the exit code alone
        (AAP 0.4.7).
    """
    import jinja2

    # Jinja2's own template faults join the isolated families here, where Jinja2 is in scope, so one
    # handler carries every rendering fault to the same fallback; the annotation is the shape mypy
    # --strict accepts for an exception tuple assembled at run time.
    faults: tuple[type[BaseException], ...] = (jinja2.TemplateError, *_ISOLATION_ERRORS)
    try:
        environment = jinja2.Environment(
            loader=jinja2.FileSystemLoader(_search_dirs(source)),
            autoescape=True,
            trim_blocks=True,
            lstrip_blocks=True,
            keep_trailing_newline=True,
            undefined=jinja2.StrictUndefined,
        )
        document = environment.get_template(source.name).render(ctx=context)
        reference = _first_external_attribute(document)
        if reference is not None:
            emit_html_fallback(
                f"report.html not written: rendering {source.name or source} produced the "
                f"external reference {reference!r}",
                redactor=redactor,
            )
            return None
        return document
    except faults as exc:
        emit_html_fallback(
            f"report.html not written: rendering {source.name or source} failed: "
            f"{type(exc).__name__}: {exc}",
            redactor=redactor,
        )
        return None


def render(
    records: Sequence[ResultElement],
    template: str | Path | None = None,
    assets: AssetsBundle | None = None,
    *,
    artifacts_root: str | Path | None = None,
    report_dir: str | Path | None = None,
    redactor: Redactor | None = None,
    log_lines: int = DEFAULT_LOG_LINES,
    run_id: str | None = None,
    profile: str | None = None,
    finished_at: str | None = None,
    artifacts_disabled: bool | None = None,
) -> str | None:
    """Render a run's result elements as the complete text of ``report.html``.

    The document is returned, never written: ``renderers.py`` hands it to
    :func:`foundry.reporting.store.write_text`, this package's only file writer (AAP 0.4.8). The
    header's run-level values and the artifact writer's flag are the caller's to supply, on the
    terms :func:`build_context` documents.

    The asset bundle is validated before anything is rendered, so a bundle that reaches outside the
    document raises rather than shipping a report that cannot open offline. The finished document is
    scanned too, but at attribute level: autoescape escapes a failure message's quotes, so a URL a
    message legitimately quotes cannot match an ``href`` or ``src``.

    Returns:
        The rendered document, or ``None`` when the template is missing, cannot be rendered, or
        pulls in a reference from outside itself -- the caller then writes no ``report.html``,
        keeps ``results.json``, writes no ``junit.xml`` and leaves the exit code alone (AAP 0.4.7).

    Raises:
        FoundryError: The asset bundle is not self-contained, a programmer error rather than a
            run fault.
        ElementTypeError: An element is neither a result record nor a run error, which
            :func:`build_context` reports before anything is rendered.
    """
    scrubber = default_redactor() if redactor is None else redactor
    bundle = DEFAULT_ASSETS if assets is None else assets
    assert_self_contained(bundle.css + bundle.js)
    context = build_context(
        records,
        assets=bundle,
        artifacts_root=artifacts_root,
        report_dir=report_dir,
        redactor=scrubber,
        log_lines=log_lines,
        run_id=run_id,
        profile=profile,
        finished_at=finished_at,
        artifacts_disabled=artifacts_disabled,
    )
    source = template_path() if template is None else Path(template)
    return _render_document(context, source, scrubber)
