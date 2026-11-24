"""Unit tests for ``foundry.reporting.compare`` over the committed comparison fixtures.

``foundry report compare`` answers one question about two ``results.json`` documents -- which tests
regressed against a baseline -- and AAP 0.14.1 makes that answer an acceptance observation: the
newly-failing, newly-passing and still-failing sets, plus exit ``1`` when the first is non-empty,
produced by this module together with the planned ``tests/plugin/test_report_compare.py``.
AAP 0.4.4 fixes the three exit codes the subcommand publishes and AAP 0.4.6 fixes the shape of the
document both of its inputs are read as.

This module pins those facts at the boundary that matters -- the committed fixture pair under
``tests/fixtures`` read through :func:`foundry.reporting.compare.diff_files`, the same entry point
``report compare`` calls -- rather than against documents built inline, which is what
``tests/unit/test_reporting.py`` already does for the package surface.

What is pinned here:

* the three sets the committed pair produces, each of them non-empty, ascending, de-duplicated and
  disjoint from the other two, so the fixture cannot silently degrade into a weaker test;
* the exit-``1`` route a regression takes: the predicate, the documented code, and the status the
  real ``report compare`` command publishes through the click group;
* the exit-``0`` route: two identical documents report no regression and publish ``0``;
* the unreadable input: ``tests/fixtures/results_unreadable.txt`` raises
  :class:`foundry.core.exceptions.DataError` naming its path, escapes the command instead of being
  swallowed, and really is non-JSON, so its classification cannot drift;
* determinism and read-only inputs: two comparisons of one pair are equal, and the three committed
  files carry the same SHA-256 digest before and after this module's work.

The fixtures under ``tests/fixtures`` are read-only inputs: nothing here writes to them. The CLI
subcommand surface as a whole -- ``--help``, the ``--json`` document, and the exit-``2`` mapping
that lives in the not-yet-written ``foundry.cli.main`` -- belongs to the planned
``tests/plugin/test_report_compare.py``, so no numeric ``2`` and no rendering style is asserted
here: the three AAP 0.7.4 label names are the only text read, and only as the proof that a
completed comparison rendered rather than that a crash returned a matching status.
"""

from __future__ import annotations

import hashlib
import json
from itertools import combinations
from pathlib import Path
from typing import Final

import pytest
from click.testing import CliRunner

from foundry.cli import exit_codes
from foundry.cli.report_cmd import comparison_exit_code, report_group
from foundry.core.exceptions import DataError
from foundry.reporting.compare import ComparisonDiff, diff_files

RESULTS_BASE: Final[str] = "results_base.json"
"""The baseline document of the committed pair: two passing, two failing and one skipped record."""

RESULTS_NEW: Final[str] = "results_new.json"
"""The newer document: the same five node ids, with one regression and one recovery."""

RESULTS_UNREADABLE: Final[str] = "results_unreadable.txt"
"""The non-JSON document the comparison's unreadable-input path is exercised with."""

FIXTURE_FILES: Final[tuple[str, ...]] = (RESULTS_BASE, RESULTS_NEW, RESULTS_UNREADABLE)
"""Every fixture this module reads, so one test can prove none of them was written."""

NEWLY_FAILING: Final[tuple[str, ...]] = ("tests/test_checkout.py::test_payment_ok",)
"""Passing in the baseline and failing in the newer document: the pair's one regression."""

NEWLY_PASSING: Final[tuple[str, ...]] = ("tests/test_checkout.py::test_payment_declined",)
"""Failing in the baseline and passing in the newer document: the pair's one recovery."""

STILL_FAILING: Final[tuple[str, ...]] = ("tests/test_cart.py::test_two_items",)
"""Failing in both documents: the failure the pair is meant to leave standing."""

FAILING_IN_BASELINE: Final[tuple[str, ...]] = (
    "tests/test_cart.py::test_two_items",
    "tests/test_checkout.py::test_payment_declined",
)
"""The two node ids the baseline reports as failing, ascending, and exactly the union of the
standing failure with the recovery -- which is what comparing the baseline with itself reports."""

SET_NAMES: Final[tuple[str, ...]] = ("newly_failing", "newly_passing", "still_failing")
"""The three fields AAP 0.4.4 has the command print, in the order it prints them."""

COMPARISON_LABELS: Final[tuple[str, ...]] = ("Newly failing", "Newly passing", "Still failing")
"""The three labelled groups AAP 0.7.4 fixes, in that order, and what a completed comparison
renders. Their presence is what separates a published exit status from a crash's status."""


# --- Shared fixtures -------------------------------------------------------


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
    """Module scope: the committed pair compared once, through the product API."""
    return diff_files(base_document, new_document)


def _sha256(path: Path) -> str:
    """Return the SHA-256 digest of ``path``'s bytes, the fingerprint a rewrite would change."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --- The three sets over the committed pair --------------------------------


def test_the_committed_pair_reports_the_documented_newly_failing_set(
    committed_diff: ComparisonDiff,
) -> None:
    assert committed_diff.newly_failing == NEWLY_FAILING


def test_the_committed_pair_reports_the_documented_newly_passing_set(
    committed_diff: ComparisonDiff,
) -> None:
    assert committed_diff.newly_passing == NEWLY_PASSING


def test_the_committed_pair_reports_the_documented_still_failing_set(
    committed_diff: ComparisonDiff,
) -> None:
    assert committed_diff.still_failing == STILL_FAILING


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


# --- The exit-1 route (AAP 0.4.4, AAP 0.14.1) ------------------------------


def test_a_regression_over_the_committed_pair_is_exit_one(committed_diff: ComparisonDiff) -> None:
    assert committed_diff.has_new_failures is True
    assert exit_codes.FAILED == 1
    assert comparison_exit_code(committed_diff) == exit_codes.FAILED


def test_the_command_publishes_exit_one_for_the_committed_pair(
    base_document: Path, new_document: Path
) -> None:
    result = CliRunner().invoke(report_group, ["compare", str(base_document), str(new_document)])
    assert result.exit_code == exit_codes.FAILED
    # CliRunner reports 1 for an unhandled exception too, so the status alone cannot tell the
    # documented ctx.exit(1) from a crash: the comparison must have rendered its three groups and
    # the invocation must have ended through SystemExit carrying that same status.
    assert isinstance(result.exception, SystemExit)
    assert result.exception.code == exit_codes.FAILED
    assert all(label in result.output for label in COMPARISON_LABELS)


# --- The exit-0 route ------------------------------------------------------


def test_two_identical_documents_report_no_regression(base_document: Path) -> None:
    same = diff_files(base_document, base_document)
    assert same.newly_failing == ()
    assert same.newly_passing == ()
    assert same.still_failing == FAILING_IN_BASELINE
    assert same.has_new_failures is False
    assert exit_codes.SUCCESS == 0
    assert comparison_exit_code(same) == exit_codes.SUCCESS


def test_the_command_publishes_exit_zero_when_nothing_newly_fails(base_document: Path) -> None:
    result = CliRunner().invoke(report_group, ["compare", str(base_document), str(base_document)])
    assert result.exit_code == exit_codes.SUCCESS
    assert result.exception is None


# --- The unreadable input --------------------------------------------------


def test_an_unreadable_document_raises_data_error_naming_its_path(
    base_document: Path, unreadable_document: Path
) -> None:
    with pytest.raises(DataError) as raised:
        diff_files(base_document, unreadable_document)
    assert raised.type is DataError
    assert raised.value.file == unreadable_document
    assert str(unreadable_document) in str(raised.value)


def test_the_unreadable_fixture_is_not_json(unreadable_document: Path) -> None:
    with pytest.raises(json.JSONDecodeError):
        json.loads(unreadable_document.read_text(encoding="utf-8"))


def test_the_command_lets_the_unreadable_document_escape_as_data_error(
    base_document: Path, unreadable_document: Path
) -> None:
    result = CliRunner().invoke(
        report_group, ["compare", str(base_document), str(unreadable_document)]
    )
    assert isinstance(result.exception, DataError)
    assert result.output == ""


# --- Determinism and read-only fixtures ------------------------------------


def test_two_comparisons_of_the_committed_pair_are_equal(
    base_document: Path, new_document: Path
) -> None:
    assert diff_files(base_document, new_document) == diff_files(base_document, new_document)


def test_the_committed_fixtures_are_only_ever_read(fixtures_dir: Path) -> None:
    paths = tuple(fixtures_dir / name for name in FIXTURE_FILES)
    before = {path.name: _sha256(path) for path in paths}
    diff_files(fixtures_dir / RESULTS_BASE, fixtures_dir / RESULTS_NEW)
    diff_files(fixtures_dir / RESULTS_BASE, fixtures_dir / RESULTS_BASE)
    with pytest.raises(DataError):
        diff_files(fixtures_dir / RESULTS_BASE, fixtures_dir / RESULTS_UNREADABLE)
    assert {path.name: _sha256(path) for path in paths} == before
