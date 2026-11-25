"""Unit tests for ``foundry.data``: the four loaders, the case model and the schema gate."""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Final

import pytest

from foundry import data
from foundry.core.exceptions import ContractError, DataError, FoundryError
from foundry.core.ids import case_id_for_row

TABULAR_FILES: Final[tuple[str, ...]] = ("cases.csv", "cases.json", "cases.yaml", "cases.jsonl")
ROWS_PER_TABLE: Final[int] = 12
DOCUMENTED_EXTENSIONS: Final[tuple[str, ...]] = ("csv", "json", "jsonl", "yaml", "yml")
DOCUMENTED_COLUMN_NAMES: Final[str] = "case_id currency tier quantity unit_price expected_total"
DOCUMENTED_COLUMNS: Final[frozenset[str]] = frozenset(DOCUMENTED_COLUMN_NAMES.split())
SCHEMA_FIXTURE: Final[str] = "case_row.schema.json"
CASE_IDS: Final[list[str]] = [f"cases-{index}" for index in range(ROWS_PER_TABLE)]
_CSV_HEADER: Final[str] = "case_id,currency,tier,quantity,unit_price,expected_total"
_SHORT_HEADER: Final[str] = "case_id,currency,quantity,unit_price,expected_total"
_VALID_ROW: Final[str] = "C-001,EUR,bronze,2,10.00,20.00"
_ROW_VALUES = dict(zip(_CSV_HEADER.split(","), _VALID_ROW.split(","), strict=True))


@pytest.fixture(scope="module")
def fixtures_dir(repo_root: Path) -> Path:
    directory = repo_root / "tests" / "fixtures"
    expected = (*TABULAR_FILES, "matrix.yaml", "header_only.csv", SCHEMA_FIXTURE)
    assert all((directory / name).is_file() for name in expected), directory
    return directory


@pytest.fixture(scope="module")
def cases_by_format(fixtures_dir: Path) -> Mapping[str, data.CaseSet]:
    return {name: data.load_cases(fixtures_dir / name) for name in TABULAR_FILES}


@dataclasses.dataclass
class _Recorder:
    argnames: list[str] = dataclasses.field(default_factory=list)
    parameters: list[object] = dataclasses.field(default_factory=list)

    def parametrize(self, argname: str, parameters: Sequence[object]) -> None:
        self.argnames.append(argname)
        self.parameters.extend(parameters)


def _table(tmp_path: Path, name: str, body: str, schema: Path) -> Path:
    (path := tmp_path / f"{name}.csv").write_text(body, encoding="utf-8")
    sidecar = tmp_path / (path.name + ".schema.json")
    sidecar.write_text(schema.read_text(encoding="utf-8"), encoding="utf-8")
    return path


def test_the_four_loaders_yield_the_same_rows_under_unique_ids(
    cases_by_format: Mapping[str, data.CaseSet], fixtures_dir: Path
) -> None:
    reference = cases_by_format["cases.csv"].cases
    for name, case_set in cases_by_format.items():
        ids = [case.id for case in case_set.cases]
        assert (len(case_set), len(ids), len(set(ids))) == (ROWS_PER_TABLE,) * 3, name
        assert frozenset(case_set.columns) == DOCUMENTED_COLUMNS, name
        assert case_set.source_file == fixtures_dir / name
        for expected, actual in zip(reference, case_set.cases, strict=True):
            assert (actual.id, actual.row_index) == (expected.id, expected.row_index), name
            assert dict(actual.values) == dict(expected.values), (name, actual.id)
    assert len({tuple(sorted(c.id for c in s.cases)) for s in cases_by_format.values()}) == 1


@pytest.mark.parametrize(
    ("name", "read"),
    [(f"cases.{suffix}", data.load_cases) for suffix in ("csv", "json", "jsonl", "yaml")]
    + [("matrix.yaml", data.load_matrix)],
)
def test_a_string_path_loads_what_a_path_loads(
    name: str, read: Callable[[str | Path], data.CaseSet | data.Matrix], fixtures_dir: Path
) -> None:
    from_path, from_string = read(fixtures_dir / name), read(str(fixtures_dir / name))
    assert from_string == from_path and [c.id for c in from_string.cases] == [
        c.id for c in from_path.cases
    ]


@pytest.mark.parametrize(
    ("body", "line"),
    [
        pytest.param(
            "orders:\n  - id: 1\n    amount: sentinel-value\n   discount: 5\n", 4, id="marked"
        ),
        pytest.param(
            "orders:\n  - id: 1\n\x00 amount: sentinel-value\n", None, id="control-character"
        ),
    ],
)
def test_a_yaml_fault_locates_itself_without_quoting_the_document(
    body: str, line: int | None, tmp_path: Path
) -> None:
    path = tmp_path / "orders.yaml"
    path.write_bytes(body.encode("utf-8"))
    with pytest.raises(DataError) as excinfo:
        data.load_document(path)
    error, message = excinfo.value, str(excinfo.value)
    assert (error.file, error.line) == (path, line)
    assert message.startswith(f"{path}:") and len(message.splitlines()) == 1
    assert "sentinel-value" not in message and "<unicode string>" not in message
    assert message.split("invalid YAML", 1)[1].strip(": ")


def test_a_case_carries_its_location_its_values_and_cannot_be_mutated(fixtures_dir: Path) -> None:
    case = data.load_cases(fixtures_dir / "cases.csv").cases[0]
    assert isinstance(case, data.Case)
    assert (case.id, case.name, case.row_index) == ("cases-0", "cases-0", 0)
    assert case.source_file == fixtures_dir / "cases.csv"
    assert dict(case.values) == dict(_ROW_VALUES)
    with pytest.raises(dataclasses.FrozenInstanceError):
        case.id = "renamed"
    with pytest.raises(TypeError):
        case.values["case_id"] = "renamed"


def test_a_row_reports_its_physical_line_or_none_for_a_parsed_document(fixtures_dir: Path) -> None:
    for name, offset, read in (
        ("cases.csv", 2, lambda line: line.split(",")[0]),
        ("cases.jsonl", 1, lambda line: json.loads(line)["case_id"]),
    ):
        physical = (fixtures_dir / name).read_text(encoding="utf-8").splitlines()
        assert len(physical) == ROWS_PER_TABLE + offset - 1  # one header line for the CSV
        for case in data.load_cases(fixtures_dir / name).cases:
            assert case.line == case.row_index + offset
            assert read(physical[case.line - 1]) == case.values["case_id"]
    for name in ("cases.json", "cases.yaml"):
        assert {case.line for case in data.load_cases(fixtures_dir / name).cases} == {None}


def test_a_case_id_is_the_stem_the_row_index_and_the_canonical_json_axes() -> None:
    assert case_id_for_row("cases", 7) == data.tabular_case_id("cases", 7) == "cases-7"
    assert data.case_stem(Path("/data/cart.csv")) == "cart"
    assert data.case_stem(Path("/data/cart.pricing.csv")) == "cart.pricing"
    forward = data.matrix_case_id("m", {"alpha": 1, "beta": "x"})
    assert forward == data.matrix_case_id("m", {"beta": "x", "alpha": 1}) == 'm-alpha=1-beta="x"'
    for value, rendered in ((1, "m-a=1"), ("1", 'm-a="1"'), (True, "m-a=true"), ("b", 'm-a="b"')):
        assert data.matrix_case_id("m", {"a": value}) == rendered
    for stem, row_index in (("", 0), ("   ", 0), ("cases", -1)):
        with pytest.raises(FoundryError):
            case_id_for_row(stem, row_index)


@pytest.mark.parametrize(
    ("body", "line", "column"),
    [
        (f"{_CSV_HEADER}\nC-001,CHF,bronze,2,10.00,20.00\n", 2, "currency"),
        (f"{_SHORT_HEADER}\nC-001,EUR,2,10.00,20.00\n", 2, "tier"),
    ],
)
def test_a_violating_row_is_reported_at_its_file_line_and_column(
    body: str, line: int, column: str, tmp_path: Path, fixtures_dir: Path
) -> None:
    data_path = _table(tmp_path, "bad", body, fixtures_dir / SCHEMA_FIXTURE)
    with pytest.raises(DataError) as excinfo:
        data.load_cases(data_path)
    error = excinfo.value
    assert (error.file, error.line, error.column) == (data_path, line, column)
    assert f"{data_path}:{line}:" in str(error) and column in str(error)


def test_the_sidecar_schema_is_found_beside_the_data_and_never_outside_it(
    tmp_path: Path, fixtures_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    schema = fixtures_dir / SCHEMA_FIXTURE
    for name in ("inside", "outside", "malf"):
        (tmp_path / name).mkdir()
    monkeypatch.chdir(tmp_path)
    name = _table(tmp_path / "inside", "relative", f"{_CSV_HEADER}\n{_VALID_ROW}\n", schema).name
    resolved = data.load_row_schema(Path("inside") / name, base_dir=Path("inside"))
    assert resolved is not None and resolved["title"] == "Foundry example data row"
    properties = resolved["properties"]
    assert isinstance(properties, Mapping) and frozenset(properties) == DOCUMENTED_COLUMNS
    case_set = data.load_cases(Path("inside") / name, schema_dir=Path("inside"))
    assert [dict(case.values) for case in case_set.cases] == [dict(_ROW_VALUES)]
    escaped = _table(tmp_path / "outside", "escaped", f"{_CSV_HEADER}\n{_VALID_ROW}\n", schema)
    with pytest.raises(ContractError) as excinfo:
        data.load_row_schema(Path("outside") / escaped.name, base_dir=Path("inside"))
    assert "outside the schema directory" in str(excinfo.value)
    assert excinfo.value.json_path == "$"
    (tmp_path / "malf" / "malf.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    sidecar = tmp_path / "malf" / "malf.schema.json"
    sidecar.write_text('{"type": 42}', encoding="utf-8")
    with pytest.raises(ContractError) as malformed:
        data.load_cases(tmp_path / "malf" / "malf.csv")
    assert str(sidecar) in str(malformed.value)
    assert "42 is not valid under any of the given schemas" in str(malformed.value)
    assert str(sidecar) not in malformed.value.detail


def test_strict_mode_rejects_an_undeclared_column_and_tolerant_mode_drops_it(
    tmp_path: Path, fixtures_dir: Path
) -> None:
    body = f"{_CSV_HEADER},note\n{_VALID_ROW},hello\n"
    schema = fixtures_dir / SCHEMA_FIXTURE
    with pytest.raises(DataError) as excinfo:
        data.load_cases(_table(tmp_path, "strict", body, schema), strict=True)
    assert (excinfo.value.line, excinfo.value.column) == (2, "note")
    tolerated = data.load_cases(_table(tmp_path, "tolerant", body, schema), strict=False)
    assert len(tolerated) == 1 and "note" not in tolerated.cases[0].values
    assert dict(tolerated.cases[0].values) == dict(_ROW_VALUES)


def test_the_registry_serves_the_five_documented_extensions() -> None:
    assert DOCUMENTED_EXTENSIONS == data.SUPPORTED_EXTENSIONS == data.supported_extensions()
    for extension in DOCUMENTED_EXTENSIONS:
        assert extension in data.loader_for(Path(f"data.{extension}")).extensions
    assert data.loader_for(Path("data/CART.CSV")) is data.loader_for(Path("data/cart.csv"))


def test_a_header_only_file_yields_no_case_and_the_documented_skip(fixtures_dir: Path) -> None:
    case_set = data.load_cases(path := fixtures_dir / "header_only.csv")
    assert case_set.is_empty and len(case_set) == 0 and case_set.cases == ()
    assert frozenset(case_set.columns) == DOCUMENTED_COLUMNS
    (parameter,) = data.build_parameters(case_set)
    assert parameter.id == "header_only-no-cases" and tuple(parameter.values) == (None,)
    (mark,) = parameter.marks
    assert mark.name == "skip" and mark.kwargs["reason"] == f"no cases in {path}"


def test_both_binding_entry_points_take_the_documented_argument(fixtures_dir: Path) -> None:
    skipped = _Recorder()
    data.parametrize(skipped, data.load_cases(fixtures_dir / "header_only.csv"))
    assert skipped.argnames == ["case"]
    assert [parameter.id for parameter in skipped.parameters] == ["header_only-no-cases"]
    rows = _Recorder()
    data.parametrize(rows, data.load_cases(fixtures_dir / "cases.csv"), argname="row")
    assert rows.argnames == ["row"]
    assert [parameter.id for parameter in rows.parameters] == CASE_IDS
    for kwargs, argname in (({}, "case"), ({"argname": "row"}, "row")):
        recorder = _Recorder()
        marker = _data_marker("cases.csv", **kwargs)
        data.generate_cases(recorder, marker=marker, data_dir=fixtures_dir, strict=True)
        assert recorder.argnames == [argname]
        assert [parameter.id for parameter in recorder.parameters] == CASE_IDS


def _data_marker(*args: object, **kwargs: object) -> pytest.Mark:
    return pytest.mark.foundry_data(*args, **kwargs).mark


@pytest.mark.parametrize(
    "marker",
    [
        pytest.param(_data_marker(), id="no-positional"),
        pytest.param(_data_marker("cases.csv", "cases.json"), id="two-positionals"),
        pytest.param(_data_marker(3), id="non-string-name"),
        pytest.param(_data_marker(""), id="blank-name"),
        pytest.param(_data_marker("cases.csv", strict=True), id="foreign-keyword"),
        pytest.param(_data_marker("cases.csv", argname=3), id="non-string-argname"),
        pytest.param(_data_marker("cases.csv", argname=" "), id="blank-argname"),
    ],
)
def test_a_malformed_data_marker_is_a_usage_error(marker: pytest.Mark, fixtures_dir: Path) -> None:
    with pytest.raises(pytest.UsageError, match="foundry_data"):
        data.generate_cases(_Recorder(), marker=marker, data_dir=fixtures_dir, strict=True)
