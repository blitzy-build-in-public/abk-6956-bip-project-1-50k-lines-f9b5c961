"""The XML character guarantee ``foundry.reporting.junit`` owes a CI reader (AAP 0.4.6).

Every record-derived string is reduced to XML 1.0's ``Char`` production before serialising -- a
failure message, a node id, a run-error message and a property value alike -- while the characters
XML does allow survive and the five ``foundry.artifact.*`` properties keep their order.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from xml.dom import minidom
from xml.etree import ElementTree

import pytest

from foundry.reporting import junit
from foundry.reporting.record import (
    ArtifactRecord,
    FailureRecord,
    Outcome,
    QuarantineRecord,
    ResultRecord,
    RunError,
)

_RUN_ID = "20250102T030405-abc123"
_PROFILE = "ci"
_STARTED_AT = datetime(2025, 1, 2, 3, 4, 5, 0, tzinfo=UTC)
_FINISHED_AT = datetime(2025, 1, 2, 3, 4, 5, 250_000, tzinfo=UTC)
_MODULE = "tests/test_cart.py"
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


def _record(**overrides: object) -> ResultRecord:
    fields: dict[str, object] = {
        "run_id": _RUN_ID,
        "started_at": _STARTED_AT,
        "finished_at": _FINISHED_AT,
        "profile": _PROFILE,
        "nodeid": f"{_MODULE}::test_total",
        "outcome": Outcome.FAILED,
        "attempts": 1,
        "duration_ms": 12.5,
    }
    fields.update(overrides)
    return ResultRecord.model_validate(fields)


def _run_error(message: str) -> RunError:
    return RunError(run_id=_RUN_ID, recorded_at=_STARTED_AT, worker_id="gw1", message=message)


def _properties(element: ElementTree.Element) -> list[tuple[str, str]]:
    holder = element.find("properties")
    assert holder is not None
    return [(prop.get("name", ""), prop.get("value", "")) for prop in holder.findall("property")]


@pytest.mark.parametrize(("label", "forbidden"), _FORBIDDEN.items(), ids=list(_FORBIDDEN))
def test_a_forbidden_character_never_reaches_the_document(
    label: str, forbidden: str, tmp_path: Path
) -> None:
    failures = (FailureRecord(phase="call", message=f"AssertionError: nope{forbidden}"),)
    artifact = ArtifactRecord(kind="request", path=f"run/request{forbidden}[31m.json", bytes=10)
    nodeid = f"{_MODULE}::test_{forbidden}total"
    elements = [_record(nodeid=nodeid, failures=failures, artifacts=(artifact,)), _record()]
    elements.append(_run_error(f"worker lost{forbidden}"))
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
    document = junit.render([_record(failures=(FailureRecord(phase="call", message=message),))])
    assert legal in document
    failure = ElementTree.fromstring(document).find("testcase/failure")
    assert failure is not None and failure.get("message") == message


def test_the_documented_properties_and_artifact_references_are_unchanged() -> None:
    verdict = QuarantineRecord(state="none")
    record = _record(outcome=Outcome.PASSED, attempts=3, artifacts=_ARTIFACTS, quarantine=verdict)
    testcase = ElementTree.fromstring(junit.render([record])).find("testcase")
    assert testcase is not None and _properties(testcase) == _EXPECTED_PROPERTIES


def test_a_markup_node_id_is_still_escaped() -> None:
    nodeid = f"{_MODULE}::test_<script>alert(1)</script>"
    document = junit.render([_record(nodeid=nodeid)])
    assert "&lt;script&gt;" in document and "<script" not in document
    testcase = ElementTree.fromstring(document).find("testcase")
    assert testcase is not None and testcase.get("name") == "test_<script>alert(1)</script>"
