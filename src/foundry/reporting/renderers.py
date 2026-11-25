"""The report formats a run writes, and the one orchestration that writes them.

``results.json``, ``report.html`` and ``junit.xml`` are the three fixed names AAP 0.4.6 publishes,
written in that order to the invocation directory (assumption A5). :data:`RENDERERS` is the one
import-time table mapping a format name to its file name and writer (AAP 0.4.8) and
:func:`write_all` is the single call a run makes. The switches are independent (AAP 0.4.5), except
the documented fallback: a template that cannot be rendered skips ``junit.xml`` too, with the exit
code untouched (AAP 0.4.7). Jinja2 is imported on the render path only (implicit requirement I5).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, TypeAlias

from foundry.core.exceptions import FoundryError
from foundry.obs.redaction import Redactor
from foundry.reporting.html import DEFAULT_LOG_LINES
from foundry.reporting.html import render as render_html
from foundry.reporting.junit import write as write_junit
from foundry.reporting.record import ResultElement
from foundry.reporting.store import write as write_results
from foundry.reporting.store import write_text

__all__: Final[tuple[str, ...]] = (
    "HTML_FILENAME",
    "JUNIT_FILENAME",
    "RENDERERS",
    "REPORT_FORMATS",
    "RESULTS_FILENAME",
    "RendererSpec",
    "ReportPaths",
    "Writer",
    "registered_formats",
    "renderer_for",
    "write_all",
    "write_html_report",
)

# --- The published report surface ------------------------------------------

RESULTS_FILENAME: Final[str] = "results.json"
"""File name of the run's result document, in the invocation directory (AAP 0.4.6)."""

HTML_FILENAME: Final[str] = "report.html"
"""File name of the self-contained HTML report, beside the result document."""

JUNIT_FILENAME: Final[str] = "junit.xml"
"""File name of the JUnit XML document, beside the result document."""

REPORT_FORMATS: Final[tuple[str, ...]] = ("json", "html", "junit")
"""The three format names, in the published write order, which :func:`write_all` follows.

A format name is the stem an operator asks for, and deliberately not the file name: ``json`` is
written to ``results.json`` because that is the name the ecosystem and ``foundry report compare``
read.
"""

_JSON_FORMAT: Final[str] = "json"
"""Format name of the result document."""

_HTML_FORMAT: Final[str] = "html"
"""Format name of the HTML report."""

_JUNIT_FORMAT: Final[str] = "junit"
"""Format name of the JUnit XML document."""

Writer: TypeAlias = Callable[..., Path | None]
"""A report writer: ``(records, path, **options)`` to the file written, or ``None``.

A writer returns the path it wrote -- normally the one it was given -- or ``None`` when it wrote
nothing, which covers both a filesystem refusal already reported at ``WARNING`` and the documented
template fallback. The keyword options are format-specific, so ``...`` is the honest signature and
each call site in :func:`write_all` passes what its writer documents.
"""


@dataclass(frozen=True, slots=True)
class RendererSpec:
    """One report format: the name it is asked for by, the file it lands in, and who writes it.

    Frozen so the registry cannot be edited in place: a caller that wants different content passes
    a different path instead of rewriting a writer.

    Attributes:
        name: The format name, one of :data:`REPORT_FORMATS`.
        filename: The fixed file name the format is written under, without a directory.
        writer: The callable that produces the document and writes it.
        order: The position the format is written at, ascending: ``json``, ``html``, ``junit``.
    """

    name: str
    filename: str
    writer: Writer
    order: int


def write_html_report(
    records: Sequence[ResultElement],
    path: str | Path,
    *,
    redactor: Redactor | None = None,
    artifacts_root: str | Path | None = None,
    report_dir: str | Path | None = None,
    log_lines: int = DEFAULT_LOG_LINES,
    run_id: str | None = None,
    profile: str | None = None,
    finished_at: str | None = None,
    artifacts_disabled: bool | None = None,
) -> Path | None:
    """Render the HTML report and write it, or report the documented fallback by returning ``None``.

    :func:`foundry.reporting.html.render` builds the document and returns it; this adapter hands it
    to :func:`foundry.reporting.store.write_text`, which is this package's only writer (AAP 0.4.8).

    Args:
        records: The run's elements: result records, run errors, or both in any order.
        path: The file to write, normally ``report.html`` in the invocation directory.
        redactor: The run's redaction engine. ``None`` uses the built-in deny-lists.
        artifacts_root: The run directory the records' artifact paths are relative to, or ``None``
            when the artifacts directory was not writable.
        report_dir: The directory the report is written to, which the panels' links are relative
            to; ``None`` uses ``path``'s own directory.
        log_lines: How many captured log lines each failure panel carries.
        run_id: The run id to label the report with, or ``None`` to take it from the records.
        profile: The profile to label the report with, on the same terms as ``run_id``.
        finished_at: The run's finish time, ISO 8601 UTC, on the same terms as ``run_id``.
        artifacts_disabled: The artifact writer's own ``disabled`` flag, which turns the panels'
            artifact links into the report's single banner (AAP 0.4.7).

    Returns:
        The path written, or ``None`` when nothing was written: the template could not be rendered
        (the fallback of AAP 0.4.7) or the filesystem refused the write. Either way the failure is
        reported once, never twice, and the run's exit code is untouched.

    Raises:
        FoundryError: The default asset bundle is not self-contained, which is a defect in
            :mod:`foundry.reporting.assets` rather than a fault of the run.
        ElementTypeError: An element is neither a result record nor a run error.
    """
    target = Path(path)
    directory = target.parent if report_dir is None else report_dir
    document = render_html(
        records,
        None,
        None,
        artifacts_root=artifacts_root,
        report_dir=directory,
        redactor=redactor,
        log_lines=log_lines,
        run_id=run_id,
        profile=profile,
        finished_at=finished_at,
        artifacts_disabled=artifacts_disabled,
    )
    if document is None:
        return None
    return write_text(document, target)


# --- The registration table ------------------------------------------------

# The format -> renderer table: populated once, here, at import, and never mutated afterwards. It is
# one of the only two import-time registration tables the tree permits, the other being
# foundry.data.registry (AAP 0.4.8); a new report format is an additive source change made in this
# literal (AAP 0.4.9), never a runtime registration.
RENDERERS: Final[dict[str, RendererSpec]] = {
    _JSON_FORMAT: RendererSpec(
        name=_JSON_FORMAT,
        filename=RESULTS_FILENAME,
        writer=write_results,
        order=0,
    ),
    _HTML_FORMAT: RendererSpec(
        name=_HTML_FORMAT,
        filename=HTML_FILENAME,
        writer=write_html_report,
        order=1,
    ),
    _JUNIT_FORMAT: RendererSpec(
        name=_JUNIT_FORMAT,
        filename=JUNIT_FILENAME,
        writer=write_junit,
        order=2,
    ),
}
"""Every report format Foundry writes, keyed by the name it is asked for by.

The values are frozen and the keys are the three names of :data:`REPORT_FORMATS`, in write order;
nothing in the tree assigns to the table after this literal runs.
"""

_FORMATS_HINT: Final[str] = f"report formats are: {', '.join(REPORT_FORMATS)}"
"""Next step offered with an unknown-format failure, naming every format that is published."""


def _write_order(spec: RendererSpec) -> int:
    """Return the position a renderer writes at: the key :func:`registered_formats` sorts by."""
    return spec.order


def renderer_for(name: str) -> RendererSpec:
    """Return the renderer registered for a report format name.

    Args:
        name: The format name, normally one of :data:`REPORT_FORMATS`.

    Returns:
        The format's frozen :class:`RendererSpec`, carrying its file name and its writer.

    Raises:
        FoundryError: ``name`` is not a registered format. The message names the format asked for
            and the hint lists every published format, so a mistyped format reads as a mistake with
            a correction rather than as a ``KeyError`` traceback.
    """
    try:
        return RENDERERS[name]
    except KeyError:
        raise FoundryError(f"unknown report format: {name}", hint=_FORMATS_HINT) from None


def registered_formats() -> tuple[str, ...]:
    """Return the registered format names in the order their files are written.

    Sorted by each spec's ``order`` rather than relying on the dict's insertion order, so the answer
    is the published write order however the literal is arranged.

    Returns:
        The format names, ``("json", "html", "junit")``.
    """
    return tuple(spec.name for spec in sorted(RENDERERS.values(), key=_write_order))


# --- The set of files a run wrote ------------------------------------------


def _text_or_none(path: Path | None) -> str | None:
    """Return ``path`` as text, or ``None`` when there is no path."""
    return None if path is None else str(path)


@dataclass(frozen=True, slots=True)
class ReportPaths:
    """What a run's report consists of: one optional path per published format.

    ``None`` is a real answer rather than a missing value: it means the format was switched off by
    the resolved configuration, or its writer wrote nothing -- a filesystem refusal the store has
    already reported at ``WARNING``, or the HTML template fallback. Freezing the set lets the caller
    report exactly what a run left behind without being able to alter it.

    Attributes:
        results: The ``results.json`` written, or ``None``.
        html: The ``report.html`` written, or ``None``.
        junit: The ``junit.xml`` written, or ``None``.
    """

    results: Path | None
    html: Path | None
    junit: Path | None

    def written(self) -> tuple[Path, ...]:
        """Return the files written, in the published write order, skipping the absent ones.

        Returns:
            Zero to three paths: ``results.json``, then ``report.html``, then ``junit.xml``.
        """
        return tuple(path for path in (self.results, self.html, self.junit) if path is not None)

    def as_dict(self) -> dict[str, str | None]:
        """Return the three formats' paths as text, keyed by format name.

        The shape a terminal summary or a machine-readable caller report needs: every published
        format is present as a key, and a format that was not written carries ``None``.

        Returns:
            A mapping of ``"json"``, ``"html"`` and ``"junit"`` to the path written or ``None``.
        """
        return {
            _JSON_FORMAT: _text_or_none(self.results),
            _HTML_FORMAT: _text_or_none(self.html),
            _JUNIT_FORMAT: _text_or_none(self.junit),
        }


# --- Writing the set -------------------------------------------------------


def write_all(
    records: Sequence[ResultElement],
    output_dir: str | Path,
    *,
    redactor: Redactor | None = None,
    artifacts_root: str | Path | None = None,
    log_lines: int = DEFAULT_LOG_LINES,
    run_id: str | None = None,
    profile: str | None = None,
    finished_at: str | None = None,
    artifacts_disabled: bool | None = None,
    write_json: bool = True,
    write_html: bool = True,
    write_junit: bool = True,
) -> ReportPaths:
    """Write a run's ``results.json``, ``report.html`` and ``junit.xml``, in that order.

    The three fixed names of AAP 0.4.6 are resolved against ``output_dir``. Each switch is honoured
    on its own (AAP 0.4.5): the HTML renderer consumes ``records`` and never the document written
    before it, so ``junit.xml`` is written unless ``write_junit`` is off or the HTML step was
    requested and fell back -- the fallback of AAP 0.4.7, which leaves the exit code untouched.
    Whether to write at all belongs to the caller: a run whose tests were all deselected writes no
    report file (AAP 0.4.6).

    Args:
        records: The run's elements: result records, run errors, or both in any order.
        output_dir: The directory the three files are written into, normally the invocation dir.
        redactor: The run's redaction engine. ``None`` uses the built-in deny-lists, so redaction
            cannot be skipped by omitting it.
        artifacts_root: The run directory the records' artifact paths are relative to, or ``None``
            when the artifacts directory was not writable.
        log_lines: How many captured log lines each HTML failure panel carries.
        run_id: The run id to label the report with, or ``None`` to take it from the records.
        profile: The profile to label the report with, on the same terms as ``run_id``.
        finished_at: The run's finish time, ISO 8601 UTC, on the same terms as ``run_id``.
        artifacts_disabled: The artifact writer's own ``disabled`` flag, which replaces the
            artifact links with the report's single banner (AAP 0.4.7).
        write_json: Whether to write ``results.json``, from the resolved ``reporting.json``.
        write_html: Whether to write ``report.html``, from the resolved ``reporting.html``.
        write_junit: Whether to write ``junit.xml``, unless the HTML step fell back (AAP 0.4.7).

    Returns:
        The files written, each carried only when it was actually written; a switch that was off, an
        isolated refusal and the HTML fallback each leave their entry ``None``.

    Raises:
        ElementTypeError: An element is neither a result record nor a run error.
    """
    base = Path(output_dir)
    results = (
        renderer_for(_JSON_FORMAT).writer(records, base / RESULTS_FILENAME, redactor=redactor)
        if write_json
        else None
    )
    html_requested = write_html
    html = (
        renderer_for(_HTML_FORMAT).writer(
            records,
            base / HTML_FILENAME,
            redactor=redactor,
            artifacts_root=artifacts_root,
            log_lines=log_lines,
            run_id=run_id,
            profile=profile,
            finished_at=finished_at,
            artifacts_disabled=artifacts_disabled,
        )
        if html_requested
        else None
    )
    # An HTML step that was requested and wrote nothing is the template fallback, and AAP 0.4.7 then
    # produces no junit.xml either; a step that was never requested leaves JUnit unaffected, and so
    # does a result document that was not written, because this writer reads the records too.
    html_fell_back = html_requested and html is None
    junit = (
        renderer_for(_JUNIT_FORMAT).writer(records, base / JUNIT_FILENAME, redactor=redactor)
        if write_junit and not html_fell_back
        else None
    )
    return ReportPaths(results=results, html=html, junit=junit)
