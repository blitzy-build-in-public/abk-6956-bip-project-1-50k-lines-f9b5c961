"""Unit tests for the ``foundry.reporting`` package.

Every renderer consumes the frozen result record, not live run state (AAP 0.4.6, 0.4.8, 0.7.2):
``record`` pins the nine outcome values, the documented fields and the relationship rules;
``store`` writes sorted records with run errors last; ``junit`` renders the suite properties, each
outcome's element, redacted messages and escaped node ids; ``html`` renders a self-contained,
byte-identical report; ``assets`` and ``renderers`` publish the inlined CSS/JS and the format
table; ``compare`` is reachable from the package surface.

Determinism and isolation (AAP 0.14.3): fixed timestamps, files under ``tmp_path``, no sleep, no
network, no patched renderer, and no import of ``http``, ``data``, ``db``, ``execution``, ``cli``
or ``plugin`` (AAP 0.4.8).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from xml.etree import ElementTree

import pydantic
import pytest

import foundry.reporting as reporting
from foundry.config.enums import ProfileName
from foundry.core.exceptions import DataError, FoundryError
from foundry.core.types import ArtifactKind
from foundry.reporting import assets, compare, html, junit, renderers, store
from foundry.reporting.record import (
    ArtifactRecord,
    FailureRecord,
    Outcome,
    QuarantineRecord,
    ResultRecord,
    RunError,
    parse_element,
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

_DOCUMENTED_OUTCOMES = frozenset(
    {
        "passed",
        "failed",
        "skipped",
        "xfailed",
        "xpassed",
        "error",
        "quarantined_failed",
        "quarantine_passed",
        "not_run",
    }
)

_DOCUMENTED_FIELDS = (
    "record_type",
    "run_id",
    "started_at",
    "finished_at",
    "profile",
    "nodeid",
    "outcome",
    "attempts",
    "duration_ms",
    "markers",
    "failures",
    "artifacts",
    "quarantine",
)


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
    return RunError(
        run_id=_RUN_ID,
        recorded_at=_STARTED_AT,
        worker_id=worker_id,
        message=message,
    )


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


def test_outcome_exposes_exactly_the_nine_documented_values() -> None:
    values = {member.value for member in Outcome}
    assert values == _DOCUMENTED_OUTCOMES
    assert len(Outcome) == len(_DOCUMENTED_OUTCOMES)
    assert tuple(Outcome) == reporting.OUTCOMES
    assert {member.value for member in reporting.OUTCOMES} == _DOCUMENTED_OUTCOMES
    assert all(str(member) == member.value for member in Outcome)


def test_result_record_exposes_the_documented_fields() -> None:
    assert set(ResultRecord.model_fields) == set(_DOCUMENTED_FIELDS)


def test_profile_names_cover_the_configuration_enum() -> None:
    assert tuple(member.value for member in ProfileName) == reporting.PROFILE_NAMES


def _frozen_cases() -> tuple[tuple[object, str, object], ...]:
    """Return one instance of each record kind with a field no caller may assign."""
    return (
        (_record(), "nodeid", "tests/other.py::test_other"),
        (_record(), "attempts", 2),
        (FailureRecord(phase="call", message="boom"), "message", "other"),
        (ArtifactRecord(kind="request", path="run/request.json", bytes=1), "bytes", 2),
        (QuarantineRecord(state="none"), "state", "failed"),
        (_run_error(), "message", "other"),
    )


@pytest.mark.parametrize(
    ("instance", "attribute", "value"),
    _frozen_cases(),
    ids=[
        "result.nodeid",
        "result.attempts",
        "failure.message",
        "artifact.bytes",
        "quarantine.state",
        "run_error.message",
    ],
)
def test_records_are_frozen(instance: object, attribute: str, value: object) -> None:
    with pytest.raises(pydantic.ValidationError):
        setattr(instance, attribute, value)


def test_a_marker_repeat_is_canonicalised_rather_than_rejected() -> None:
    record = _record(markers=("b", "a", "b"))
    assert record.markers == ("a", "b")


@pytest.mark.parametrize(
    ("attempts", "accepted"),
    [(0, False), (-1, False), (1, True), (3, True)],
    ids=["zero", "negative", "one", "three"],
)
def test_attempts_counts_at_least_the_first_attempt(attempts: int, accepted: bool) -> None:
    if accepted:
        assert _record(attempts=attempts).attempts == attempts
        return
    with pytest.raises(pydantic.ValidationError):
        _record(attempts=attempts)


@pytest.mark.parametrize(
    ("duration_ms", "accepted"),
    [(-1.0, False), (-0.001, False), (0.0, True), (12.5, True)],
    ids=["negative", "just-negative", "zero", "fractional"],
)
def test_duration_is_never_negative(duration_ms: float, accepted: bool) -> None:
    if accepted:
        assert _record(duration_ms=duration_ms).duration_ms == duration_ms
        return
    with pytest.raises(pydantic.ValidationError):
        _record(duration_ms=duration_ms)


@pytest.mark.parametrize(
    ("path", "accepted"),
    [
        ("tests__test_cart.py__test_total/request.json", True),
        ("run/nested/response.json", True),
        ("/etc/passwd", False),
        ("../outside/request.json", False),
        ("run/../../outside/request.json", False),
    ],
    ids=["flat", "nested", "absolute", "parent", "escaping-parent"],
)
def test_artifact_paths_stay_inside_the_run_directory(path: str, accepted: bool) -> None:
    if accepted:
        assert ArtifactRecord(kind="request", path=path, bytes=1).path == path
        return
    with pytest.raises(pydantic.ValidationError):
        ArtifactRecord(kind="request", path=path, bytes=1)


@pytest.mark.parametrize(
    ("outcome", "state", "accepted"),
    [
        (Outcome.PASSED, "none", True),
        (Outcome.FAILED, "none", True),
        (Outcome.FAILED, "failed", False),
        (Outcome.SKIPPED, "passed", False),
        (Outcome.QUARANTINED_FAILED, "failed", True),
        (Outcome.QUARANTINED_FAILED, "none", False),
        (Outcome.QUARANTINED_FAILED, "passed", False),
        (Outcome.QUARANTINE_PASSED, "passed", True),
        (Outcome.QUARANTINE_PASSED, "failed", False),
        (Outcome.QUARANTINE_PASSED, "none", False),
    ],
)
def test_quarantine_state_agrees_with_the_outcome(
    outcome: Outcome, state: str, accepted: bool
) -> None:
    quarantine = QuarantineRecord.model_validate({"state": state})
    if accepted:
        record = _record(outcome=outcome, quarantine=quarantine)
        assert record.quarantine.state == state
        return
    with pytest.raises(pydantic.ValidationError):
        _record(outcome=outcome, quarantine=quarantine)


@pytest.mark.parametrize(
    ("phase", "accepted"),
    [
        ("setup", True),
        ("call", True),
        ("teardown", True),
        ("collect", False),
        ("CALL", False),
    ],
)
def test_a_failure_phase_is_one_pytest_runs(phase: str, accepted: bool) -> None:
    if accepted:
        assert FailureRecord(phase=phase, message="boom").phase == phase
        return
    with pytest.raises(pydantic.ValidationError):
        FailureRecord(phase=phase, message="boom")


def test_a_malformed_element_is_reported_as_a_data_error(tmp_path: Path) -> None:
    source = tmp_path / "results.json"
    with pytest.raises(DataError) as excinfo:
        parse_element({"record_type": "result", "run_id": "not-a-run-id"}, source=source)
    assert excinfo.value.file == source
    assert str(source) in str(excinfo.value)


def test_store_sorts_result_records_by_node_id() -> None:
    records = [_record_for("c"), _record_for("a"), _record_for("b")]
    payload = json.loads(store.dumps(records))
    assert [entry["nodeid"] for entry in payload] == [
        f"{_MODULE}::a",
        f"{_MODULE}::b",
        f"{_MODULE}::c",
    ]


def test_store_places_run_errors_after_every_result_record() -> None:
    records = [
        _run_error(worker_id="gw2", message="second"),
        _record_for("a"),
        _run_error(worker_id="gw1", message="first"),
    ]
    payload = json.loads(store.dumps(records))
    assert [entry["record_type"] for entry in payload] == ["result", "run_error", "run_error"]
    assert [entry["worker_id"] for entry in payload[1:]] == ["gw1", "gw2"]


def test_every_element_carries_its_record_type_discriminator() -> None:
    payload = json.loads(store.dumps([_record_for("a"), _run_error()]))
    assert payload[0]["record_type"] == "result"
    assert "nodeid" in payload[0]
    assert payload[1]["record_type"] == "run_error"
    assert "nodeid" not in payload[1]
    assert "outcome" not in payload[1]


def test_the_document_is_a_json_array_and_an_empty_run_is_an_empty_array() -> None:
    assert isinstance(json.loads(store.dumps([_record()])), list)
    assert json.loads(store.dumps([])) == []
    assert store.dumps([]) == "[]\n"


def test_a_failure_message_is_redacted_before_it_reaches_the_document() -> None:
    record = _record_for(
        "a",
        outcome=Outcome.FAILED,
        failures=(FailureRecord(phase="call", message=f"boom: token={_SECRET}"),),
    )
    document = store.dumps([record])
    assert _SECRET not in document
    assert "token=***" in document


def test_two_writes_of_one_run_are_byte_identical(tmp_path: Path) -> None:
    records = [_record_for("b"), _record_for("a"), _run_error()]
    first = store.write(records, tmp_path / "first" / "results.json")
    second = store.write(records, tmp_path / "second" / "results.json")
    assert first is not None
    assert second is not None
    expected = store.dumps(records)
    assert first.read_text(encoding="utf-8") == expected
    assert second.read_text(encoding="utf-8") == expected


def _junit_root(*records: object) -> ElementTree.Element:
    """Render the elements and parse the document back into an element tree."""
    return ElementTree.fromstring(junit.render(list(records)))


def _properties(element: ElementTree.Element) -> dict[str, str]:
    """Return an element's ``<properties>`` as a name-to-value mapping."""
    holder = element.find("properties")
    assert holder is not None
    return {prop.get("name", ""): prop.get("value", "") for prop in holder.findall("property")}


def test_the_suite_carries_the_run_identity() -> None:
    root = _junit_root(_record(), _run_error())
    assert root.tag == "testsuite"
    assert root.get("tests") == "1"
    assert _properties(root) == {"foundry.run_id": _RUN_ID, "foundry.profile": _PROFILE}


def test_a_testcase_derives_its_attributes_and_properties_from_the_record(tmp_path: Path) -> None:
    _run_dir, artifacts = _captured_run(tmp_path)
    record = _record_for("a", attempts=3, markers=("slow",), artifacts=artifacts)
    testcase = _junit_root(record).find("testcase")
    assert testcase is not None
    assert (testcase.get("classname"), testcase.get("name"), testcase.get("time")) == (
        "tests.test_cart",
        "a",
        "0.013",
    )
    assert _properties(testcase) == {
        "foundry.profile": _PROFILE,
        "foundry.outcome": "passed",
        "foundry.attempts": "3",
        "foundry.quarantine": "none",
        "foundry.artifact.request": "tests__test_cart.py__a/request.json",
        "foundry.artifact.response": "tests__test_cart.py__a/response.json",
        "foundry.artifact.stdout": "tests__test_cart.py__a/stdout.log",
        "foundry.artifact.stderr": "tests__test_cart.py__a/stderr.log",
        "foundry.artifact.traceback": "tests__test_cart.py__a/traceback.txt",
    }


def test_a_failure_element_carries_a_redacted_message_and_every_phase() -> None:
    record = _record_for(
        "a",
        outcome=Outcome.FAILED,
        failures=(FailureRecord(phase="call", message=f"AssertionError: token={_SECRET}"),),
    )
    document = junit.render([record])
    assert _SECRET not in document
    failure = ElementTree.fromstring(document).find("testcase/failure")
    assert failure is not None
    assert "***" in failure.get("message", "")
    assert (failure.text or "").startswith("[call] ")
    # ``error`` is the other failing outcome of AAP 0.4.6, and it renders as the sibling element.
    errored = _junit_root(_record(outcome=Outcome.ERROR))
    assert errored.find("testcase/error") is not None and errored.get("errors") == "1"


@pytest.mark.parametrize(
    ("outcome", "message"),
    [
        (Outcome.SKIPPED, "foundry: skipped"),
        (Outcome.XFAILED, "foundry: expected failure"),
        (Outcome.QUARANTINED_FAILED, "foundry: quarantined failure"),
        (Outcome.NOT_RUN, "foundry: not run"),
    ],
    ids=["skipped", "xfailed", "quarantined", "not-run"],
)
def test_an_outcome_that_does_not_count_renders_its_exact_skip_message(
    outcome: Outcome, message: str
) -> None:
    state = "failed" if outcome is Outcome.QUARANTINED_FAILED else "none"
    record = _record(outcome=outcome, quarantine=QuarantineRecord.model_validate({"state": state}))
    root = _junit_root(record)
    testcase = root.find("testcase")
    assert testcase is not None
    assert testcase.find("failure") is None
    skipped = testcase.find("skipped")
    assert skipped is not None
    assert skipped.get("message") == message
    if outcome is Outcome.QUARANTINED_FAILED:
        assert message == junit.QUARANTINE_SKIP_MESSAGE
    assert root.get("failures") == "0"
    assert root.get("skipped") == "1"


def test_a_quarantined_test_that_passed_carries_a_removal_note() -> None:
    record = _record(outcome=Outcome.QUARANTINE_PASSED, quarantine=QuarantineRecord(state="passed"))
    testcase = _junit_root(record).find("testcase")
    assert testcase is not None
    assert testcase.find("skipped") is None
    note = testcase.find("system-out")
    assert note is not None
    assert "remove the foundry_quarantine marker" in (note.text or "")
    # ``xpassed`` is the other success outcome of AAP 0.4.6 that carries a note, and it renders it.
    xpassed = _junit_root(_record(outcome=Outcome.XPASSED)).find("testcase/system-out")
    assert xpassed is not None and junit.XPASSED_NOTE in (xpassed.text or "")


def test_a_run_error_becomes_a_system_err_on_the_suite() -> None:
    root = _junit_root(_record(), _run_error(worker_id="gw3", message="worker died"))
    assert [element.text for element in root.findall("system-err")] == ["gw3: worker died"]


def test_markup_in_a_node_id_is_escaped_and_round_trips() -> None:
    document = junit.render([_record(nodeid=_SCRIPT_NODEID)])
    assert "&lt;script&gt;" in document
    assert "<script>" not in document
    testcase = ElementTree.fromstring(document).find("testcase")
    assert testcase is not None
    assert testcase.get("name") == "test_<script>alert(1)</script>"


def test_write_produces_exactly_the_rendered_document(tmp_path: Path) -> None:
    records = [_record_for("a"), _run_error()]
    target = tmp_path / "junit.xml"
    assert junit.write(records, target) == target
    assert target.read_text(encoding="utf-8") == junit.render(records)


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
    assert '<section class="quarantined">' in document
    assert "quarantined_failed" in document


def test_the_removal_note_is_the_canonical_junit_note() -> None:
    record = _record(outcome=Outcome.QUARANTINE_PASSED, quarantine=QuarantineRecord(state="passed"))
    document = _render([record])
    assert html.QUARANTINE_REMOVAL_NOTE == junit.QUARANTINE_PASSED_NOTE
    assert document.count(html.QUARANTINE_REMOVAL_NOTE) == 1


def test_a_failure_panel_links_the_dumps_and_shows_the_log_tail(tmp_path: Path) -> None:
    record, run_dir = _failing_capture(tmp_path)
    document = _render([record], artifacts_root=run_dir, report_dir=tmp_path)
    assert '<details class="panel"' in document
    assert 'href="artifacts/' in document
    assert "request dump" in document and "response dump" in document
    assert "request.json" in document and "response.json" in document
    assert "log line two" in document
    assert _SECRET not in document


def test_a_run_with_no_records_states_that_nothing_was_collected() -> None:
    document = _render([], run_id=_RUN_ID, profile=_PROFILE)
    assert "no tests collected" in document
    assert "<table" not in document


def test_two_renders_of_one_run_are_byte_identical(tmp_path: Path) -> None:
    record, run_dir = _failing_capture(tmp_path)
    options: dict[str, object] = {"artifacts_root": run_dir, "report_dir": tmp_path}
    assert _render([record], **options) == _render([record], **options)


def test_the_render_uses_the_template_shipped_beside_the_renderer(repo_root: Path) -> None:
    shipped = repo_root / "src" / "foundry" / "reporting" / "templates" / "report.html.j2"
    assert html.template_path() == shipped
    assert html.render([_record()], shipped) is not None


def test_the_report_is_complete_without_javascript() -> None:
    record = _record_for(
        "a", outcome=Outcome.FAILED, failures=(FailureRecord(phase="call", message="boom"),)
    )
    document = _render([record])
    assert "<details" in document
    start = document.index("<html")
    assert "data-foundry-js=" not in document[start : document.index(">", start)]


@pytest.mark.parametrize(("name", "text"), [("css", assets.CSS), ("js", assets.JS)])
def test_an_inlined_asset_is_self_contained(name: str, text: str) -> None:
    assert text, name
    assert not assets.contains_external_reference(text)
    assets.assert_self_contained(text)


_RENDERER_IDS = ("json", "html", "junit")
_REPORT_FILENAMES = ("results.json", "report.html", "junit.xml")


def test_the_table_publishes_the_documented_formats() -> None:
    assert renderers.registered_formats() == _RENDERER_IDS
    assert [spec.name for spec in renderers.RENDERERS.values()] == list(_RENDERER_IDS)
    assert [spec.filename for spec in renderers.RENDERERS.values()] == list(_REPORT_FILENAMES)
    assert sorted(spec.order for spec in renderers.RENDERERS.values()) == [0, 1, 2]
    assert all(callable(spec.writer) for spec in renderers.RENDERERS.values())


def test_an_unknown_format_names_the_published_ones() -> None:
    with pytest.raises(FoundryError) as excinfo:
        renderers.renderer_for("xml")
    assert "html" in (excinfo.value.hint or "")


def test_writing_a_run_leaves_the_registration_table_untouched(tmp_path: Path) -> None:
    before = [(name, id(spec)) for name, spec in renderers.RENDERERS.items()]
    written = renderers.write_all([_record_for("a")], tmp_path)
    assert [path.name for path in written.written()] == list(_REPORT_FILENAMES)
    assert [(name, id(spec)) for name, spec in renderers.RENDERERS.items()] == before


def _inline_document(outcome: str) -> str:
    """Return a small result document holding one result record and one run error."""
    return json.dumps(
        [
            {
                "record_type": "result",
                "run_id": _RUN_ID,
                "started_at": "2025-01-02T03:04:05.000+00:00",
                "finished_at": "2025-01-02T03:04:05.250+00:00",
                "profile": _PROFILE,
                "nodeid": _NODEID,
                "outcome": outcome,
                "attempts": 1,
                "duration_ms": 1.0,
                "markers": [],
                "failures": [],
                "artifacts": [],
                "quarantine": {"state": "none"},
            },
            {
                "record_type": "run_error",
                "run_id": _RUN_ID,
                "recorded_at": "2025-01-02T03:04:06.000+00:00",
                "worker_id": "gw0",
                "message": "worker lost",
            },
        ]
    )


def test_the_comparison_is_reachable_from_the_package_surface() -> None:
    assert reporting.diff is compare.diff


def test_the_comparison_ignores_run_error_elements(tmp_path: Path) -> None:
    source = tmp_path / "inline.json"
    difference = compare.diff(
        store.loads(_inline_document("failed"), source=source),
        store.loads(_inline_document("passed"), source=source),
    )
    assert difference.newly_passing == (_NODEID,)
    assert difference.newly_failing == ()
    assert difference.still_failing == ()
    assert "worker lost" not in str(difference.as_dict())
