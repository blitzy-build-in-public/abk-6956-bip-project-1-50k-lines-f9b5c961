"""The set of report files one run writes, and the isolation of the write itself (AAP 0.4.5-0.4.7).
The three documents are independent -- no switch suppresses another, and only the AAP 0.4.7
fallback costs ``junit.xml`` too -- and a plumbing fault logs one ``WARNING`` and no document."""

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from foundry.core.exceptions import ElementTypeError
from foundry.reporting import html, renderers, store
from foundry.reporting.record import FailureRecord, Outcome, ResultElement, ResultRecord

_RUN_ID = "20250102T030405-abc123"
_FINISHED_AT = datetime(2025, 1, 2, 3, 4, 5, 250_000, tzinfo=UTC)
_WHEN = "2025-01-02T03:04:05.000+00:00"
_SURROGATE = "\ud800"
_LONG_MESSAGE = "".join(f"the checkout total was recomputed ({n}). " for n in range(19))
_BASE = {
    "run_id": _RUN_ID,
    "started_at": datetime(2025, 1, 2, 3, 4, 5, tzinfo=UTC),
    "finished_at": _FINISHED_AT,
    "profile": "ci",
    "outcome": Outcome.PASSED,
    "attempts": 1,
    "duration_ms": 12.5,
}


def _record_for(name: str, **overrides: object) -> ResultRecord:
    fields = {**_BASE, "nodeid": f"tests/test_cart.py::{name}", **overrides}
    return ResultRecord.model_validate(fields)


def _failed(name: str, message: str) -> ResultRecord:
    failures = (FailureRecord(phase="call", message=message),)
    return _record_for(name, outcome=Outcome.FAILED, failures=failures)


def _foreign() -> list[ResultElement]:
    return cast("list[ResultElement]", [object()])


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        ({"write_json": False}, ["report.html", "junit.xml"]),
        ({"write_json": False, "write_junit": False}, ["report.html"]),
        ({"write_html": True}, ["results.json", "report.html", "junit.xml"]),
        ({"write_html": False}, ["results.json", "junit.xml"]),
    ],
)
def test_each_document_is_written_whatever_the_other_switches_are(
    tmp_path: Path, options: dict[str, bool], expected: list[str]
) -> None:
    written = renderers.write_all([_record_for("a")], tmp_path, **options)
    assert [path.name for path in written.written()] == expected
    assert sorted(path.name for path in tmp_path.iterdir()) == sorted(expected)


def test_the_callers_finish_time_and_banner_flag_reach_the_written_report(tmp_path: Path) -> None:
    written = renderers.write_all([], tmp_path, finished_at=_WHEN, artifacts_disabled=True)
    assert written.html == tmp_path / "report.html"
    document = written.html.read_text(encoding="utf-8")
    assert f"finished_at={_WHEN}" in document and 'class="banner"' in document


def test_the_html_fallback_keeps_only_the_result_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(renderers, "render_html", lambda *args, **kwargs: None)
    written = renderers.write_all([_record_for("a")], tmp_path)
    assert (written.results, written.html, written.junit) == (tmp_path / "results.json", None, None)
    assert [path.name for path in tmp_path.iterdir()] == ["results.json"]


@pytest.mark.parametrize(
    ("write_json", "document"),
    [(True, "results.json"), (False, "report.html")],
    ids=["result-document-first", "html-path-alone"],
)
def test_a_foreign_element_is_refused_before_a_document_is_written(
    tmp_path: Path, write_json: bool, document: str
) -> None:
    with pytest.raises(ElementTypeError, match=document):
        renderers.write_all(_foreign(), tmp_path, write_json=write_json)
    with pytest.raises(ElementTypeError, match="report.html"):
        renderers.write_html_report(_foreign(), tmp_path / "report.html")
    assert list(tmp_path.iterdir()) == []


def test_a_lone_surrogate_costs_no_document(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    message = f"path=/tmp/{_SURROGATE}bad: failure"
    with caplog.at_level(logging.WARNING, logger="foundry"):
        written = renderers.write_all([_failed("a", message)], tmp_path)
    assert [p.name for p in written.written()] == ["results.json", "report.html", "junit.xml"]
    assert isinstance(json.loads((tmp_path / "results.json").read_text(encoding="utf-8")), list)
    names = [record.getMessage() for record in caplog.records if record.levelno >= logging.WARNING]
    assert sum(1 for message in names if str(tmp_path / "results.json") in message) == 1
    assert all(sum(1 for message in names if str(p) in message) <= 1 for p in written.written())


def test_an_unencodable_path_is_isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    logger = logging.getLogger("foundry")
    messages: list[str] = []
    collector = logging.Handler(level=logging.WARNING)
    collector.emit = lambda record: messages.append(record.getMessage())
    monkeypatch.setattr(logger, "propagate", False)
    monkeypatch.setattr(logger, "level", logging.WARNING)
    logger.addHandler(collector)
    try:
        assert store.write_text("{}\n", tmp_path / f"report{_SURROGATE}.json") is None
    finally:
        logger.removeHandler(collector)
    assert len(messages) == 1
    head, separator, tail = messages[0].partition(_SURROGATE)
    assert separator == _SURROGATE and tail.startswith(".json")
    assert head.endswith(f"could not write report file {tmp_path / 'report'}")
    assert list(tmp_path.iterdir()) == []


def test_a_long_failure_message_reaches_both_sinks_whole(tmp_path: Path) -> None:
    failed = _failed("long_message", _LONG_MESSAGE)
    document = tmp_path / "results.json"
    assert store.write([failed], document) == document
    stored = json.loads(document.read_text(encoding="utf-8"))
    assert stored[0]["failures"][0]["message"] == _LONG_MESSAGE
    rendered = html.render([failed], report_dir=tmp_path)
    assert rendered is not None and _LONG_MESSAGE in rendered
