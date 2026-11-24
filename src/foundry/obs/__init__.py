"""Observability for Foundry: the logger, the log ring, the redactor, the artifact writer.

``foundry.obs`` is the foundation package every other Foundry package logs, redacts and
persists through. :mod:`foundry.obs.logging` owns the single ``foundry`` logger and its one
``NullHandler``, :mod:`foundry.obs.capture` the bounded per-test log ring,
:mod:`foundry.obs.redaction` the one redaction engine every sink calls before it writes,
:mod:`foundry.obs.artifacts` and :mod:`foundry.obs.manifest` the per-test artifact writer and
its manifest, and :mod:`foundry.obs.events` the structured lifecycle lines a run emits. This
module is the barrel naming their public surface, so a caller imports from ``foundry.obs``
rather than from a sibling.

**Import rule** (AAP 0.4.8). The package imports :mod:`foundry.core` and nothing else of
Foundry, and no third-party distribution at all. Every sibling is standard-library-only, and
that is what keeps this package -- and ``foundry.plugin``, which imports
:mod:`foundry.obs.logging` at module scope -- inside the 150 ms startup budget (AAP 0.14.2). A
third-party import added to any sibling re-exported here lands on that critical path, so it
belongs inside the function that needs it.

**Who writes.** Only :mod:`foundry.obs.artifacts`, :mod:`foundry.cassette.store` and
:mod:`foundry.reporting.store` write files, each inside its own declared directory.

**How faults are reported.** Foundry's own capture, redaction and reporting wrap their work: a
plumbing fault logs at ``WARNING`` on the ``foundry`` logger and changes neither a test's
outcome nor the exit code, the documented HTML-report fallback being the sole exception.
Library code never logs above ``WARNING``.

Importing this module defines names only. The ``NullHandler`` that :mod:`foundry.obs.logging`
adds at import is the package's only import-time side effect, and root logging stays untouched.
"""

from .artifacts import ARTIFACT_FILENAMES as ARTIFACT_FILENAMES
from .artifacts import KEPT_WHEN_PASSED as KEPT_WHEN_PASSED
from .artifacts import ArtifactRef as ArtifactRef
from .artifacts import ArtifactWriter as ArtifactWriter
from .artifacts import node_dir as node_dir
from .capture import DEFAULT_CAPTURE_LINES as DEFAULT_CAPTURE_LINES
from .capture import MAX_CAPTURE_LINES as MAX_CAPTURE_LINES
from .capture import LogRing as LogRing
from .capture import format_record as format_record
from .capture import tail_lines as tail_lines
from .events import LifecycleEvent as LifecycleEvent
from .events import emit_artifact_written as emit_artifact_written
from .events import emit_attempt as emit_attempt
from .events import emit_cassette_mode as emit_cassette_mode
from .events import emit_html_fallback as emit_html_fallback
from .events import emit_plumbing_failure as emit_plumbing_failure
from .events import emit_report_written as emit_report_written
from .events import emit_resolution as emit_resolution
from .events import emit_run_start as emit_run_start
from .events import emit_sharding_decision as emit_sharding_decision
from .events import event_line as event_line
from .events import log_event as log_event
from .logging import LOGGER_NAME as LOGGER_NAME
from .logging import NULL_HANDLER as NULL_HANDLER
from .logging import attach_handler as attach_handler
from .logging import configure as configure
from .logging import detach_handler as detach_handler
from .logging import get_logger as get_logger
from .logging import level_for as level_for
from .manifest import MANIFEST_FILENAME as MANIFEST_FILENAME
from .manifest import MANIFEST_SCHEMA_VERSION as MANIFEST_SCHEMA_VERSION
from .manifest import ManifestDocument as ManifestDocument
from .manifest import ManifestEntry as ManifestEntry
from .manifest import build_document as build_document
from .manifest import render as render
from .manifest import sha256_hex as sha256_hex
from .manifest import timestamp as timestamp
from .redaction import DEFAULT_HEADER_DENY_LIST as DEFAULT_HEADER_DENY_LIST
from .redaction import DEFAULT_QUERY_DENY_LIST as DEFAULT_QUERY_DENY_LIST
from .redaction import REDACTED as REDACTED
from .redaction import Redactor as Redactor
from .redaction import default_redactor as default_redactor

__all__ = (
    "ARTIFACT_FILENAMES",
    "ArtifactRef",
    "ArtifactWriter",
    "DEFAULT_CAPTURE_LINES",
    "DEFAULT_HEADER_DENY_LIST",
    "DEFAULT_QUERY_DENY_LIST",
    "KEPT_WHEN_PASSED",
    "LOGGER_NAME",
    "LifecycleEvent",
    "LogRing",
    "MANIFEST_FILENAME",
    "MANIFEST_SCHEMA_VERSION",
    "MAX_CAPTURE_LINES",
    "ManifestDocument",
    "ManifestEntry",
    "NULL_HANDLER",
    "REDACTED",
    "Redactor",
    "attach_handler",
    "build_document",
    "configure",
    "default_redactor",
    "detach_handler",
    "emit_artifact_written",
    "emit_attempt",
    "emit_cassette_mode",
    "emit_html_fallback",
    "emit_plumbing_failure",
    "emit_report_written",
    "emit_resolution",
    "emit_run_start",
    "emit_sharding_decision",
    "event_line",
    "format_record",
    "get_logger",
    "level_for",
    "log_event",
    "node_dir",
    "render",
    "sha256_hex",
    "tail_lines",
    "timestamp",
)
