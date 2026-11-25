"""Unit tests for the ``foundry.reporting`` package: record schema, store, JUnit, formats, compare.
Every record-derived string the JUnit document carries is reduced to XML 1.0's ``Char`` production
first, and the artifact properties keep their order (AAP 0.4.6). The HTML page's own cases live in
``tests/unit/test_reporting_html.py``."""

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import get_args
from xml.dom import minidom
from xml.etree import ElementTree

import pydantic
import pytest

import foundry.reporting as reporting
from foundry.config.enums import ProfileName
from foundry.core.exceptions import DataError, FoundryError
from foundry.core.types import ArtifactKind
from foundry.reporting import assets, compare, junit, renderers, store
from foundry.reporting.record import (
    ArtifactRecord,
    FailureRecord,
    Outcome,
    QuarantineRecord,
    ResultRecord,
    RunError,
    parse_element,
)

_RUN_ID = "20250102T030405-abc123"
_STARTED_AT = datetime(2025, 1, 2, 3, 4, 5, tzinfo=UTC)
_FINISHED_AT = datetime(2025, 1, 2, 3, 4, 5, 250_000, tzinfo=UTC)
_MODULE = "tests/test_cart.py"
_PROFILE = "ci"
_SECRET = "hunter2-secret-9f3"
_RENDERER_IDS = ("json", "html", "junit")
_BASE = {
    "run_id": _RUN_ID,
    "started_at": _STARTED_AT,
    "finished_at": _FINISHED_AT,
    "profile": _PROFILE,
    "outcome": Outcome.PASSED,
    "attempts": 1,
    "duration_ms": 12.5,
}
# Every character XML 1.0's Char production excludes, one case each, and the three it allows.
_FORBIDDEN = {
    "ansi-escape": "\x1b",
    "nul": "\x00",
    "vertical-tab": "\x0b",
    "form-feed": "\x0c",
    "backspace": "\x08",
    "lone-surrogate": "\ud800",
    "noncharacter-fffe": "\ufffe",
    "noncharacter-ffff": "\uffff",
}
_LEGAL = {"del": "\x7f", "c1-open": "\x80", "c1-close": "\x9f"}
_ARTIFACTS = tuple(
    ArtifactRecord(kind=kind, path=f"tests__test_cart.py__test_total/{name}", bytes=len(name))
    for kind, name in (
        ("request", "request.json"),
        ("response", "response.json"),
        ("stdout", "stdout.log"),
        ("stderr", "stderr.log"),
        ("traceback", "traceback.txt"),
    )
)
_EXPECTED_PROPERTIES = [
    ("foundry.profile", _PROFILE),
    ("foundry.outcome", "passed"),
    ("foundry.attempts", "3"),
    ("foundry.quarantine", "none"),
    *[(f"foundry.artifact.{artifact.kind}", artifact.path) for artifact in _ARTIFACTS],
]


def _record_for(name: str = "total", **overrides: object) -> ResultRecord:
    fields = {**_BASE, "nodeid": f"{_MODULE}::{name}", **overrides}
    return ResultRecord.model_validate(fields)


def _failed_record(**overrides: object) -> ResultRecord:
    return _record_for("test_total", outcome=Outcome.FAILED, **overrides)


def _run_error(worker_id: str = "gw1", message: str = "worker lost") -> RunError:
    return RunError(run_id=_RUN_ID, recorded_at=_STARTED_AT, worker_id=worker_id, message=message)


def _failure(phase: str, message: str = "boom") -> FailureRecord:
    return FailureRecord(phase=phase, message=message)


def _artifact(path: str) -> ArtifactRecord:
    return ArtifactRecord(kind="request", path=path, bytes=1)


def _properties(element: ElementTree.Element) -> dict[str, str]:
    found = element.findall("properties/property")
    return {prop.get("name", ""): prop.get("value", "") for prop in found}


def _property_pairs(element: ElementTree.Element) -> list[tuple[str, str]]:
    # The published property order, which a dict would lose.
    holder = element.find("properties")
    assert holder is not None
    return [(prop.get("name", ""), prop.get("value", "")) for prop in holder.findall("property")]


def test_the_package_publishes_the_documented_enumeration() -> None:
    assert " ".join(member.value for member in Outcome) == (
        "passed failed skipped xfailed xpassed error quarantined_failed quarantine_passed not_run"
    )
    assert tuple(Outcome) == reporting.OUTCOMES and all(str(m) == m.value for m in Outcome)
    assert " ".join(ResultRecord.model_fields) == (
        "record_type run_id started_at finished_at profile nodeid outcome attempts duration_ms"
        " markers failures artifacts quarantine artifacts_disabled"
    )
    assert [member.value for member in ProfileName] == list(reporting.PROFILE_NAMES)
    kinds = "request response stdout stderr traceback log_tail manifest"
    assert " ".join(get_args(ArtifactKind)) == kinds


def test_records_are_frozen() -> None:
    cases = [
        (_record_for(), "nodeid", "tests/other.py::test_other"),
        (_failure("call"), "message", "other"),
        (_artifact("run/request.json"), "bytes", 2),
        (QuarantineRecord(state="none"), "state", "failed"),
        (_run_error(), "message", "other"),
    ]
    for instance, attribute, value in cases:
        with pytest.raises(pydantic.ValidationError):
            setattr(instance, attribute, value)


_REJECTED = (
    pytest.param(lambda: _record_for(attempts=0), id="attempts-zero"),
    pytest.param(lambda: _record_for(duration_ms=-0.001), id="duration-negative"),
    pytest.param(lambda: _artifact("/etc/passwd"), id="path-root"),
    pytest.param(lambda: _artifact("../o.json"), id="path-parent"),
    pytest.param(lambda: _artifact("r/../../o.json"), id="path-escape"),
    pytest.param(lambda: _failure("collect"), id="phase-collect"),
    pytest.param(lambda: _failure("CALL"), id="phase-uppercase"),
)


@pytest.mark.parametrize("build", _REJECTED)
def test_a_documented_invalid_value_is_refused(build: Callable[[], object]) -> None:
    with pytest.raises(pydantic.ValidationError):
        build()


@pytest.mark.parametrize(
    ("outcome", "state"),
    [
        (Outcome.PASSED, "none"),
        (Outcome.FAILED, "none"),
        (Outcome.QUARANTINED_FAILED, "failed"),
        (Outcome.QUARANTINE_PASSED, "passed"),
    ],
)
def test_quarantine_state_agrees_with_the_outcome(outcome: Outcome, state: str) -> None:
    for candidate in ("none", "failed", "passed"):
        quarantine = QuarantineRecord(state=candidate)
        if candidate == state:
            assert _record_for(outcome=outcome, quarantine=quarantine).quarantine.state == state
        else:
            with pytest.raises(pydantic.ValidationError):
                _record_for(outcome=outcome, quarantine=quarantine)


def test_the_store_sorts_redacts_and_writes_byte_identically(tmp_path: Path) -> None:
    elements = [_run_error("gw2", "second"), _record_for("c"), _record_for("a"), _run_error()]
    payload = json.loads(store.dumps(elements))
    assert [entry["nodeid"] for entry in payload[:2]] == [f"{_MODULE}::a", f"{_MODULE}::c"]
    assert [e["record_type"] for e in payload[2:]] == ["run_error"] * 2
    assert [e["worker_id"] for e in payload[2:]] == ["gw1", "gw2"]
    assert "nodeid" in payload[0] and "nodeid" not in payload[2] and "outcome" not in payload[2]
    failures = (_failure("call", f"boom: token={_SECRET}"),)
    failing = [_record_for("b"), _record_for("a", outcome=Outcome.FAILED, failures=failures)]
    assert _SECRET not in (document := store.dumps(failing)) and "token=***" in document
    assert store.dumps([]) == "[]\n"
    source = tmp_path / "broken.json"
    with pytest.raises(DataError) as excinfo:
        parse_element({"record_type": "result", "run_id": "not-a-run-id"}, source=source)
    assert excinfo.value.file == source and str(source) in str(excinfo.value)
    first, second = [store.write(failing, tmp_path / n / "results.json") for n in ("a", "b")]
    assert first is not None and second is not None
    assert first.read_text(encoding="utf-8") == store.dumps(failing) == second.read_text()


def test_the_suite_carries_the_run_identity_and_a_testcase_its_attributes() -> None:
    failures = (_failure("call", f"AssertionError: token={_SECRET}"),)
    failed = _record_for("a", outcome=Outcome.FAILED, failures=failures)
    document = junit.render([failed, _run_error("gw3", "worker died")])
    root = ElementTree.fromstring(document)
    assert root.tag == "testsuite" and root.get("tests") == "1" and root.get("failures") == "1"
    assert _properties(root) == {"foundry.run_id": _RUN_ID, "foundry.profile": "ci"}
    assert [element.text for element in root.findall("system-err")] == ["gw3: worker died"]
    assert _SECRET not in document
    failure = root.find("testcase/failure")
    assert failure is not None and "***" in failure.get("message", "")
    assert (failure.text or "").startswith("[call] ")
    errored = ElementTree.fromstring(junit.render([_record_for(outcome=Outcome.ERROR)]))
    assert errored.find("testcase/error") is not None and errored.get("errors") == "1"
    # AAP 0.4.6: the case carries its module, its name and its duration; the artifact properties
    # are the subject of the cases below.
    el = ElementTree.fromstring(junit.render([_record_for("a", attempts=3)])).find("testcase")
    assert el is not None and (el.get("classname"), el.get("name")) == ("tests.test_cart", "a")
    assert el.get("time") == "0.013"


@pytest.mark.parametrize(
    ("outcome", "message"),
    [
        (Outcome.SKIPPED, "foundry: skipped"),
        (Outcome.XFAILED, "foundry: expected failure"),
        (Outcome.QUARANTINED_FAILED, "foundry: quarantined failure"),
        (Outcome.NOT_RUN, "foundry: not run"),
    ],
)
def test_a_non_counting_outcome_skips_with_its_message(outcome: Outcome, message: str) -> None:
    state = "failed" if outcome is Outcome.QUARANTINED_FAILED else "none"
    record = _record_for(outcome=outcome, quarantine=QuarantineRecord(state=state))
    root = ElementTree.fromstring(junit.render([record]))
    testcase = root.find("testcase")
    assert testcase is not None and testcase.find("failure") is None
    skipped = testcase.find("skipped")
    assert skipped is not None and skipped.get("message") == message
    assert root.get("failures") == "0" and root.get("skipped") == "1"


def test_a_quarantined_test_that_passed_carries_a_removal_note() -> None:
    quarantine = QuarantineRecord(state="passed")
    record = _record_for(outcome=Outcome.QUARANTINE_PASSED, quarantine=quarantine)
    testcase = ElementTree.fromstring(junit.render([record])).find("testcase")
    assert testcase is not None and testcase.find("skipped") is None
    note = testcase.find("system-out")
    assert note is not None and "remove the foundry_quarantine marker" in (note.text or "")
    xpassed = ElementTree.fromstring(junit.render([_record_for(outcome=Outcome.XPASSED)]))
    assert junit.XPASSED_NOTE in (xpassed.findtext("testcase/system-out") or "")


def test_the_format_table_is_stable_and_writes_three_documents(tmp_path: Path) -> None:
    specs = list(renderers.RENDERERS.values())
    assert renderers.registered_formats() == _RENDERER_IDS == tuple(s.name for s in specs)
    assert [spec.filename for spec in specs] == ["results.json", "report.html", "junit.xml"]
    assert sorted(spec.order for spec in specs) == [0, 1, 2]
    assert all(callable(spec.writer) for spec in specs)
    assert all(t and not assets.contains_external_reference(t) for t in (assets.CSS, assets.JS))
    assets.assert_self_contained(assets.CSS + assets.JS)
    with pytest.raises(FoundryError) as excinfo:
        renderers.renderer_for("xml")
    assert "html" in (excinfo.value.hint or "")
    before = [(name, id(spec)) for name, spec in renderers.RENDERERS.items()]
    written = renderers.write_all([_record_for("a")], tmp_path)
    assert [path.name for path in written.written()] == ["results.json", "report.html", "junit.xml"]
    assert [(name, id(spec)) for name, spec in renderers.RENDERERS.items()] == before


def test_the_comparison_is_reachable_from_the_package_barrel() -> None:
    assert reporting.diff is compare.diff


@pytest.mark.parametrize(("label", "forbidden"), _FORBIDDEN.items(), ids=list(_FORBIDDEN))
def test_a_forbidden_character_never_reaches_the_document(
    label: str, forbidden: str, tmp_path: Path
) -> None:
    failures = (FailureRecord(phase="call", message=f"AssertionError: nope{forbidden}"),)
    artifact = ArtifactRecord(kind="request", path=f"run/request{forbidden}[31m.json", bytes=10)
    nodeid = f"{_MODULE}::test_{forbidden}total"
    elements = [_failed_record(nodeid=nodeid, failures=failures, artifacts=(artifact,))]
    elements += [_failed_record(), _run_error(message=f"worker lost{forbidden}")]
    document = junit.render(elements)
    assert forbidden not in document
    survivors = ("AssertionError: nope", "test_total", "worker lost", "request[31m.json")
    assert all(word in document for word in survivors)
    assert junit.NO_FAILURE_DETAIL_MESSAGE in document
    ElementTree.fromstring(document)
    minidom.parseString(document.encode("utf-8"))
    target = tmp_path / f"{label}.xml"
    assert junit.write(elements, target) == target
    assert target.read_text(encoding="utf-8") == document


@pytest.mark.parametrize(("label", "legal"), _LEGAL.items(), ids=list(_LEGAL))
def test_the_characters_xml_allows_survive_unchanged(label: str, legal: str) -> None:
    message = f"keep{legal}inside"
    failures = (FailureRecord(phase="call", message=message),)
    document = junit.render([_failed_record(failures=failures)])
    assert legal in document
    failure = ElementTree.fromstring(document).find("testcase/failure")
    assert failure is not None and failure.get("message") == message


def test_the_documented_properties_and_artifact_references_are_unchanged() -> None:
    chosen = {"outcome": Outcome.PASSED, "attempts": 3, "artifacts": _ARTIFACTS}
    record = _record_for(quarantine=QuarantineRecord(state="none"), **chosen)
    testcase = ElementTree.fromstring(junit.render([record])).find("testcase")
    assert testcase is not None and _property_pairs(testcase) == _EXPECTED_PROPERTIES


def test_a_markup_node_id_is_still_escaped() -> None:
    document = junit.render([_failed_record(nodeid=f"{_MODULE}::test_<script>alert(1)</script>")])
    assert "&lt;script&gt;" in document and "<script" not in document
    testcase = ElementTree.fromstring(document).find("testcase")
    assert testcase is not None and testcase.get("name") == "test_<script>alert(1)</script>"
