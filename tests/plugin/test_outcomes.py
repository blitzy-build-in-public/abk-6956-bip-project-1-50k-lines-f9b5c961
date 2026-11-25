"""The nine published outcomes, produced by real runs: ``xfailed``, ``xpassed`` and ``not_run``.

AAP 0.4.6 publishes exactly nine outcome values -- ``passed``, ``failed``, ``skipped``,
``xfailed``, ``xpassed``, ``error``, ``quarantined_failed``, ``quarantine_passed`` and ``not_run``
-- and AAP 0.14.3 forbids a capability that exists only in a unit test, so every one of them has to
come out of a session pytest itself started. Six already do, from the modules that own their states:
this one covers the three that the suite never produced, and nothing here is asserted against a
hand-built record, a mocked plugin, CLI or renderer (AAP 0.14.5).

**``xfailed`` and ``xpassed``** (AAP 0.4.6). pytest marks the two reports ``wasxfail``: a test whose
own outcome is a failure under ``@pytest.mark.xfail`` is ``xfailed``, and one that passes there is
``xpassed``. Neither fails the run in the default non-strict mode, so the exit code 0 beside those
two records is what separates the mapping from a run that merely tolerated two failures.

**``not_run``** (AAP 0.4.6, assumption A12). An item that never executed because a worker was lost
is written with the partial data available rather than silently omitted -- the alternative A12
displaces. AAP 0.4.7's worker-crash row is the same event seen from the run: partial results plus a
``run_error`` element naming the worker id and exit code 1. The case below kills one worker of a
two-worker run and asserts all four facts at once, because each one alone is satisfiable by a
degenerate run: an empty document would satisfy the exit code, and a document that dropped the lost
worker's items would satisfy the run error.

**Why the run forbids worker restarts.** ``pytest-xdist`` replaces a crashed worker by default and
re-sends it the tests it had not run, which includes the test that kills it: the run then crashes,
replaces and crashes again without ever ending. ``--max-worker-restart=0`` is xdist's own switch for
"do not replace", so the loss is observed once, exactly as AAP 0.4.7 describes it.

**Determinism** (AAP 0.14.3): no sleep and no wall-clock reading; the killed item is the only test
of its own file, so ``--dist loadfile`` gives the worker that dies one file's work and the outcome
does not depend on how the rest of the suite is spread.
"""

from __future__ import annotations

import json
from typing import Any, Final

import pytest

REACHES: tuple[str, ...] = ("format:results.json",)
"""Both cases read the result document a real session wrote (AAP 0.4.6).

``format:results.json`` is one of the four enumerations ``tests/plugin/test_reachability.py``
requires a real-entry-point test for, and this module is one of the modules that provides it: the
document is parsed from ``pytester.path``, which is the invocation directory the run wrote it to
(assumption A5).
"""

# --- The nine values AAP 0.4.6 publishes, spelled as it spells them --------

_XFAILED: Final[str] = "xfailed"
"""The outcome of a test that failed under ``@pytest.mark.xfail`` (AAP 0.4.6)."""

_XPASSED: Final[str] = "xpassed"
"""The outcome of a test that passed under ``@pytest.mark.xfail`` (AAP 0.4.6)."""

_NOT_RUN: Final[str] = "not_run"
"""The outcome of an item that never executed because a worker was lost (AAP 0.4.6, A12)."""

_PASSED: Final[str] = "passed"
"""The outcome a surviving test keeps, which is what makes the crashed run's document partial."""

_RESULT_KIND: Final[str] = "result"
"""The ``record_type`` of a per-test record, the discriminator AAP 0.4.6 defines."""

_RUN_ERROR_KIND: Final[str] = "run_error"
"""The ``record_type`` of the element that describes a failure of the run itself (AAP 0.4.6)."""

_RESULTS_NAME: Final[str] = "results.json"
"""The fixed name AAP 0.4.6 gives the result document (assumption A6)."""

_MINIMUM_ATTEMPTS: Final[int] = 1
"""The record schema's floor for ``attempts``, which a ``not_run`` record carries too (A12)."""

# --- The two runs' expected codes and their xdist arguments ----------------

_XFAIL_EXIT: Final[int] = 0
"""Neither an ``xfailed`` nor an ``xpassed`` test fails the run in the default non-strict mode."""

_CRASH_EXIT: Final[int] = 1
"""The code AAP 0.4.7 gives a run that lost a worker, whatever the surviving tests did."""

_WORKERS: Final[str] = "2"
"""Two workers, so one of them can be lost while the other keeps its own items running."""

_DIST_LOADFILE: Final[str] = "loadfile"
"""xdist's per-file distribution, which keeps the killed item's file one worker's whole work."""

_NO_RESTART: Final[str] = "--max-worker-restart=0"
"""xdist's switch that stops a crashed worker being replaced, so the crash cannot repeat."""

_WORKER_PREFIX: Final[str] = "gw"
"""The prefix xdist gives its worker ids, so a non-empty ``worker_id`` can be seen to name one."""

# --- The generated projects ------------------------------------------------
# Each is a whole project, so a run's configuration comes from the defaults alone and the marker or
# the process exit under test is the only difference between it and a green one.

_XFAIL_AND_XPASS: Final[str] = '''"""One xfail-marked failure and one xfail-marked pass."""

import pytest


@pytest.mark.xfail(reason="fails on purpose, so this attempt is xfailed")
def test_known_failure() -> None:
    """Fail under the marker, which pytest reports as ``wasxfail``."""
    assert False, "xfail sentinel"


@pytest.mark.xfail(reason="passes, so the marker is stale")
def test_stale_marker() -> None:
    """Pass under the marker, which pytest reports as an unexpected pass."""
    assert True
'''

_CRASHING: Final[str] = '''"""A test that takes its xdist worker down with it."""

import os


def test_crashes_the_worker() -> None:
    """End the worker process mid-call, which xdist reports as a lost node."""
    os._exit(3)
'''


def _healthy_source(test: str) -> str:
    """Return a module holding one test named ``test`` that passes unconditionally.

    One file per healthy test is what makes the crash deterministic: ``--dist loadfile`` hands whole
    files to workers, so the worker that dies owns the crashing file alone and the others cannot
    lose an item to it.
    """
    return (
        '"""A healthy test, whose record proves the crashed run kept its partial results."""\n'
        "\n"
        "\n"
        f"def {test}() -> None:\n"
        '    """Pass, so nothing but the lost worker can explain a missing record."""\n'
        "    assert True\n"
    )


# --- Reading what a run wrote ----------------------------------------------


def _nodeid(stem: str, test: str) -> str:
    """Return the node id of ``test`` in ``<stem>.py``, as pytest reports it under the rootdir."""
    return f"{stem}.py::{test}"


def _document(pytester: pytest.Pytester) -> list[dict[str, Any]]:
    """Return the ``results.json`` array the run wrote, naming the directory when it is absent.

    AAP 0.4.6 makes the document a JSON array whose elements are discriminated by ``record_type``,
    so a document that is not an array of objects is a contract breach rather than something to
    read around.
    """
    path = pytester.path / _RESULTS_NAME
    assert path.is_file(), (
        f"{_RESULTS_NAME} was not written to {pytester.path}; the directory holds "
        f"{sorted(entry.name for entry in pytester.path.iterdir())}"
    )
    document = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(document, list), (
        f"{_RESULTS_NAME} is not a JSON array but {type(document).__name__}"
    )
    elements: list[dict[str, Any]] = []
    for element in document:
        assert isinstance(element, dict), f"{_RESULTS_NAME} holds a non-object: {element!r}"
        assert element.get("record_type") in {_RESULT_KIND, _RUN_ERROR_KIND}, (
            f"unexpected element kind in {_RESULTS_NAME}: {element!r}"
        )
        elements.append(element)
    return elements


def _results(pytester: pytest.Pytester) -> dict[str, dict[str, Any]]:
    """Return the run's result records keyed by node id, one per item the document names."""
    return {
        str(element["nodeid"]): element
        for element in _document(pytester)
        if element.get("record_type") == _RESULT_KIND
    }


def _run_errors(pytester: pytest.Pytester) -> list[dict[str, Any]]:
    """Return the ``run_error`` elements in document order, which AAP 0.4.6 places after results."""
    return [
        element for element in _document(pytester) if element.get("record_type") == _RUN_ERROR_KIND
    ]


# --- xfailed and xpassed (AAP 0.4.6) ---------------------------------------


def test_xfail_and_xpass_are_recorded_as_their_own_outcomes(
    pytester: pytest.Pytester, clean_foundry_env: None
) -> None:
    """A marked failure is ``xfailed`` and a marked pass ``xpassed``, in a run that still exits 0.

    The exit code is asserted beside the outcomes, because the default non-strict mode makes an
    ``xpassed`` test green and an ``xfailed`` one green too: a mapping that collapsed both into
    ``failed`` would leave the exit code 1, and one that collapsed them into ``passed`` would leave
    the two published values unproduced.
    """
    pytester.makepyfile(test_xfail=_XFAIL_AND_XPASS)

    result = pytester.runpytest()

    assert result.ret == _XFAIL_EXIT
    result.assert_outcomes(xfailed=1, xpassed=1)
    records = _results(pytester)
    expected = {
        _nodeid("test_xfail", "test_known_failure"): _XFAILED,
        _nodeid("test_xfail", "test_stale_marker"): _XPASSED,
    }
    assert {nodeid: record["outcome"] for nodeid, record in records.items()} == expected
    for record in records.values():
        assert record["attempts"] >= _MINIMUM_ATTEMPTS
    assert _run_errors(pytester) == [], "an xfail-marked run is not a failure of the run itself"


# --- not_run, through a lost xdist worker (AAP 0.4.6, 0.4.7, assumption A12) ---


def test_lost_worker_names_its_items_not_run_beside_the_run_error(
    pytester: pytest.Pytester, clean_foundry_env: None
) -> None:
    """One dead worker leaves its items ``not_run``, keeps the other records and exits 1.

    Assumption A12 is the reason a lost item is a record at all: its node id is one the worker
    collected, so the document can name it ``not_run`` with the partial data available -- no
    failures, artifacts or quarantine verdict -- instead of omitting it. AAP 0.4.7's row is the run
    as a whole: the surviving tests keep their records and one ``run_error`` names the worker.
    """
    pytester.makepyfile(
        test_a_first=_healthy_source("test_alpha"),
        test_b_crash=_CRASHING,
        test_c_third=_healthy_source("test_gamma"),
        test_d_fourth=_healthy_source("test_delta"),
    )

    result = pytester.runpytest_subprocess("-n", _WORKERS, "--dist", _DIST_LOADFILE, _NO_RESTART)

    assert result.ret == _CRASH_EXIT
    records = _results(pytester)
    crashed = _nodeid("test_b_crash", "test_crashes_the_worker")
    assert set(records) == {
        _nodeid("test_a_first", "test_alpha"),
        crashed,
        _nodeid("test_c_third", "test_gamma"),
        _nodeid("test_d_fourth", "test_delta"),
    }, "every collected item is named once: A12 forbids dropping the lost worker's items"
    lost = records[crashed]
    assert lost["outcome"] == _NOT_RUN
    assert lost["attempts"] >= _MINIMUM_ATTEMPTS
    assert lost["failures"] == [] and lost["artifacts"] == [], "partial data is nothing observed"
    survivors = [
        nodeid
        for nodeid, record in records.items()
        if nodeid != crashed and record["outcome"] == _PASSED
    ]
    assert survivors, f"the document holds no surviving result record: {sorted(records)}"
    errors = _run_errors(pytester)
    assert len(errors) == 1, f"one lost worker writes one run_error element, got {errors!r}"
    error = errors[0]
    assert str(error["worker_id"]).startswith(_WORKER_PREFIX), (
        f"the run_error does not name a worker id: {error!r}"
    )
    assert str(error["message"]).strip(), f"the run_error carries no message: {error!r}"
