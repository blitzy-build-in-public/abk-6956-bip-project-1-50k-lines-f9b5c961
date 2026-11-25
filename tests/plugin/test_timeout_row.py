"""The ``execution.timeout`` row of AAP 0.4.7, observed through pytest itself.

AAP 0.4.7 fixes five facts about a test that outlives ``execution.timeout``: the outcome is
``failed``, ``phase`` is recorded as ``call``, the timeout is reported in the failure message,
partial stdout and the log tail are attached, and the run exits 1. The unit tests of
``foundry.execution.timeout`` reach the policy that derives the timer's settings, but AAP 0.14.1
criterion 13 says a capability that exists only in a unit test satisfies no criterion here, so this
module makes that row's observation through a real session: one throwaway project, one session
``pytester`` starts, and assertions made only against the documents and files that session wrote --
``results.json`` and ``artifacts/<run_id>/<sanitized-nodeid>/`` (AAP 0.4.6). No plugin, policy,
capture or report writer is mocked.

**Why the generated test logs as well as prints.** The two attached artifacts have different
sources: ``stdout.log`` comes from the stream pytest captured, ``log_tail.log`` from the bounded
ring ``foundry.execution.coordinator`` attaches to the ``foundry`` logger. A body that only printed
would leave no tail to attach, which is why the generated test emits one record on
``foundry.probe`` first, and why each marker is looked for in the one file it can reach.

**Why it busy-waits instead of sleeping.** AAP 0.14.2 caps a sleep at 100 ms and AAP 0.14.3 rejects
``time.sleep`` outside injected fakes, so the body spends its second on ``time.monotonic()`` and
never waits on the clock. Nothing here reads a wall-clock time or opens a socket.

**Determinism** (AAP 0.14.3): the project is generated per test under ``pytester``'s temporary
directory, the limit arrives as a command-line override, and every assertion compares a recorded
field or a file's content rather than a position or a duration.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final, cast

import pytest

# No name enumerated by tests/plugin/test_reachability.py is reached here: the timeout row is a
# state-table behaviour of the plugin, reached through the option group rather than through a
# fixture, subcommand, data loader or report format. The empty tuple is the declaration it parses.
REACHES: tuple[str, ...] = ()

_LIMIT_SECONDS: Final[float] = 0.1
"""The ``execution.timeout`` this module resolves, in seconds (AAP 0.4.5's range allows it)."""

_LIMIT_ARGUMENT: Final[str] = str(_LIMIT_SECONDS)
"""The same limit as the command line spells it, and as the failure message reports it."""

_LIMIT_OPTION: Final[str] = "--foundry-timeout"
"""The named override of AAP 0.4.4, whose target is ``execution.timeout``."""

_BUSY_WAIT_SECONDS: Final[float] = 1.0
"""How long the generated body stays busy: ten times the limit, so the timer fires inside it."""

_LOGGED_MARKER: Final[str] = "timeout probe logged before the limit"
"""The record the generated test emits on ``foundry.probe``: only ``log_tail.log`` can hold it."""

_PRINTED_MARKER: Final[str] = "partial stdout printed before the limit"
"""The line the generated test prints, which only ``stdout.log`` can hold."""

_TIMEOUT_MARKER: Final[str] = f"Timeout (>{_LIMIT_ARGUMENT}s)"
"""Pytest-timeout's own report of the exceeded limit, which the row puts in the failure message."""

_RESULTS_NAME: Final[str] = "results.json"
"""The result document AAP 0.4.6 writes to the invocation directory (assumption A5)."""

_ARTIFACTS_DIRNAME: Final[str] = "artifacts"
"""The default ``artifacts.dir`` the run resolves against its invocation directory (AAP 0.4.5)."""

_TRACEBACK_NAME: Final[str] = "traceback.txt"
"""The artifact file name AAP 0.4.6 gives the rendered failure, which the record links."""


def _timed_out_source() -> str:
    """Return the generated module: one test that logs, prints and outlives the limit.

    The three steps are the row's evidence in the order a real test produces it -- the log record
    reaches the bounded ring, the print reaches pytest's captured stream, then the busy wait keeps
    the body inside the call phase until the per-item timer fires.
    """
    return (
        '"""A test that exceeds ``execution.timeout``, so the state row has its subject."""\n'
        "\n"
        "import logging\n"
        "import time\n"
        "\n"
        "\n"
        "def test_outlives_the_limit() -> None:\n"
        f'    logging.getLogger("foundry.probe").info({_LOGGED_MARKER!r})\n'
        f"    print({_PRINTED_MARKER!r})\n"
        f"    deadline = time.monotonic() + {_BUSY_WAIT_SECONDS!r}\n"
        "    while time.monotonic() < deadline:\n"
        "        pass\n"
    )


def _document(pytester: pytest.Pytester) -> list[dict[str, object]]:
    """Return the elements of the run's ``results.json``, in document order."""
    source = pytester.path / _RESULTS_NAME
    listing = sorted(entry.name for entry in pytester.path.iterdir())
    assert source.is_file(), f"the run wrote no {_RESULTS_NAME}: {listing}"
    document: object = json.loads(source.read_text(encoding="utf-8"))
    assert isinstance(document, list), document
    for element in document:
        assert isinstance(element, dict), element
    return [cast("dict[str, object]", element) for element in document]


def _only_result(pytester: pytest.Pytester) -> dict[str, object]:
    """Return the run's single result record.

    Requirement I3 makes the record the controller's, so exactly one is written per node id, and a
    second one would mean a per-attempt write the worker boundary would corrupt.
    """
    results = [element for element in _document(pytester) if element.get("record_type") == "result"]
    assert len(results) == 1, results
    return results[0]


def _run(pytester: pytest.Pytester) -> dict[str, object]:
    """Write the project, run it at the limit and return the run's single result record.

    Both cases read one record, so the run-level fact of the row -- exit code 1 -- is asserted here
    beside the record count, leaving each case to assert the part of the record it is about.
    """
    pytester.makepyfile(test_timeout=_timed_out_source())
    result = pytester.runpytest(_LIMIT_OPTION, _LIMIT_ARGUMENT)
    assert result.ret == 1, f"AAP 0.4.7 gives the timeout row exit code 1, not {result.ret}"
    result.assert_outcomes(failed=1)
    return _only_result(pytester)


def _failure(record: dict[str, object]) -> dict[str, object]:
    """Return the record's one failure entry, which the timeout puts in the call phase."""
    failures = record.get("failures")
    assert isinstance(failures, list) and len(failures) == 1, failures
    failure = failures[0]
    assert isinstance(failure, dict), failure
    return cast("dict[str, object]", failure)


def _artifact_files(pytester: pytest.Pytester, record: dict[str, object]) -> dict[str, Path]:
    """Return the run's artifacts keyed by kind, as the files the record names.

    AAP 0.4.6 makes each ``artifacts[].path`` relative to the run directory
    ``artifacts/<run_id>/`` and one sanitized-nodeid directory deep, so every path is joined to that
    directory and must name a file the run really wrote: a record that listed an artifact nobody
    could open would be an attachment the report's panel cannot follow.
    """
    run_id = record.get("run_id")
    assert isinstance(run_id, str), record
    run_dir = pytester.path / _ARTIFACTS_DIRNAME / run_id
    entries = record.get("artifacts")
    assert isinstance(entries, list), record
    files: dict[str, Path] = {}
    for entry in entries:
        assert isinstance(entry, dict), entry
        kind = entry.get("kind")
        path = entry.get("path")
        assert isinstance(kind, str) and isinstance(path, str), entry
        captured = run_dir / path
        assert captured.parent.parent == run_dir, f"not one node directory deep: {captured}"
        assert captured.is_file(), f"the record lists {captured}, which the run did not write"
        files[kind] = captured
    return files


def test_a_test_over_the_limit_fails_in_the_call_phase_with_the_timeout_message(
    pytester: pytest.Pytester, clean_foundry_env: None
) -> None:
    """AAP 0.4.7's ``execution.timeout`` row: exit 1, outcome ``failed``, phase ``call``, message.

    The message is asserted as pytest-timeout's own spelling of the limit rather than as the whole
    rendered failure: the record's ``message`` is the traceback text, whose last line names the
    limit that fired, and that is the third fact the row states.
    """
    record = _run(pytester)

    assert record["outcome"] == "failed", record
    failure = _failure(record)
    assert failure.get("phase") == "call", failure
    message = failure.get("message")
    assert isinstance(message, str), failure
    assert _TIMEOUT_MARKER in message, message


def test_the_timed_out_record_attaches_partial_stdout_and_the_log_tail(
    pytester: pytest.Pytester, clean_foundry_env: None
) -> None:
    """The row's fourth fact: partial stdout and the log tail are attached, with their content.

    Each marker is looked for in the one file it can reach -- the printed line in ``stdout.log``,
    the ``foundry.probe`` record in ``log_tail.log`` -- so a listed but empty artifact, or one
    written from the wrong source, fails here. The rendered traceback the record links is required
    as well, because it is the link a reader and the HTML failure panel follow.
    """
    record = _run(pytester)
    files = _artifact_files(pytester, record)

    assert {"stdout", "log_tail", "traceback"} <= set(files), sorted(files)
    assert _PRINTED_MARKER in files["stdout"].read_text(encoding="utf-8")
    assert _LOGGED_MARKER in files["log_tail"].read_text(encoding="utf-8")
    assert _TIMEOUT_MARKER in files["traceback"].read_text(encoding="utf-8")
    assert str(_failure(record).get("traceback_path", "")).endswith(_TRACEBACK_NAME), record
