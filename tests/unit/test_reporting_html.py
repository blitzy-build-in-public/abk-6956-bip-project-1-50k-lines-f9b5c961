"""The rendered ``report.html`` page: self-containment, structure and determinism (AAP 0.4.7).
Every assertion reads the document a browser would open: nothing referencing an absolute scheme, a
``<link>`` or a ``src=``; the stylesheet inlined; every node id escaped; the rows sorted by node id;
the Quarantined and Errors regions named landmarks."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import pytest

from foundry.reporting import html, junit
from foundry.reporting import record as rec

_RUN_ID = "20250102T030405-abc123"
_STARTED_AT = datetime(2025, 1, 2, 3, 4, 5, 0, tzinfo=UTC)
_FINISHED_AT = datetime(2025, 1, 2, 3, 4, 5, 250_000, tzinfo=UTC)
_SECRET = "hunter2-secret-9f3"
_SCRIPT_NODEID = "tests/test_cart.py::test_<script>alert(1)</script>"
# A four-line failure message shaped as pytest reports a failed assertion.
_TRACEBACK: Final[str] = (
    "AssertionError: the checkout total did not match\n> assert total == 3\n"
    "E assert 4 == 3\ntests/test_cart.py:12: AssertionError"
)
_BASE = {
    "run_id": _RUN_ID,
    "started_at": _STARTED_AT,
    "finished_at": _FINISHED_AT,
    "profile": "ci",
    "outcome": rec.Outcome.PASSED,
    "attempts": 1,
    "duration_ms": 12.5,
}


def _record(name: str = "total", **overrides: object) -> rec.ResultRecord:
    fields = {**_BASE, "nodeid": f"tests/test_cart.py::{name}", **overrides}
    return rec.ResultRecord.model_validate(fields)


def _run_error(worker_id: str = "gw1", message: str = "lost") -> rec.RunError:
    return rec.RunError(
        run_id=_RUN_ID, recorded_at=_STARTED_AT, worker_id=worker_id, message=message
    )


# The artifacts one failing test's panel links, and the tail it shows beneath them.
_CAPTURED: Final[tuple[tuple[str, str, str], ...]] = (
    ("request", "request.json", '{"method": "GET", "path": "/cart"}'),
    ("response", "response.json", '{"status": 200, "body": "ok"}'),
    ("traceback", "traceback.txt", "Traceback: AssertionError\n"),
    ("log_tail", "log_tail.log", "log line one\nlog line two\n"),
)


def _failing_capture(
    tmp_path: Path, directory: str = "tests__test_cart.py__a", *, log_tail: bool = True
) -> tuple[rec.ResultRecord, Path]:
    run_dir = tmp_path / "artifacts" / _RUN_ID
    artifacts = []
    for kind, name, text in _CAPTURED:
        if kind == "log_tail" and not log_tail:
            continue
        relative = f"{directory}/{name}"
        (run_dir / relative).parent.mkdir(parents=True, exist_ok=True)
        (run_dir / relative).write_text(text, encoding="utf-8")
        artifacts.append(rec.ArtifactRecord(kind=kind, path=relative, bytes=len(text.encode())))
    failures = (rec.FailureRecord(phase="call", message=f"AssertionError: token={_SECRET}"),)
    record = _record("a", outcome=rec.Outcome.FAILED, artifacts=tuple(artifacts), failures=failures)
    return record, run_dir


def _render(records: list[object], **options: object) -> str:
    document = html.render(list(records), **options)
    assert document is not None
    return document


def test_a_failure_page_is_self_contained_and_links_relative_to_its_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record, run_dir = _failing_capture(tmp_path)
    options: dict[str, object] = {"artifacts_root": run_dir, "report_dir": tmp_path}
    document = _render([record, _record(nodeid=_SCRIPT_NODEID)], **options)
    lowered = document.lower()
    assert [scheme for scheme in ("http://", "https://") if scheme in lowered] == []
    assert "url(http" not in lowered and "url(//" not in lowered and "<style>" in lowered
    assert "<link" not in lowered and 'src="' not in lowered
    assert '<details class="panel"' in document and 'href="artifacts/' in document
    assert "request dump" in document and "request.json" in document and "log line two" in document
    assert _SECRET not in document and "&lt;script&gt;alert(1)&lt;/script&gt;" in document
    start = document.index("<html")
    assert "data-foundry-js=" not in document[start : document.index(">", start)]
    assert _render([record, _record(nodeid=_SCRIPT_NODEID)], **options) == document
    monkeypatch.chdir(tmp_path)
    assert _render([record], artifacts_root=run_dir) == _render([record], **options)
    assert html.template_path().name == "report.html.j2"


def test_the_page_lists_every_record_in_its_named_region() -> None:
    failed = rec.QuarantineRecord(state="failed")
    passed = rec.QuarantineRecord(state="passed")
    records = [
        _record("a"),
        _record("b", outcome=rec.Outcome.FAILED, duration_ms=1500.0, markers=("slow", "api")),
        _record("c", outcome=rec.Outcome.QUARANTINED_FAILED, quarantine=failed),
        _record("d", outcome=rec.Outcome.QUARANTINE_PASSED, quarantine=passed),
        _run_error(),
    ]
    document = _render(records)
    assert document.index("test_cart.py::a") < document.index("test_cart.py::b")
    assert ">passed<" in document and ">failed<" in document and "12.5 ms" in document
    assert "1.50 s" in document and ">ci<" in document and "api, slow" in document
    assert document.count('<th scope="row" class="nodeid">') == 4 and document.count("<main>") == 1
    main = document.index("<main>")
    assert main < document.index('<ul class="summary">') < document.index("</main>")
    assert '<section aria-label="outcome counts">' in document
    assert '<section class="quarantined" aria-labelledby="quarantined-heading">' in document
    assert "quarantined_failed" in document and 'aria-labelledby="errors-heading"' in document
    assert document.count(html.QUARANTINE_REMOVAL_NOTE) == 1
    assert html.QUARANTINE_REMOVAL_NOTE == junit.QUARANTINE_PASSED_NOTE


@pytest.mark.parametrize(
    ("workers", "options", "banner"),
    [
        ((), {}, False),
        ((), {"run_id": _RUN_ID, "profile": "ci"}, False),
        (("gw0", "gw2"), {"artifacts_disabled": False}, False),
        ((), {"artifacts_disabled": True}, True),
    ],
    ids=["no-records", "labelled-no-records", "run-errors-only", "artifacts-disabled"],
)
def test_the_header_labels_an_unknown_finish_time_and_a_disabled_writer(
    workers: tuple[str, ...], options: dict[str, object], banner: bool
) -> None:
    document = _render([_run_error(worker) for worker in workers], **options)
    assert "finished_at=unknown" in document and "finished_at=</span>" not in document
    assert "no tests collected" in document and "<table" not in document
    assert 'class="table-scroll"' not in document and ('class="banner"' in document) is banner


def test_a_multi_line_failure_message_keeps_its_line_structure() -> None:
    failures = (rec.FailureRecord(phase="call", message=_TRACEBACK),)
    document = _render([_record("a", outcome=rec.Outcome.FAILED, failures=failures)])
    assert document.count('<pre class="message">') == 1
    block = document.split('<pre class="message">', 1)[1].split("</pre>", 1)[0]
    assert block.splitlines() == [
        "AssertionError: the checkout total did not match",
        "&gt; assert total == 3",
        "E assert 4 == 3",
        "tests/test_cart.py:12: AssertionError",
    ]


def test_the_traceback_row_links_the_double_encoded_path(tmp_path: Path) -> None:
    record, run_dir = _failing_capture(tmp_path, "test_cart.py%3A%3Atest_total")
    document = _render([record], artifacts_root=run_dir, report_dir=tmp_path)
    row = document.split("<dt>traceback</dt>", 1)[1].split("</dd>", 1)[0].strip()
    assert row.startswith('<dd><a href="')
    href, _, remainder = row[len('<dd><a href="') :].partition('">')
    assert remainder.endswith("</a>")
    link_text = remainder[: -len("</a>")]
    assert "%253A%253A" in href and "%3A%3A" not in href
    assert link_text == href.replace("%25", "%") and (tmp_path / link_text).is_file()


def test_a_panel_of_a_test_that_logged_nothing_reports_an_empty_tail(tmp_path: Path) -> None:
    record, run_dir = _failing_capture(tmp_path, log_tail=False)
    document = _render([record], artifacts_root=run_dir, report_dir=tmp_path)
    assert "log tail captured no lines" in document and "log tail unavailable" not in document


@pytest.mark.parametrize(
    ("milliseconds", "display"),
    [
        (0.0, "0.0 ms"),
        (0.001, "< 0.01 ms"),
        (0.04, "0.04 ms"),
        (0.5, "0.50 ms"),
        (3.5, "3.5 ms"),
        (12.5, "12.5 ms"),
        (999.9, "999.9 ms"),
        (1000.0, "1.00 s"),
        (3_600_000.0, "3600.00 s"),
    ],
)
def test_a_duration_renders_at_its_own_magnitude(milliseconds: float, display: str) -> None:
    assert html._format_duration(milliseconds) == display
    assert (display == "0.0 ms") is (milliseconds == 0.0)


@pytest.mark.parametrize(
    ("names", "workers", "counted", "errors"),
    [
        (("a",), (), "<span>1 test</span>", None),
        (("a", "b"), (), "<span>2 tests</span>", None),
        (("a",), ("gw0",), "<span>1 test</span>", "<span>1 run error</span>"),
        (("a",), ("gw0", "gw2", "gw3"), "<span>1 test</span>", "<span>3 run errors</span>"),
    ],
    ids=["one-test", "two-tests", "one-run-error", "three-run-errors"],
)
def test_the_header_counts_the_tests_and_only_the_run_errors_the_run_has(
    names: tuple[str, ...], workers: tuple[str, ...], counted: str, errors: str | None
) -> None:
    elements = [*(_record(name) for name in names), *(_run_error(worker) for worker in workers)]
    assert html.build_context(elements).run_error_count == len(workers)
    document = _render(elements)
    header = document[document.index('<header class="report-header">') : document.index("<main>")]
    assert counted in header and "1 tests" not in header
    assert ("run error" in header) is (errors is not None) and (errors is None or errors in header)
    assert document.count('<span class="count">') == 9


def test_a_control_character_in_a_run_error_is_spelled_out() -> None:
    document = _render([_run_error("gw0", "worker died: \x00bell\x07end")])
    assert "worker died: \\x00bell\\x07end" in document
    unprintable = {member for member in document if ord(member) < 0x20 and member not in "\n\t"}
    assert unprintable == set(), repr(unprintable)
