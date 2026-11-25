"""The result-set comparison behind ``foundry report compare``, over the committed fixture pair.
AAP 0.14.1 makes the three sets an acceptance observation; ``has_new_failures`` is the predicate the
command's exit code follows, and the CLI surface itself belongs to ``tests/unit/test_cli.py``."""

from __future__ import annotations

import json
from collections.abc import Sequence
from itertools import combinations
from pathlib import Path
from typing import Final, TypeAlias

import pytest

from foundry.core.exceptions import DataError, FoundryError
from foundry.reporting.compare import ComparisonDiff, diff_files
from foundry.reporting.record import Outcome

RESULTS_BASE: Final[str] = "results_base.json"
RESULTS_NEW: Final[str] = "results_new.json"
RESULTS_UNREADABLE: Final[str] = "results_unreadable.txt"
FIXTURE_FILES: Final[tuple[str, ...]] = (RESULTS_BASE, RESULTS_NEW, RESULTS_UNREADABLE)
EXPECTED_SETS: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    ("newly_failing", ("tests/test_checkout.py::test_payment_ok",)),
    ("newly_passing", ("tests/test_checkout.py::test_payment_declined",)),
    ("still_failing", ("tests/test_cart.py::test_two_items",)),
)
SET_NAMES: Final[tuple[str, ...]] = tuple(name for name, _ in EXPECTED_SETS)
MODULE_PATH: Final[str] = "tests/test_cart.py"
_STATE: Final[dict[str, str]] = {"quarantined_failed": "failed", "quarantine_passed": "passed"}
# Every outcome value and whether it counts as failing. A quarantined failure does not: AAP 0.4.7
# keeps it out of the failing set, so counting one would report a regression the run never saw.
CLASSIFICATION: Final[tuple[tuple[str, bool], ...]] = (
    ("passed", False),
    ("failed", True),
    ("skipped", False),
    ("xfailed", False),
    ("xpassed", False),
    ("error", True),
    ("quarantined_failed", False),
    ("quarantine_passed", False),
    ("not_run", False),
)
_Rows: TypeAlias = Sequence[dict[str, object]]


def _record(nodeid: str, outcome: str) -> dict[str, object]:
    return {
        "record_type": "result",
        "run_id": "20250102T030405-abc123",
        "started_at": "2025-01-02T03:04:05+00:00",
        "finished_at": "2025-01-02T03:04:05.250000+00:00",
        "profile": "ci",
        "nodeid": nodeid,
        "outcome": outcome,
        "attempts": 1,
        "duration_ms": 12.5,
        "quarantine": {"state": _STATE.get(outcome, "none")},
    }


def _run_error(worker_id: str = "gw0") -> dict[str, object]:
    return {
        "record_type": "run_error",
        "run_id": "20250102T030405-abc123",
        "recorded_at": "2025-01-02T03:04:06+00:00",
        "worker_id": worker_id,
        "message": "worker lost",
    }


def _compare(tmp: Path, base: _Rows, new: _Rows) -> ComparisonDiff:
    for name, rows in (("base", base), ("new", new)):
        (tmp / f"{name}.json").write_text(json.dumps(list(rows)) + "\n", encoding="utf-8")
    return diff_files(tmp / "base.json", tmp / "new.json")


def _failing(path: Path) -> set[str]:
    # Deliberately independent of the comparator: the AAP 0.4.7 rule applied to the raw document.
    failing = {outcome for outcome, counts in CLASSIFICATION if counts}
    rows = json.loads(path.read_text(encoding="utf-8"))
    return {r["nodeid"] for r in rows if r["record_type"] == "result" and r["outcome"] in failing}


@pytest.fixture(scope="module")
def documents(repo_root: Path) -> Path:
    directory = repo_root / "tests" / "fixtures"
    missing = [name for name in FIXTURE_FILES if not (directory / name).is_file()]
    assert not missing, f"comparison fixtures missing from {directory}: {', '.join(missing)}"
    return directory


@pytest.fixture(scope="module")
def pair(documents: Path) -> ComparisonDiff:
    return diff_files(documents / RESULTS_BASE, documents / RESULTS_NEW)


@pytest.mark.parametrize(("name", "expected"), EXPECTED_SETS)
def test_the_committed_pair_reports_the_documented_set(
    pair: ComparisonDiff, name: str, expected: tuple[str, ...]
) -> None:
    node_ids: tuple[str, ...] = getattr(pair, name)
    assert node_ids == expected and node_ids == tuple(sorted(set(node_ids)))


@pytest.mark.parametrize(("left", "right"), tuple(combinations(SET_NAMES, 2)))
def test_the_sets_are_pairwise_disjoint(pair: ComparisonDiff, left: str, right: str) -> None:
    assert set(getattr(pair, left)).isdisjoint(getattr(pair, right))


def test_the_committed_pair_matches_the_transitions_its_documents_show(
    documents: Path, pair: ComparisonDiff
) -> None:
    base, new = _failing(documents / RESULTS_BASE), _failing(documents / RESULTS_NEW)
    assert set(pair.newly_failing) == new - base
    assert set(pair.newly_passing) == base - new and set(pair.still_failing) == base & new


def test_the_classification_table_covers_every_outcome() -> None:
    assert sorted(outcome for outcome, _ in CLASSIFICATION) == sorted(m.value for m in Outcome)


@pytest.mark.parametrize(("outcome", "counts_as_failing"), CLASSIFICATION)
def test_each_outcome_is_classified_as_documented(
    tmp_path: Path, outcome: str, counts_as_failing: bool
) -> None:
    nodeid = f"{MODULE_PATH}::outcome_{outcome}"
    result = _compare(tmp_path, [_record(nodeid, outcome)], [_record(nodeid, "failed")])
    assert result.still_failing == ((nodeid,) if counts_as_failing else ())
    assert result.newly_failing == (() if counts_as_failing else (nodeid,))


def test_a_document_compared_with_itself_reports_no_regression_or_recovery(documents: Path) -> None:
    same = diff_files(documents / RESULTS_BASE, documents / RESULTS_BASE)
    assert same.newly_failing == () and same.newly_passing == ()
    assert same.still_failing == (
        "tests/test_cart.py::test_two_items",
        "tests/test_checkout.py::test_payment_declined",
    )
    assert same.has_new_failures is False


def test_a_failure_free_pair_reports_nothing_at_all(tmp_path: Path) -> None:
    nodeid = f"{MODULE_PATH}::ok"
    result = _compare(tmp_path, [_record(nodeid, "passed")], [_record(nodeid, "passed")])
    assert result.is_empty is True and result.has_new_failures is False
    assert result.as_dict() == {"newly_failing": [], "newly_passing": [], "still_failing": []}
    assert result.counts() == {"newly_failing": 0, "newly_passing": 0, "still_failing": 0}


def test_run_error_elements_reach_none_of_the_sets(tmp_path: Path) -> None:
    standing, recovered, fresh = (f"{MODULE_PATH}::{n}" for n in ("standing", "recovered", "fresh"))
    base = [_record(standing, "failed"), _record(recovered, "failed")]
    new = [_record(standing, "failed"), _record(recovered, "passed"), _record(fresh, "error")]
    without_errors = _compare(tmp_path, base, new)
    with_errors = _compare(tmp_path, [*base, _run_error()], [*new, _run_error("gw1")])
    assert with_errors == without_errors
    reported = {*with_errors.newly_failing, *with_errors.newly_passing, *with_errors.still_failing}
    assert reported == {standing, recovered, fresh}
    ok = _record(f"{MODULE_PATH}::ok", "passed")
    assert _compare(tmp_path, [ok], [ok, _run_error("gw2")]).is_empty is True


def test_a_nodeid_in_only_one_document_follows_the_documented_rule(tmp_path: Path) -> None:
    shared, vanished, appeared = (f"{MODULE_PATH}::{n}" for n in ("shared", "vanished", "appeared"))
    result = _compare(
        tmp_path,
        [_record(shared, "failed"), _record(vanished, "failed")],
        [_record(shared, "failed"), _record(appeared, "error")],
    )
    assert result.newly_failing == (appeared,) and result.still_failing == (shared,)
    # AAP 0.4.4 reads newly_passing as baseline-failing minus newer-failing.
    assert result.newly_passing == (vanished,)


def test_an_unreadable_document_raises_data_error_naming_its_path(documents: Path) -> None:
    unreadable = documents / RESULTS_UNREADABLE
    with pytest.raises(json.JSONDecodeError):
        json.loads(unreadable.read_text(encoding="utf-8"))
    with pytest.raises(DataError) as raised:
        diff_files(documents / RESULTS_BASE, unreadable)
    assert isinstance(raised.value, FoundryError)
    assert raised.value.file == unreadable and str(unreadable) in str(raised.value)


@pytest.mark.parametrize(
    ("name", "payload"),
    (
        ("an_object", "{}"),
        ("integers", "[1, 2, 3]"),
        ("no_record_type", '[{"nodeid": "tests/test_cart.py::ok", "outcome": "failed"}]'),
    ),
)
def test_a_parsed_document_that_is_not_a_result_document_raises_data_error(
    tmp_path: Path, documents: Path, name: str, payload: str
) -> None:
    target = tmp_path / f"{name}.json"
    target.write_text(payload, encoding="utf-8")
    with pytest.raises(DataError) as raised:
        diff_files(documents / RESULTS_BASE, target)
    assert isinstance(raised.value, FoundryError)
    assert raised.value.file == target and str(target) in str(raised.value)
