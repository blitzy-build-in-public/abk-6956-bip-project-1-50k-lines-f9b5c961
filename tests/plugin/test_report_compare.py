"""``foundry report compare`` through a real ``main()`` call: the three labelled groups in their
fixed order, the ``--json`` document, exit 1 on a regression and exit 2 on a document that cannot
be read. The committed documents are copied into ``tmp_path`` and are only ever read."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

import pytest
from click.testing import CliRunner, Result

from foundry.cli.exit_codes import FAILED, SUCCESS, USAGE_OR_CONFIG
from foundry.cli.main import main

REACHES: tuple[str, ...] = ("command:report", "format:results.json")

_CART: Final[str] = "tests/test_cart.py::test_two_items"
_EMPTY_CART: Final[str] = "tests/test_cart.py::test_empty_cart"
_OK: Final[str] = "tests/test_checkout.py::test_payment_ok"
_DECLINED: Final[str] = "tests/test_checkout.py::test_payment_declined"
_STOCK: Final[str] = "tests/test_inventory.py::test_stock_level"

_LABELS: Final[tuple[str, ...]] = ("Newly failing", "Newly passing", "Still failing")
_EXPECTED: Final[dict[str, tuple[str, ...]]] = {
    "newly_failing": (_OK,),
    "newly_passing": (_DECLINED,),
    "still_failing": (_CART,),
}
_STANDING: Final[tuple[str, ...]] = (_CART, _DECLINED)
_FILES: Final[dict[str, str]] = {
    "base": "results_base.json",
    "new": "results_new.json",
    "bad": "results_unreadable.txt",
}
_EMPTY: Final[str] = "(none)"
_FAILURE: Final[dict[str, object]] = {
    "message": "AssertionError: rewritten by the test",
    "phase": "call",
    "traceback_path": None,
}
_RUN_ERROR: Final[dict[str, object]] = {
    "record_type": "run_error",
    "run_id": "20240501T130000-4d5e6f",
    "recorded_at": "2024-05-01T13:00:05.000+00:00",
    "worker_id": "gw0",
    "message": "worker gw0 disappeared",
}


class _Entry:
    name: Final[str] = "foundry"

    def main(self, args: Sequence[str] | None = None, **extra: object) -> int:
        del extra
        raise SystemExit(main(list(args or ())))


def _compare(*arguments: str) -> Result:
    # Only COLUMNS is set, so the width rich renders at is fixed rather than the host terminal's.
    return CliRunner().invoke(_Entry(), ["report", "compare", *arguments], env={"COLUMNS": "200"})


def _elements(path: Path) -> list[dict[str, object]]:
    document = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(document, list), f"{path} does not hold a JSON array"
    return document


def _failing(path: Path) -> set[str]:
    results = [item for item in _elements(path) if item.get("record_type") == "result"]
    return {str(item["nodeid"]) for item in results if item["outcome"] in {"failed", "error"}}


def _failing_sets(base: Path, new: Path) -> dict[str, tuple[str, ...]]:
    base_failing, new_failing = _failing(base), _failing(new)
    return {
        "newly_failing": tuple(sorted(new_failing - base_failing)),
        "newly_passing": tuple(sorted(base_failing - new_failing)),
        "still_failing": tuple(sorted(base_failing & new_failing)),
    }


def _rendered(sets: Mapping[str, tuple[str, ...]]) -> list[str]:
    lines: list[str] = []
    for label, values in zip(_LABELS, sets.values(), strict=True):
        lines.append(f"{label} ({len(values)}):")
        lines.extend([f"  {value}" for value in values] or [f"  {_EMPTY}"])
    return lines


def _with_outcomes(source: Path, destination: Path, outcomes: Mapping[str, str]) -> Path:
    elements = _elements(source)
    for element in elements:
        outcome = outcomes.get(str(element.get("nodeid", "")))
        if outcome is not None:
            element["outcome"] = outcome
            element["failures"] = [] if outcome == "passed" else [dict(_FAILURE)]
    destination.write_text(json.dumps(elements, indent=2) + "\n", encoding="utf-8")
    return destination


@pytest.fixture
def copies(repo_root: Path, tmp_path: Path) -> dict[str, Path]:
    for name in _FILES.values():
        (tmp_path / name).write_bytes((repo_root / "tests" / "fixtures" / name).read_bytes())
    return {key: tmp_path / name for key, name in _FILES.items()}


def test_the_groups_render_in_order_and_the_json_document_holds_the_three_sets(
    copies: dict[str, Path],
) -> None:
    base, new = str(copies["base"]), str(copies["new"])
    result = _compare(base, new)
    assert result.exit_code == FAILED and result.stderr == ""
    assert result.stdout.splitlines() == _rendered(_EXPECTED)
    assert _failing_sets(Path(base), Path(new)) == _EXPECTED
    documented = _compare(base, new, "--json")
    assert documented.exit_code == FAILED and documented.stderr == ""
    document = json.loads(documented.stdout)
    assert tuple(document) == tuple(_EXPECTED)
    assert document == {key: list(value) for key, value in _EXPECTED.items()}


def test_a_group_with_several_nodeids_lists_them_ascending(
    copies: dict[str, Path], tmp_path: Path
) -> None:
    regressed = _with_outcomes(
        copies["new"], tmp_path / "regressed.json", {_EMPTY_CART: "failed", _STOCK: "failed"}
    )
    document = json.loads(_compare(str(copies["new"]), str(regressed), "--json").stdout)
    assert document["newly_failing"] == [_EMPTY_CART, _STOCK]
    assert document["still_failing"] == [_CART, _OK]


def test_identical_documents_exit_0_and_list_the_standing_failures(
    copies: dict[str, Path],
) -> None:
    base = str(copies["base"])
    result = _compare(base, base)
    assert result.exit_code == SUCCESS
    expected = {"newly_failing": (), "newly_passing": (), "still_failing": _STANDING}
    assert result.stdout.splitlines() == _rendered(expected)
    assert json.loads(_compare(base, base, "--json").stdout) == {
        "newly_failing": [],
        "newly_passing": [],
        "still_failing": list(_STANDING),
    }


@pytest.mark.parametrize("position", ["base", "new"])
def test_unreadable_document_exits_2(copies: dict[str, Path], position: str) -> None:
    bad = copies["bad"]
    arguments = (
        (str(bad), str(copies["new"])) if position == "base" else (str(copies["base"]), str(bad))
    )
    result = _compare(*arguments)
    assert result.exit_code == USAGE_OR_CONFIG and result.stdout == ""
    assert str(bad) in result.stderr and "Traceback" not in result.stderr


def test_run_error_elements_are_ignored(copies: dict[str, Path], tmp_path: Path) -> None:
    with_error = tmp_path / "with_error.json"
    elements = [*_elements(copies["new"]), dict(_RUN_ERROR)]
    with_error.write_text(json.dumps(elements, indent=2) + "\n", encoding="utf-8")
    result = _compare(str(copies["base"]), str(with_error))
    assert result.exit_code == FAILED and result.stdout.splitlines() == _rendered(_EXPECTED)
    expected = {key: list(value) for key, value in _EXPECTED.items()}
    assert json.loads(_compare(str(copies["base"]), str(with_error), "--json").stdout) == expected
