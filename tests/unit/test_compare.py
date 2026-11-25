"""Unit tests for the result-set comparison behind ``foundry report compare``.

``foundry.reporting.compare.diff_files`` answers one question about two ``results.json`` documents
-- which tests regressed against a baseline -- and AAP 0.14.1 makes that answer an acceptance
observation, so this module reads the committed fixture pair rather than only documents built
inline. What it pins:

* the three sets the committed pair produces, each non-empty, ascending, de-duplicated and disjoint
  from the other two, so the fixture cannot silently degrade into a weaker test;
* the classification of all nine outcome values, one outcome at a time with a message naming it;
* the empty-newly-failing case, over a self-comparison, an identical copy and a failure-free pair;
* ``run_error`` elements, which describe the run and so reach none of the three sets;
* node ids present in only one document, including one that disappears entirely;
* unreadable and not-a-Foundry-document inputs, which raise ``DataError`` rather than a bare
  decoding error;
* determinism: repeated calls agree and stay ordered, the comparator mutates neither document, and
  the committed fixtures carry the same SHA-256 digest after this module has run.

The CLI surface -- ``--help``, ``--json``, the three labelled groups and the exit-code mapping --
belongs to ``tests/unit/test_cli.py`` and ``tests/plugin/test_report_compare.py``; here
``ComparisonDiff.has_new_failures`` is the predicate asserted, which AAP 0.4.4 has exit ``0``
follow.

Two readings differ from this module's brief and follow the product, which the AAP outranks:
``quarantined_failed`` does not count as failing, because AAP 0.4.7 keeps a quarantined failure from
failing a run in ``report`` mode; and a document compared with itself reports its standing failures
rather than three empty sets, because AAP 0.4.4 reads ``newly_passing`` as "failing in the baseline
and no longer failing".
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from itertools import combinations
from pathlib import Path
from typing import Final

import pytest

from foundry.core.exceptions import DataError, FoundryError
from foundry.reporting.compare import ComparisonDiff, diff, diff_files
from foundry.reporting.record import Outcome, ResultElement, parse_element

# --- The committed fixtures ------------------------------------------------

RESULTS_BASE: Final[str] = "results_base.json"
"""The baseline document of the committed pair: two passing, two failing and one skipped record."""

RESULTS_NEW: Final[str] = "results_new.json"
"""The newer document: the same five node ids, carrying one regression and one recovery."""

RESULTS_UNREADABLE: Final[str] = "results_unreadable.txt"
"""The non-JSON document the unreadable-input path is exercised with."""

FIXTURE_FILES: Final[tuple[str, ...]] = (RESULTS_BASE, RESULTS_NEW, RESULTS_UNREADABLE)
"""Every fixture this module reads, so one test can prove none of them was written."""

EXPECTED_SETS: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    ("newly_failing", ("tests/test_checkout.py::test_payment_ok",)),
    ("newly_passing", ("tests/test_checkout.py::test_payment_declined",)),
    ("still_failing", ("tests/test_cart.py::test_two_items",)),
)
"""The documented answer for the committed pair, one entry per set AAP 0.4.4 has the command print.

``test_payment_ok`` passed in the baseline and fails in the newer document,
``test_payment_declined`` is the reverse, and ``test_two_items`` fails in both, so the pair
exercises all three transitions.
"""

SET_NAMES: Final[tuple[str, ...]] = tuple(name for name, _ in EXPECTED_SETS)
"""The three field names of :class:`ComparisonDiff` a comparison reports on."""

FAILING_IN_BASELINE: Final[tuple[str, ...]] = (
    "tests/test_cart.py::test_two_items",
    "tests/test_checkout.py::test_payment_declined",
)
"""The baseline's failing node ids, ascending: the union of the recovery and the standing failure.

Comparing the baseline with itself leaves exactly these in ``still_failing``, which is why a
self-comparison is not the "three empty sets" case.
"""

# Fixed document vocabulary: no assertion here reads the host's clock, a real run id or a duration.
RUN_ID: Final[str] = "20250102T030405-abc123"
PROFILE: Final[str] = "ci"
STARTED_AT: Final[str] = "2025-01-02T03:04:05+00:00"
FINISHED_AT: Final[str] = "2025-01-02T03:04:05.250000+00:00"
RECORDED_AT: Final[str] = "2025-01-02T03:04:06+00:00"
MODULE_PATH: Final[str] = "tests/test_cart.py"

QUARANTINE_STATE_BY_OUTCOME: Final[dict[str, str]] = {
    "quarantined_failed": "failed",
    "quarantine_passed": "passed",
}
"""The quarantine state AAP 0.7.2 requires for each quarantined outcome, and ``none`` otherwise."""

DOCUMENTED_CLASSIFICATION: Final[tuple[tuple[str, bool], ...]] = (
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
"""Every outcome value and whether it counts as failing in a comparison (AAP 0.4.6, AAP 0.4.7).

``failed`` and ``error`` are the two a run fails on, the pair
:data:`foundry.reporting.record.FAILING_OUTCOMES` holds; the comparator reads that set rather than
restating it. The quarantine outcomes stay outside it: AAP 0.4.7 makes a quarantined failure leave
exit code ``0`` in ``report`` mode, so counting one here would report a regression the run never
saw, and a passing quarantined test is a success whose only follow-up is the report's advice to
remove the marker. ``skipped``, ``xfailed``, ``xpassed`` and ``not_run`` do not fail a run either.
"""


# --- Helpers ---------------------------------------------------------------


def _record(nodeid: str, outcome: str) -> dict[str, object]:
    """Return one ``results.json`` result element shaped as AAP 0.4.6 tabulates it."""
    return {
        "record_type": "result",
        "run_id": RUN_ID,
        "started_at": STARTED_AT,
        "finished_at": FINISHED_AT,
        "profile": PROFILE,
        "nodeid": nodeid,
        "outcome": outcome,
        "attempts": 1,
        "duration_ms": 12.5,
        "markers": [],
        "failures": [],
        "artifacts": [],
        "quarantine": {"state": QUARANTINE_STATE_BY_OUTCOME.get(outcome, "none")},
    }


def _run_error(worker_id: str = "gw0") -> dict[str, object]:
    """Return one ``results.json`` run-error element: the kind that carries no node id."""
    return {
        "record_type": "run_error",
        "run_id": RUN_ID,
        "recorded_at": RECORDED_AT,
        "worker_id": worker_id,
        "message": "worker lost",
    }


def _write_document(path: Path, *elements: dict[str, object]) -> Path:
    """Write ``elements`` as the JSON array a result document holds, and return the path."""
    path.write_text(json.dumps(list(elements)) + "\n", encoding="utf-8")
    return path


def _compare(
    tmp_path: Path, base: Sequence[dict[str, object]], new: Sequence[dict[str, object]]
) -> ComparisonDiff:
    """Compare two synthesised documents written into ``tmp_path``, via the product's reader."""
    return diff_files(
        _write_document(tmp_path / "base.json", *base),
        _write_document(tmp_path / "new.json", *new),
    )


def _parsed(elements: Sequence[dict[str, object]], source: Path) -> list[ResultElement]:
    """Return ``elements`` validated by the record model, as the document reader validates them."""
    return [parse_element(element, source=source) for element in elements]


def _failing_nodeids(path: Path) -> set[str]:
    """Return the node ids a stored document reports as failing, read from its outcome values.

    Deliberately independent of the comparator: it applies the AAP 0.4.7 rule to the raw document,
    so a comparison that drifted from that rule disagrees with it instead of agreeing with itself.
    """
    failing = {outcome for outcome, counts in DOCUMENTED_CLASSIFICATION if counts}
    document = json.loads(path.read_text(encoding="utf-8"))
    return {
        entry["nodeid"]
        for entry in document
        if entry["record_type"] == "result" and entry["outcome"] in failing
    }


def _sha256(path: Path) -> str:
    """Return the SHA-256 digest of ``path``'s bytes, the fingerprint a rewrite would change."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --- Fixtures --------------------------------------------------------------


@pytest.fixture(scope="module")
def fixtures_dir(repo_root: Path) -> Path:
    """Module scope: the committed comparison fixtures, checked for the files this module reads."""
    directory = repo_root / "tests" / "fixtures"
    missing = [name for name in FIXTURE_FILES if not (directory / name).is_file()]
    assert not missing, f"comparison fixtures missing from {directory}: {', '.join(missing)}"
    return directory


@pytest.fixture(scope="module")
def base_document(fixtures_dir: Path) -> Path:
    """Module scope: the committed baseline document."""
    return fixtures_dir / RESULTS_BASE


@pytest.fixture(scope="module")
def new_document(fixtures_dir: Path) -> Path:
    """Module scope: the committed newer document."""
    return fixtures_dir / RESULTS_NEW


@pytest.fixture(scope="module")
def unreadable_document(fixtures_dir: Path) -> Path:
    """Module scope: the committed non-JSON document."""
    return fixtures_dir / RESULTS_UNREADABLE


@pytest.fixture(scope="module")
def committed_diff(base_document: Path, new_document: Path) -> ComparisonDiff:
    """Module scope: the committed pair compared once, through the product's file entry point."""
    return diff_files(base_document, new_document)


# --- The three sets over the committed pair --------------------------------


@pytest.mark.parametrize(("name", "expected"), EXPECTED_SETS)
def test_the_committed_pair_reports_the_documented_set(
    committed_diff: ComparisonDiff, name: str, expected: tuple[str, ...]
) -> None:
    assert getattr(committed_diff, name) == expected


@pytest.mark.parametrize("name", SET_NAMES)
def test_no_committed_set_is_empty(committed_diff: ComparisonDiff, name: str) -> None:
    node_ids: tuple[str, ...] = getattr(committed_diff, name)
    assert node_ids, f"{name} is empty, so the committed pair no longer exercises it"


@pytest.mark.parametrize("name", SET_NAMES)
def test_every_committed_set_is_sorted_and_de_duplicated(
    committed_diff: ComparisonDiff, name: str
) -> None:
    node_ids: tuple[str, ...] = getattr(committed_diff, name)
    assert node_ids == tuple(sorted(set(node_ids)))


@pytest.mark.parametrize(("left", "right"), tuple(combinations(SET_NAMES, 2)))
def test_the_committed_sets_are_pairwise_disjoint(
    committed_diff: ComparisonDiff, left: str, right: str
) -> None:
    left_ids: tuple[str, ...] = getattr(committed_diff, left)
    right_ids: tuple[str, ...] = getattr(committed_diff, right)
    assert set(left_ids).isdisjoint(right_ids)


def test_the_committed_pair_matches_the_transitions_its_documents_show(
    base_document: Path, new_document: Path, committed_diff: ComparisonDiff
) -> None:
    base_failing = _failing_nodeids(base_document)
    new_failing = _failing_nodeids(new_document)
    assert set(committed_diff.newly_failing) == new_failing - base_failing
    assert set(committed_diff.newly_passing) == base_failing - new_failing
    assert set(committed_diff.still_failing) == base_failing & new_failing


# --- Which outcomes count as failing (AAP 0.4.6, AAP 0.4.7) -----------------


def test_the_classification_table_covers_every_outcome() -> None:
    documented = sorted(outcome for outcome, _ in DOCUMENTED_CLASSIFICATION)
    assert documented == sorted(member.value for member in Outcome)


@pytest.mark.parametrize(("outcome", "counts_as_failing"), DOCUMENTED_CLASSIFICATION)
def test_each_outcome_is_classified_as_documented(
    tmp_path: Path, outcome: str, counts_as_failing: bool
) -> None:
    nodeid = f"{MODULE_PATH}::outcome_{outcome}"
    result = _compare(tmp_path, [_record(nodeid, outcome)], [_record(nodeid, "failed")])
    if counts_as_failing:
        assert result.still_failing == (nodeid,), f"outcome {outcome!r} must count as failing"
        assert result.newly_failing == ()
    else:
        assert result.newly_failing == (nodeid,), f"outcome {outcome!r} must not count as failing"
        assert result.still_failing == ()


# --- The empty-newly-failing case ------------------------------------------


def test_a_document_compared_with_itself_reports_no_regression_or_recovery(
    base_document: Path,
) -> None:
    same = diff_files(base_document, base_document)
    assert same.newly_failing == ()
    assert same.newly_passing == ()
    assert same.still_failing == FAILING_IN_BASELINE
    assert same.has_new_failures is False


def test_an_identical_copy_compares_as_the_committed_pair(
    tmp_path: Path, base_document: Path, new_document: Path, committed_diff: ComparisonDiff
) -> None:
    copies = []
    for path in (base_document, new_document):
        copy = tmp_path / path.name
        copy.write_bytes(path.read_bytes())
        copies.append(copy)
    assert diff_files(*copies) == committed_diff


def test_a_failure_free_pair_reports_nothing_at_all(tmp_path: Path) -> None:
    nodeid = f"{MODULE_PATH}::ok"
    result = _compare(tmp_path, [_record(nodeid, "passed")], [_record(nodeid, "passed")])
    assert result.is_empty is True
    assert result.has_new_failures is False
    assert result.as_dict() == {"newly_failing": [], "newly_passing": [], "still_failing": []}
    assert result.counts() == {"newly_failing": 0, "newly_passing": 0, "still_failing": 0}


# --- Run-error elements (AAP 0.4.6) ----------------------------------------


def test_run_error_elements_reach_none_of_the_sets(tmp_path: Path) -> None:
    standing = f"{MODULE_PATH}::standing"
    recovered = f"{MODULE_PATH}::recovered"
    fresh = f"{MODULE_PATH}::fresh"
    base = [_record(standing, "failed"), _record(recovered, "failed")]
    new = [_record(standing, "failed"), _record(recovered, "passed"), _record(fresh, "error")]
    without_errors = _compare(tmp_path, base, new)
    with_errors = _compare(
        tmp_path, [*base, _run_error()], [*new, _run_error("gw1"), _run_error("gw2")]
    )
    assert with_errors == without_errors
    reported = {
        *with_errors.newly_failing,
        *with_errors.newly_passing,
        *with_errors.still_failing,
    }
    assert reported == {standing, recovered, fresh}


def test_a_run_error_alone_is_not_a_difference(tmp_path: Path) -> None:
    record = _record(f"{MODULE_PATH}::ok", "passed")
    plain = _write_document(tmp_path / "plain.json", record)
    with_error = _write_document(tmp_path / "with_error.json", record, _run_error("gw1"))
    result = diff_files(plain, with_error)
    assert result.is_empty is True
    assert result == diff_files(plain, plain)


# --- A node id present in only one document --------------------------------


def test_a_nodeid_present_in_only_one_document_is_placed_by_the_documented_rule(
    tmp_path: Path,
) -> None:
    shared = f"{MODULE_PATH}::shared"
    vanished = f"{MODULE_PATH}::vanished"
    appeared = f"{MODULE_PATH}::appeared"
    result = _compare(
        tmp_path,
        [_record(shared, "failed"), _record(vanished, "failed")],
        [_record(shared, "failed"), _record(appeared, "error")],
    )
    assert result.newly_failing == (appeared,), (
        "a failing node id only the newer document reports is a regression"
    )
    assert result.still_failing == (shared,), (
        "a failure both documents report is a standing failure, so the vanished node id is not one"
    )
    assert result.newly_passing == (vanished,), (
        "AAP 0.4.4 reads newly_passing as baseline-failing minus newer-failing, so a node id the "
        "newer document no longer reports counts as no longer failing"
    )


# --- Unreadable and non-Foundry inputs -------------------------------------


def test_an_unreadable_document_raises_data_error_naming_its_path(
    base_document: Path, unreadable_document: Path
) -> None:
    with pytest.raises(DataError) as raised:
        diff_files(base_document, unreadable_document)
    assert raised.type is DataError
    assert isinstance(raised.value, FoundryError)
    assert raised.value.file == unreadable_document
    assert str(unreadable_document) in str(raised.value)


def test_the_unreadable_fixture_is_not_json(unreadable_document: Path) -> None:
    with pytest.raises(json.JSONDecodeError):
        json.loads(unreadable_document.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("name", "payload"),
    (
        ("an_object", "{}"),
        ("integers", "[1, 2, 3]"),
        (
            "element_without_record_type",
            '[{"nodeid": "tests/test_cart.py::ok", "outcome": "failed"}]',
        ),
    ),
)
def test_a_parsed_document_that_is_not_a_result_document_raises_data_error(
    tmp_path: Path, base_document: Path, name: str, payload: str
) -> None:
    target = tmp_path / f"{name}.json"
    target.write_text(payload, encoding="utf-8")
    with pytest.raises(DataError) as raised:
        diff_files(base_document, target)
    assert isinstance(raised.value, FoundryError)
    assert raised.value.file == target
    assert str(target) in str(raised.value)


# --- Determinism and read-only fixtures ------------------------------------


def test_two_comparisons_of_the_committed_pair_are_equal(
    base_document: Path, new_document: Path
) -> None:
    first = diff_files(base_document, new_document)
    second = diff_files(base_document, new_document)
    assert first == second
    for name in SET_NAMES:
        node_ids: tuple[str, ...] = getattr(second, name)
        assert node_ids == tuple(sorted(node_ids))


def test_the_comparator_does_not_mutate_the_records_it_is_given(tmp_path: Path) -> None:
    source = tmp_path / "results.json"
    base = _parsed(
        [_record(f"{MODULE_PATH}::standing", "failed"), _record(f"{MODULE_PATH}::fixed", "passed")],
        source,
    )
    new = _parsed(
        [_record(f"{MODULE_PATH}::standing", "failed"), _record(f"{MODULE_PATH}::fixed", "error")],
        source,
    )
    before = ([element.to_dict() for element in base], [element.to_dict() for element in new])
    diff(base, new)
    after = ([element.to_dict() for element in base], [element.to_dict() for element in new])
    assert after == before


def test_the_committed_fixtures_are_only_ever_read(fixtures_dir: Path) -> None:
    paths = tuple(fixtures_dir / name for name in FIXTURE_FILES)
    before = {path.name: _sha256(path) for path in paths}
    diff_files(fixtures_dir / RESULTS_BASE, fixtures_dir / RESULTS_NEW)
    diff_files(fixtures_dir / RESULTS_BASE, fixtures_dir / RESULTS_BASE)
    with pytest.raises(DataError):
        diff_files(fixtures_dir / RESULTS_BASE, fixtures_dir / RESULTS_UNREADABLE)
    assert {path.name: _sha256(path) for path in paths} == before
