"""The reporting package: a run's report built from immutable result records.

``foundry.reporting`` is the one place a run's report is produced, and this module is the barrel
every other Foundry package reaches it through. :mod:`foundry.reporting.record` defines the frozen
result record, the nine outcome values and the run-error element; :mod:`foundry.reporting.store`
writes and reads ``results.json``; :mod:`foundry.reporting.junit` renders ``junit.xml``;
:mod:`foundry.reporting.html` renders the self-contained ``report.html`` from
:mod:`foundry.reporting.assets` and :mod:`foundry.reporting.panels`; and
:mod:`foundry.reporting.compare` diffs two result documents.

**The reporting boundary** (AAP 0.4.8). This package imports ``foundry.core`` and ``foundry.obs``
and no capability package, and it consumes result records exclusively: by the time a record
arrives here the secrets it carries are already scrubbed, so capture and rendering stay
independently testable and no renderer inherits a redaction decision made while a test was
running.

**Who writes.** :func:`foundry.reporting.store.write_text` is this package's only file writer; the
JUnit writer and the HTML renderer hand it their documents as text instead of opening a file
themselves (AAP 0.4.8).

**Where state lives.** :mod:`foundry.reporting.renderers` holds the single import-time
registration table the package is permitted -- the report formats a run writes -- and nothing else
here mutates module state. Importing this module therefore does no work beyond defining names: no
I/O, no environment read, no logger configuration and no template-engine import, which keeps it
inside the pytest plugin's startup budget (AAP 0.14.2, implicit requirement I5).

The names re-exported below are the stable import surface (AAP 0.4.3): additive in a later
release, never renamed or removed (AAP 0.4.9).
"""

from typing import Final

# The self-contained report's inline CSS and JS, and the check that a document references no
# external asset.
from .assets import DEFAULT_ASSETS as DEFAULT_ASSETS
from .assets import AssetsBundle as AssetsBundle
from .assets import assert_self_contained as assert_self_contained
from .assets import default_assets as default_assets

# The newly-failing / newly-passing / still-failing comparison behind `foundry report compare`.
from .compare import ComparisonDiff as ComparisonDiff
from .compare import diff as diff
from .compare import diff_files as diff_files

# The HTML report: its context, the template location and the renderer.
from .html import TEMPLATE_NAME as TEMPLATE_NAME
from .html import ReportContext as ReportContext
from .html import build_context as build_context
from .html import render as render_html
from .html import template_path as template_path

# The JUnit XML writer, its suite name and the quarantine skip marker a CI consumer asserts on.
from .junit import JUNIT_SUITE_NAME as JUNIT_SUITE_NAME
from .junit import QUARANTINE_SKIP_MESSAGE as QUARANTINE_SKIP_MESSAGE
from .junit import render as render_junit
from .junit import write as write_junit

# One failure panel per failing record, carrying its artifact links and log tail.
from .panels import FailurePanel as FailurePanel
from .panels import build_panels as build_panels

# The result record, its supporting records, the outcome vocabulary and the element codec.
from .record import FAILING_OUTCOMES as FAILING_OUTCOMES
from .record import OUTCOMES as OUTCOMES
from .record import PROFILE_NAMES as PROFILE_NAMES
from .record import QUARANTINE_OUTCOMES as QUARANTINE_OUTCOMES
from .record import ArtifactRecord as ArtifactRecord
from .record import FailureRecord as FailureRecord
from .record import Outcome as Outcome
from .record import QuarantineRecord as QuarantineRecord
from .record import ResultElement as ResultElement
from .record import ResultRecord as ResultRecord
from .record import RunError as RunError
from .record import element_to_dict as element_to_dict
from .record import parse_element as parse_element

# The report formats, their filenames, and the single entry point that writes a whole run.
from .renderers import HTML_FILENAME as HTML_FILENAME
from .renderers import JUNIT_FILENAME as JUNIT_FILENAME
from .renderers import RENDERERS as RENDERERS
from .renderers import RESULTS_FILENAME as RESULTS_FILENAME
from .renderers import RendererSpec as RendererSpec
from .renderers import ReportPaths as ReportPaths
from .renderers import registered_formats as registered_formats
from .renderers import renderer_for as renderer_for
from .renderers import write_all as write_all
from .renderers import write_html_report as write_html_report

# The result document's writer and reader, aliased so a call site that also imports the JUnit and
# HTML writers says which document it addresses.
from .store import loads as loads
from .store import read as read_results
from .store import write as write_results

__all__: Final[tuple[str, ...]] = (
    "ArtifactRecord",
    "AssetsBundle",
    "ComparisonDiff",
    "DEFAULT_ASSETS",
    "FAILING_OUTCOMES",
    "FailurePanel",
    "FailureRecord",
    "HTML_FILENAME",
    "JUNIT_FILENAME",
    "JUNIT_SUITE_NAME",
    "OUTCOMES",
    "Outcome",
    "PROFILE_NAMES",
    "QUARANTINE_OUTCOMES",
    "QUARANTINE_SKIP_MESSAGE",
    "QuarantineRecord",
    "RENDERERS",
    "RESULTS_FILENAME",
    "RendererSpec",
    "ReportContext",
    "ReportPaths",
    "ResultElement",
    "ResultRecord",
    "RunError",
    "TEMPLATE_NAME",
    "assert_self_contained",
    "build_context",
    "build_panels",
    "default_assets",
    "diff",
    "diff_files",
    "element_to_dict",
    "loads",
    "parse_element",
    "read_results",
    "registered_formats",
    "render_html",
    "render_junit",
    "renderer_for",
    "template_path",
    "write_all",
    "write_html_report",
    "write_junit",
    "write_results",
)
