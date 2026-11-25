"""Data-driven expansion: a 12-row table and a 3x4 matrix become 12 stable cases (AAP 0.14.1)."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Callable, Sequence
from itertools import product
from pathlib import Path
from typing import Final, TypeAlias

import pytest

from foundry.core.exceptions import DataError
from foundry.data.cases import Case, build_parameters
from foundry.data.matrix import expand_axes
from foundry.data.registry import load_cases, load_matrix

RunPytest: TypeAlias = Callable[..., subprocess.CompletedProcess[str]]
TABULAR_FIXTURES: Final[tuple[str, ...]] = ("cases.csv", "cases.json", "cases.yaml", "cases.jsonl")
CASES_PER_FILE: Final[int] = 12
CASE_IDS: Final[tuple[str, ...]] = tuple(f"cases-{index}" for index in range(CASES_PER_FILE))
_COLUMNS = ("case_id", "currency", "expected_total", "quantity", "tier", "unit_price")
MATRIX_AXES: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    ("currency", ("EUR", "GBP", "USD")),
    ("tier", ("bronze", "silver", "gold", "platinum")),
)
_EXTRA_FIXTURES = ("header_only.csv", "missing_column.csv", "matrix.yaml")
AXIS_SEGMENT: Final[re.Pattern[str]] = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=")
MALFORMED_MARKERS: Final[tuple[str, ...]] = (
    "@pytest.mark.foundry_data()",
    '@pytest.mark.foundry_data("cases.csv", "cases.json")',
    "@pytest.mark.foundry_data(3)",
    '@pytest.mark.foundry_data("cases.csv", strict=True)',
    '@pytest.mark.foundry_data("cases.csv", argname=3)',
)
ACCEPTED_FORM: Final[str] = 'foundry_data("<file>"[, argname="<name>"])'
UNMARKED_MODULE: Final[str] = (
    'import sys\n\n\ndef test_one() -> None:\n    assert "foundry.data" not in sys.modules\n'
)


@pytest.fixture(scope="module")
def fixtures_dir(repo_root: Path) -> Path:
    directory = repo_root / "tests" / "fixtures"
    names = (*TABULAR_FIXTURES, *_EXTRA_FIXTURES)
    assert all((directory / name).is_file() for name in names), f"fixtures missing in {directory}"
    return directory


@pytest.fixture
def data_dir(tmp_path: Path, fixtures_dir: Path) -> Path:
    directory = tmp_path / "testdata"
    directory.mkdir()
    for name in (*TABULAR_FIXTURES, *_EXTRA_FIXTURES):
        shutil.copy(fixtures_dir / name, directory / name)
    return directory


def _ids(cases: Sequence[Case]) -> tuple[str, ...]:
    return tuple(case.id for case in cases)


def _collected(module: str) -> list[str]:
    return [f"{module}::test_case[{case_id}]" for case_id in CASE_IDS]


def _outcomes(project: Path) -> list[str]:
    document = json.loads((project / "results.json").read_text(encoding="utf-8"))
    return [item["outcome"] for item in document if item.get("record_type") == "result"]


_PARAMETRISED_MODULE: Final[str] = (
    "from pathlib import Path\n\nimport pytest\n\nfrom foundry.data.cases import build_parameters\n"
    "from foundry.data.registry import load_cases\n"
    'DATA = Path(__file__).resolve().parent / "testdata"\n'
    'CASES = load_cases(DATA / "{data_name}")\n'
    '\n\n@pytest.mark.parametrize("case", build_parameters(CASES))\n'
    "def test_case(case: object) -> None:\n    assert case is not None\n"
)


def _files(fixtures_dir: Path, data_name: str, module: str) -> dict[str, str]:
    data = {f"testdata/{data_name}": (fixtures_dir / data_name).read_text(encoding="utf-8")}
    return {**data, module: _PARAMETRISED_MODULE.format(data_name=data_name)}


def _run(project: Path, run_pytest: RunPytest, *args: str) -> subprocess.CompletedProcess[str]:
    sinks = [f"--foundry-artifacts.dir={project / 'artifacts'}"]
    sinks.append(f"--foundry-cassette.dir={project / 'cassettes'}")
    sinks.append(f"--foundry-json-report={project / 'results.json'}")
    return run_pytest(*args, "-p", "no:cacheprovider", *sinks, cwd=project, timeout=120.0)


def _clean(project: Path, run_pytest: RunPytest, *args: str) -> subprocess.CompletedProcess[str]:
    result = _run(project, run_pytest, *args)
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    return result


def _nodeids(output: str, module: str) -> list[str]:
    return [line.strip() for line in output.splitlines() if line.startswith(f"{module}::")]


def _marked(marker: str, argument: str = "case") -> str:
    tail = f"def test_case({argument}: object) -> None:\n    assert {argument} is not None\n"
    return f"import pytest\n\n\n{marker}\n{tail}"


@pytest.fixture(scope="module")
def marker_project(fixtures_dir: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    project = tmp_path_factory.mktemp("marker")
    files = {
        "test_cases.py": _marked('@pytest.mark.foundry_data("cases.csv")'),
        "test_rows.py": _marked('@pytest.mark.foundry_data("cases.csv", argname="row")', "row"),
        "test_empty.py": _marked('@pytest.mark.foundry_data("header_only.csv")'),
        "test_plain.py": UNMARKED_MODULE,
    }
    for name in ("cases.csv", "header_only.csv"):
        files[f"testdata/{name}"] = (fixtures_dir / name).read_text(encoding="utf-8")
    for index, marker in enumerate(MALFORMED_MARKERS):
        files[f"bad/test_bad_{index}.py"] = _marked(marker)
    for relative, text in files.items():
        (project / relative).parent.mkdir(parents=True, exist_ok=True)
        (project / relative).write_text(text, encoding="utf-8")
    return project


@pytest.mark.parametrize(
    ("data_name", "first_line"),
    [("cases.csv", 2), ("cases.json", None), ("cases.yaml", None), ("cases.jsonl", 1)],
)
def test_tabular_file_expands_to_twelve_located_cases(
    data_dir: Path, clean_foundry_env: None, data_name: str, first_line: int | None
) -> None:
    path = data_dir / data_name
    case_set = load_cases(path)
    assert len(case_set) == len(case_set.cases) == CASES_PER_FILE  # AAP 0.10.1: a contract.
    assert [(case.id, case.row_index, case.line, case.source_file) for case in case_set.cases] == [
        (case_id, index, None if first_line is None else first_line + index, path)
        for index, case_id in enumerate(CASE_IDS)
    ]
    parameter_ids = [getattr(parameter, "id", None) for parameter in build_parameters(case_set)]
    assert parameter_ids == list(CASE_IDS) and sorted(case_set.columns) == sorted(_COLUMNS)
    reference = {
        case.id: dict(case.values) for case in load_cases(data_dir / TABULAR_FIXTURES[0]).cases
    }
    assert {case.id: dict(case.values) for case in case_set.cases} == reference


def test_matrix_expands_to_twelve_axis_ordered_combinations(
    data_dir: Path, clean_foundry_env: None
) -> None:
    path = data_dir / "matrix.yaml"
    matrix = load_matrix(path)
    assert len(matrix.cases) == CASES_PER_FILE  # three axes by four: twelve (AAP 0.10.1).
    ids = _ids(matrix.cases)
    axis_names = tuple(name for name, _ in MATRIX_AXES)
    assert matrix.axes == MATRIX_AXES  # AAP 0.7.2: axes sorted by name.
    for case in matrix.cases:
        assert case.id.startswith("matrix-") and tuple(AXIS_SEGMENT.findall(case.id)) == axis_names
        assert tuple(sorted(case.values)) == axis_names
        assert (case.line, case.source_file) == (None, path)
    assert ids[0] == 'matrix-currency="EUR"-tier="bronze"'
    assert ids[-1] == 'matrix-currency="USD"-tier="platinum"'
    combinations = product(*(values for _, values in MATRIX_AXES))
    assert [dict(case.values) for case in matrix.cases] == [
        dict(zip(axis_names, combination, strict=True)) for combination in combinations
    ]
    assert _ids(load_matrix(path).cases) == ids
    assert _ids(expand_axes(source_file=path, axes=dict(reversed(MATRIX_AXES))).cases) == ids


def test_collection_order_is_identical_across_two_runs(
    write_project: Callable[..., Path], run_pytest: RunPytest, fixtures_dir: Path, tmp_path: Path
) -> None:
    module = "test_cases.py"
    project = write_project(tmp_path / "project", files=_files(fixtures_dir, "cases.csv", module))
    first = _clean(project, run_pytest, "--collect-only", "-q")
    second = _clean(project, run_pytest, "--collect-only", "-q")
    expected = sorted(_collected(module))  # two runs, one id set and order: xdist's parity.
    assert [sorted(_nodeids(run.stdout, module)) for run in (first, second)] == [expected, expected]


def test_missing_column_fails_collection_naming_file_line_and_column(
    write_project: Callable[..., Path],
    run_pytest: RunPytest,
    fixtures_dir: Path,
    tmp_path: Path,
    clean_foundry_env: None,
) -> None:
    module = "test_rows.py"
    files = _files(fixtures_dir, "missing_column.csv", module)
    project = write_project(tmp_path / "project", files=files)
    data_file = project / "testdata" / "missing_column.csv"
    with pytest.raises(DataError) as excinfo:
        load_cases(data_file)
    located = (excinfo.value.file, excinfo.value.line, excinfo.value.column)
    assert located == (data_file, 3, "expected_total")
    result = _run(project, run_pytest, "--collect-only", "-q")
    combined = f"{result.stdout}\n{result.stderr}"
    assert result.returncode == 2, combined
    assert f"{data_file.resolve()}:3:" in combined
    assert "(column 'expected_total')" in combined and _nodeids(result.stdout, module) == []


def test_the_marker_collects_one_item_per_case_and_passes_strict_markers(
    marker_project: Path, run_pytest: RunPytest
) -> None:
    collected = _clean(marker_project, run_pytest, "--collect-only", "-q", "test_cases.py")
    assert _nodeids(collected.stdout, "test_cases.py") == _collected("test_cases.py")
    result = _clean(marker_project, run_pytest, "-q", "--strict-markers", "test_cases.py")
    assert f"{CASES_PER_FILE} passed" in result.stdout
    assert _outcomes(marker_project) == ["passed"] * CASES_PER_FILE
    assert f"@pytest.mark.{ACCEPTED_FORM}" in _clean(marker_project, run_pytest, "--markers").stdout
    empty = _clean(marker_project, run_pytest, "-q", "-rs", "test_empty.py")
    resolved = (marker_project / "testdata" / "header_only.csv").resolve()
    assert f"no cases in {resolved}" in empty.stdout and "1 skipped" in empty.stdout


def test_the_argname_keyword_binds_a_differently_named_argument(
    marker_project: Path, run_pytest: RunPytest
) -> None:
    result = _clean(marker_project, run_pytest, "-q", "test_rows.py")
    assert f"{CASES_PER_FILE} passed" in result.stdout
    collected = _clean(marker_project, run_pytest, "--collect-only", "-q", "test_rows.py")
    assert _nodeids(collected.stdout, "test_rows.py") == _collected("test_rows.py")


def test_a_malformed_marker_is_a_usage_error_naming_the_accepted_form(
    marker_project: Path, run_pytest: RunPytest
) -> None:
    result = _run(marker_project, run_pytest, "-q", "bad")
    combined = f"{result.stdout}\n{result.stderr}"
    assert result.returncode == 2, combined
    assert combined.count("UsageError: the data marker takes") == len(MALFORMED_MARKERS)
    assert combined.count(ACCEPTED_FORM) >= len(MALFORMED_MARKERS)


def test_an_unmarked_run_never_imports_the_data_engine(
    marker_project: Path, run_pytest: RunPytest
) -> None:
    assert "1 passed" in _clean(marker_project, run_pytest, "-q", "test_plain.py").stdout
