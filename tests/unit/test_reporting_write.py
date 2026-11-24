"""The set of report files one run writes, and the isolation of the write itself (AAP 0.4.5-0.4.7).

Findings this module pins, at the two decision points ``foundry.reporting.renderers.write_all``
and ``foundry.reporting.store.write_text`` own:

* cr06-html-gated-on-json -- ``report.html`` and ``junit.xml`` must not be made dependent on
  ``results.json`` having been written. AAP 0.4.5 publishes ``reporting.html`` and
  ``reporting.json`` as independent keys, and every writer consumes the records it is handed, so a
  run with ``reporting.json`` off still writes its report. The one dependency that remains is the
  documented fallback of AAP 0.4.7: when the HTML template cannot be rendered, ``report.html`` is
  skipped and ``junit.xml`` is not produced either, with the exit code untouched.
* qa04-encode-fault-isolation -- a character UTF-8 cannot encode (a lone surrogate, the shape
  ``os.fsdecode`` gives a path with undecodable bytes) must cost that one string rather than every
  document. AAP 0.1.2 and 0.14.3 require a plumbing fault to log at ``WARNING`` on the ``foundry``
  logger and to change neither a test's outcome nor the run's exit code.

Determinism and isolation: records are built inline from fixed timestamps, so no assertion reads
the wall clock; every document goes under ``tmp_path``; nothing sleeps and nothing touches the
network. The one record whose message must carry a surrogate is observed on a handler attached to
the ``foundry`` logger with that logger's propagation off while it is made, so no unencodable
character reaches pytest's ``TestReport.sections`` and no xdist worker dies ferrying it on.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest

from foundry.reporting import renderers, store
from foundry.reporting.record import FailureRecord, Outcome, ResultRecord

# --- Fixed fixtures --------------------------------------------------------
#
# A run id of the shape AAP 0.4.3 publishes, a profile from AAP 0.4.5's four, a node id carrying a
# module path, and two fixed instants, so no assertion here depends on the host's clock.

_RUN_ID = "20250102T030405-abc123"
_PROFILE = "ci"
_STARTED_AT = datetime(2025, 1, 2, 3, 4, 5, 0, tzinfo=UTC)
_FINISHED_AT = datetime(2025, 1, 2, 3, 4, 5, 250_000, tzinfo=UTC)
_MODULE = "tests/test_cart.py"

_REPORT_FILENAMES = ("results.json", "report.html", "junit.xml")
"""The three fixed names of AAP 0.4.6, in the order the set is written."""

_LONE_SURROGATE = "\ud800"
"""A lone surrogate, the shape ``os.fsdecode`` gives a path with undecodable bytes: UTF-8
refuses it, so the filesystem encoding refuses a file name that carries it."""

_LONE_SURROGATE_MESSAGE = f"path=/tmp/{_LONE_SURROGATE}bad: failure"
"""A failure message carrying a surrogate, which UTF-8 cannot encode."""

_UNENCODABLE_REPORT_NAME = f"report{_LONE_SURROGATE}.json"
"""A report file name carrying that surrogate, as a path derived from a node id can."""


def _record_for(name: str, **overrides: object) -> ResultRecord:
    """Return a record whose node id is the module path plus ``name``."""
    fields: dict[str, object] = {
        "run_id": _RUN_ID,
        "started_at": _STARTED_AT,
        "finished_at": _FINISHED_AT,
        "profile": _PROFILE,
        "nodeid": f"{_MODULE}::{name}",
        "outcome": Outcome.PASSED,
        "attempts": 1,
        "duration_ms": 12.5,
    }
    fields.update(overrides)
    return ResultRecord.model_validate(fields)


# --- The switches are independent (AAP 0.4.5) ------------------------------


@pytest.mark.parametrize("write_junit", [True, False])
def test_the_report_does_not_depend_on_the_result_document(
    tmp_path: Path, write_junit: bool
) -> None:
    """``reporting.json`` off must not suppress ``report.html`` nor the JUnit document."""
    written = renderers.write_all(
        [_record_for("a")], tmp_path, write_json=False, write_junit=write_junit
    )
    expected = ["report.html", "junit.xml"] if write_junit else ["report.html"]
    assert written.results is None
    assert [path.name for path in written.written()] == expected
    assert sorted(path.name for path in tmp_path.iterdir()) == sorted(expected)


@pytest.mark.parametrize("write_html", [True, False])
def test_the_result_document_is_written_whatever_the_html_switch_is(
    tmp_path: Path, write_html: bool
) -> None:
    """``reporting.html`` off must not suppress ``results.json`` nor the JUnit document."""
    written = renderers.write_all([_record_for("a")], tmp_path, write_html=write_html)
    expected = list(_REPORT_FILENAMES) if write_html else ["results.json", "junit.xml"]
    assert (written.html is not None) is write_html
    assert [path.name for path in written.written()] == expected
    assert sorted(path.name for path in tmp_path.iterdir()) == sorted(expected)


# --- The documented fallback (AAP 0.4.7) -----------------------------------


def test_the_html_fallback_keeps_only_the_result_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A requested HTML step that wrote nothing suppresses ``junit.xml`` and nothing else."""
    monkeypatch.setattr(renderers, "render_html", lambda *args, **kwargs: None)
    written = renderers.write_all([_record_for("a")], tmp_path)
    assert (written.results, written.html, written.junit) == (
        tmp_path / "results.json",
        None,
        None,
    )
    assert [path.name for path in tmp_path.iterdir()] == ["results.json"]


# --- Failure isolation of the write (AAP 0.1.2, AAP 0.14.3) ----------------


class _FoundryRecordCollector(logging.Handler):
    """Collect the records the ``foundry`` logger emits, so none of them reaches pytest.

    pytest's logging plugin copies every captured log line into ``TestReport.sections``, and
    xdist ferries that report to the controller through execnet, which UTF-8-encodes every
    string. A record whose message names a path carrying a lone surrogate cannot be encoded, so
    the worker dies and the test's result can be dropped while the run still exits 0. Attached to
    the ``foundry`` logger with that logger's propagation off, this handler observes the record
    and nothing leaves the worker.
    """

    def __init__(self) -> None:
        """Collect warnings only, since a plumbing fault is emitted at ``WARNING``."""
        super().__init__(level=logging.WARNING)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        """Retain ``record``; a handler never raises into the code that logged."""
        self.records.append(record)


def test_a_lone_surrogate_costs_no_document(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An unencodable character is reported once per document and costs no report file."""
    failed = _record_for(
        "a",
        outcome=Outcome.FAILED,
        failures=(FailureRecord(phase="call", message=_LONE_SURROGATE_MESSAGE),),
    )
    with caplog.at_level(logging.WARNING, logger="foundry"):
        written = renderers.write_all([failed], tmp_path)
    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.name.startswith("foundry") and record.levelno >= logging.WARNING
    ]
    assert [path.name for path in written.written()] == list(_REPORT_FILENAMES)
    assert sorted(path.name for path in tmp_path.iterdir()) == sorted(_REPORT_FILENAMES)
    assert isinstance(json.loads((tmp_path / "results.json").read_text(encoding="utf-8")), list)
    assert sum(1 for message in warnings if str(tmp_path / "results.json") in message) == 1
    assert all(
        sum(1 for message in warnings if str(path) in message) <= 1 for path in written.written()
    )


def test_an_unencodable_path_is_isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A file name UTF-8 cannot encode is a plumbing fault, not an exception for the session.

    A report path can be derived from a node id, and a node id decoded from a path with
    undecodable bytes carries a surrogate, which the filesystem encoding refuses: the widened
    guard in :func:`foundry.reporting.store.write_text` turns that into one ``WARNING`` and a
    ``None`` return rather than an exception out of the write (AAP 0.1.2, AAP 0.14.3).

    The record is observed on :class:`_FoundryRecordCollector` rather than through ``caplog``
    because its message names the path and so carries the surrogate, and a captured record
    would reach the report xdist ferries to the controller. The comparisons stay structural --
    record count, level, logger name, and the text on either side of the surrogate -- so a
    failing run reports no unencodable character either.
    """
    foundry_logger = logging.getLogger("foundry")
    collector = _FoundryRecordCollector()
    # Neither propagate the record to pytest's capture nor let a level another test left on the
    # logger filter it.
    monkeypatch.setattr(foundry_logger, "propagate", False)
    monkeypatch.setattr(foundry_logger, "level", logging.WARNING)
    foundry_logger.addHandler(collector)
    try:
        assert store.write_text("{}\n", tmp_path / _UNENCODABLE_REPORT_NAME) is None
    finally:
        foundry_logger.removeHandler(collector)

    assert len(collector.records) == 1
    record = collector.records[0]
    assert record.name == "foundry"
    assert record.levelno == logging.WARNING
    head, separator, tail = record.getMessage().partition(_LONE_SURROGATE)
    assert separator == _LONE_SURROGATE
    assert head.endswith(f"could not write report file {tmp_path / 'report'}")
    assert tail.startswith(".json")
    assert list(tmp_path.iterdir()) == []
