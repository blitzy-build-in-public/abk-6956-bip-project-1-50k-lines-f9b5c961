"""What ``--foundry-retries 2`` does to one item, observed through pytest itself (criterion 6).

AAP 0.14.1's criterion 6 asks for the retry half of the retries-and-quarantine observation: a test
that fails twice and then passes, run with ``--foundry-retries 2``, ends ``passed`` with
``attempts: 3`` in ``results.json``. Assumption A2 is what produces that arithmetic --
``execution.retries`` counts the repeats *after* the first attempt (AAP 0.4.5) -- so a resolved
``2`` is three attempts and the default ``0`` is one attempt and no more.

Every case drives pytest over a throwaway project, so the retry driver in
``foundry.execution.coordinator``, the ``user_properties`` stamp that carries the attempt list to
the controller (requirement I3) and the controller's single ``results.json`` write are all
exercised the way a user's suite exercises them. No plugin, coordinator or report writer is mocked,
and the only inputs are the generated project, a ``foundry.toml`` and the command line.

**The counter file.** A failure has to be deterministic without depending on process memory: one
child run shares this interpreter with the next, so an attempt counter held in a module global would
survive between tests and make the second run of this module disagree with the first. Each generated
test module therefore counts its own attempts in a file under ``pytester.path``, whose absolute path
is formatted into the generated source. The body fails while the count is below three, so attempts
one and two fail and the third passes -- the one sequence that separates ``attempts: 3`` from
``attempts: 1``. Nothing here sleeps and nothing reads a clock (AAP 0.14.3): the timing assertions
read the record's own fields rather than measure wall clock.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final, cast

import pytest

# The module drives no enumerated fixture, subcommand, data loader or report format through a real
# entry point: it observes the retry driver. The empty tuple is the declaration that
# tests/plugin/test_reachability.py parses.
REACHES: tuple[str, ...] = ()

_COUNTER_NAME: Final[str] = "attempts.txt"
"""The generated project's attempt counter, one file per ``pytester.path``."""

_LEDGER_NAME: Final[str] = "fixture-ledger.txt"
"""What a generated fixture appends to each time Foundry's driver recreates it."""

_ARTIFACTS_DIRNAME: Final[str] = "artifacts"
"""The default ``artifacts.dir`` the run resolves against its invocation directory (AAP 0.4.5)."""

_TOML_RETRIES: Final[str] = "[execution]\nretries = 2\n"
"""The file layer's spelling of the retry budget, which must match ``--foundry-retries 2``."""

_ATTEMPTS_WITH_TWO_RETRIES: Final[int] = 3
"""How many attempts ``--foundry-retries 2`` allows: two repeats after the first (assumption A2).

The generated bodies fail below this count and pass on it, so one constant states both the source's
threshold and the count every assertion expects.
"""

_RETRIES_ABOVE_RANGE: Final[int] = 11
"""One past the largest accepted ``execution.retries`` of AAP 0.4.5, which is ``0..10``."""

_RECORD_KEYS: Final[frozenset[str]] = frozenset(
    {
        "record_type",
        "run_id",
        "started_at",
        "finished_at",
        "profile",
        "nodeid",
        "outcome",
        "attempts",
        "duration_ms",
        "markers",
        "failures",
        "artifacts",
        "quarantine",
    }
)
"""The thirteen keys a result record carries, in AAP 0.4.6's order and spelled as it spells them."""


def _flaky_source(counter: Path) -> str:
    """Return a module whose only test fails twice and passes on its third attempt.

    The counter's absolute path is substituted into the generated source, so the attempt number
    survives whatever process runs the body: the record's ``attempts`` is then the driver's own
    count rather than an artefact of in-process reuse (assumption A2).
    """
    return (
        '"""Fail twice, then pass: the attempt count is the observable (assumption A2)."""\n'
        "\n"
        "import pathlib\n"
        "\n"
        f"_COUNTER = pathlib.Path({str(counter)!r})\n"
        "\n"
        "\n"
        "def test_flaky_then_green() -> None:\n"
        '    seen = int(_COUNTER.read_text(encoding="utf-8")) if _COUNTER.exists() else 0\n'
        "    seen += 1\n"
        '    _COUNTER.write_text(str(seen), encoding="utf-8")\n'
        f'    assert seen >= {_ATTEMPTS_WITH_TWO_RETRIES}, f"attempt {{seen}} of 3 still failing"\n'
    )


def _fixture_source(ledger: Path, counter: Path, *, scope: str, follower: bool = False) -> str:
    """Return a module whose fixture appends to ``ledger`` once each time it is created.

    ``scope`` is ``function`` or ``session``, the two lifecycles AAP 0.7.1 separates; the bodies are
    identical either way, so the scope word is the only difference between the two cases.

    ``follower`` appends a second, always-passing test, and exists for the session-scoped case:
    pytest finalizes a session-scoped fixture when the last collected item finishes, so a retried
    item that is also the last item sees the fixture rebuilt for each attempt whatever its scope.
    The follower keeps the session boundary open across the retried item's attempts, which is the
    run shape AAP 0.7.1 describes.
    """
    lines = [
        '"""Record how often a fixture is created across Foundry\'s attempts."""\n',
        "\n",
        "import pathlib\n",
        "\n",
        "import pytest\n",
        "\n",
        f"_LEDGER = pathlib.Path({str(ledger)!r})\n",
        f"_COUNTER = pathlib.Path({str(counter)!r})\n",
        "\n",
        "\n",
        f'@pytest.fixture(scope="{scope}")\n',
        "def machine() -> str:\n",
        '    with _LEDGER.open("a", encoding="utf-8") as handle:\n',
        '        handle.write("created\\n")\n',
        '    return "machine"\n',
        "\n",
        "\n",
        "def test_flaky_with_fixture(machine: str) -> None:\n",
        '    assert machine == "machine"\n',
        '    seen = int(_COUNTER.read_text(encoding="utf-8")) if _COUNTER.exists() else 0\n',
        "    seen += 1\n",
        '    _COUNTER.write_text(str(seen), encoding="utf-8")\n',
        f"    assert seen >= {_ATTEMPTS_WITH_TWO_RETRIES}, ",
        'f"attempt {seen} of 3 still failing"\n',
    ]
    if follower:
        lines.extend(
            [
                "\n",
                "\n",
                "def test_after_the_flaky_one(machine: str) -> None:\n",
                '    assert machine == "machine"\n',
            ]
        )
    return "".join(lines)


_PERMANENT_SOURCE: Final[str] = '''"""Fail every attempt: the exhausted-budget case."""


def test_always_fails() -> None:
    assert False, "permanent failure sentinel"
'''
"""A test that never passes, so every attempt the driver allows is spent and fails."""


# --- Reading what a run produced --------------------------------------------


def _listing(directory: Path) -> list[str]:
    """Return the names in ``directory``, sorted, so a failure message shows what a run wrote."""
    return sorted(entry.name for entry in directory.iterdir())


def _document(pytester: pytest.Pytester) -> list[dict[str, object]]:
    """Return the elements of the run's ``results.json``, in document order.

    A missing document fails with the directory's listing rather than a ``FileNotFoundError``: a run
    that wrote no report at all is the fact the configuration-fault case asserts.
    """
    source = pytester.path / "results.json"
    assert source.is_file(), f"the run wrote no results.json: {_listing(pytester.path)}"
    document: object = json.loads(source.read_text(encoding="utf-8"))
    assert isinstance(document, list), document
    elements: list[dict[str, object]] = []
    for element in document:
        assert isinstance(element, dict), element
        elements.append(cast("dict[str, object]", element))
    return elements


def _only_result(pytester: pytest.Pytester) -> dict[str, object]:
    """Return the run's single result record.

    Requirement I3 makes the record the controller's: the attempt list rides the report's
    ``user_properties`` and exactly one record per node id is written, so a second record for one
    item would be a per-attempt write that a worker boundary would corrupt.
    """
    results = [element for element in _document(pytester) if element.get("record_type") == "result"]
    assert len(results) == 1, results
    return results[0]


def _result_for(pytester: pytest.Pytester, function: str) -> dict[str, object]:
    """Return the record whose node id names ``function``, for a project holding several tests."""
    matching = [
        element
        for element in _document(pytester)
        if element.get("record_type") == "result"
        and str(element.get("nodeid", "")).partition("::")[2] == function
    ]
    assert len(matching) == 1, matching
    return matching[0]


def _counter_value(counter: Path) -> int:
    """Return how many times the generated test body ran, read back from its counter file."""
    return int(counter.read_text(encoding="utf-8"))


def _ledger_entries(ledger: Path) -> list[str]:
    """Return one entry per fixture creation, in the order the attempts produced them."""
    assert ledger.is_file(), f"the fixture was never created: {ledger}"
    return ledger.read_text(encoding="utf-8").splitlines()


def _number_field(record: dict[str, object], key: str) -> float:
    """Return ``key``'s value as a number, failing when the record carries anything else.

    AAP 0.7.2 validates ``attempts`` as a positive integer and ``duration_ms`` as a non-negative
    number, so the type is asserted before the bound rather than only the bound.
    """
    value = record.get(key)
    assert isinstance(value, (int, float)) and not isinstance(value, bool), (key, value)
    return float(value)


def _assert_nodeid(record: dict[str, object], *, module: str, function: str) -> None:
    """Assert ``record`` names the generated test, tolerating a rootdir prefix on its path.

    ``nodeid`` is the unsanitised identifier AAP 0.4.6 publishes, and its path part carries whatever
    prefix the child run's root directory gives it, so the module and the test function are compared
    rather than the whole path.
    """
    nodeid = record.get("nodeid")
    assert isinstance(nodeid, str), nodeid
    path_part, separator, function_part = nodeid.partition("::")
    assert separator == "::", nodeid
    assert Path(path_part).name == module, nodeid
    assert function_part == function, nodeid


def _failure_for(record: dict[str, object], phase: str) -> dict[str, object]:
    """Return the record's failure entry for ``phase``, or fail showing what it does carry."""
    failures = record.get("failures")
    assert isinstance(failures, list), failures
    matching = [
        cast("dict[str, object]", entry)
        for entry in failures
        if isinstance(entry, dict) and entry.get("phase") == phase
    ]
    assert matching, failures
    return matching[0]


def _assert_captured_traceback(
    pytester: pytest.Pytester, record: dict[str, object], failure: dict[str, object]
) -> None:
    """Assert the failure names the run-relative traceback artifact the run really captured.

    AAP 0.4.6 makes ``traceback_path`` "relative to the run directory or ``null``" and lists
    ``traceback.txt`` among the files of ``artifacts/<run_id>/<sanitized-nodeid>/``, so a failure
    whose capture succeeded names a path rather than ``null``: ``null`` is the state AAP 0.4.7
    reserves for a run whose artifact directory could not be written. The value is checked as a
    path -- no leading separator, no ``..`` segment, one node directory deep -- because it is what
    the HTML failure panel links and what a reader joins to the run directory, and the file it
    names must exist, so the field reports a capture rather than a guess.
    """
    run_id = record.get("run_id")
    assert isinstance(run_id, str), record
    traceback_path = failure.get("traceback_path")
    assert isinstance(traceback_path, str), failure
    assert not traceback_path.startswith("/"), f"the traceback path is absolute: {traceback_path}"
    assert ".." not in Path(traceback_path).parts, f"the path escapes the run: {traceback_path}"
    assert traceback_path.endswith("traceback.txt"), traceback_path
    run_dir = pytester.path / _ARTIFACTS_DIRNAME / run_id
    captured = run_dir / traceback_path
    assert captured.is_file(), f"the record names {captured}, which the run did not write"
    assert captured.parent.parent == run_dir, f"not one node directory deep: {captured}"


# --- The retry budget, through pytest itself --------------------------------


def test_retries_turn_two_failures_into_a_pass(
    pytester: pytest.Pytester, clean_foundry_env: None
) -> None:
    """Two failed attempts and a passing third end as one passing record with ``attempts: 3``.

    Criterion 6's retry observation, end to end: the exit code the run ends with, the attempt count
    the counter file proves the driver really spent, and the single record the controller wrote.
    """
    counter = pytester.path / _COUNTER_NAME
    pytester.makepyfile(test_flaky_retried=_flaky_source(counter))
    result = pytester.runpytest("--foundry-retries", "2")

    assert result.ret == 0
    result.assert_outcomes(passed=1)
    assert _counter_value(counter) == _ATTEMPTS_WITH_TWO_RETRIES
    record = _only_result(pytester)
    assert record["outcome"] == "passed"
    assert record["attempts"] == _ATTEMPTS_WITH_TWO_RETRIES
    assert record["failures"] == []
    assert record["quarantine"] == {"state": "none"}
    _assert_nodeid(record, module="test_flaky_retried.py", function="test_flaky_then_green")


def test_default_retries_do_not_repeat(pytester: pytest.Pytester, clean_foundry_env: None) -> None:
    """The same project with no override runs its body once, and one failure ends the run.

    This pins assumption A2's baseline: the default ``execution.retries`` of ``0`` is one attempt,
    not one repeat on top of an attempt, so a failing test is reported after a single run.
    """
    counter = pytester.path / _COUNTER_NAME
    pytester.makepyfile(test_flaky_default=_flaky_source(counter))
    result = pytester.runpytest()

    assert result.ret == 1
    result.assert_outcomes(failed=1)
    assert _counter_value(counter) == 1
    record = _only_result(pytester)
    assert record["outcome"] == "failed"
    assert record["attempts"] == 1
    assert _failure_for(record, "call")["message"], record


def test_exhausted_retries_fail_with_attempt_count_and_message(
    pytester: pytest.Pytester, clean_foundry_env: None
) -> None:
    """A test that never passes spends all three attempts and fails with its own message.

    The record then carries what AAP 0.4.6 publishes: the total attempt count, a ``call``-phase
    failure whose message names the assertion, the run-relative traceback the capture wrote beside
    the test's other artifacts, and the two fields AAP 0.7.2 validates.
    """
    pytester.makepyfile(test_permanent=_PERMANENT_SOURCE)
    result = pytester.runpytest("--foundry-retries", "2")

    assert result.ret == 1
    result.assert_outcomes(failed=1)
    record = _only_result(pytester)
    assert record["outcome"] == "failed"
    assert record["attempts"] == _ATTEMPTS_WITH_TWO_RETRIES
    failure = _failure_for(record, "call")
    assert "permanent failure sentinel" in str(failure["message"])
    _assert_captured_traceback(pytester, record, failure)
    assert _number_field(record, "attempts") >= 1
    assert _number_field(record, "duration_ms") >= 0
    assert set(record) == _RECORD_KEYS


def test_retries_from_the_file_layer_match_the_cli(
    pytester: pytest.Pytester, clean_foundry_env: None
) -> None:
    """``foundry.toml``'s ``[execution] retries = 2`` buys the same three attempts as the option.

    The file layer and the command line feed one resolved configuration, so the driver cannot tell
    which of them set the budget -- which is what lets a project fix its retry policy once.
    """
    counter = pytester.path / _COUNTER_NAME
    pytester.makefile(".toml", foundry=_TOML_RETRIES)
    pytester.makepyfile(test_flaky_from_toml=_flaky_source(counter))
    result = pytester.runpytest()

    assert result.ret == 0
    assert _counter_value(counter) == _ATTEMPTS_WITH_TWO_RETRIES
    record = _only_result(pytester)
    assert record["outcome"] == "passed"
    assert record["attempts"] == _ATTEMPTS_WITH_TWO_RETRIES
    _assert_nodeid(record, module="test_flaky_from_toml.py", function="test_flaky_then_green")


def test_function_scoped_fixture_is_recreated_per_attempt(
    pytester: pytest.Pytester, clean_foundry_env: None
) -> None:
    """Each attempt runs the whole protocol, so a function-scoped fixture is built three times.

    AAP 0.7.1's consequence, observed: the driver calls pytest's own protocol once per attempt, so
    setup and teardown bracket every attempt and the fixture ledger holds one entry per attempt.
    """
    counter = pytester.path / _COUNTER_NAME
    ledger = pytester.path / _LEDGER_NAME
    pytester.makepyfile(test_flaky_fixture=_fixture_source(ledger, counter, scope="function"))
    result = pytester.runpytest("--foundry-retries", "2")

    assert result.ret == 0
    entries = _ledger_entries(ledger)
    assert len(entries) == _ATTEMPTS_WITH_TWO_RETRIES, entries
    assert _only_result(pytester)["attempts"] == _ATTEMPTS_WITH_TWO_RETRIES


def test_session_scoped_fixture_persists_across_attempts(
    pytester: pytest.Pytester, clean_foundry_env: None
) -> None:
    """A session-scoped fixture is created once while the attempts run inside one session.

    The other half of AAP 0.7.1's fixture lifecycle: three attempts, one creation, so the retry
    driver does not rebuild the session between them. The second item in the project is what keeps
    the session boundary open, for the reason :func:`_fixture_source` records, and it passes.
    """
    counter = pytester.path / _COUNTER_NAME
    ledger = pytester.path / _LEDGER_NAME
    pytester.makepyfile(
        test_flaky_session_fixture=_fixture_source(ledger, counter, scope="session", follower=True)
    )
    result = pytester.runpytest("--foundry-retries", "2")

    assert result.ret == 0
    result.assert_outcomes(passed=2)
    entries = _ledger_entries(ledger)
    assert len(entries) == 1, entries
    record = _result_for(pytester, "test_flaky_with_fixture")
    assert record["attempts"] == _ATTEMPTS_WITH_TWO_RETRIES
    assert record["outcome"] == "passed"


def test_retries_are_bounded_by_the_documented_range(
    pytester: pytest.Pytester, clean_foundry_env: None
) -> None:
    """A value outside ``0..10`` is a configuration fault, not a retry policy.

    AAP 0.4.5's accepted range and A4's exit code: the session stops during configuration with code
    ``2``, the message names ``execution.retries``, no test body runs and no report is written.
    """
    counter = pytester.path / _COUNTER_NAME
    pytester.makepyfile(test_flaky_out_of_range=_flaky_source(counter))
    result = pytester.runpytest("--foundry-retries", str(_RETRIES_ABOVE_RANGE))

    assert result.ret == 2
    output = result.stdout.str() + result.stderr.str()
    assert "execution.retries" in output, output
    assert "0..10" in output, output
    assert not counter.exists(), "no test body may run when the configuration is rejected"
    assert not (pytester.path / "results.json").exists(), _listing(pytester.path)
