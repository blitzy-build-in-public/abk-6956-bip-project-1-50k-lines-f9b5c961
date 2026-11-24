"""The renderer of Foundry's self-contained, offline-openable ``report.html``.

:func:`build_context` assembles the sorted, immutable :class:`ReportContext` the template reads and
:func:`render` renders it through Jinja2 with ``autoescape=True``, returning text and writing no
file. AAP 0.4.7 (the fallback), 0.4.8, 0.7.4, 0.14.1, 0.14.2.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Final

from foundry.core.ids import to_iso8601
from foundry.obs.events import emit_html_fallback
from foundry.obs.redaction import Redactor, default_redactor
from foundry.reporting.assets import DEFAULT_ASSETS, AssetsBundle, assert_self_contained
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

Resolved from this module's own location, so one path serves a wheel, an editable install and a
source checkout.
"""

TEMPLATE_NAME: Final[str] = "report.html.j2"
"""File name of the single report template inside :data:`TEMPLATE_DIR`."""

REPORT_TITLE: Final[str] = "foundry report"
"""The document's title: English-only, light theme, no run id and no timestamp (assumption A14)."""

DEFAULT_LOG_LINES: Final[int] = DEFAULT_LOG_TAIL_LINES
"""Captured log lines each failure panel carries.

Taken from :data:`foundry.reporting.panels.DEFAULT_LOG_TAIL_LINES`, the default of the
``logging.capture_lines`` key, which this package may not read itself (AAP 0.4.8).
"""

QUARANTINE_REMOVAL_NOTE: Final[str] = (
    "marked foundry_quarantine and passed: remove the marker so that a later failure of this test "
    "fails the run again"
)
"""Recommendation rendered beside a ``quarantine_passed`` entry (assumption A11)."""

_UNKNOWN: Final[str] = "unknown"
"""What ``run_id`` and ``profile`` fall back to when no record exists and no override was given.

A fabricated run id could collide with a real one, so this literal is the last resort.
"""

_MILLISECONDS_PER_SECOND: Final[float] = 1000.0
"""Milliseconds in a second, the unit boundary :func:`_format_duration` switches at."""

# --- The view model the template renders ------------------------------------


@dataclass(frozen=True, slots=True)
class OutcomeCount:
    """One outcome's tally in the report's summary strip.

    All nine outcomes are present in every report, zero counts included, so the strip is the same
    width whatever a run produced (criterion 14).

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
    took, which profile it ran under and which markers it held -- plus the names of the artifacts
    captured for it. The artifact column is file names only: a failure panel owns the links.

    Attributes:
        nodeid: The test's pytest node id, unsanitised, exactly as ``results.json`` records it.
        outcome: The test's final outcome.
        outcome_value: The outcome's bare value, so a template can render it without the enum.
        css_class: The class the stylesheet styles the outcome badge by, such as ``outcome-passed``.
        duration_ms: Wall-clock milliseconds across all attempts, the value criterion 14 normalises.
        duration_display: The same duration formatted for a reader, such as ``12.4 ms``.
        profile: The profile the test ran under.
        markers: The item's marker names, sorted and de-duplicated by the record.
        artifacts: The captured files' names, sorted; never paths, so a row cannot leak the artifact
            directory layout and cannot link one.
    """

    nodeid: str
    outcome: Outcome
    outcome_value: str
    css_class: str
    duration_ms: float
    duration_display: str
    profile: str
    markers: tuple[str, ...]
    artifacts: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class QuarantinedRow:
    """One entry of the report's Quarantined section.

    A quarantined test is a known-flaky one: its failure is listed so a person sees it while the run
    still succeeds in ``report`` mode (AAP 0.4.7); a ``quarantine_passed`` entry carries the removal
    recommendation instead, because a marked test that passes can lose its marker (assumption A11).

    Attributes:
        nodeid: The test's pytest node id, unsanitised.
        outcome: ``quarantined_failed`` or ``quarantine_passed``.
        note: :data:`QUARANTINE_REMOVAL_NOTE` for a marked test that passed, else an empty string.
    """

    nodeid: str
    outcome: Outcome
    note: str = ""


@dataclass(frozen=True, slots=True)
class RunErrorRow:
    """One entry of the report's Errors section: a failure of the run itself.

    It carries the worker the error came from and its message, and deliberately no timestamp: the
    determinism criterion normalises four fields and not this one (AAP 0.7.4).

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
        finished_at: The latest ``finished_at`` of the run's result records, ISO 8601 UTC, or an
            empty string for a run that recorded no result. The only date the report renders.
        title: The document title, :data:`REPORT_TITLE` unless a caller overrides it.
        total_tests: How many tests the report holds a result record for.
        no_tests: Whether the run collected nothing, which the template renders as an explicit
            statement rather than an empty table.
        artifacts_disabled: Whether the run had tests but captured no artifact for any of them, in
            which case the template shows one banner instead of artifact links.
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
    does not render as ``12500.0 ms``. Only the formatted copy changes -- the row keeps the reported
    ``duration_ms``, the field criterion 14 normalises.
    """
    if milliseconds < _MILLISECONDS_PER_SECOND:
        return f"{milliseconds:.1f} ms"
    return f"{milliseconds / _MILLISECONDS_PER_SECOND:.2f} s"


def _artifact_names(record: ResultRecord) -> tuple[str, ...]:
    """Return the captured files' names, sorted: a row names what was captured, panels link it.

    Only the last path component is kept, so no directory layout reaches the report (AAP 0.7.4).
    """
    return tuple(sorted({PurePosixPath(artifact.path).name for artifact in record.artifacts}))


def _row_for(record: ResultRecord) -> ReportRow:
    """Build one result record's row of the results table.

    Returns:
        The row, with every value copied from the record and nothing derived from the clock, the
        host or the worker that produced it.
    """
    return ReportRow(
        nodeid=record.nodeid,
        outcome=record.outcome,
        outcome_value=record.outcome.value,
        css_class=_outcome_css_class(record.outcome),
        duration_ms=record.duration_ms,
        duration_display=_format_duration(record.duration_ms),
        profile=record.profile,
        markers=record.markers,
        artifacts=_artifact_names(record),
    )


def _counts(results: Sequence[ResultRecord]) -> tuple[OutcomeCount, ...]:
    """Tally the run's outcomes into the nine summary chips, in outcome declaration order.

    Every outcome is present, zero counts included, so the strip is the same width whatever a run
    produced. The tallies are counted from the records rather than from a mutable accumulator.

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


def _quarantined_rows(rows: Sequence[ReportRow]) -> tuple[QuarantinedRow, ...]:
    """Collect the Quarantined section from the run's rows.

    The rows are already sorted by node id, so the section inherits that order, and only the two
    outcomes of :data:`foundry.reporting.record.QUARANTINE_OUTCOMES` are selected.

    Returns:
        One :class:`QuarantinedRow` per quarantined result, in node-id order; empty when nothing was
        quarantined, which is what makes the template omit the section altogether.
    """
    return tuple(
        QuarantinedRow(nodeid=row.nodeid, outcome=row.outcome, note=_removal_note(row.outcome))
        for row in rows
        if row.outcome in QUARANTINE_OUTCOMES
    )


def _run_id_for(results: Sequence[ResultRecord], override: str | None) -> str:
    """Return the run id the report's header carries.

    ``plugin.py`` knows the run id even when no record exists, which is the zero-tests case the
    override exists for.

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


def _finished_at(results: Sequence[ResultRecord]) -> str:
    """Return the latest record ``finished_at`` as the report's only date, or ``""`` for none.

    ``started_at``, the run id's timestamp and the clock are never rendered (criterion 14).
    """
    if not results:
        return ""
    return to_iso8601(max(record.finished_at for record in results))


def _artifacts_disabled(results: Sequence[ResultRecord]) -> bool:
    """Return whether the report must state that artifact capture was disabled.

    Derived from the records, which is the state AAP 0.4.7 describes when the artifacts directory is
    not writable. A run that collected nothing had nothing to capture, so it is not that state.
    """
    if not results:
        return False
    return not any(record.artifacts for record in results)


# --- Building the context ---------------------------------------------------


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
) -> ReportContext:
    """Assemble the immutable view model one report is rendered from.

    Public so a test can assert what the template will see without rendering anything. The builder
    performs no I/O of its own: reading the captured log tails is
    :func:`foundry.reporting.panels.build_panels`'s work, and it isolates every fault it meets.

    Determinism (AAP 0.13.1, criterion 14): the result records are sorted by node id, the run errors
    by worker id and message, the counts follow the outcome declaration order, and every other
    collection is derived from those. Redaction (AAP 0.3.2): a run error's message is scrubbed here
    and failure messages and log lines where the panels are built, while contract values are left
    alone, because redacting one would break ``foundry report compare``.

    Returns:
        The context, with every collection sorted and every free-text value redacted.
    """
    scrubber = default_redactor() if redactor is None else redactor
    bundle = DEFAULT_ASSETS if assets is None else assets
    results = sorted(
        (element for element in records if isinstance(element, ResultRecord)),
        key=_record_sort_key,
    )
    run_errors = sorted(
        (element for element in records if isinstance(element, RunError)),
        key=_run_error_sort_key,
    )
    rows = tuple(_row_for(record) for record in results)
    return ReportContext(
        run_id=_run_id_for(results, run_id),
        profile=_profile_for(results, profile),
        finished_at=_finished_at(results),
        title=REPORT_TITLE,
        total_tests=len(rows),
        no_tests=not rows,
        artifacts_disabled=_artifacts_disabled(results),
        counts=_counts(results),
        rows=rows,
        quarantined=_quarantined_rows(rows),
        panels=build_panels(
            records,
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


def _search_dirs(source: Path) -> list[str]:
    """Return the template search path: ``source``'s directory first, then the package's.

    The package directory stays on the path so a template can ``{% include %}`` a partial that
    ships beside it.
    """
    directory = source.parent.resolve()
    if directory == TEMPLATE_DIR:
        return [str(TEMPLATE_DIR)]
    return [str(directory), str(TEMPLATE_DIR)]


def _render_document(context: ReportContext, source: Path, redactor: Redactor) -> str | None:
    """Render ``context`` through ``source``, isolating every template fault into a warning.

    Jinja2 and Markup are imported here rather than at module scope, because the library's import
    cost must not reach the plugin's startup budget (implicit requirement I5). The asset strings are
    marked because they are module-owned constants and not run data, so the ``<style>`` and
    ``<script>`` payloads survive whatever the template does with them, while every value that came
    from a record stays escaped by ``autoescape=True``.

    Returns:
        The rendered document, or ``None`` when the template is missing or cannot be rendered. The
        caller then skips ``report.html``, keeps ``results.json`` and writes no ``junit.xml``
        (AAP 0.4.7); the run's exit code is untouched.
    """
    import jinja2
    from markupsafe import Markup

    try:
        environment = jinja2.Environment(
            loader=jinja2.FileSystemLoader(_search_dirs(source)),
            autoescape=True,
            trim_blocks=True,
            lstrip_blocks=True,
            keep_trailing_newline=True,
            undefined=jinja2.StrictUndefined,
        )
        trusted = replace(
            context,
            assets=AssetsBundle(
                css=Markup(context.assets.css),
                js=Markup(context.assets.js),
                version=context.assets.version,
            ),
        )
        return environment.get_template(source.name).render(ctx=trusted)
    except (jinja2.TemplateError, OSError) as exc:
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
) -> str | None:
    """Render a run's result elements as the complete text of ``report.html``.

    The document is returned, never written: ``renderers.py`` hands it to
    :func:`foundry.reporting.store.write_text`, this package's only file writer (AAP 0.4.8).

    The asset bundle is validated before anything is rendered, so a bundle that reaches outside the
    document raises now rather than shipping a report that cannot open offline; the assertion is
    deliberately not applied to the finished document, because a failure message may legitimately
    quote a URL.

    Returns:
        The rendered document, or ``None`` when the template is missing or cannot be rendered -- in
        which case the caller writes no ``report.html``, keeps ``results.json``, writes no
        ``junit.xml``, and leaves the exit code alone (AAP 0.4.7).

    Raises:
        FoundryError: The asset bundle is not self-contained, which is a programmer error in
            :mod:`foundry.reporting.assets` rather than a fault of the run.
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
    )
    source = template_path() if template is None else Path(template)
    return _render_document(context, source, scrubber)
