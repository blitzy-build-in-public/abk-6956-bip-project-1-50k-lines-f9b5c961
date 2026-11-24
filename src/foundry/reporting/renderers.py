"""The report formats a run writes, and the one orchestration that writes them.

Three files make up a run's report, with the fixed names AAP 0.4.6 publishes: ``results.json``,
``report.html`` and ``junit.xml``, written to the invocation directory (assumption A5). This module
is the one place that maps a format name to the file name it is written under and to the writer that
produces it, and :func:`write_all` is the single call a run makes to produce the set. Nothing else
in the tree decides a report's file name, or the order in which the three are written.

Order of writes
    ``results.json`` first, then ``report.html``, then ``junit.xml``. The three switches are
    independent, as AAP 0.4.5 publishes them: ``reporting.json`` decides whether the result
    document is written and ``reporting.html`` whether the report is, so a run with
    ``reporting.json`` off still writes ``report.html``, because every writer here consumes the
    records it is given and never the file written before it. The one dependency the state table
    does add is the documented HTML fallback: when ``write_html`` asks for ``report.html`` and the
    template cannot be rendered, ``report.html`` is skipped and ``junit.xml`` is not produced
    either, with the run's exit code untouched (AAP 0.4.7). Writing in this order is what keeps
    that fallback reachable, and turning ``write_html`` off is not a fallback.

The registration table
    :data:`RENDERERS` is populated once, at import, from the literals below, and is never mutated
    afterwards: it is one of the only two import-time registration tables the tree permits, the
    other being :mod:`foundry.data.registry` (AAP 0.4.8). Its entries are frozen, so a spec cannot
    be edited in place either. A new report format is an additive source change made here
    (AAP 0.4.9), not a runtime registration, which is why this module publishes no ``register()``.

Failure isolation
    Writing a report is plumbing, and plumbing never changes a test's outcome or a run's exit code
    (AAP 0.14.3). Every writer this module calls has already isolated a filesystem or encoding
    refusal into one ``WARNING`` on the ``foundry`` logger and a ``None`` return, so nothing here
    catches ``OSError`` and :func:`write_all` reports exactly what was written. What is *not*
    absorbed is a caller defect: an element that is neither a result record nor a run error raises
    :class:`foundry.core.exceptions.ElementTypeError` from the writer that met it, because that is
    a bug in the caller rather than a fault of the run.

Whether to write at all
    The decision belongs to the caller, not here. A run whose tests were all deselected exits ``4``
    and writes no report file (AAP 0.4.6), and :mod:`foundry.plugin` is what knows the session's
    state; this module only writes, on request, into the directory it is given.

Writes and import direction
    This module opens no file and creates no directory: every write goes through
    :func:`foundry.reporting.store.write` or :func:`foundry.reporting.store.write_text`, which are
    the package's only writers (AAP 0.4.8). It imports ``foundry.core``, ``foundry.obs`` and its own
    package's modules, never a capability or interface package -- in particular it never reads
    configuration, so the switches ``write_json``, ``write_html`` and ``write_junit`` arrive from
    the caller's resolved configuration. Importing it defines names and builds the one table: no
    I/O, no environment read, no logger configuration. Jinja2 in particular is imported inside
    :func:`foundry.reporting.html.render`, on the render path only, and so stays off the plugin's
    startup budget (implicit requirement I5).
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
"""The three format names, in the order their files are written.

The order is the published write order: it is what :func:`registered_formats` returns and what
:func:`write_all` follows. A format name is the stem an operator asks for, and it is deliberately
not the file name -- ``json`` is written to ``results.json`` because that is the name the ecosystem
and ``foundry report compare`` read.
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
template fallback. The extra keyword options are format-specific: only the HTML writer accepts a
redactor, an artifacts root, a log-line count, a run id and a profile. ``...`` is therefore the
honest signature, and each call site in :func:`write_all` passes what its writer documents.
"""


@dataclass(frozen=True, slots=True)
class RendererSpec:
    """One report format: the name it is asked for by, the file it lands in, and who writes it.

    Frozen so the table's entries cannot be edited in place: the registry is published data, and a
    caller that wants different content passes a different path instead of rewriting a writer.

    Attributes:
        name: The format name, one of :data:`REPORT_FORMATS`.
        filename: The fixed file name the format is written under, without a directory.
        writer: The callable that produces the document and writes it.
        order: The position the format is written at, ascending: ``0`` for ``json``, ``1`` for
            ``html``, ``2`` for ``junit``.
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
) -> Path | None:
    """Render the HTML report and write it, or report the documented fallback by returning ``None``.

    :func:`foundry.reporting.html.render` builds the document and returns it; this adapter hands it
    to :func:`foundry.reporting.store.write_text`, which is this package's only writer (AAP 0.4.8).
    The document is rendered through the default template and the default asset bundle, both of
    which the HTML module owns.

    Args:
        records: The run's elements: result records, run errors, or both in any order.
        path: The file to write, normally ``report.html`` in the invocation directory.
        redactor: The run's redaction engine, built from ``secrets.redact_keys``. ``None`` uses the
            built-in header and query deny-lists.
        artifacts_root: The run directory the records' artifact paths are relative to, or ``None``
            when the artifacts directory was not writable.
        report_dir: The directory the report is written to, which the failure panels' links are
            relative to. ``None`` uses ``path``'s own directory, so the links stay correct wherever
            the report is written.
        log_lines: How many captured log lines each failure panel carries.
        run_id: The run id to label the report with, or ``None`` to take it from the records.
        profile: The profile to label the report with, on the same terms as ``run_id``.

    Returns:
        The path written, or ``None`` when nothing was written: the template could not be rendered
        (the fallback of AAP 0.4.7, already reported at ``WARNING`` by the HTML module) or the
        filesystem refused the write (reported at ``WARNING`` by the store). Either way the failure
        is reported once, never twice, and the run's exit code is untouched.

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

The values are frozen and the keys are the three names of :data:`REPORT_FORMATS`, in write order.
The dict itself is a plain module-level table rather than a mapping proxy because the published
shape is something a caller iterates; nothing in the tree assigns to it after this literal runs.
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
            a correction rather than as a ``KeyError`` traceback (AAP 0.4.3).
    """
    try:
        return RENDERERS[name]
    except KeyError:
        raise FoundryError(f"unknown report format: {name}", hint=_FORMATS_HINT) from None


def registered_formats() -> tuple[str, ...]:
    """Return the registered format names in the order their files are written.

    Derived from :data:`RENDERERS` and sorted by each spec's ``order`` rather than relying on the
    dict's insertion order, so the answer is the published write order even if the literal is ever
    reordered. It equals :data:`REPORT_FORMATS`.

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
    write_json: bool = True,
    write_html: bool = True,
    write_junit: bool = True,
) -> ReportPaths:
    """Write a run's ``results.json``, ``report.html`` and ``junit.xml``, in that order.

    The three fixed names of AAP 0.4.6 are resolved against ``output_dir``. A caller relocating the
    document -- ``--foundry-json-report PATH`` -- passes that path's directory, so the other two
    follow beside it (assumption A5/A6); a caller that wants the document somewhere its siblings
    should not go calls :func:`foundry.reporting.store.write` directly instead.

    Each switch is honoured on its own; the one dependency between the steps is the one the state
    table documents:

    1. ``results.json``, when ``write_json``. Its writer reports a filesystem or encoding refusal at
       ``WARNING`` and returns ``None`` rather than raising.
    2. ``report.html``, when ``write_html``, whatever ``write_json`` was: the renderer consumes the
       ``records`` argument and never the document written by step 1, so ``reporting.json`` and
       ``reporting.html`` are the independent switches AAP 0.4.5 publishes.
    3. ``junit.xml``, when ``write_junit`` and the HTML step did not fall back. A template that
       cannot be rendered means ``report.html`` is skipped, ``results.json`` alone remains and
       ``junit.xml`` is deliberately not produced either, with the run's exit code unchanged
       (AAP 0.4.7). Turning ``write_html`` off is not a fallback and neither is turning
       ``write_json`` off, so JUnit is still written in both cases.

    Nothing here raises on a plumbing fault -- every writer isolates ``OSError`` and
    ``UnicodeEncodeError`` into a ``WARNING`` and a ``None`` -- and the returned
    :class:`ReportPaths` is an exact account of what was written. A caller defect does surface: an
    element that is neither a result record nor a run error raises
    :class:`foundry.core.exceptions.ElementTypeError` from the writer that met it, because that is
    not a plumbing fault.

    Whether to write at all is the caller's decision, made from the session's state: a run whose
    tests were all deselected exits ``4`` and writes no report file at all (AAP 0.4.6), and only the
    plugin knows that.

    Args:
        records: The run's elements: result records, run errors, or both in any order.
        output_dir: The directory the three files are written into, normally the invocation
            directory.
        redactor: The run's redaction engine, built from ``secrets.redact_keys``. ``None`` uses the
            built-in header and query deny-lists, so redaction cannot be skipped by omitting it.
        artifacts_root: The run directory the records' artifact paths are relative to, or ``None``
            when the artifacts directory was not writable.
        log_lines: How many captured log lines each HTML failure panel carries.
        run_id: The run id to label the report with, or ``None`` to take it from the records.
        profile: The profile to label the report with, on the same terms as ``run_id``.
        write_json: Whether to write ``results.json``, from the resolved ``reporting.json``.
        write_html: Whether to write ``report.html``, from the resolved ``reporting.html``.
        write_junit: Whether to write ``junit.xml``. A requested HTML step that fell back suppresses
            it (AAP 0.4.7); the other two switches do not change it.

    Returns:
        The files written, each carried only when it was actually written: a switch that was off, a
        refusal a writer isolated into a ``WARNING`` and the HTML fallback each leave their entry
        ``None``.

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
