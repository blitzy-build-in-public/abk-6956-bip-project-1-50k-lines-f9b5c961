"""AAP 0.4.7's ``execution.timeout`` row as a session shows it: exit 1, outcome ``failed``,
phase ``call``, the limit in the message, partial stdout and the log tail attached. The body
logs before printing, and its second of work is a ``time.monotonic()`` loop rather than a sleep."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final, cast

import pytest

REACHES: tuple[str, ...] = ()

_LIMIT_SECONDS: Final[float] = 0.1
_LIMIT_ARGUMENT: Final[str] = str(_LIMIT_SECONDS)
_LOGGED_MARKER: Final[str] = "timeout probe logged before the limit"
_PRINTED_MARKER: Final[str] = "partial stdout printed before the limit"
_TIMEOUT_MARKER: Final[str] = f"Timeout (>{_LIMIT_ARGUMENT}s)"
_RESULTS_NAME: Final[str] = "results.json"
_ARTIFACTS_DIRNAME: Final[str] = "artifacts"
_TRACEBACK_NAME: Final[str] = "traceback.txt"

# The log record reaches the ring, the print reaches the captured stream, then the busy wait keeps
# the body inside the call phase until the per-item timer fires.
_TIMED_OUT_SOURCE: Final[str] = f"""import logging
import time


def test_outlives_the_limit() -> None:
    logging.getLogger("foundry.probe").info({_LOGGED_MARKER!r})
    print({_PRINTED_MARKER!r})
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        pass
"""


def _document(pytester: pytest.Pytester) -> list[dict[str, object]]:
    source = pytester.path / _RESULTS_NAME
    listing = sorted(entry.name for entry in pytester.path.iterdir())
    assert source.is_file(), f"the run wrote no {_RESULTS_NAME}: {listing}"
    document: object = json.loads(source.read_text(encoding="utf-8"))
    assert isinstance(document, list), document
    assert all(isinstance(element, dict) for element in document), document
    return [cast("dict[str, object]", element) for element in document]


def _run(pytester: pytest.Pytester) -> dict[str, object]:
    # Both cases read the run's single record, so the row's exit code and record count are asserted
    # here: a second record would be a per-attempt write the worker boundary would corrupt.
    pytester.makepyfile(test_timeout=_TIMED_OUT_SOURCE)
    result = pytester.runpytest("--foundry-timeout", _LIMIT_ARGUMENT)
    assert result.ret == 1, f"AAP 0.4.7 gives the timeout row exit code 1, not {result.ret}"
    result.assert_outcomes(failed=1)
    results = [element for element in _document(pytester) if element.get("record_type") == "result"]
    assert len(results) == 1, results
    return results[0]


def _failure(record: dict[str, object]) -> dict[str, object]:
    failures = record.get("failures")
    assert isinstance(failures, list) and len(failures) == 1, failures
    assert isinstance(failures[0], dict), failures
    return cast("dict[str, object]", failures[0])


def _artifact_files(pytester: pytest.Pytester, record: dict[str, object]) -> dict[str, Path]:
    # AAP 0.4.6 makes each path relative to artifacts/<run_id>/ and one node directory deep, so a
    # listed artifact nobody could open fails here rather than in the report's failure panel.
    run_id = record.get("run_id")
    assert isinstance(run_id, str), record
    run_dir = pytester.path / _ARTIFACTS_DIRNAME / run_id
    entries = record.get("artifacts")
    assert isinstance(entries, list), record
    files: dict[str, Path] = {}
    for entry in entries:
        assert isinstance(entry, dict), entry
        kind, path = entry.get("kind"), entry.get("path")
        assert isinstance(kind, str) and isinstance(path, str), entry
        captured = run_dir / path
        assert captured.parent.parent == run_dir, f"not one node directory deep: {captured}"
        assert captured.is_file(), f"the record lists {captured}, which the run did not write"
        files[kind] = captured
    return files


def test_a_test_over_the_limit_fails_in_the_call_phase_with_the_timeout_message(
    pytester: pytest.Pytester, clean_foundry_env: None
) -> None:
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
    record = _run(pytester)
    files = _artifact_files(pytester, record)
    assert {"stdout", "log_tail", "traceback"} <= set(files), sorted(files)
    assert _PRINTED_MARKER in files["stdout"].read_text(encoding="utf-8")
    assert _LOGGED_MARKER in files["log_tail"].read_text(encoding="utf-8")
    assert _TIMEOUT_MARKER in files["traceback"].read_text(encoding="utf-8")
    assert str(_failure(record).get("traceback_path", "")).endswith(_TRACEBACK_NAME), record
