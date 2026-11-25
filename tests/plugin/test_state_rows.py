"""AAP 0.4.7's fixture-fault and interrupt rows as a session shows them: the halves the constructor
seam in test_http.py and the exit-code seam in test_cli.py cannot see. The interrupt case needs a
child process, because pytester's in-process runner re-raises an inner session's interrupt."""

from __future__ import annotations

import json
from typing import Any, Final

import pytest

REACHES: tuple[str, ...] = ("fixture:foundry_api", "format:results.json", "format:report.html")

_RESULTS_FILE: Final[str] = "results.json"
_REPORT_FILE: Final[str] = "report.html"
_JUNIT_FILE: Final[str] = "junit.xml"
_EXTERNAL_SCHEMES: Final[tuple[str, ...]] = ("http://", "https://")
_CHILD_TIMEOUT_SECONDS: Final[float] = 180.0

_API_MODULE: Final[str] = "test_api_without_a_base_url"
_INTERRUPT_MODULE: Final[str] = "test_interrupt_in_the_call_phase"

# Each project holds one module and nothing else, so the run resolves the defaults: no foundry.toml
# and no FOUNDRY_* variable names http.base_url.
_API_WITHOUT_BASE_URL: Final[str] = '''def test_requests_the_configured_client(foundry_api) -> None:
    """Request a path, which the fixture refuses to serve without a base URL."""
    foundry_api.get("/health")


def test_still_runs_after_the_error() -> None:
    assert True
'''

_INTERRUPT_IN_CALL: Final[str] = """def test_before_the_interrupt() -> None:
    assert True


def test_interrupts_in_the_call_phase() -> None:
    raise KeyboardInterrupt


def test_after_the_interrupt() -> None:
    assert False, "the session stopped at the interrupt, so this test must never be reached"
"""

_ERRORING_NODE: Final[str] = f"{_API_MODULE}.py::test_requests_the_configured_client"
_CONTINUED_NODE: Final[str] = f"{_API_MODULE}.py::test_still_runs_after_the_error"
_COMPLETED_NODE: Final[str] = f"{_INTERRUPT_MODULE}.py::test_before_the_interrupt"
_INTERRUPTED_NODE: Final[str] = f"{_INTERRUPT_MODULE}.py::test_interrupts_in_the_call_phase"
_UNREACHED_NODE: Final[str] = f"{_INTERRUPT_MODULE}.py::test_after_the_interrupt"


def _read(pytester: pytest.Pytester, name: str) -> str:
    path = pytester.path / name
    assert path.is_file(), (
        f"{name} was not written to {pytester.path}; the directory holds "
        f"{sorted(entry.name for entry in pytester.path.iterdir())}"
    )
    return path.read_text(encoding="utf-8")


def _records(pytester: pytest.Pytester) -> dict[str, dict[str, Any]]:
    # Neither project loses a worker, so a non-result element is a contract breach.
    document = json.loads(_read(pytester, _RESULTS_FILE))
    assert isinstance(document, list), f"{_RESULTS_FILE} is not a JSON array"
    records: dict[str, dict[str, Any]] = {}
    for element in document:
        assert isinstance(element, dict), f"{_RESULTS_FILE} holds a non-object: {element!r}"
        assert element.get("record_type") == "result", f"unexpected element kind: {element!r}"
        records[str(element["nodeid"])] = element
    return records


def _outcomes(records: dict[str, dict[str, Any]]) -> dict[str, str]:
    return {nodeid: str(record["outcome"]) for nodeid, record in records.items()}


def test_a_fixture_without_a_base_url_errors_one_test_and_the_session_runs_on(
    pytester: pytest.Pytester, clean_foundry_env: None
) -> None:
    pytester.makepyfile(**{_API_MODULE: _API_WITHOUT_BASE_URL})
    result = pytester.runpytest("-q")
    assert result.ret == 1, (
        f"the run exited {result.ret}, not 1: a fixture that cannot be built errors the one test "
        f"that requested it rather than ending the session\n{result.stdout.str()}"
    )
    result.assert_outcomes(passed=1, errors=1)
    records = _records(pytester)
    assert _outcomes(records) == {_ERRORING_NODE: "error", _CONTINUED_NODE: "passed"}, (
        f"the session must record this outcome per test and no other: {_outcomes(records)}"
    )
    error = records[_ERRORING_NODE]
    phases = [failure["phase"] for failure in error["failures"]]
    assert phases == ["setup"], f"the fault must be reported in setup, not in {phases}"
    messages = "\n".join(str(failure["message"]) for failure in error["failures"])
    assert "http.base_url" in messages, (
        f"the erroring test's failure message must name http.base_url: {messages[:2000]}"
    )
    for name in (_RESULTS_FILE, _JUNIT_FILE, _REPORT_FILE):
        _read(pytester, name)
    report = _read(pytester, _REPORT_FILE)
    assert _ERRORING_NODE in report and _CONTINUED_NODE in report, (
        "report.html must render both the error the fixture caused and the test that ran after it"
    )


def test_an_interrupt_flushes_partial_results_and_writes_the_report(
    pytester: pytest.Pytester, clean_foundry_env: None
) -> None:
    pytester.makepyfile(**{_INTERRUPT_MODULE: _INTERRUPT_IN_CALL})
    result = pytester.runpytest_subprocess("-q", timeout=_CHILD_TIMEOUT_SECONDS)
    assert result.ret == 2, (
        f"the interrupted run exited {result.ret}, not 2: pytest's interrupt code is preserved, "
        f"not remapped\n{result.stdout.str()}{result.stderr.str()}"
    )
    records = _records(pytester)
    assert records, "results.json holds no record: the test that completed must be flushed"
    assert records[_COMPLETED_NODE]["outcome"] == "passed", (
        f"the completed test must be recorded passed: {_outcomes(records)}"
    )
    passed = sorted(n for n, outcome in _outcomes(records).items() if outcome == "passed")
    assert passed == [_COMPLETED_NODE], (
        f"exactly the test that finished may read as passed: {_outcomes(records)}"
    )
    for nodeid in (_INTERRUPTED_NODE, _UNREACHED_NODE):
        record = records.get(nodeid)
        assert record is None or record["outcome"] == "not_run", (
            f"{nodeid} carries {record} where the flush may only report it not_run or leave it out"
        )
    report = _read(pytester, _REPORT_FILE)
    assert _COMPLETED_NODE in report, "report.html must render the flushed run"
    assert str(records[_COMPLETED_NODE]["run_id"]) in report, (
        "report.html must be this run's report, so it carries the run id results.json records"
    )
    for scheme in _EXTERNAL_SCHEMES:
        assert scheme not in report, (
            f"report.html references {scheme}, so it stops opening offline (assumption I10)"
        )
    _read(pytester, _JUNIT_FILE)
