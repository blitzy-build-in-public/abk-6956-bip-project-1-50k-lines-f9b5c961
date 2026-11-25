"""AAP 0.14.1 criterion 9, comparison half: ``foundry report compare`` and its three groups.

The command answers one question about two ``results.json`` documents -- which tests regressed --
and this module observes every published part of the answer: the three labelled groups in the fixed
order AAP 0.7.4 gives them (Newly failing, Newly passing, Still failing), the node ids sorted inside
each, the single JSON document ``--json`` prints (AAP 0.7.3), and the three exit codes of AAP 0.4.4
(``0`` when nothing newly fails, ``1`` when something does, ``2`` when a document cannot be read).

The documents are the committed pair ``tests/fixtures/results_base.json`` ->
``tests/fixtures/results_new.json``. They carry the same five node ids from two runs and differ in
exactly two outcomes, so the answer is fixed: newly failing is
``tests/test_checkout.py::test_payment_ok``, newly passing is
``tests/test_checkout.py::test_payment_declined``, and still failing is
``tests/test_cart.py::test_two_items``. Every test derives those sets from the documents it read
*and* asserts them as literals, so a comparator fault and a drifted fixture both fail it.
Everything this module writes lands in ``tmp_path``; the committed fixtures are only ever read.

Two facts about the harness shape the module. First, the comparison is driven through
:func:`foundry.cli.main.main` -- the function the installed console script calls -- because that
function is the only place a ``DataError`` becomes exit ``2``; invoking ``foundry.cli.main``'s group
directly would observe the raw exception and click's exit ``1`` instead. ``CliRunner.invoke`` calls
``cli.main(args=..., prog_name=...)`` on the object it is handed, so :class:`_ConsoleEntry` adapts a
plain function to that shape and raises ``SystemExit`` with the status, exactly as the installed
wrapper does. click 8.5.1 reports ``stdout`` and ``stderr`` separately, which is what lets the
unreadable-document test assert that nothing at all reached stdout.

Second, one set follows the fixtures rather than a shorthand: the baseline document already holds
two failing tests, so comparing it with itself reports those two as *still* failing while exiting
``0``; only the newly failing and newly passing groups are empty. Both the ``--json`` document and
the rendered groups are asserted against the files, never the other way round.

The pure comparison logic over this same pair belongs to ``tests/unit/test_compare.py``; this module
owns the rendered surface -- group order, labels, exit codes and the ``--json`` document.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Final

import pytest
from click.testing import CliRunner, Result

from foundry.cli.exit_codes import FAILED, SUCCESS, USAGE_OR_CONFIG
from foundry.cli.main import main

REACHES: tuple[str, ...] = (
    "command:report",
    "format:results.json",
)
"""The public surfaces this module drives through a real entry point (AAP 0.14.1, criterion 13).

``tests/plugin/test_reachability.py`` reads this declaration: ``command:report`` is the subcommand
every test here invokes through :func:`foundry.cli.main.main`, and ``format:results.json`` is honest
because every assertion compares two real result documents the subcommand read.
"""

_FIXTURES_DIR: Final[str] = "fixtures"
"""The directory under ``tests/`` that holds the committed result documents."""

_RESULTS_BASE: Final[str] = "results_base.json"
"""The baseline document: the earlier of the two runs AAP 0.14.1 compares."""

_RESULTS_NEW: Final[str] = "results_new.json"
"""The newer document: the same five node ids with two outcomes swapped."""

_RESULTS_UNREADABLE: Final[str] = "results_unreadable.txt"
"""A file that is not a result document, for the exit ``2`` state of AAP 0.4.4."""

_LABEL_NEWLY_FAILING: Final[str] = "Newly failing"
"""The first group's label, the regressions (AAP 0.7.4)."""

_LABEL_NEWLY_PASSING: Final[str] = "Newly passing"
"""The second group's label, the recoveries (AAP 0.7.4)."""

_LABEL_STILL_FAILING: Final[str] = "Still failing"
"""The third group's label, the failures both runs report (AAP 0.7.4)."""

_LABELS: Final[tuple[str, ...]] = (
    _LABEL_NEWLY_FAILING,
    _LABEL_NEWLY_PASSING,
    _LABEL_STILL_FAILING,
)
"""The three labels in the fixed order AAP 0.7.4 publishes, which the output must preserve."""

_NEWLY_FAILING_KEY: Final[str] = "newly_failing"
_NEWLY_PASSING_KEY: Final[str] = "newly_passing"
_STILL_FAILING_KEY: Final[str] = "still_failing"

_JSON_KEYS: Final[tuple[str, ...]] = (
    _NEWLY_FAILING_KEY,
    _NEWLY_PASSING_KEY,
    _STILL_FAILING_KEY,
)
"""The three keys AAP 0.7.3 fixes, in the order ``sort_keys`` writes them."""

_NONE_MARKER: Final[str] = "(none)"
"""What an empty group prints, so all three groups are always visible (AAP 0.7.4)."""

_COLUMNS: Final[str] = "200"
"""The terminal width the runner pins, so rich never wraps a node id an assertion reads."""

_NODEID_SEPARATOR: Final[str] = "::"
"""The pytest node id separator, the token a group holding no node id must not contain."""

_EXPECTED_NEWLY_FAILING: Final[tuple[str, ...]] = ("tests/test_checkout.py::test_payment_ok",)
"""The one regression: passed in the baseline, failed in the newer run."""

_EXPECTED_NEWLY_PASSING: Final[tuple[str, ...]] = ("tests/test_checkout.py::test_payment_declined",)
"""The one recovery: failed in the baseline, passed in the newer run."""

_EXPECTED_STILL_FAILING: Final[tuple[str, ...]] = ("tests/test_cart.py::test_two_items",)
"""The one standing failure: failing in both documents, which is exit ``0`` on its own."""

_STANDING_FAILURES: Final[tuple[str, ...]] = (
    "tests/test_cart.py::test_two_items",
    "tests/test_checkout.py::test_payment_declined",
)
"""Every failure the baseline reports, ascending: what it lists against itself."""

_FAILING_OUTCOMES: Final[frozenset[str]] = frozenset({"failed", "error"})
"""The outcomes that count against a run, restated here so the test derives the sets itself."""

_FAILURE_ENTRY: Final[Mapping[str, object]] = MappingProxyType(
    {
        "message": "AssertionError: rewritten by the test",
        "phase": "call",
        "traceback_path": None,
    }
)
"""A failure entry a rewritten record carries, in the shape AAP 0.4.6 publishes."""

_RUN_ERROR: Final[Mapping[str, object]] = MappingProxyType(
    {
        "record_type": "run_error",
        "run_id": "20240501T130000-4d5e6f",
        "recorded_at": "2024-05-01T13:00:05.000+00:00",
        "worker_id": "gw0",
        "message": "worker gw0 disappeared",
    }
)
"""One run-error element as AAP 0.4.6 shapes it: no node id, no outcome, nothing to decide."""

_TRACEBACK_HEADING: Final[str] = "Traceback"
"""The text a stack trace would start with, which a reported error must never contain."""


class _ConsoleEntry:
    """The console entry point in the shape :meth:`click.testing.CliRunner.invoke` calls.

    ``CliRunner`` reaches for ``cli.main(args=..., prog_name=...)`` and reads ``cli.name`` for the
    default program name, so the plain function ``foundry.cli.main`` publishes cannot be handed to
    it directly. This adapter calls the real :func:`foundry.cli.main.main` and raises
    ``SystemExit`` with the status it returned -- the one line the installed script's wrapper
    adds -- so ``CliRunner`` publishes the documented code and captures both streams the command
    wrote.
    """

    name: Final[str] = "foundry"

    def main(
        self,
        args: Sequence[str] | None = None,
        prog_name: str | None = None,
        **extra: object,
    ) -> int:
        """Run one command line through the console entry point and end with its status."""
        del prog_name, extra
        raise SystemExit(main(list(args or ())))


_CONSOLE: Final[_ConsoleEntry] = _ConsoleEntry()
"""The single adapter instance, stateless so every test may share it."""


def _compare(*arguments: str) -> Result:
    """Run ``foundry report compare`` with ``arguments`` and return the captured result.

    The runner isolates the environment and the two streams; only ``COLUMNS`` is set, so the width
    rich renders at is fixed rather than the host terminal's.
    """
    runner = CliRunner()
    return runner.invoke(_CONSOLE, ["report", "compare", *arguments], env={"COLUMNS": _COLUMNS})


def _fixture(repo_root: Path, name: str, destination: Path) -> Path:
    """Copy the committed fixture ``name`` into ``destination`` and return the copy.

    The copies are what every invocation reads, so no test can mutate the committed document even by
    accident, and every path an assertion compares against is absolute.
    """
    source = repo_root / "tests" / _FIXTURES_DIR / name
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(source.read_bytes())
    return destination


def _elements(path: Path) -> list[dict[str, object]]:
    """Return the result document at ``path`` as its list of JSON objects, unchanged."""
    document = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(document, list), f"{path} does not hold a JSON array"
    return document


def _failing_nodeids(path: Path) -> set[str]:
    """Return the node ids ``path`` reports as failing, computed independently of the comparator.

    Only ``result`` elements carry an outcome, so a ``run_error`` element cannot contribute a node
    id here either -- the rule AAP 0.4.6 states for the comparator.
    """
    failing: set[str] = set()
    for element in _elements(path):
        if element.get("record_type") != "result":
            continue
        if element["outcome"] in _FAILING_OUTCOMES:
            failing.add(str(element["nodeid"]))
    return failing


def _expected_sets(base: Path, new: Path) -> dict[str, tuple[str, ...]]:
    """Derive the three comparison sets from two documents, sorted, without the comparator."""
    base_failing = _failing_nodeids(base)
    new_failing = _failing_nodeids(new)
    return {
        _NEWLY_FAILING_KEY: tuple(sorted(new_failing - base_failing)),
        _NEWLY_PASSING_KEY: tuple(sorted(base_failing - new_failing)),
        _STILL_FAILING_KEY: tuple(sorted(base_failing & new_failing)),
    }


def _documented_sets() -> dict[str, tuple[str, ...]]:
    """Return the three sets the committed pair is documented to produce (AAP 0.14.1)."""
    return {
        _NEWLY_FAILING_KEY: _EXPECTED_NEWLY_FAILING,
        _NEWLY_PASSING_KEY: _EXPECTED_NEWLY_PASSING,
        _STILL_FAILING_KEY: _EXPECTED_STILL_FAILING,
    }


def _groups(printed: str) -> dict[str, tuple[str, ...]]:
    """Split the rendered output into one node-id tuple per labelled group, in printed order.

    A heading is a label followed by its size in parentheses; the node ids follow on the indented
    lines beneath it. An empty group carries the ``(none)`` marker instead of a node id, which is
    dropped here so an empty group parses as an empty tuple.
    """
    groups: dict[str, tuple[str, ...]] = {}
    current: str | None = None
    collected: list[str] = []
    for line in printed.splitlines():
        heading = next((name for name in _LABELS if line.startswith(f"{name} (")), None)
        if heading is not None:
            if current is not None:
                groups[current] = tuple(collected)
            current = heading
            collected = []
            continue
        text = line.strip()
        if current is not None and text and text != _NONE_MARKER:
            collected.append(text)
    if current is not None:
        groups[current] = tuple(collected)
    return groups


def _with_outcomes(source: Path, destination: Path, outcomes: Mapping[str, str]) -> Path:
    """Write a copy of ``source`` with the listed node ids' outcomes rewritten, and return it.

    The rewrite is the least a derived document needs: an outcome, plus the failure entry AAP 0.4.6
    pairs with a failing one. Every other field comes from the committed record, so the copy stays a
    document the reader accepts.
    """
    elements = _elements(source)
    for element in elements:
        outcome = outcomes.get(str(element.get("nodeid", "")))
        if outcome is None:
            continue
        element["outcome"] = outcome
        element["failures"] = [] if outcome == "passed" else [dict(_FAILURE_ENTRY)]
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(elements, indent=2) + "\n", encoding="utf-8")
    return destination


def _with_run_error(source: Path, destination: Path) -> Path:
    """Write a copy of ``source`` with one ``run_error`` element appended, and return it."""
    elements = _elements(source)
    elements.append(dict(_RUN_ERROR))
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(elements, indent=2) + "\n", encoding="utf-8")
    return destination


@pytest.fixture
def documents(repo_root: Path, tmp_path: Path) -> tuple[Path, Path]:
    """Function scope: the committed baseline and newer documents, copied into ``tmp_path``."""
    return (
        _fixture(repo_root, _RESULTS_BASE, tmp_path / "base.json"),
        _fixture(repo_root, _RESULTS_NEW, tmp_path / "new.json"),
    )


@pytest.fixture
def unreadable(repo_root: Path, tmp_path: Path) -> Path:
    """Function scope: a copy of the file that is not a result document."""
    return _fixture(repo_root, _RESULTS_UNREADABLE, tmp_path / "unreadable.txt")


# --- The rendered surface (AAP 0.7.4) ---------------------------------------


def test_three_groups_in_fixed_order(documents: tuple[Path, Path]) -> None:
    """Three labels, once each and in the fixed order, each with its node ids beneath it."""
    base, new = documents
    printed = _compare(str(base), str(new)).stdout
    assert printed.splitlines() == [
        f"{_LABEL_NEWLY_FAILING} (1):",
        f"  {_EXPECTED_NEWLY_FAILING[0]}",
        f"{_LABEL_NEWLY_PASSING} (1):",
        f"  {_EXPECTED_NEWLY_PASSING[0]}",
        f"{_LABEL_STILL_FAILING} (1):",
        f"  {_EXPECTED_STILL_FAILING[0]}",
    ]
    positions = [printed.index(label) for label in _LABELS]
    assert positions == sorted(positions)
    assert all(printed.count(label) == 1 for label in _LABELS)


def test_a_group_with_several_nodeids_lists_them_ascending(
    documents: tuple[Path, Path], tmp_path: Path
) -> None:
    """Two node ids in a group print in ascending order, whatever order the records arrive in."""
    base, new = documents
    regressed = _with_outcomes(
        new,
        tmp_path / "regressed.json",
        {
            "tests/test_cart.py::test_empty_cart": "failed",
            "tests/test_inventory.py::test_stock_level": "failed",
        },
    )
    groups = _groups(_compare(str(new), str(regressed)).stdout)
    assert groups[_LABEL_NEWLY_FAILING] == (
        "tests/test_cart.py::test_empty_cart",
        "tests/test_inventory.py::test_stock_level",
    )
    assert groups[_LABEL_STILL_FAILING] == (
        "tests/test_cart.py::test_two_items",
        "tests/test_checkout.py::test_payment_ok",
    )
    assert groups[_LABEL_NEWLY_PASSING] == ()

    recovered = _with_outcomes(
        base,
        tmp_path / "recovered.json",
        {
            "tests/test_cart.py::test_two_items": "passed",
            "tests/test_checkout.py::test_payment_declined": "passed",
        },
    )
    recovered_groups = _groups(_compare(str(base), str(recovered)).stdout)
    assert recovered_groups[_LABEL_NEWLY_PASSING] == (
        "tests/test_cart.py::test_two_items",
        "tests/test_checkout.py::test_payment_declined",
    )
    assert recovered_groups[_LABEL_NEWLY_FAILING] == ()
    for nodeids in (*groups.values(), *recovered_groups.values()):
        assert list(nodeids) == sorted(nodeids)


# --- The exit codes (AAP 0.4.4) --------------------------------------------


def test_exit_1_when_newly_failing_is_non_empty(documents: tuple[Path, Path]) -> None:
    """A regression is exit ``1``, and each group holds exactly the set the documents imply."""
    base, new = documents
    result = _compare(str(base), str(new))
    assert result.exit_code == FAILED
    assert result.stderr == ""
    assert _expected_sets(base, new) == _documented_sets()
    groups = _groups(result.stdout)
    assert groups[_LABEL_NEWLY_FAILING] == _EXPECTED_NEWLY_FAILING
    assert groups[_LABEL_NEWLY_PASSING] == _EXPECTED_NEWLY_PASSING
    assert groups[_LABEL_STILL_FAILING] == _EXPECTED_STILL_FAILING


def test_exit_0_when_nothing_newly_fails(documents: tuple[Path, Path]) -> None:
    """A document against itself is exit ``0``, with the standing failures still listed."""
    base, _new = documents
    result = _compare(str(base), str(base))
    assert result.exit_code == SUCCESS
    groups = _groups(result.stdout)
    assert groups[_LABEL_NEWLY_FAILING] == ()
    assert groups[_LABEL_NEWLY_PASSING] == ()
    assert groups[_LABEL_STILL_FAILING] == _STANDING_FAILURES
    assert result.stdout.count(_NONE_MARKER) == 2
    for nodeids in (groups[_LABEL_NEWLY_FAILING], groups[_LABEL_NEWLY_PASSING]):
        assert all(_NODEID_SEPARATOR not in nodeid for nodeid in nodeids)


# --- The machine-readable document (AAP 0.7.3) -----------------------------


def test_json_document_is_single_and_exact(documents: tuple[Path, Path]) -> None:
    """``--json`` is one parsable document holding the three sorted sets and nothing else."""
    base, new = documents
    result = _compare(str(base), str(new), "--json")
    assert result.exit_code == FAILED
    assert result.stderr == ""
    document = json.loads(result.stdout)
    assert tuple(document) == _JSON_KEYS
    assert document == {key: list(value) for key, value in _documented_sets().items()}
    assert _documented_sets() == _expected_sets(base, new)


def test_json_exit_0_for_identical_documents(documents: tuple[Path, Path]) -> None:
    """The same document twice is exit ``0``: no regression, and the standing failures listed."""
    base, _new = documents
    result = _compare(str(base), str(base), "--json")
    assert result.exit_code == SUCCESS
    assert json.loads(result.stdout) == {
        _NEWLY_FAILING_KEY: [],
        _NEWLY_PASSING_KEY: [],
        _STILL_FAILING_KEY: list(_STANDING_FAILURES),
    }


@pytest.mark.parametrize("position", ["base", "new"])
def test_unreadable_document_exits_2(
    documents: tuple[Path, Path], unreadable: Path, position: str
) -> None:
    """An unreadable document is exit ``2`` on stderr, in either argument position."""
    base, new = documents
    arguments = (str(unreadable), str(new)) if position == "base" else (str(base), str(unreadable))
    result = _compare(*arguments)
    assert result.exit_code == USAGE_OR_CONFIG
    assert result.stdout == ""
    assert str(unreadable) in result.stderr
    assert _TRACEBACK_HEADING not in result.stderr


# --- The run-error element (AAP 0.4.6) -------------------------------------


def test_run_error_elements_are_ignored(documents: tuple[Path, Path], tmp_path: Path) -> None:
    """A ``run_error`` element carries no node id, so it changes none of the three sets."""
    base, new = documents
    with_error = _with_run_error(new, tmp_path / "new_with_run_error.json")
    result = _compare(str(base), str(with_error))
    assert result.exit_code == FAILED
    groups = _groups(result.stdout)
    assert groups[_LABEL_NEWLY_FAILING] == _EXPECTED_NEWLY_FAILING
    assert groups[_LABEL_NEWLY_PASSING] == _EXPECTED_NEWLY_PASSING
    assert groups[_LABEL_STILL_FAILING] == _EXPECTED_STILL_FAILING
    assert _expected_sets(base, with_error) == _documented_sets()
    assert json.loads(_compare(str(base), str(with_error), "--json").stdout) == {
        key: list(value) for key, value in _documented_sets().items()
    }
