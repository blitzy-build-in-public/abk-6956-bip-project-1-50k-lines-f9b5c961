"""``--foundry-retries 2`` on real runs: two failed attempts and a passing third end as one
``passed`` record with ``attempts: 3``, the default retries nothing, and the budget is bounded.
Each generated test counts its attempts in a file, so no run depends on process memory."""

import json
from pathlib import Path
from typing import Final

import pytest

REACHES: tuple[str, ...] = ()

_ATTEMPTS: Final[int] = 3
_COUNTER: Final[str] = "attempts.txt"
_LEDGER: Final[str] = "fixture-ledger.txt"
_TOML: Final[str] = "[execution]\nretries = 2\n"
_RETRIES_ABOVE_RANGE: Final[int] = 11
_PERMANENT: Final[str] = (
    'def test_always_fails():\n    assert False, "permanent failure sentinel"\n'
)


def _flaky_source(counter: Path) -> str:
    return f"""import pathlib

COUNTER = pathlib.Path({str(counter)!r})


def test_flaky_then_green() -> None:
    seen = int(COUNTER.read_text(encoding="utf-8")) if COUNTER.exists() else 0
    seen += 1
    COUNTER.write_text(str(seen), encoding="utf-8")
    assert seen >= {_ATTEMPTS}, f"attempt {{seen}} of 3 still failing"
"""


def _fixture_source(ledger: Path, counter: Path, scope: str, follower: bool) -> str:
    module = f'''import pathlib

import pytest

LEDGER = pathlib.Path({str(ledger)!r})
COUNTER = pathlib.Path({str(counter)!r})


@pytest.fixture(scope="{scope}")
def machine() -> str:
    with LEDGER.open("a", encoding="utf-8") as handle:
        handle.write("created\\n")
    return "machine"


def test_flaky_with_fixture(machine: str) -> None:
    assert machine == "machine"
    seen = int(COUNTER.read_text(encoding="utf-8")) if COUNTER.exists() else 0
    seen += 1
    COUNTER.write_text(str(seen), encoding="utf-8")
    assert seen >= {_ATTEMPTS}, f"attempt {{seen}} of 3 still failing"
'''
    if follower:
        module += (
            "\n\ndef test_after_the_flaky_one(machine: str) -> None:\n"
            '    assert machine == "machine"\n'
        )
    return module


def _listing(directory: Path) -> list[str]:
    return sorted(entry.name for entry in directory.iterdir())


def _result_for(pytester: pytest.Pytester, function: str) -> dict[str, object]:
    document = pytester.path / "results.json"
    assert document.is_file(), f"the run wrote no results.json: {_listing(pytester.path)}"
    elements = json.loads(document.read_text(encoding="utf-8"))
    matching = [
        item
        for item in elements
        if item.get("record_type") == "result" and str(item["nodeid"]).endswith(f"::{function}")
    ]
    assert len(matching) == 1, matching
    return matching[0]


def _ledger_entries(ledger: Path) -> list[str]:
    assert ledger.is_file(), f"the fixture was never created: {ledger}"
    return ledger.read_text(encoding="utf-8").splitlines()


@pytest.mark.parametrize(
    ("foundry_toml", "argv"), [(None, ("--foundry-retries", "2")), (_TOML, ())], ids=["cli", "file"]
)
def test_two_failures_and_a_pass_end_as_three_attempts(
    pytester: pytest.Pytester,
    clean_foundry_env: None,
    foundry_toml: str | None,
    argv: tuple[str, ...],
) -> None:
    counter = pytester.path / _COUNTER
    if foundry_toml is not None:
        pytester.makefile(".toml", foundry=foundry_toml)
    pytester.makepyfile(test_flaky=_flaky_source(counter))
    result = pytester.runpytest(*argv)
    assert result.ret == 0
    result.assert_outcomes(passed=1)
    assert int(counter.read_text(encoding="utf-8")) == _ATTEMPTS
    record = _result_for(pytester, "test_flaky_then_green")
    assert record["nodeid"] == "test_flaky.py::test_flaky_then_green"
    assert record["outcome"] == "passed" and record["attempts"] == _ATTEMPTS
    assert record["failures"] == [] and record["quarantine"] == {"state": "none"}


def test_default_retries_run_the_body_once(
    pytester: pytest.Pytester, clean_foundry_env: None
) -> None:
    counter = pytester.path / _COUNTER
    pytester.makepyfile(test_flaky=_flaky_source(counter))
    result = pytester.runpytest()
    assert result.ret == 1
    result.assert_outcomes(failed=1)
    assert int(counter.read_text(encoding="utf-8")) == 1
    record = _result_for(pytester, "test_flaky_then_green")
    assert record["outcome"] == "failed" and record["attempts"] == 1
    assert record["failures"][0]["message"], record


def test_exhausted_retries_fail_with_the_attempt_count(
    pytester: pytest.Pytester, clean_foundry_env: None
) -> None:
    pytester.makepyfile(test_permanent=_PERMANENT)
    result = pytester.runpytest("--foundry-retries", "2")
    assert result.ret == 1
    result.assert_outcomes(failed=1)
    record = _result_for(pytester, "test_always_fails")
    assert record["outcome"] == "failed" and record["attempts"] == _ATTEMPTS
    assert isinstance(record["attempts"], int) and record["attempts"] >= 1
    assert isinstance(record["duration_ms"], (int, float)) and record["duration_ms"] >= 0
    failure = record["failures"][0]
    assert failure["phase"] == "call" and "permanent failure sentinel" in str(failure["message"])
    # The capture names a run-relative traceback the run really wrote, one node directory deep.
    traceback_path = failure["traceback_path"]
    assert isinstance(traceback_path, str) and traceback_path.endswith("traceback.txt")
    assert not traceback_path.startswith("/") and ".." not in Path(traceback_path).parts
    run_dir = pytester.path / "artifacts" / str(record["run_id"])
    captured = run_dir / traceback_path
    assert captured.is_file() and captured.parent.parent == run_dir, captured


@pytest.mark.parametrize(
    ("scope", "follower", "green", "entries"),
    [("function", False, 1, 3), ("session", True, 2, 1)],
    ids=["function-scoped", "session-scoped"],
)
def test_fixture_lifecycle_across_attempts(
    pytester: pytest.Pytester,
    clean_foundry_env: None,
    scope: str,
    follower: bool,
    green: int,
    entries: int,
) -> None:
    counter = pytester.path / _COUNTER
    ledger = pytester.path / _LEDGER
    pytester.makepyfile(test_flaky=_fixture_source(ledger, counter, scope, follower))
    result = pytester.runpytest("--foundry-retries", "2")
    assert result.ret == 0
    result.assert_outcomes(passed=green)
    assert len(_ledger_entries(ledger)) == entries, _ledger_entries(ledger)
    record = _result_for(pytester, "test_flaky_with_fixture")
    assert record["outcome"] == "passed" and record["attempts"] == _ATTEMPTS


def test_retries_are_bounded_by_the_documented_range(
    pytester: pytest.Pytester, clean_foundry_env: None
) -> None:
    counter = pytester.path / _COUNTER
    pytester.makepyfile(test_flaky=_flaky_source(counter))
    result = pytester.runpytest("--foundry-retries", str(_RETRIES_ABOVE_RANGE))
    assert result.ret == 2
    output = result.stdout.str() + result.stderr.str()
    assert "execution.retries" in output and "0..10" in output, output
    assert not counter.exists(), "no test body may run when the configuration is rejected"
    assert not (pytester.path / "results.json").exists(), _listing(pytester.path)
