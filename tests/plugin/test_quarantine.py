"""Quarantine via ``pytester``: both modes, their report sections, and the removal note."""

import json
from collections.abc import Mapping
from typing import Any, Final
from xml.etree import ElementTree

import pytest

REACHES: tuple[str, ...] = ()

_MARKER: Final[str] = "foundry_quarantine"
_SKIP_MESSAGE: Final[str] = "foundry: quarantined failure"
_NOTE_ACTION: Final[str] = "remove"
_STRICT_OPTION: Final[str] = "--foundry-execution.quarantine_mode"
_STRICT_TOML: Final[str] = '[execution]\nquarantine_mode = "strict"\n'
_STRICT_INI: Final[str] = "[pytest]\naddopts = -ra --strict-markers\n"
# The report's Quarantined section anchor: unique in the document, unlike the word quarantine.
_HEADING: Final[str] = 'id="quarantined-heading"'

_QUARANTINED_FAILURE: Final[str] = (
    "import pytest\n\n\n@pytest.mark.foundry_quarantine\ndef test_known_flake() -> None:\n"
    '    assert False, "quarantine sentinel"\n'
)

_QUARANTINED_PASS: Final[str] = (
    "import pytest\n\n\n@pytest.mark.foundry_quarantine\ndef test_recovered() -> None:\n"
    "    assert True\n"
)

_PLAIN_FAILURE: Final[str] = (
    'def test_plain_failure() -> None:\n    assert False, "plain sentinel"\n'
)

_MIXED_FAILURES: Final[str] = (
    'import pytest\n\n\ndef test_plain_failure() -> None:\n    assert False, "plain sentinel"\n'
    "\n\n@pytest.mark.foundry_quarantine\ndef test_known_flake() -> None:\n"
    '    assert False, "quarantine sentinel"\n'
)


def _read(pytester: pytest.Pytester, name: str) -> str:
    path = pytester.path / name
    assert path.is_file(), f"{name} was not written to {pytester.path}"
    return path.read_text(encoding="utf-8")


def _records(pytester: pytest.Pytester) -> dict[str, dict[str, Any]]:
    document = json.loads(_read(pytester, "results.json"))
    assert isinstance(document, list), document
    assert [item.get("record_type") for item in document] == ["result"] * len(document)
    return {str(item["nodeid"]): item for item in document}


def _only_record(pytester: pytest.Pytester) -> dict[str, Any]:
    records = _records(pytester)
    assert len(records) == 1, f"expected one result record, got {sorted(records)}"
    return next(iter(records.values()))


def _assert_record_invariants(record: Mapping[str, Any]) -> None:
    """Assert the AAP 0.7.2 invariants every outcome keeps, quarantine state included."""
    expected = {"quarantined_failed": "failed", "quarantine_passed": "passed"}
    assert record["attempts"] >= 1 and record["duration_ms"] >= 0
    assert record["markers"] == sorted(set(record["markers"]))
    assert record["quarantine"] == {"state": expected.get(record["outcome"], "none")}


def _testcase(pytester: pytest.Pytester, test: str) -> ElementTree.Element:
    cases = ElementTree.parse(pytester.path / "junit.xml").getroot().iter("testcase")
    found = [case for case in cases if case.get("name") == test]
    assert found, f"junit.xml carries no <testcase> named {test!r}"
    return found[0]


def _skipped_messages(case: ElementTree.Element) -> list[str]:
    return [element.get("message", "") for element in case.iter("skipped")]


def _quarantined_section(pytester: pytest.Pytester) -> str:
    text = _read(pytester, "report.html")
    start = text.find(_HEADING)
    assert start >= 0, "report.html carries no Quarantined section (AAP 0.4.6)"
    return text[start : text.find("</section>", start)]


def test_report_mode_keeps_the_run_green(
    pytester: pytest.Pytester, clean_foundry_env: None
) -> None:
    pytester.makepyfile(test_flake=_QUARANTINED_FAILURE)
    pytester.makeini(_STRICT_INI)
    result = pytester.runpytest()
    assert result.ret == 0
    result.assert_outcomes(failed=1)
    assert "unknown mark" not in result.stdout.str().lower()
    nodeid = "test_flake.py::test_known_flake"
    record = _only_record(pytester)
    assert record["nodeid"] == nodeid and _MARKER in record["markers"]
    assert record["outcome"] == "quarantined_failed" and record["quarantine"] == {"state": "failed"}
    assert record["attempts"] == 1 and [item["phase"] for item in record["failures"]] == ["call"]
    _assert_record_invariants(record)
    assert _skipped_messages(_testcase(pytester, "test_known_flake")) == [_SKIP_MESSAGE]
    section = _quarantined_section(pytester)
    assert nodeid in section and "quarantined_failed" in section


@pytest.mark.parametrize(
    ("argv", "foundry_toml"),
    [((_STRICT_OPTION, "strict"), None), ((), _STRICT_TOML)],
    ids=["cli", "file"],
)
def test_strict_mode_counts_the_record_against_the_run(
    pytester: pytest.Pytester,
    clean_foundry_env: None,
    argv: tuple[str, ...],
    foundry_toml: str | None,
) -> None:
    pytester.makepyfile(test_flake=_QUARANTINED_FAILURE)
    if foundry_toml is not None:
        pytester.makefile(".toml", foundry=foundry_toml)
    result = pytester.runpytest(*argv)
    assert result.ret == 1
    result.assert_outcomes(failed=1)
    record = _only_record(pytester)
    assert record["outcome"] == "quarantined_failed" and record["quarantine"] == {"state": "failed"}
    _assert_record_invariants(record)
    # AAP 0.4.6 maps an outcome and not a mode, so the CI-visible element is identical either way.
    assert _skipped_messages(_testcase(pytester, "test_known_flake")) == [_SKIP_MESSAGE]


def test_passing_quarantined_test_is_a_success_with_a_removal_note(
    pytester: pytest.Pytester, clean_foundry_env: None
) -> None:
    pytester.makepyfile(test_recovered=_QUARANTINED_PASS)
    result = pytester.runpytest()
    assert result.ret == 0
    result.assert_outcomes(passed=1)
    nodeid = "test_recovered.py::test_recovered"
    record = _only_record(pytester)
    assert record["nodeid"] == nodeid and record["outcome"] == "quarantine_passed"
    assert record["quarantine"] == {"state": "passed"} and _MARKER in record["markers"]
    _assert_record_invariants(record)
    case = _testcase(pytester, "test_recovered")
    assert _skipped_messages(case) == []
    note = "".join(element.text or "" for element in case.iter("system-out"))
    assert _MARKER in note and _NOTE_ACTION in note.lower()
    section = _quarantined_section(pytester).lower()
    assert nodeid.lower() in section and _MARKER in section and _NOTE_ACTION in section


def test_quarantine_does_not_mask_a_real_failure(
    pytester: pytest.Pytester, clean_foundry_env: None
) -> None:
    pytester.makepyfile(test_mixed=_MIXED_FAILURES)
    result = pytester.runpytest()
    assert result.ret == 1
    result.assert_outcomes(failed=2)
    records = _records(pytester)
    assert len(records) == 2, sorted(records)
    plain = records["test_mixed.py::test_plain_failure"]
    flake = records["test_mixed.py::test_known_flake"]
    assert (plain["outcome"], plain["quarantine"]) == ("failed", {"state": "none"})
    assert (flake["outcome"], flake["quarantine"]) == ("quarantined_failed", {"state": "failed"})
    for record in (plain, flake):
        _assert_record_invariants(record)


def test_control_unquarantined_failure_fails_the_run(
    pytester: pytest.Pytester, clean_foundry_env: None
) -> None:
    pytester.makepyfile(test_plain=_PLAIN_FAILURE)
    result = pytester.runpytest()
    assert result.ret == 1
    result.assert_outcomes(failed=1)
    record = _only_record(pytester)
    assert record["nodeid"] == "test_plain.py::test_plain_failure"
    assert record["outcome"] == "failed" and record["quarantine"] == {"state": "none"}
    assert record["markers"] == []
    _assert_record_invariants(record)
    assert _HEADING not in _read(pytester, "report.html")
