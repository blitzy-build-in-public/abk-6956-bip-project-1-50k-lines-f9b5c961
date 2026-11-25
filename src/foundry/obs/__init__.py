"""Observability for Foundry: the logger, the log ring, the redactor, the artifact writer.

The barrel over ``logging``, ``capture``, ``redaction``, ``artifacts``, ``manifest`` and
``events`` (AAP 0.4.8): one logger, one bounded log ring, the single redaction engine every sink
calls before it writes, the artifact writer with its manifest, and a run's lifecycle lines.
Importing it binds names only and performs no I/O; ``pytest``, imported by ``artifacts`` for the
``StashKey`` that publishes the run id, is the one third-party module on the path.
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
from .events import emit_no_config_file as emit_no_config_file
from .events import emit_plumbing_failure as emit_plumbing_failure
from .events import emit_report_written as emit_report_written
from .events import emit_resolution as emit_resolution
from .events import emit_run_start as emit_run_start
from .events import emit_sharding_decision as emit_sharding_decision
from .events import event_line as event_line
from .events import log_event as log_event
from .logging import LOGGER_NAME as LOGGER_NAME
from .logging import NULL_HANDLER as NULL_HANDLER
from .logging import RedactionFilter as RedactionFilter
from .logging import attach_handler as attach_handler
from .logging import configure as configure
from .logging import detach_handler as detach_handler
from .logging import get_logger as get_logger
from .logging import install_redaction_filter as install_redaction_filter
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
    "RedactionFilter",
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
    "emit_no_config_file",
    "emit_plumbing_failure",
    "emit_report_written",
    "emit_resolution",
    "emit_run_start",
    "emit_sharding_decision",
    "event_line",
    "format_record",
    "get_logger",
    "install_redaction_filter",
    "level_for",
    "log_event",
    "node_dir",
    "render",
    "sha256_hex",
    "tail_lines",
    "timestamp",
)
