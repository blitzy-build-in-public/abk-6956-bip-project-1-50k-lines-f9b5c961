"""Unit tests for the ``foundry.data`` package: the four loaders, the case model, the matrix
expander, the stable case ids, the schema gate and the extension registry.

A data-driven test is only as trustworthy as the collection it was planned from, so this module
pins the properties a regression would break rather than the implementation that provides them
today: the four format readers over one shared data set produce the same cases, a case carries the
location it was read from, a matrix expands to one deterministic cross-product, a case id derives
from the file and a position alone so every shard agrees (implicit requirement I6), a rejected row
is reported with its file, line and column (AAP 0.4.7), the registry serves exactly five suffixes
and lists them when asked for another (AAP 0.3.2), and a file that parses to no data rows becomes
the documented skip rather than a collection error (AAP 0.4.7).

Everything here is driven through the package's public surface -- the ``foundry.data`` barrel and
the two ``foundry.core`` modules the ids delegate to -- because that is what
``foundry.plugin``'s ``pytest_generate_tests`` delegation and the ``foundry_data`` fixture call.
``foundry.reporting`` and ``foundry.cli`` are never imported: ``foundry.data`` may not depend on
them (AAP 0.4.8), and a test that reached them would hide the break.

Four expectations follow the modules on disk rather than the brief this file was first written
against, because the module is authoritative (AAP 0.7.2, AAP 0.4.7):

* A *ragged* row -- one carrying fewer values than the header declares -- is reported with its line
  and no column, because a width fault is about the row rather than about a named column. The
  ``required``-column case is proven against a ``tmp_path`` copy of the row schema, which does name
  the column.
* The JSON and YAML readers document ``line=None`` on every row, since a parsed document has no
  per-element line notion; the case id then comes from ``row_index``. Only CSV and JSON Lines
  report a physical line, and both are asserted against the line that really holds the record.
* The skip an empty data file produces is built by :func:`foundry.data.build_parameters` and bound
  by :func:`foundry.data.parametrize`, so one test pins the parameter and another the binding.
* A matrix axis value is rendered as canonical JSON, which is what keeps ``1`` and ``"1"`` distinct.

Determinism (AAP 0.14.3): the committed fixtures under ``tests/fixtures`` are only ever read, every
file this module writes lives under ``tmp_path``, no wall-clock value is asserted, no sleep is
introduced and no socket is opened.
"""

from __future__ import annotations

import dataclasses
import inspect
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

import pytest

from foundry.core.exceptions import DataError, FoundryError
from foundry.core.ids import case_id_for_row
from foundry.data import (
    SUPPORTED_EXTENSIONS,
    Case,
    CaseSet,
    build_parameters,
    case_stem,
    expand_axes,
    load_cases,
    load_matrix,
    loader_for,
    matrix_case_id,
    parametrize,
    supported_extensions,
    tabular_case_id,
)
from foundry.data.loaders import yaml as yaml_loader

TABULAR_FILES: Final[tuple[str, ...]] = ("cases.csv", "cases.json", "cases.yaml", "cases.jsonl")
"""One data set in all four formats: same twelve records, one file per reader."""

ROWS_PER_TABLE: Final[int] = 12
"""The number of data rows each ``cases.*`` fixture carries -- the fixture's own contract."""

DOCUMENTED_EXTENSIONS: Final[tuple[str, ...]] = ("csv", "json", "jsonl", "yaml", "yml")
"""The suffixes AAP 0.3.2 says the registry serves, sorted the way it reports them."""

DOCUMENTED_COLUMNS: Final[frozenset[str]] = frozenset(
    {"case_id", "currency", "tier", "quantity", "unit_price", "expected_total"}
)
"""The six columns every ``cases.*`` fixture declares."""

MATRIX_AXES: Final[tuple[tuple[str, tuple[object, ...]], ...]] = (
    ("currency", ("EUR", "GBP", "USD")),
    ("tier", ("bronze", "silver", "gold", "platinum")),
)
"""``matrix.yaml``'s axes, sorted by name and exactly as declared: 3 x 4 -> 12 cases."""

SCHEMA_FIXTURE: Final[str] = "case_row.schema.json"
"""The committed row schema the schema tests copy beside a ``tmp_path`` data file."""

_CSV_HEADER: Final[str] = "case_id,currency,tier,quantity,unit_price,expected_total"
"""The header ``cases.csv`` and the schema fixture agree on."""

_VALID_ROW: Final[str] = "C-001,EUR,bronze,2,10.00,20.00"
"""One row the schema fixture accepts, as the CSV text a test writes."""


# --- Shared fixtures and helpers -------------------------------------------


@pytest.fixture(scope="module")
def fixtures_dir(repo_root: Path) -> Path:
    """Module scope: the committed data fixtures, checked for the files this module reads."""
    directory = repo_root / "tests" / "fixtures"
    expected = (
        *TABULAR_FILES,
        "matrix.yaml",
        "header_only.csv",
        "missing_column.csv",
        "unsupported_extension.txt",
        SCHEMA_FIXTURE,
    )
    missing = [name for name in expected if not (directory / name).is_file()]
    assert not missing, f"data fixtures missing from {directory}: {', '.join(missing)}"
    return directory


@pytest.fixture(scope="module")
def cases_by_format(fixtures_dir: Path) -> Mapping[str, CaseSet]:
    """Module scope: every ``cases.*`` fixture loaded once, keyed by its file name."""
    return {name: load_cases(fixtures_dir / name) for name in TABULAR_FILES}


class _ParametrizeRecorder:
    """A stand-in for ``pytest.Metafunc`` that records the single call ``parametrize`` makes.

    :func:`foundry.data.parametrize` touches its ``metafunc`` in exactly one way --
    ``metafunc.parametrize(argname, parameters)`` -- so a recorder proves both halves of the
    adapter's contract: the argument the cases are bound to, and the parameters themselves.
    """

    def __init__(self) -> None:
        self.argnames: list[str] = []
        self.parameters: list[object] = []

    def parametrize(self, argname: str, parameters: Sequence[object]) -> None:
        """Record one parametrisation call."""
        self.argnames.append(argname)
        self.parameters.extend(parameters)


def _write_table_with_schema(tmp_path: Path, name: str, body: str, schema: Path) -> Path:
    """Write a CSV data file and its sidecar schema under ``tmp_path``, returning the data path.

    The schema fixture is copied, never mutated, and both files are new names under ``tmp_path``,
    so ``tests/fixtures`` is never written to. The sidecar takes the
    ``<data file name>.schema.json`` spelling, which is the one
    :func:`foundry.data.schema_path_for` tries first.
    """
    data_path = tmp_path / f"{name}.csv"
    data_path.write_text(body, encoding="utf-8")
    sidecar = tmp_path / (data_path.name + ".schema.json")
    sidecar.write_text(schema.read_text(encoding="utf-8"), encoding="utf-8")
    return data_path


# --- The four loaders ------------------------------------------------------


@pytest.mark.parametrize("name", TABULAR_FILES)
def test_each_loader_yields_every_data_row(name: str, fixtures_dir: Path) -> None:
    """Each format loads all twelve records, with the six columns the data set declares."""
    case_set = load_cases(fixtures_dir / name)
    assert len(case_set) == ROWS_PER_TABLE
    assert len(case_set.cases) == ROWS_PER_TABLE
    assert frozenset(case_set.columns) == DOCUMENTED_COLUMNS
    assert case_set.source_file == fixtures_dir / name


def test_the_four_loaders_agree_on_ids_and_values(
    cases_by_format: Mapping[str, CaseSet],
) -> None:
    """Every format yields the same sorted id tuple, and the same values case for case.

    Values are compared as dicts rather than as ordered tuples because the readers differ in
    column order by design -- CSV and YAML keep the header's order, JSON and JSON Lines keep the
    document's key order -- and a dropped or reordered *cell* is what this is here to catch.
    """
    id_tuples = {
        name: tuple(sorted(case.id for case in case_set))
        for name, case_set in cases_by_format.items()
    }
    assert len(set(id_tuples.values())) == 1, id_tuples

    reference = cases_by_format["cases.csv"].cases
    for name, case_set in cases_by_format.items():
        assert len(case_set.cases) == len(reference), name
        for expected, actual in zip(reference, case_set.cases, strict=True):
            assert actual.id == expected.id, name
            assert actual.row_index == expected.row_index, name
            assert dict(actual.values) == dict(expected.values), (name, actual.id)


# --- The case model --------------------------------------------------------


def test_a_case_carries_its_location_and_its_values(
    cases_by_format: Mapping[str, CaseSet], fixtures_dir: Path
) -> None:
    """A case publishes the six fields AAP 0.7.2 fixes, with its own file and row position."""
    case = cases_by_format["cases.csv"].cases[0]
    assert isinstance(case, Case)
    assert case.id == "cases-0"
    assert case.name == case.id  # no ``name`` column, so the label falls back to the id
    assert case.source_file == fixtures_dir / "cases.csv"
    assert case.row_index == 0
    assert dict(case.values) == {
        "case_id": "C-001",
        "currency": "EUR",
        "tier": "bronze",
        "quantity": "2",
        "unit_price": "10.00",
        "expected_total": "20.00",
    }


def test_a_case_cannot_be_mutated(cases_by_format: Mapping[str, CaseSet]) -> None:
    """The model is frozen and its ``values`` mapping is read-only, so a planned case stays put."""
    case = cases_by_format["cases.csv"].cases[0]
    with pytest.raises(dataclasses.FrozenInstanceError):
        case.id = "renamed"
    with pytest.raises(TypeError):
        case.values["case_id"] = "renamed"


def test_csv_rows_report_the_physical_line_they_start_on(fixtures_dir: Path) -> None:
    """``line`` is the 1-based file line with the header excluded, and it holds that record."""
    path = fixtures_dir / "cases.csv"
    physical = path.read_text(encoding="utf-8").splitlines()
    assert len(physical) == ROWS_PER_TABLE + 1
    for case in load_cases(path).cases:
        assert case.row_index is not None
        assert case.line == case.row_index + 2  # one header line, and lines count from one
        assert physical[case.line - 1].split(",")[0] == case.values["case_id"]


def test_jsonl_rows_report_the_physical_line_they_start_on(fixtures_dir: Path) -> None:
    """A JSON Lines record has a real line in the file, so that line is what the reader reports."""
    path = fixtures_dir / "cases.jsonl"
    physical = path.read_text(encoding="utf-8").splitlines()
    assert len(physical) == ROWS_PER_TABLE
    for case in load_cases(path).cases:
        assert case.row_index is not None
        assert case.line == case.row_index + 1
        assert json.loads(physical[case.line - 1])["case_id"] == case.values["case_id"]


@pytest.mark.parametrize("name", ["cases.json", "cases.yaml"])
def test_parsed_documents_report_no_line_but_a_row_index(name: str, fixtures_dir: Path) -> None:
    """JSON and YAML document ``line=None``, because a parsed document has no per-row line.

    The readers record that honestly rather than inventing a line, and the case id then comes from
    ``row_index``, which every reader populates (AAP 0.7.2).
    """
    case_set = load_cases(fixtures_dir / name)
    assert [case.row_index for case in case_set.cases] == list(range(ROWS_PER_TABLE))
    assert {case.line for case in case_set.cases} == {None}
    assert {case.source_file for case in case_set.cases} == {fixtures_dir / name}


# --- Matrix expansion ------------------------------------------------------


def test_the_matrix_expands_to_its_cross_product(fixtures_dir: Path) -> None:
    """``matrix.yaml`` expands to one case per combination, in a contiguous, unique order."""
    matrix = load_matrix(fixtures_dir / "matrix.yaml")
    assert matrix.name == "matrix"
    assert matrix.source_file == fixtures_dir / "matrix.yaml"
    assert len(matrix.cases) == 12
    assert len({case.id for case in matrix.cases}) == 12
    assert [case.row_index for case in matrix.cases] == list(range(12))
    assert {case.line for case in matrix.cases} == {None}


def test_the_matrix_axes_are_sorted_by_name(fixtures_dir: Path) -> None:
    """The expansion order is fixed by the axis names, not by how the document wrote them."""
    matrix = load_matrix(fixtures_dir / "matrix.yaml")
    assert matrix.axes == MATRIX_AXES
    names = [name for name, _ in matrix.axes]
    assert names == sorted(names)


def test_every_matrix_case_fixes_one_value_of_every_axis(fixtures_dir: Path) -> None:
    """Each case maps every axis to one declared value, and every value is used at least once."""
    matrix = load_matrix(fixtures_dir / "matrix.yaml")
    axis_names = [name for name, _ in matrix.axes]
    observed: dict[str, set[object]] = {name: set() for name in axis_names}
    for case in matrix.cases:
        assert list(case.values) == axis_names
        for name, value in case.values.items():
            observed[name].add(value)
    assert observed == {name: set(values) for name, values in matrix.axes}
    expected = 1
    for _, values in matrix.axes:
        expected *= len(values)
    assert len(matrix.cases) == expected == 12


def test_the_matrix_expansion_is_repeatable(fixtures_dir: Path) -> None:
    """Two independent expansions of one document agree value for value."""
    first = load_matrix(fixtures_dir / "matrix.yaml")
    second = load_matrix(fixtures_dir / "matrix.yaml")
    assert [case.id for case in first.cases] == [case.id for case in second.cases]
    assert [dict(case.values) for case in first.cases] == [
        dict(case.values) for case in second.cases
    ]


def test_the_matrix_expansion_ignores_the_axis_insertion_order(fixtures_dir: Path) -> None:
    """Rebuilding the same axis mapping in the opposite order yields the same ids and order."""
    path = fixtures_dir / "matrix.yaml"
    axes: dict[str, tuple[object, ...]] = dict(MATRIX_AXES)
    forward = expand_axes(source_file=path, axes=axes)
    reverse = expand_axes(source_file=path, axes=dict(reversed(list(axes.items()))))
    assert forward.axes == reverse.axes
    assert [case.id for case in forward.cases] == [case.id for case in reverse.cases]


def test_the_matrix_reaches_pytest_through_the_same_adapter(fixtures_dir: Path) -> None:
    """A matrix becomes a case set and then parameters, exactly as a table does."""
    case_set = load_matrix(fixtures_dir / "matrix.yaml").to_case_set()
    assert case_set.columns == ("currency", "tier")
    parameters = build_parameters(case_set)
    assert [parameter.id for parameter in parameters] == [case.id for case in case_set.cases]
    assert parameters[0].id == 'matrix-currency="EUR"-tier="bronze"'


# --- Case ids --------------------------------------------------------------


def test_a_row_id_is_the_stem_and_the_row_index() -> None:
    """AAP 0.7.2's ``f"{stem}-{row_index}"``, through both published spellings."""
    assert case_id_for_row("cases", 7) == "cases-7"
    assert tabular_case_id("cases", 7) == "cases-7"
    assert case_stem(Path("/data/cart.csv")) == "cart"
    assert case_stem(Path("/data/cart.pricing.csv")) == "cart.pricing"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1, "m-a=1"),
        ("1", 'm-a="1"'),
        (True, "m-a=true"),
        (1.5, "m-a=1.5"),
        ("bronze", 'm-a="bronze"'),
    ],
)
def test_a_matrix_id_renders_each_value_as_canonical_json(value: object, expected: str) -> None:
    """A number stays unquoted and a string is quoted, so ``1`` and ``"1"`` stay distinct ids."""
    assert matrix_case_id("m", {"a": value}) == expected


def test_a_matrix_id_sorts_the_axes_and_ignores_their_insertion_order() -> None:
    """Axes are joined in name order, so the caller's mapping order cannot perturb an id."""
    forward = matrix_case_id("m", {"alpha": 1, "beta": "x"})
    reverse = matrix_case_id("m", {"beta": "x", "alpha": 1})
    assert forward == reverse == 'm-alpha=1-beta="x"'
    assert matrix_case_id("matrix", {"tier": "bronze", "currency": "EUR"}) == (
        'matrix-currency="EUR"-tier="bronze"'
    )


@pytest.mark.parametrize(("stem", "row_index"), [("", 0), ("   ", 0), ("cases", -1)])
def test_a_malformed_row_id_is_refused(stem: str, row_index: int) -> None:
    """A blank stem or a negative index raises rather than producing a malformed id."""
    with pytest.raises(FoundryError):
        case_id_for_row(stem, row_index)


@pytest.mark.parametrize("name", TABULAR_FILES)
def test_case_ids_are_unique_within_one_loader(name: str, fixtures_dir: Path) -> None:
    ids = [case.id for case in load_cases(fixtures_dir / name).cases]
    assert len(ids) == len(set(ids)) == ROWS_PER_TABLE


# --- Schema enforcement ----------------------------------------------------


def test_a_violating_value_is_reported_at_its_line_and_column(
    tmp_path: Path, fixtures_dir: Path
) -> None:
    """A row the schema rejects names the file, the line and the column it broke (AAP 0.4.5)."""
    data_path = _write_table_with_schema(
        tmp_path,
        "bad_value",
        f"{_CSV_HEADER}\nC-001,CHF,bronze,2,10.00,20.00\n",
        fixtures_dir / SCHEMA_FIXTURE,
    )
    with pytest.raises(DataError) as excinfo:
        load_cases(data_path)
    error = excinfo.value
    assert error.file == data_path
    assert error.line == 2
    assert error.column == "currency"
    message = str(error)
    assert str(data_path) in message
    assert ":2:" in message
    assert "currency" in message


def test_a_row_missing_a_required_column_names_the_column(
    tmp_path: Path, fixtures_dir: Path
) -> None:
    """The ``required`` check reports the column the row lacks, before any value is looked at."""
    data_path = _write_table_with_schema(
        tmp_path,
        "short_columns",
        "case_id,currency,quantity,unit_price,expected_total\nC-001,EUR,2,10.00,20.00\n",
        fixtures_dir / SCHEMA_FIXTURE,
    )
    with pytest.raises(DataError) as excinfo:
        load_cases(data_path)
    assert excinfo.value.file == data_path
    assert excinfo.value.line == 2
    assert excinfo.value.column == "tier"
    assert "tier" in str(excinfo.value)


def test_strict_mode_rejects_an_undeclared_column(tmp_path: Path, fixtures_dir: Path) -> None:
    """``data.schema_strict=True`` makes a column the schema does not declare a fault."""
    data_path = _write_table_with_schema(
        tmp_path,
        "extra_strict",
        f"{_CSV_HEADER},note\n{_VALID_ROW},hello\n",
        fixtures_dir / SCHEMA_FIXTURE,
    )
    with pytest.raises(DataError) as excinfo:
        load_cases(data_path, strict=True)
    assert excinfo.value.line == 2
    assert excinfo.value.column == "note"
    assert "note" in str(excinfo.value)


def test_tolerant_mode_loads_the_same_file_and_drops_the_extra_column(
    tmp_path: Path, fixtures_dir: Path
) -> None:
    """``data.schema_strict=False`` tolerates the extra column and keeps the declared ones."""
    data_path = _write_table_with_schema(
        tmp_path,
        "extra_tolerant",
        f"{_CSV_HEADER},note\n{_VALID_ROW},hello\n",
        fixtures_dir / SCHEMA_FIXTURE,
    )
    case_set = load_cases(data_path, strict=False)
    assert len(case_set) == 1
    assert "note" not in case_set.cases[0].values
    assert dict(case_set.cases[0].values) == {
        "case_id": "C-001",
        "currency": "EUR",
        "tier": "bronze",
        "quantity": "2",
        "unit_price": "10.00",
        "expected_total": "20.00",
    }


def test_a_ragged_row_reports_its_line_and_no_column(fixtures_dir: Path) -> None:
    """A width fault is about the row, so it names the line and leaves ``column`` unset."""
    path = fixtures_dir / "missing_column.csv"
    with pytest.raises(DataError) as excinfo:
        load_cases(path)
    error = excinfo.value
    assert error.file == path
    assert error.line == 3
    assert error.column is None
    assert f"{path}:3:" in str(error)


# --- The extension registry ------------------------------------------------


def test_the_registry_serves_the_five_documented_extensions() -> None:
    """AAP 0.3.2's list, asserted from the constant, the accessor and the lookup table alike."""
    assert DOCUMENTED_EXTENSIONS == SUPPORTED_EXTENSIONS == supported_extensions()
    for extension in DOCUMENTED_EXTENSIONS:
        assert extension in loader_for(Path(f"data.{extension}")).extensions
    assert loader_for(Path("data/CART.CSV")) is loader_for(Path("data/cart.csv"))


def test_an_unsupported_extension_lists_every_supported_extension(fixtures_dir: Path) -> None:
    """The rejection names the suffix it met and every suffix that would have worked."""
    path = fixtures_dir / "unsupported_extension.txt"
    with pytest.raises(DataError) as excinfo:
        loader_for(path)
    error = excinfo.value
    assert error.file == path
    assert error.line is None
    message = str(error)
    assert "'txt'" in message
    for extension in DOCUMENTED_EXTENSIONS:
        assert extension in message


def test_loading_a_file_leaves_the_registry_unchanged(fixtures_dir: Path) -> None:
    """The table is populated at import and reading files never mutates it."""
    before = supported_extensions()
    load_cases(fixtures_dir / "cases.csv")
    load_matrix(fixtures_dir / "matrix.yaml")
    assert supported_extensions() == before
    with pytest.raises(DataError):
        loader_for(fixtures_dir / "unsupported_extension.txt")
    assert supported_extensions() == before


# --- A file with no data rows ----------------------------------------------


def test_a_header_only_file_yields_no_cases(fixtures_dir: Path) -> None:
    """The file parses, keeps its columns and contributes no case and no exception."""
    case_set = load_cases(fixtures_dir / "header_only.csv")
    assert case_set.is_empty
    assert len(case_set) == 0
    assert case_set.cases == ()
    assert frozenset(case_set.columns) == DOCUMENTED_COLUMNS


def test_an_empty_file_becomes_the_documented_skip_parameter(fixtures_dir: Path) -> None:
    """``build_parameters`` returns one skipped parameter whose reason names the file."""
    path = fixtures_dir / "header_only.csv"
    parameters = build_parameters(load_cases(path))
    assert len(parameters) == 1
    parameter = parameters[0]
    assert parameter.id == "header_only-no-cases"
    assert tuple(parameter.values) == (None,)
    (mark,) = parameter.marks
    assert mark.name == "skip"
    assert mark.kwargs["reason"] == f"no cases in {path}"


def test_parametrize_binds_the_skip_parameter_to_the_default_argument(fixtures_dir: Path) -> None:
    """The adapter binds the skip to ``case`` rather than parametrising nothing at all."""
    recorder = _ParametrizeRecorder()
    parametrize(recorder, load_cases(fixtures_dir / "header_only.csv"))
    assert recorder.argnames == ["case"]
    assert [parameter.id for parameter in recorder.parameters] == ["header_only-no-cases"]
    (mark,) = recorder.parameters[0].marks
    assert mark.kwargs["reason"] == f"no cases in {fixtures_dir / 'header_only.csv'}"


def test_parametrize_binds_every_case_to_the_named_argument(fixtures_dir: Path) -> None:
    """A non-empty set binds one parameter per case, in file order, under the caller's name."""
    recorder = _ParametrizeRecorder()
    parametrize(recorder, load_cases(fixtures_dir / "cases.csv"), argname="row")
    assert recorder.argnames == ["row"]
    assert [parameter.id for parameter in recorder.parameters] == [
        f"cases-{index}" for index in range(ROWS_PER_TABLE)
    ]


# --- YAML safety -----------------------------------------------------------


def test_the_yaml_loader_names_safe_load_and_no_other_loader() -> None:
    """The YAML reader reaches PyYAML only through ``yaml.safe_load`` (AAP 0.14.3).

    This is the loader's own contract, checked on the module object so a reader that swapped the
    loader is caught wherever it lives; the tree-wide scan for unchecked deserialisation belongs to
    ``tests/unit/test_no_unsafe_parsing.py``.
    """
    source = inspect.getsource(yaml_loader)
    assert "yaml.safe_load(" in source
    assert "yaml.load(" not in source
    assert "full_load" not in source
    assert "unsafe_load" not in source
