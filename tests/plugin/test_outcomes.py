"""The three outcomes no other module produces -- ``xfailed``, ``xpassed``, ``not_run`` -- each read
back from a real session: A12 records a lost worker's item with its partial data, beside the one
``run_error`` naming the worker. Nothing here mocks the plugin, the CLI or a renderer (AAP 0.14.5).
"""

from __future__ import annotations

import json
from typing import Any, Final

import pytest

REACHES: tuple[str, ...] = ("format:results.json",)

_RESULTS_NAME: Final[str] = "results.json"
_NO_RESTART: Final[str] = "--max-worker-restart=0"

# Each project below is whole, so the marker or the process exit under test is its only difference
# from a green run.
_XFAIL_AND_XPASS: Final[str] = """import pytest


@pytest.mark.xfail(reason="fails on purpose, so this attempt is xfailed")
def test_known_failure() -> None:
    assert False, "xfail sentinel"


@pytest.mark.xfail(reason="passes, so the marker is stale")
def test_stale_marker() -> None:
    assert True
"""

_CRASHING: Final[str] = """import os


def test_crashes_the_worker() -> None:
    os._exit(3)
"""


def _healthy_source(test: str) -> str:
    # One file per healthy test, so the worker that dies owns the crashing file alone.
    return f'''def {test}() -> None:
    """Pass, so only the lost worker can explain a gap."""
    assert True
'''


def _document(pytester: pytest.Pytester) -> list[dict[str, Any]]:
    path = pytester.path / _RESULTS_NAME
    assert path.is_file(), f"{_RESULTS_NAME} was not written to {pytester.path}"
    document = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(document, list), f"{_RESULTS_NAME} is not a JSON array: {document!r}"
    assert all(isinstance(element, dict) for element in document), "a non-object element"
    kinds = {element.get("record_type") for element in document}
    assert kinds <= {"result", "run_error"}, f"unexpected element kind in {_RESULTS_NAME}: {kinds}"
    return document


def _results(pytester: pytest.Pytester) -> dict[str, dict[str, Any]]:
    return {
        str(element["nodeid"]): element
        for element in _document(pytester)
        if element.get("record_type") == "result"
    }


def _run_errors(pytester: pytest.Pytester) -> list[dict[str, Any]]:
    return [element for element in _document(pytester) if element.get("record_type") == "run_error"]


def test_xfail_and_xpass_are_recorded_as_their_own_outcomes(
    pytester: pytest.Pytester, clean_foundry_env: None
) -> None:
    pytester.makepyfile(test_xfail=_XFAIL_AND_XPASS)

    result = pytester.runpytest()

    assert result.ret == 0, f"a non-strict xfail run stays green, but it exited {result.ret}"
    result.assert_outcomes(xfailed=1, xpassed=1)
    records = _results(pytester)
    assert {nodeid: record["outcome"] for nodeid, record in records.items()} == {
        "test_xfail.py::test_known_failure": "xfailed",
        "test_xfail.py::test_stale_marker": "xpassed",
    }, f"the two marked outcomes are collapsed or misnamed: {records}"
    assert all(record["attempts"] >= 1 for record in records.values())
    assert _run_errors(pytester) == [], "an xfail-marked run is not a failure of the run itself"


def test_lost_worker_names_its_items_not_run_beside_the_run_error(
    pytester: pytest.Pytester, clean_foundry_env: None
) -> None:
    pytester.makepyfile(
        test_a_first=_healthy_source("test_alpha"),
        test_b_crash=_CRASHING,
        test_c_third=_healthy_source("test_gamma"),
        test_d_fourth=_healthy_source("test_delta"),
    )

    result = pytester.runpytest_subprocess("-n", "2", "--dist", "loadfile", _NO_RESTART)

    assert result.ret == 1, f"a lost worker fails the run (AAP 0.4.7), but it exited {result.ret}"
    records = _results(pytester)
    crashed = "test_b_crash.py::test_crashes_the_worker"
    assert set(records) == {
        "test_a_first.py::test_alpha",
        crashed,
        "test_c_third.py::test_gamma",
        "test_d_fourth.py::test_delta",
    }, "every collected item is named once: A12 forbids dropping the lost worker's items"
    lost = records[crashed]
    assert lost["outcome"] == "not_run", f"the lost item's outcome is {lost['outcome']}"
    assert lost["attempts"] >= 1
    assert lost["failures"] == [] and lost["artifacts"] == [], "partial data is nothing observed"
    survivors = [nodeid for nodeid, record in records.items() if record["outcome"] == "passed"]
    assert survivors, f"the document holds no surviving result record: {sorted(records)}"
    errors = _run_errors(pytester)
    assert len(errors) == 1, f"one lost worker writes one run_error element, got {errors!r}"
    assert str(errors[0]["worker_id"]).startswith("gw"), f"no worker id: {errors[0]!r}"
    assert str(errors[0]["message"]).strip(), f"the run_error carries no message: {errors[0]!r}"
