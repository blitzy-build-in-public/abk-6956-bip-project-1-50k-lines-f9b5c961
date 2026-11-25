"""The data-collection states of AAP 0.14.1 criterion 5 through ``pytester``: an unsupported
extension, a short row located by file, line and column, and a header-only skip.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Final

import pytest

REACHES: tuple[str, ...] = (
    "fixture:foundry_data",
    "loader:csv",
    "loader:json",
    "loader:yaml",
    "loader:jsonl",
)

_LOADERS: Final[tuple[str, ...]] = ("cases.csv", "cases.json", "cases.jsonl", "cases.yaml")
_CASES: Final[int] = 12
_UNSUPPORTED: Final[str] = "unsupported_extension.txt"
_MISSING_COL: Final[str] = "missing_column.csv"
_EMPTY: Final[str] = "header_only.csv"
_SUPPORTED_LINE: Final[str] = "supported extensions: csv, json, jsonl, yaml, yml"

_COLLECTING_CONFTEST: Final[str] = """import pathlib

import foundry.data


def pytest_generate_tests(metafunc):
    name = getattr(metafunc.module, "DATA_FILE", None)
    if name is not None:
        path = pathlib.Path(__file__).with_name(name)
        foundry.data.parametrize(metafunc, foundry.data.load_cases(path))
"""

_USING_FIXTURE: Final[str] = """def test_the_fixture_reads_the_records(foundry_data) -> None:
    cases = foundry_data.cases("cases.csv")
    assert [case.id for case in cases] == [f"cases-{i}" for i in range(12)]
    assert [case.row_index for case in cases] == list(range(12))
    combinations = foundry_data.matrix("matrix.yaml")
    assert len(combinations) == 12
    assert len({combination.id for combination in combinations}) == 12
"""


def _case_module(data_file: str) -> str:
    return f"DATA_FILE = {data_file!r}\n\n\ndef test_case(case) -> None:\n    assert case.id\n"


def _loader_modules() -> dict[str, str]:
    return {f"test_{Path(record).suffix[1:]}.py": _case_module(record) for record in _LOADERS}


def _build_project(pytester: pytest.Pytester, repo_root: Path, modules: Mapping[str, str]) -> Path:
    # Copied rather than referenced, so no inner run can write back into the repository's fixtures.
    shutil.copytree(repo_root / "tests" / "fixtures", pytester.path, dirs_exist_ok=True)
    pytester.makeconftest(_COLLECTING_CONFTEST)
    for name, content in modules.items():
        (pytester.path / name).write_text(content, encoding="utf-8", newline="\n")
    return pytester.path


def _elements(project: Path) -> list[Mapping[str, object]]:
    document = project / "results.json"
    if not document.is_file():
        return []
    elements: object = json.loads(document.read_text(encoding="utf-8"))
    assert isinstance(elements, list), f"{document} does not hold a JSON array"
    return [item for item in elements if isinstance(item, dict)]


def _outcomes(project: Path) -> list[str]:
    results = [item for item in _elements(project) if item.get("record_type") == "result"]
    return sorted(str(item["outcome"]) for item in results)


def test_unsupported_extension_errors_then_continues_past_the_error(
    pytester: pytest.Pytester, repo_root: Path, clean_foundry_env: None
) -> None:
    modules = {"test_unsupported.py": _case_module(_UNSUPPORTED)}
    modules["test_valid.py"] = _case_module(_LOADERS[0])
    _build_project(pytester, repo_root, modules)
    result = pytester.runpytest("-q")
    assert result.ret == 2
    output = result.stdout.str()
    assert _UNSUPPORTED in output and "'txt'" in output and "DataError" in output
    assert _SUPPORTED_LINE in output, output
    assert "passed" not in _outcomes(pytester.path)
    continued = pytester.runpytest("-q", "--continue-on-collection-errors")
    assert continued.ret == 1
    continued.assert_outcomes(passed=_CASES, errors=1)
    assert _UNSUPPORTED in continued.stdout.str()
    assert _outcomes(pytester.path) == ["passed"] * _CASES


def test_missing_column_reports_file_line_and_column(
    pytester: pytest.Pytester, repo_root: Path, clean_foundry_env: None
) -> None:
    _build_project(pytester, repo_root, {"test_missing.py": _case_module(_MISSING_COL)})
    result = pytester.runpytest("-q")
    assert result.ret == 2
    output = result.stdout.str()
    assert f"{_MISSING_COL}:3:" in output
    assert "(column 'expected_total')" in output
    assert "row 1 has 5 values but the header declares 6 columns" in output


def test_header_only_file_skips_with_reason(
    pytester: pytest.Pytester, repo_root: Path, clean_foundry_env: None
) -> None:
    _build_project(pytester, repo_root, {"test_empty.py": _case_module(_EMPTY)})
    result = pytester.runpytest("-q", "-rs")
    assert result.ret == 0
    result.assert_outcomes(skipped=1)
    reasons = [line for line in result.stdout.str().splitlines() if "no cases in " in line]
    assert len(reasons) == 1 and reasons[0].rstrip().endswith(_EMPTY)
    assert _outcomes(pytester.path) == ["skipped"]


def test_four_loaders_agree_and_collection_is_deterministic(
    pytester: pytest.Pytester, repo_root: Path, clean_foundry_env: None
) -> None:
    modules = _loader_modules()
    _build_project(pytester, repo_root, modules)
    result = pytester.runpytest("-q")
    assert result.ret == 0
    result.assert_outcomes(passed=_CASES * len(_LOADERS))
    groups: dict[str, list[str]] = {}
    for item in _elements(pytester.path):
        module, _, rest = str(item["nodeid"]).partition("::")
        groups.setdefault(module, []).append(rest.rpartition("[")[2].rstrip("]"))
    assert sorted(groups) == sorted(modules)
    expected = sorted(f"cases-{index}" for index in range(_CASES))
    assert all(sorted(ids) == expected for ids in groups.values())
    runs = (pytester.runpytest("-q", "--collect-only"), pytester.runpytest("-q", "--collect-only"))
    first, second = (tuple(x.strip() for x in run.stdout.lines if "::" in x) for run in runs)
    assert len(first) == _CASES * len(_LOADERS)
    # Position for position, not merely as sets: collection emits the cases in one settled order.
    assert first == second


def test_foundry_data_fixture_reads_committed_files(
    pytester: pytest.Pytester, repo_root: Path, clean_foundry_env: None
) -> None:
    _build_project(pytester, repo_root, {"test_fixture_data.py": _USING_FIXTURE})
    result = pytester.runpytest("-q", f"--foundry-data.dir={pytester.path}")
    assert result.ret == 0
    result.assert_outcomes(passed=1)
