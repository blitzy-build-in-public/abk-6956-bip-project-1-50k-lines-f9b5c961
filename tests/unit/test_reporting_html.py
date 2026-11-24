"""The rendered ``report.html`` page: self-containment, structure and determinism (AAP 0.4.7).

Every assertion here reads the document a browser would open, which is what ``html`` promises it
produces: a self-contained, byte-identical report from the frozen result records of one run, with
nothing referencing an absolute scheme, a ``<link>`` or a ``src=``; the stylesheet inlined; every
node id escaped; the rows sorted by node id; the Quarantined and Errors regions named landmarks;
and the Jinja2 template shipped beside the renderer supplying the page rather than a copy of it.

Determinism and isolation (AAP 0.14.3): fixed timestamps, files under ``tmp_path``, no sleep, no
network, no patched renderer, and no import of ``http``, ``data``, ``db``, ``execution``, ``cli``
or ``plugin`` (AAP 0.4.8).

The record model, the ``results.json`` store, the JUnit document, the format table and the
comparison stay in ``tests/unit/test_reporting.py``; the HTML page's group lives here so that
neither module exceeds the 700-line ceiling of AAP 0.14.2.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from foundry.core.types import ArtifactKind
from foundry.reporting import html, junit
from foundry.reporting.record import (
    ArtifactRecord,
    FailureRecord,
    Outcome,
    QuarantineRecord,
    ResultRecord,
    RunError,
)

# Fixed run id, profile, node id and instants, so no assertion depends on the host's clock.

_RUN_ID = "20250102T030405-abc123"
_PROFILE = "ci"
_STARTED_AT = datetime(2025, 1, 2, 3, 4, 5, 0, tzinfo=UTC)
_FINISHED_AT = datetime(2025, 1, 2, 3, 4, 5, 250_000, tzinfo=UTC)
_MODULE = "tests/test_cart.py"
_NODEID = f"{_MODULE}::test_total"

_SECRET = "hunter2-secret-9f3"
"""A sentinel no document may contain: distinctive enough that a match is a real leak."""

_SCRIPT_NODEID = f"{_MODULE}::test_<script>alert(1)</script>"
"""A node id carrying markup, which every renderer must treat as text."""

_ABSOLUTE_SCHEMES = ("http://", "https://")


def _record(**overrides: object) -> ResultRecord:
    """Return a passing result record whose every field the caller did not override is fixed."""
    fields: dict[str, object] = {
        "run_id": _RUN_ID,
        "started_at": _STARTED_AT,
        "finished_at": _FINISHED_AT,
        "profile": _PROFILE,
        "nodeid": _NODEID,
        "outcome": Outcome.PASSED,
        "attempts": 1,
        "duration_ms": 12.5,
    }
    fields.update(overrides)
    return ResultRecord.model_validate(fields)


def _record_for(name: str, **overrides: object) -> ResultRecord:
    """Return a record whose node id is the module path plus ``name``."""
    return _record(nodeid=f"{_MODULE}::{name}", **overrides)


def _run_error(worker_id: str = "gw1", message: str = "worker lost") -> RunError:
    """Return a run-level error element, the second kind ``results.json`` holds."""
    return RunError(run_id=_RUN_ID, recorded_at=_STARTED_AT, worker_id=worker_id, message=message)


def _write_artifact(
    run_dir: Path,
    relative: str,
    text: str,
    *,
    kind: ArtifactKind = "request",
) -> ArtifactRecord:
    """Write one captured artifact under ``run_dir`` and return the record that references it."""
    target = run_dir / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return ArtifactRecord(kind=kind, path=relative, bytes=len(text.encode("utf-8")))


_CAPTURED_FILES: tuple[tuple[ArtifactKind, str, str], ...] = (
    ("request", "request.json", '{"method": "GET", "path": "/cart"}'),
    ("response", "response.json", '{"status": 200, "body": "ok"}'),
    ("stdout", "stdout.log", "captured stdout\n"),
    ("stderr", "stderr.log", "captured stderr\n"),
    ("traceback", "traceback.txt", "Traceback: AssertionError\n"),
    ("log_tail", "log_tail.log", "log line one\nlog line two\n"),
)


def _captured_run(tmp_path: Path) -> tuple[Path, tuple[ArtifactRecord, ...]]:
    """Write one file per artifact kind under a run directory and return the six references."""
    run_dir = tmp_path / "artifacts" / _RUN_ID
    references = tuple(
        _write_artifact(run_dir, f"tests__test_cart.py__a/{name}", text, kind=kind)
        for kind, name, text in _CAPTURED_FILES
    )
    return run_dir, references


def _render(records: list[object], **options: object) -> str:
    """Render the report and fail rather than pass on the documented template fallback."""
    document = html.render(list(records), **options)
    assert document is not None
    return document


def _failing_capture(tmp_path: Path) -> tuple[ResultRecord, Path]:
    """Return a failing record carrying captured artifacts and the run directory holding them."""
    run_dir, artifacts = _captured_run(tmp_path)
    record = _record_for(
        "a",
        outcome=Outcome.FAILED,
        artifacts=artifacts,
        failures=(FailureRecord(phase="call", message=f"AssertionError: token={_SECRET}"),),
    )
    return record, run_dir


def test_the_report_references_nothing_from_outside_itself(tmp_path: Path) -> None:
    record, run_dir = _failing_capture(tmp_path)
    document = _render([record], artifacts_root=run_dir, report_dir=tmp_path)
    lowered = document.lower()
    assert [scheme for scheme in _ABSOLUTE_SCHEMES if scheme in lowered] == []
    assert "url(http" not in lowered and "url(//" not in lowered
    assert "<link" not in lowered and 'src="' not in lowered
    assert "<style>" in lowered


def test_a_node_id_carrying_markup_is_escaped() -> None:
    document = _render([_record(nodeid=_SCRIPT_NODEID)])
    assert "&lt;script&gt;" in document
    assert _SCRIPT_NODEID not in document


def test_the_table_lists_every_record_sorted_by_node_id() -> None:
    records = [
        _record_for("b", outcome=Outcome.FAILED, duration_ms=1500.0, markers=("slow", "api")),
        _record_for("a"),
    ]
    document = _render(records)
    assert document.index("test_cart.py::a") < document.index("test_cart.py::b")
    assert ">passed<" in document and ">failed<" in document
    assert "12.5 ms" in document and "1.50 s" in document
    assert f">{_PROFILE}<" in document and "api, slow" in document


def test_a_quarantined_failure_is_listed_in_its_own_section() -> None:
    quarantine = QuarantineRecord(state="failed")
    record = _record(outcome=Outcome.QUARANTINED_FAILED, quarantine=quarantine)
    document = _render([record])
    assert '<section class="quarantined" aria-labelledby="quarantined-heading">' in document
    assert "quarantined_failed" in document


def test_each_row_is_headed_by_its_test_and_every_region_is_a_named_landmark() -> None:
    """Each result row is headed by its test, and the counted regions sit in a named landmark.

    The summary strip renders inside ``<main>`` so landmark navigation reaches the outcome counts,
    and the Quarantined and Errors sections are named by the heading their ``aria-labelledby``
    points at.
    """
    records = [
        _record(),
        _record_for(
            "b", outcome=Outcome.QUARANTINED_FAILED, quarantine=QuarantineRecord(state="failed")
        ),
        _run_error(),
    ]
    document = _render(records)
    assert document.count('<th scope="row" class="nodeid">') == 2
    assert document.count("<main>") == 1
    summary = document.index('<ul class="summary">')
    assert document.index("<main>") < summary < document.index("</main>")
    assert '<section aria-label="outcome counts">' in document
    assert 'aria-labelledby="quarantined-heading"' in document
    assert 'aria-labelledby="errors-heading"' in document


def test_the_removal_note_is_the_canonical_junit_note() -> None:
    record = _record(outcome=Outcome.QUARANTINE_PASSED, quarantine=QuarantineRecord(state="passed"))
    assert html.QUARANTINE_REMOVAL_NOTE == junit.QUARANTINE_PASSED_NOTE
    assert _render([record]).count(html.QUARANTINE_REMOVAL_NOTE) == 1


def test_a_failure_panel_links_the_dumps_and_shows_the_log_tail(tmp_path: Path) -> None:
    record, run_dir = _failing_capture(tmp_path)
    document = _render([record], artifacts_root=run_dir, report_dir=tmp_path)
    assert '<details class="panel"' in document
    assert 'href="artifacts/' in document
    assert "request dump" in document and "response dump" in document
    assert "request.json" in document and "response.json" in document
    assert "log line two" in document
    assert _SECRET not in document


def _hrefs(document: str) -> list[str]:
    """Return every ``href`` value a rendered document carries, in document order."""
    return [remainder.split('"', 1)[0] for remainder in document.split('href="')[1:]]


def test_the_link_base_defaults_to_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An omitted ``report_dir`` links from the working directory, where ``report.html`` is written.

    AAP 0.14.6 A5 writes the report there, so a document rendered for another directory is told it.
    """
    record, run_dir = _failing_capture(tmp_path)
    monkeypatch.chdir(tmp_path)
    from_working_directory = _hrefs(_render([record], artifacts_root=run_dir))
    explicit = _hrefs(_render([record], artifacts_root=run_dir, report_dir=tmp_path))
    assert explicit != []
    assert from_working_directory == explicit
    assert all(href.startswith("artifacts/") for href in explicit)

    elsewhere = tmp_path / "reports"
    elsewhere.mkdir()
    other = _hrefs(_render([record], artifacts_root=run_dir, report_dir=elsewhere))
    assert other != explicit
    assert all(not href.startswith("/") and "://" not in href for href in other)


def test_a_run_with_no_records_states_that_nothing_was_collected() -> None:
    document = _render([], run_id=_RUN_ID, profile=_PROFILE)
    assert "no tests collected" in document
    assert "<table" not in document


@pytest.mark.parametrize("workers", [(), ("gw0", "gw2")], ids=["no-records", "run-errors-only"])
def test_a_run_that_recorded_no_result_still_labels_its_finish_time(
    workers: tuple[str, ...],
) -> None:
    document = _render([_run_error(worker) for worker in workers], run_id=_RUN_ID, profile=_PROFILE)
    assert "finished_at=unknown" in document
    assert "finished_at=</span>" not in document


@pytest.mark.parametrize(("writer_disabled", "banner"), [(True, True), (False, False)])
def test_the_artifact_writers_disabled_flag_governs_the_banner(
    writer_disabled: bool, banner: bool
) -> None:
    document = _render([_record_for("a")], artifacts_disabled=writer_disabled)
    assert ('class="banner"' in document) is banner


def test_two_renders_of_one_run_are_byte_identical(tmp_path: Path) -> None:
    record, run_dir = _failing_capture(tmp_path)
    options: dict[str, object] = {"artifacts_root": run_dir, "report_dir": tmp_path}
    assert _render([record], **options) == _render([record], **options)


def test_the_render_uses_the_template_shipped_beside_the_renderer(repo_root: Path) -> None:
    """The shipped template is the one rendered, and the page title comes from ``ctx.title``."""
    shipped = repo_root / "src" / "foundry" / "reporting" / "templates" / "report.html.j2"
    assert html.template_path() == shipped
    document = html.render([_record()], shipped)
    assert document is not None
    assert f"<title>{html.REPORT_TITLE} — {_RUN_ID}</title>" in document
    assert f"<h1>{html.REPORT_TITLE}</h1>" in document
    source = shipped.read_text(encoding="utf-8")
    assert "<h1>{{ ctx.title }}</h1>" in source and "{{ ctx.run_id }}</title>" in source


def test_the_report_is_complete_without_javascript() -> None:
    record = _record_for(
        "a", outcome=Outcome.FAILED, failures=(FailureRecord(phase="call", message="boom"),)
    )
    document = _render([record])
    assert "<details" in document
    start = document.index("<html")
    assert "data-foundry-js=" not in document[start : document.index(">", start)]
