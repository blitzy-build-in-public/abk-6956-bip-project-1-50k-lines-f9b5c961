"""AAP 0.4.7's fixture-fault and interrupt rows, driven through a session and read back.

Two of AAP 0.4.7's rows describe what a whole run does, and each of them was asserted at one seam
only: the ``http.base_url``-unset row at the ``SessionSettings(require_base_url=True)`` constructor
in ``tests/unit/test_http.py``, the ``KeyboardInterrupt`` row's exit code in
``tests/unit/test_cli.py``. Neither seam can observe the half a consumer depends on -- an erroring
fixture that leaves the rest of the session running, and a run stopped mid-suite with the work it
finished still on disk -- because both are facts about a run rather than about a call.

**The rows, in AAP 0.4.7's words.** Row "``http.base_url`` unset while ``foundry_api`` is used": the
fixture raises ``ConfigError`` naming ``http.base_url``, that test errors and the rest of the
session still runs, exit 1. Row "``KeyboardInterrupt``": partial results are flushed, the report is
written, pytest's interrupt code is preserved (2).

**Real entry points** (AAP 0.14.5). Each case runs pytest itself over a project written into
``pytester``'s own directory and then reads the documents that session wrote, so the evidence is a
run's exit code and a run's files, never a hand-built record. Nothing here mocks the plugin, the
configuration layer or a renderer.

**Why the interrupt case runs in a child process.** ``pytester``'s in-process runner re-raises an
inner session's ``KeyboardInterrupt`` in the calling test, so that route cannot observe the
interrupt code this row pins. A child process also observes the code the way a user's shell does,
as the process's own exit status. The fixture case, whose session ends normally, runs in process.

**Determinism** (AAP 0.14.3): no sleep, no wall-clock reading, both documents compared by key rather
than by position, and each case drives its own project in a directory ``pytester`` owns.
"""

from __future__ import annotations

import json
from typing import Any, Final

import pytest

REACHES: tuple[str, ...] = (
    "fixture:foundry_api",
    "format:results.json",
    "format:report.html",
)
"""What these cases reach through a real session, for ``tests/plugin/test_reachability.py``.

The fixture case requests ``foundry_api`` in a session pytest started, and both cases read the
``results.json`` and ``report.html`` that session wrote, so all three entries are claims this module
keeps (AAP 0.4.3, 0.4.6, 0.14.5).
"""

# --- The contract values AAP 0.4.4, 0.4.6 and 0.4.7 fix ----------------------

_BASE_URL_KEY: Final[str] = "http.base_url"
"""The key ``foundry_api`` requires and the AAP 0.4.7 row says the ``ConfigError`` names."""

_FAILURE_EXIT: Final[int] = 1
"""The exit code the fixture row ends on: a test outcome, not a configuration fault (AAP 0.4.7)."""

_INTERRUPT_EXIT: Final[int] = 2
"""pytest's interrupt code, which AAP 0.4.7 preserves rather than remapping (AAP 0.4.4)."""

_ERROR_OUTCOME: Final[str] = "error"
"""The outcome a fixture fault lands as, pytest reporting it in setup apart from a failure."""

_PASSED_OUTCOME: Final[str] = "passed"
"""The outcome a completed, successful test carries in ``results.json`` (AAP 0.4.6)."""

_NOT_RUN_OUTCOME: Final[str] = "not_run"
"""The outcome an item that never executed carries (assumption A12), never a pass."""

_RESULTS_FILE: Final[str] = "results.json"
_REPORT_FILE: Final[str] = "report.html"
_JUNIT_FILE: Final[str] = "junit.xml"
"""AAP 0.4.6's three report documents, under the fixed names it writes them as."""

_EXTERNAL_SCHEMES: Final[tuple[str, ...]] = ("http://", "https://")
"""The references AAP 0.1.1's assumption I10 keeps out of a report that has to open offline."""

_CHILD_TIMEOUT_SECONDS: Final[float] = 180.0
"""Ceiling on the interrupted child, so a hang fails this case instead of stalling the suite."""

_SETUP_PHASE: Final[str] = "setup"
"""The phase a broken fixture reports in, which makes the row's test an error rather than a fail."""

# --- The generated projects -------------------------------------------------
# Each project holds one module and nothing else, so the run's configuration is the defaults: no
# ``foundry.toml``, no ``pyproject.toml`` and no ``FOUNDRY_*`` variable names ``http.base_url``.

_API_MODULE: Final[str] = "test_api_without_a_base_url"
_INTERRUPT_MODULE: Final[str] = "test_interrupt_in_the_call_phase"
"""The two generated module names, which every node id asserted below is built from."""

_API_WITHOUT_BASE_URL: Final[str] = '''"""A suite whose client has no base URL."""


def test_requests_the_configured_client(foundry_api) -> None:
    """Request a path, which the fixture refuses to serve without a base URL."""
    foundry_api.get("/health")


def test_still_runs_after_the_error() -> None:
    """Pass, so the record set shows the session went on past the erroring test."""
    assert True
'''
"""AAP 0.4.7's fixture row: the failure is the one the ``foundry_api`` fixture raises in setup."""

_INTERRUPT_IN_CALL: Final[str] = '''"""A suite interrupted in its middle test's call phase."""


def test_before_the_interrupt() -> None:
    """Pass, so the flush has a completed result to carry."""
    assert True


def test_interrupts_in_the_call_phase() -> None:
    """Raise the interrupt from the call phase, as an interrupt at the keyboard does."""
    raise KeyboardInterrupt


def test_after_the_interrupt() -> None:
    """Never run, because the session stops at the interrupt."""
    assert False, "the session stopped at the interrupt, so this test must never be reached"
'''
"""AAP 0.4.7's interrupt row: one completed test, the interrupt, and a test left unrun behind it."""

_ERRORING_NODE: Final[str] = f"{_API_MODULE}.py::test_requests_the_configured_client"
_CONTINUED_NODE: Final[str] = f"{_API_MODULE}.py::test_still_runs_after_the_error"
_COMPLETED_NODE: Final[str] = f"{_INTERRUPT_MODULE}.py::test_before_the_interrupt"
_INTERRUPTED_NODE: Final[str] = f"{_INTERRUPT_MODULE}.py::test_interrupts_in_the_call_phase"
_UNREACHED_NODE: Final[str] = f"{_INTERRUPT_MODULE}.py::test_after_the_interrupt"
"""The five node ids the two projects collect, as pytest reports them in the project directory."""

# --- Reading what a run wrote ----------------------------------------------


def _read(pytester: pytest.Pytester, name: str) -> str:
    """Return the text of one report file the run wrote, naming the directory when it is absent.

    A missing document is the interesting failure here, so the assertion says what the run did leave
    behind instead of raising a bare ``FileNotFoundError``.
    """
    path = pytester.path / name
    assert path.is_file(), (
        f"{name} was not written to {pytester.path}; the directory holds "
        f"{sorted(entry.name for entry in pytester.path.iterdir())}"
    )
    return path.read_text(encoding="utf-8")


def _records(pytester: pytest.Pytester) -> dict[str, dict[str, Any]]:
    """Return the run's result records, keyed by node id, from the ``results.json`` it wrote.

    AAP 0.4.6 makes the document a JSON array discriminated by ``record_type``; neither project here
    loses a worker, so an element that is anything but a result record is a contract breach rather
    than something to skip over.
    """
    document = json.loads(_read(pytester, _RESULTS_FILE))
    assert isinstance(document, list), (
        f"{_RESULTS_FILE} is not a JSON array but {type(document).__name__}"
    )
    records: dict[str, dict[str, Any]] = {}
    for element in document:
        assert isinstance(element, dict), f"{_RESULTS_FILE} holds a non-object element: {element!r}"
        assert element.get("record_type") == "result", f"unexpected element kind: {element!r}"
        records[str(element["nodeid"])] = element
    return records


def _outcomes(records: dict[str, dict[str, Any]]) -> dict[str, str]:
    """Return each recorded node id with its outcome, so an unexpected record is visible at once."""
    return {nodeid: str(record["outcome"]) for nodeid, record in records.items()}


# --- Row 5: `http.base_url` unset while `foundry_api` is used (AAP 0.4.7) ---


def test_a_fixture_without_a_base_url_errors_one_test_and_the_session_runs_on(
    pytester: pytest.Pytester, clean_foundry_env: None
) -> None:
    """The row as a session shows it: one error naming the key, one pass, exit 1, three documents.

    ``tests/unit/test_http.py`` proves the constructor refuses a session that requires a base URL.
    What it cannot show is this row's second half, which is the half the row is about: the fault
    lands on the test that requested the fixture, the run is not a configuration fault that ends the
    session in ``pytest_configure`` (assumption A4), and it ends on 1 -- so both the other test's
    record and the run's own report documents have to be there.
    """
    pytester.makepyfile(**{_API_MODULE: _API_WITHOUT_BASE_URL})

    result = pytester.runpytest("-q")

    assert result.ret == _FAILURE_EXIT, (
        f"the run exited {result.ret}, not {_FAILURE_EXIT}: a fixture that cannot be built errors "
        f"the one test that requested it rather than ending the session\n{result.stdout.str()}"
    )
    result.assert_outcomes(passed=1, errors=1)
    records = _records(pytester)
    assert _outcomes(records) == {
        _ERRORING_NODE: _ERROR_OUTCOME,
        _CONTINUED_NODE: _PASSED_OUTCOME,
    }, f"the session must record this outcome per test and no other: {_outcomes(records)}"
    error = records[_ERRORING_NODE]
    phases = [failure["phase"] for failure in error["failures"]]
    assert phases == [_SETUP_PHASE], (
        f"the fault must be reported in {_SETUP_PHASE}, where a fixture runs, not in {phases}"
    )
    messages = "\n".join(str(failure["message"]) for failure in error["failures"])
    assert _BASE_URL_KEY in messages, (
        f"the erroring test's failure message must name {_BASE_URL_KEY}: {messages[:2000]}"
    )
    for name in (_RESULTS_FILE, _JUNIT_FILE, _REPORT_FILE):
        _read(pytester, name)
    report = _read(pytester, _REPORT_FILE)
    assert _ERRORING_NODE in report and _CONTINUED_NODE in report, (
        "report.html must render both records: the error the fixture caused and the test that ran "
        "after it"
    )


# --- Row 16: `KeyboardInterrupt` (AAP 0.4.7) -------------------------------


def test_an_interrupt_flushes_partial_results_and_writes_the_report(
    pytester: pytest.Pytester, clean_foundry_env: None
) -> None:
    """The row as a session shows it: finished work survives, unfinished work never reads as passed.

    ``tests/unit/test_cli.py`` pins the exit code the interrupt maps to. What no unit test can show
    is the row's other half -- that a run stopped mid-suite still leaves the records it completed
    and the report rendered from them on disk, and records nothing as passed that never ran.
    """
    pytester.makepyfile(**{_INTERRUPT_MODULE: _INTERRUPT_IN_CALL})

    result = pytester.runpytest_subprocess("-q", timeout=_CHILD_TIMEOUT_SECONDS)

    assert result.ret == _INTERRUPT_EXIT, (
        f"the interrupted run exited {result.ret}, not {_INTERRUPT_EXIT}: pytest's interrupt code "
        f"is preserved, not remapped\n{result.stdout.str()}{result.stderr.str()}"
    )
    records = _records(pytester)
    assert records, (
        "results.json holds no record at all: the test that completed before the interrupt must be "
        "flushed on the way out"
    )
    assert records[_COMPLETED_NODE]["outcome"] == _PASSED_OUTCOME, (
        f"the completed test must be recorded {_PASSED_OUTCOME}: {_outcomes(records)}"
    )
    passed = sorted(
        nodeid for nodeid, outcome in _outcomes(records).items() if outcome == _PASSED_OUTCOME
    )
    assert passed == [_COMPLETED_NODE], (
        "exactly the test that finished may read as passed; the interrupted test and the one after "
        f"it either appear as {_NOT_RUN_OUTCOME} or are absent: {_outcomes(records)}"
    )
    for nodeid in (_INTERRUPTED_NODE, _UNREACHED_NODE):
        record = records.get(nodeid)
        assert record is None or record["outcome"] == _NOT_RUN_OUTCOME, (
            f"{nodeid} carries {record} where the flush may only report it {_NOT_RUN_OUTCOME} or "
            "leave it out"
        )
    report = _read(pytester, _REPORT_FILE)
    assert _COMPLETED_NODE in report, (
        "report.html must render the run that was flushed: the completed test's node id is missing"
    )
    assert str(records[_COMPLETED_NODE]["run_id"]) in report, (
        "report.html must be this run's report, so it has to carry the run id results.json records"
    )
    for scheme in _EXTERNAL_SCHEMES:
        assert scheme not in report, (
            f"report.html references {scheme}, so it stops opening offline (assumption I10)"
        )
    _read(pytester, _JUNIT_FILE)
