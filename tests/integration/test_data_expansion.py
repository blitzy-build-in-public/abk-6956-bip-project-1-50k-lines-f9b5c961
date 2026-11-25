"""End-to-end evidence for data-driven expansion and for collection-order stability.

AAP 0.10.1 gives this module one row -- "CSV and matrix expansion to 12 stable cases each" -- and it
is the evidence for AAP 0.14.1 criterion 5. It carries a second job that implicit requirement I6
forces: the ids a data file expands to, and the order they are collected in, must be identical in
every process, because ``pytest-xdist`` aborts a sharded run whose workers collect different node
ids.

Everything here is asserted at the level a user observes it -- the case list a loader returns, the
ids pytest renders, the node ids a real ``--collect-only`` run prints, the skip reason an empty file
produces and the text of a collection error -- rather than at the level of a loader's internals.
Per-loader unit detail lives in ``tests/unit/test_data.py`` and the same error states driven through
``pytester`` in ``tests/plugin/test_data_collection_errors.py``, so neither is repeated here.

**The collection seam.** ``foundry.plugin.pytest_generate_tests`` owns it: a test marked
``@pytest.mark.foundry_data("<file>"[, argname="<name>"])`` is parametrised over that file's cases
by the plugin alone, the name resolved against ``data.dir``, one parameter per case and each
carrying the case's own id. A file with no data rows is one skipped item reading
``no cases in <file>``; a ``DataError`` from the loader is a collection error naming the file, the
line and the column (AAP 0.3.3, AAP 0.4.7, AAP 0.7.1). A project that builds its parameters at
import keeps the public expansion API (``load_cases`` -> ``CaseSet`` -> ``build_parameters`` ->
``pytest.mark.parametrize``), as ``examples/checkout/test_contracts.py`` does. Both routes are
driven below through a real ``pytest --collect-only`` run, which is what makes criterion 5's
expansion and I6's stability evidence from a real entry point.

Determinism (AAP 0.14.3): the committed fixtures under ``tests/fixtures`` are only ever read, every
file this module writes lives under ``tmp_path``, no wall-clock value is asserted and every
comparison of a collection is a sorted one.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Callable, Sequence
from itertools import product
from pathlib import Path
from typing import Final, Protocol, TypeAlias, cast

import pytest

from foundry.core.exceptions import DataError
from foundry.data.cases import Case, build_parameters
from foundry.data.matrix import expand_axes
from foundry.data.registry import (
    SUPPORTED_EXTENSIONS,
    load_cases,
    load_matrix,
    loader_for,
    supported_extensions,
)

RunPytest: TypeAlias = Callable[..., subprocess.CompletedProcess[str]]
"""The shape of the ``run_pytest`` fixture: ``python -m pytest`` in a child, output captured."""


class _Parameter(Protocol):
    """The one member of the object ``pytest.param`` returns that this module reads.

    pytest publishes no public class for it, which is why ``build_parameters`` is annotated
    ``list[object]``; the adapter's own contract is that each parameter's ``id`` is the case id.
    """

    id: str | None


TABULAR_FIXTURES: Final[tuple[str, ...]] = ("cases.csv", "cases.json", "cases.yaml", "cases.jsonl")
"""One data set in all four formats, sharing the stem ``cases`` and therefore its case ids."""

CASES_PER_FILE: Final[int] = 12
"""The row count each ``cases.*`` fixture carries and the case count ``matrix.yaml`` expands to.

AAP 0.10.1 states both counts as contracts: a reader that dropped or duplicated a row would make a
sharded run plan a different suite, so a count that drifts is a failure rather than a fixture edit.
"""

DOCUMENTED_COLUMNS: Final[tuple[str, ...]] = (
    "case_id",
    "currency",
    "expected_total",
    "quantity",
    "tier",
    "unit_price",
)
"""The six columns every ``cases.*`` fixture declares, sorted: the data set the four share."""

MATRIX_AXES: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    ("currency", ("EUR", "GBP", "USD")),
    ("tier", ("bronze", "silver", "gold", "platinum")),
)
"""``matrix.yaml``'s axes as the document declares them, sorted by name: 3 x 4 -> 12 cases."""

MATRIX_STEM: Final[str] = "matrix"
"""The stem every id in ``matrix.yaml`` starts with (AAP 0.7.2)."""

TABULAR_ID: Final[re.Pattern[str]] = re.compile(r"cases-\d+")
"""The id format AAP 0.7.2 fixes for a table row: the file's stem, a hyphen and the row index."""

AXIS_SEGMENT: Final[re.Pattern[str]] = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=")
"""One ``axis=`` segment of a matrix case id, which is what the axis order is read back from."""

NO_CASES_EXTRA_FILES: Final[tuple[str, ...]] = (
    "header_only.csv",
    "missing_column.csv",
    "unsupported_extension.txt",
    "matrix.yaml",
)
"""The fixtures beside the four ``cases.*`` files that the data-directory fixture also copies."""

_ISOLATION_FLAGS: Final[tuple[str, ...]] = (
    "--foundry-artifacts.dir",
    "artifacts",
    "--foundry-cassette.dir",
    "cassettes",
    "--foundry-json-report",
    "results.json",
)
"""The options that keep every report, artifact and cassette a child run writes inside its project.

AAP 0.14.3 makes the repository root off limits for a test's output: the child's current directory
is a ``tmp_path`` project, and these three options place each sink relative to it explicitly rather
than relying on a default.
"""

_SUBPROCESS_TIMEOUT: Final[float] = 120.0
"""A bound on one child run, so a hung collection fails here instead of stalling the suite."""


# --- Shared fixtures -------------------------------------------------------


@pytest.fixture(scope="module")
def fixtures_dir(repo_root: Path) -> Path:
    """Module scope: the committed data fixtures, checked for every file this module reads."""
    directory = repo_root / "tests" / "fixtures"
    expected = (*TABULAR_FIXTURES, *NO_CASES_EXTRA_FILES)
    missing = [name for name in expected if not (directory / name).is_file()]
    assert not missing, f"data fixtures missing from {directory}: {', '.join(missing)}"
    return directory


@pytest.fixture
def data_dir(tmp_path: Path, fixtures_dir: Path) -> Path:
    """Function scope: a writable copy of the data fixtures a loader reads by name.

    The fixtures are copied rather than loaded in place, for the reason AAP 0.14.3 gives: a project
    hands ``data.dir`` a directory of its own, so resolution is exercised against one, and a fault
    that wrote to the loader's input could never reach the repository's committed files.
    """
    directory = tmp_path / "testdata"
    directory.mkdir()
    for name in (*TABULAR_FIXTURES, *NO_CASES_EXTRA_FILES):
        shutil.copy(fixtures_dir / name, directory / name)
    return directory


# --- Helpers ---------------------------------------------------------------


def _ids(cases: Sequence[Case]) -> tuple[str, ...]:
    """Return the ids of ``cases`` in the order the loader produced them."""
    return tuple(case.id for case in cases)


def _values_by_id(cases: Sequence[Case]) -> dict[str, dict[str, object]]:
    """Return each case's values keyed by its id, as plain dicts compared for equality only.

    A ``Case`` freezes its values as a read-only mapping, so copying them into a ``dict`` is what
    lets two formats' cases be compared without depending on either format's column order.
    """
    return {case.id: dict(case.values) for case in cases}


def _fixture_texts(fixtures_dir: Path, *names: str) -> dict[str, str]:
    """Return committed fixtures keyed by the ``testdata/`` path a generated project sees."""
    return {f"testdata/{name}": (fixtures_dir / name).read_text(encoding="utf-8") for name in names}


def _parametrised_module(data_name: str) -> str:
    """Return the text of a project module that collects one test per case of one data file.

    The module is the project-side half of the surface described in this file's docstring: it reads
    the data file through ``foundry.data.registry.load_cases`` at import time and binds the cases
    through ``foundry.data.cases.build_parameters``, which is the adapter
    ``pytest.mark.parametrize`` receives. Nothing here registers a marker, and no ``-p`` flag is
    needed: the installed ``pytest11`` entry point activates Foundry on its own (AAP 0.4.2).
    """
    return "\n".join(
        [
            '"""A generated project module: one parameter per case, read at collection time."""',
            "",
            "from pathlib import Path",
            "",
            "import pytest",
            "",
            "from foundry.data.cases import build_parameters",
            "from foundry.data.registry import load_cases",
            "",
            'DATA = Path(__file__).resolve().parent / "testdata"',
            f'CASES = load_cases(DATA / "{data_name}")',
            "",
            "",
            '@pytest.mark.parametrize("case", build_parameters(CASES))',
            "def test_case(case: object) -> None:",
            '    """Consume the case this parametrisation bound."""',
            "    assert case is not None",
            "",
        ]
    )


def _collect(project: Path, run_pytest: RunPytest) -> subprocess.CompletedProcess[str]:
    """Collect ``project`` with a real ``pytest --collect-only``, writing nothing outside it."""
    return run_pytest(
        "--collect-only",
        "-q",
        "-p",
        "no:cacheprovider",
        *_ISOLATION_FLAGS,
        cwd=project,
        timeout=_SUBPROCESS_TIMEOUT,
    )


def _run(project: Path, run_pytest: RunPytest, *args: str) -> subprocess.CompletedProcess[str]:
    """Run one real pytest session in ``project``, with every output sink placed inside it."""
    return run_pytest(
        *args,
        "-p",
        "no:cacheprovider",
        *_ISOLATION_FLAGS,
        cwd=project,
        timeout=_SUBPROCESS_TIMEOUT,
    )


def _nodeids(output: str, module: str) -> list[str]:
    """Return the node ids a ``--collect-only -q`` run printed for ``module``, in run order."""
    return [line.strip() for line in output.splitlines() if line.startswith(f"{module}::")]


def _project_files(fixtures_dir: Path, data_name: str, module: str) -> dict[str, str]:
    """Return the files a generated project holds: one data fixture and its parametrising module.

    The mapping is handed to the ``write_project`` fixture of ``tests/conftest.py``, which owns the
    writing and the containment check, so this module never creates a project directory itself.
    """
    return {**_fixture_texts(fixtures_dir, data_name), module: _parametrised_module(data_name)}


def _marked_module(data_name: str, *, argument: str = "case") -> str:
    """Return the text of a project module the plugin parametrises from one data file.

    The module reads no file and builds no parameters: the ``foundry_data`` marker is the whole
    project-side surface, and ``argument`` is the name its ``argname`` keyword binds.
    """
    marker = f'@pytest.mark.foundry_data("{data_name}")'
    if argument != "case":
        marker = f'@pytest.mark.foundry_data("{data_name}", argname="{argument}")'
    return "\n".join(
        [
            '"""A generated project module: the marker alone parametrises this test."""',
            "",
            "import pytest",
            "",
            "",
            marker,
            f"def test_case({argument}: object) -> None:",
            '    """Consume the case the plugin bound to this item."""',
            f"    assert {argument} is not None",
            "",
        ]
    )


def _marked_files(
    fixtures_dir: Path,
    *,
    module: str,
    data_name: str = "cases.csv",
    argument: str = "case",
) -> dict[str, str]:
    """Return the files of a project whose one test the plugin parametrises over ``data_name``."""
    return {
        **_fixture_texts(fixtures_dir, data_name),
        module: _marked_module(data_name, argument=argument),
    }


# --- Tabular expansion and the four formats' shared contract ----------------


@pytest.mark.parametrize(
    ("data_name", "first_line", "line_step"),
    [
        ("cases.csv", 2, 1),
        ("cases.json", None, None),
        ("cases.yaml", None, None),
        ("cases.jsonl", 1, 1),
    ],
)
def test_tabular_file_expands_to_twelve_located_cases(
    data_dir: Path,
    clean_foundry_env: None,
    data_name: str,
    first_line: int | None,
    line_step: int | None,
) -> None:
    """One data file expands to its rows as located cases, whatever its format (AAP 0.10.1).

    ``clean_foundry_env`` is requested because a loader must answer from the file alone: no ambient
    ``FOUNDRY_*`` variable may change what is collected (AAP 0.14.3).
    """
    path = data_dir / data_name
    case_set = load_cases(path)

    # AAP 0.10.1 "CSV and matrix expansion to 12 stable cases each": the fixture's row count is a
    # contract, so a reader that dropped, duplicated or reordered a row fails here.
    assert len(case_set) == CASES_PER_FILE
    assert len(case_set.cases) == CASES_PER_FILE
    assert not case_set.is_empty

    # AAP 0.7.2: a tabular case id is the file's stem, a hyphen and the 0-based row index, taken in
    # file order. The stem is shared, so all four formats produce the same twelve ids.
    ids = _ids(case_set.cases)
    assert ids == tuple(f"cases-{index}" for index in range(CASES_PER_FILE))
    assert len(set(ids)) == CASES_PER_FILE
    for case in case_set.cases:
        assert TABULAR_ID.fullmatch(case.id) is not None

    # The adapter pytest receives (AAP 0.4.7): one parameter per case, in case order, each labelled
    # with its case id. No timestamp, host name or worker id enters an id, which is the whole of the
    # stability requirement I6 rests on.
    parameters = cast("list[_Parameter]", build_parameters(case_set))
    assert [parameter.id for parameter in parameters] == list(ids)

    # The four fixtures carry one data set, so every format declares the same six columns; only the
    # order a reader first met them in differs, which is why the comparison is sorted.
    assert sorted(case_set.columns) == sorted(DOCUMENTED_COLUMNS)

    # A case names the file it came from, so a failure is traceable to the data (AAP 0.7.2), and it
    # carries the position the id was built from.
    for case in case_set.cases:
        assert case.source_file == path
    assert [case.row_index for case in case_set.cases] == list(range(CASES_PER_FILE))

    # CSV and JSON Lines name a physical line; a JSON or YAML document has no per-row line notion
    # and reports the row position alone rather than inventing one (AAP 0.7.2).
    if first_line is None:
        assert [case.line for case in case_set.cases] == [None] * CASES_PER_FILE
    else:
        assert line_step is not None
        expected_lines = [first_line + line_step * index for index in range(CASES_PER_FILE)]
        assert [case.line for case in case_set.cases] == expected_lines


def test_the_four_formats_expand_to_one_shared_data_set(
    data_dir: Path, clean_foundry_env: None
) -> None:
    """CSV, JSON, YAML and JSON Lines are one contract: equal ids and equal values (AAP 0.10.1)."""
    loaded = {name: load_cases(data_dir / name) for name in TABULAR_FIXTURES}
    reference_name = TABULAR_FIXTURES[0]
    reference_ids = _ids(loaded[reference_name].cases)
    reference_values = _values_by_id(loaded[reference_name].cases)
    assert len(reference_ids) == CASES_PER_FILE

    for name in TABULAR_FIXTURES[1:]:
        cases = loaded[name].cases
        # The id set, not merely its size: a reader that numbered rows differently would rename
        # every test that reads the file and break collection parity between shards (I6).
        assert _ids(cases) == reference_ids, f"{name} disagrees with {reference_name} on case ids"
        # The values too, compared as mappings so a format's column order is not a difference.
        assert _values_by_id(cases) == reference_values, f"{name} disagrees on row values"


# --- Matrix expansion ------------------------------------------------------


def test_matrix_expands_to_twelve_axis_ordered_combinations(
    data_dir: Path, clean_foundry_env: None
) -> None:
    """A 3 x 4 matrix expands to twelve cases whose ids name every axis in sorted order."""
    path = data_dir / "matrix.yaml"
    matrix = load_matrix(path)

    # AAP 0.10.1: the fixture declares a three-value axis and a four-value axis, so the cross
    # product is exactly twelve cases.
    assert len(matrix.cases) == CASES_PER_FILE
    ids = _ids(matrix.cases)
    assert len(set(ids)) == CASES_PER_FILE

    # AAP 0.7.2: the axes are sorted by name, and the expansion varies the alphabetically first
    # axis slowest, which is the one order a serial run and every worker agree on.
    axis_names = tuple(axis for axis, _ in MATRIX_AXES)
    assert axis_names == tuple(sorted(axis_names))
    assert matrix.axes == MATRIX_AXES

    for case in matrix.cases:
        # Every id starts with the file's stem, and its "axis=" segments appear in sorted-axis
        # order -- the property that makes the id independent of how the document was written.
        assert case.id.startswith(f"{MATRIX_STEM}-")
        assert tuple(AXIS_SEGMENT.findall(case.id)) == axis_names
        assert tuple(sorted(case.values)) == axis_names
        # A combination is not a line of the file, so no line is reported (AAP 0.7.2).
        assert case.line is None
        assert case.source_file == path

    # The values are rendered as canonical JSON, which is what keeps 1 and "1" distinct; one id at
    # each end of the expansion pins the exact spelling.
    assert ids[0] == 'matrix-currency="EUR"-tier="bronze"'
    assert ids[-1] == 'matrix-currency="USD"-tier="platinum"'

    expected_combinations = [
        dict(zip(axis_names, combination, strict=True))
        for combination in product(*(values for _, values in MATRIX_AXES))
    ]
    assert [dict(case.values) for case in matrix.cases] == expected_combinations

    # I6: expansion is a total function of the file, and re-reading it yields the same ids; the
    # same axes written in the other order yield the same ids too, because the expander sorts them.
    assert _ids(load_matrix(path).cases) == ids
    reversed_axes = dict(reversed(MATRIX_AXES))
    assert _ids(expand_axes(source_file=path, axes=reversed_axes).cases) == ids


# --- Collection stability (implicit requirement I6) ------------------------


def test_collection_order_is_identical_across_two_runs(
    write_project: Callable[..., Path],
    run_pytest: RunPytest,
    fixtures_dir: Path,
    tmp_path: Path,
) -> None:
    """Two consecutive ``--collect-only`` runs collect byte-identical node ids (I6).

    This is the property ``pytest-xdist``'s collection-parity check depends on: workers collect
    independently and the controller aborts the run when the collections differ in content or in
    order, so a case id derived from anything but the file and the row position would make sharding
    unusable.
    """
    module = "test_cases.py"
    project = write_project(
        tmp_path / "project", files=_project_files(fixtures_dir, "cases.csv", module)
    )

    first = _collect(project, run_pytest)
    second = _collect(project, run_pytest)

    # Two separate runs really happened, and each collected the whole data file: asserting on the
    # two completed processes is what stops a vacuous pass on two empty outputs.
    assert first.returncode == 0, f"{first.stdout}\n{first.stderr}"
    assert second.returncode == 0, f"{second.stdout}\n{second.stderr}"
    first_ids = sorted(_nodeids(first.stdout, module))
    second_ids = sorted(_nodeids(second.stdout, module))
    assert len(first_ids) == CASES_PER_FILE
    assert len(second_ids) == CASES_PER_FILE
    assert first_ids == second_ids

    # And the ids collected are the ones AAP 0.7.2 fixes, rendered by pytest as the parameter id.
    assert first_ids == sorted(
        f"{module}::test_case[cases-{index}]" for index in range(CASES_PER_FILE)
    )


# --- The plugin's own collection seam (AAP 0.3.3, AAP 0.7.1) ---------------

_MALFORMED_DATA_MARKERS: Final[tuple[str, ...]] = (
    "@pytest.mark.foundry_data()",
    '@pytest.mark.foundry_data("cases.csv", "cases.json")',
    "@pytest.mark.foundry_data(3)",
    '@pytest.mark.foundry_data("cases.csv", strict=True)',
    '@pytest.mark.foundry_data("cases.csv", argname=3)',
)
"""The marker shapes the delegation refuses: no positional, two, a non-string, a foreign keyword."""

_ACCEPTED_FORM: Final[str] = 'foundry_data("<file>"[, argname="<name>"])'
"""The marker form ``--markers`` and every usage error publish for the data marker."""

_UNMARKED_MODULE: Final[str] = '''"""An unmarked item, collected without the data engine."""

import sys


def test_one() -> None:
    """The hook returned before importing anything from ``foundry.data`` (AAP 0.4.8)."""
    assert "foundry.data" not in sys.modules
'''
"""The project module the import-discipline test runs: it asserts the engine stayed unloaded."""


def test_the_marker_collects_one_item_per_case_through_the_plugin(
    write_project: Callable[..., Path], run_pytest: RunPytest, fixtures_dir: Path, tmp_path: Path
) -> None:
    """``foundry_data("cases.csv")`` alone collects and runs one item per case (AAP 0.3.3).

    The module reads no file and builds no parameters, so the twelve node ids are the plugin's own
    delegation, and they are the cases' ids rather than anything a run decides.
    """
    module = "test_cases.py"
    project = write_project(tmp_path / "project", files=_marked_files(fixtures_dir, module=module))
    expected = [f"{module}::test_case[cases-{index}]" for index in range(CASES_PER_FILE)]

    collected = _collect(project, run_pytest)
    assert collected.returncode == 0, f"{collected.stdout}\n{collected.stderr}"
    assert _nodeids(collected.stdout, module) == expected

    result = _run(project, run_pytest, "-q")
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert f"{CASES_PER_FILE} passed" in result.stdout
    document = json.loads((project / "results.json").read_text(encoding="utf-8"))
    outcomes = sorted(record["outcome"] for record in document if record["record_type"] == "result")
    assert outcomes == ["passed"] * CASES_PER_FILE


def test_the_data_marker_passes_strict_markers(
    write_project: Callable[..., Path], run_pytest: RunPytest, fixtures_dir: Path, tmp_path: Path
) -> None:
    """``--strict-markers`` accepts it, and ``--markers`` documents its accepted form."""
    project = write_project(
        tmp_path / "project", files=_marked_files(fixtures_dir, module="test_cases.py")
    )
    result = _run(project, run_pytest, "-q", "--strict-markers")
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert f"{CASES_PER_FILE} passed" in result.stdout

    listed = _run(project, run_pytest, "--markers")
    assert listed.returncode == 0, f"{listed.stdout}\n{listed.stderr}"
    assert f"@pytest.mark.{_ACCEPTED_FORM}" in listed.stdout


def test_the_argname_keyword_binds_a_differently_named_argument(
    write_project: Callable[..., Path], run_pytest: RunPytest, fixtures_dir: Path, tmp_path: Path
) -> None:
    """``argname`` binds the cases to the test's own argument name instead of ``case``.

    The module's only argument is ``row``, so without the keyword every item would error as an
    unknown fixture: a green run carrying the case ids is the evidence the keyword reached the
    adapter.
    """
    module = "test_rows.py"
    project = write_project(
        tmp_path / "project", files=_marked_files(fixtures_dir, module=module, argument="row")
    )

    result = _run(project, run_pytest, "-q")
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert f"{CASES_PER_FILE} passed" in result.stdout
    ids = _nodeids(_collect(project, run_pytest).stdout, module)
    assert ids == [f"{module}::test_case[cases-{index}]" for index in range(CASES_PER_FILE)]


def test_a_marked_header_only_file_skips_with_the_no_cases_reason(
    write_project: Callable[..., Path], run_pytest: RunPytest, fixtures_dir: Path, tmp_path: Path
) -> None:
    """A marked file with a header and no rows is one skip reading ``no cases in <file>``, at 0."""
    project = write_project(
        tmp_path / "project",
        files=_marked_files(fixtures_dir, module="test_empty.py", data_name="header_only.csv"),
    )

    result = _run(project, run_pytest, "-q", "-rs")
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    resolved = (project / "testdata" / "header_only.csv").resolve()
    assert f"no cases in {resolved}" in result.stdout
    assert "1 skipped" in result.stdout


def test_a_malformed_marker_is_a_usage_error_naming_the_accepted_form(
    write_project: Callable[..., Path], run_pytest: RunPytest, fixtures_dir: Path, tmp_path: Path
) -> None:
    """Every malformed data marker is one collection error naming the accepted form, at exit 2.

    A marker in the owned ``foundry_`` namespace is never silently ignored: a typo that would
    otherwise collect an unparametrised item and fail as an unknown fixture is reported instead.
    """
    files = _fixture_texts(fixtures_dir, "cases.csv")
    for index, marker in enumerate(_MALFORMED_DATA_MARKERS):
        files[f"test_bad_{index}.py"] = (
            f"import pytest\n\n\n{marker}\ndef test_case(case: object) -> None:\n"
            "    assert case is not None\n"
        )
    project = write_project(tmp_path / "project", files=files)

    result = _run(project, run_pytest, "-q")
    combined = f"{result.stdout}\n{result.stderr}"
    assert result.returncode == 2, combined
    assert combined.count("UsageError: the data marker takes") == len(_MALFORMED_DATA_MARKERS)
    assert combined.count(_ACCEPTED_FORM) >= len(_MALFORMED_DATA_MARKERS)


def test_an_unmarked_run_never_imports_the_data_engine(
    write_project: Callable[..., Path], run_pytest: RunPytest, tmp_path: Path
) -> None:
    """A run whose tests carry no data marker imports nothing from ``foundry.data`` (AAP 0.4.8).

    Every test of every project passes through ``pytest_generate_tests``, so the hook has to return
    before the engine behind it is loaded: the inner item asserts the engine is absent when it runs.
    """
    project = write_project(tmp_path / "project", files={"test_plain.py": _UNMARKED_MODULE})
    result = _run(project, run_pytest, "-q")
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "1 passed" in result.stdout


# --- Collection errors and the empty-file state ----------------------------


def test_missing_column_fails_collection_naming_file_line_and_column(
    write_project: Callable[..., Path],
    run_pytest: RunPytest,
    fixtures_dir: Path,
    tmp_path: Path,
    clean_foundry_env: None,
) -> None:
    """A row short of its header is a collection error naming file, line and column (AAP 0.4.7)."""
    module = "test_rows.py"
    project = write_project(
        tmp_path / "project", files=_project_files(fixtures_dir, "missing_column.csv", module)
    )
    data_file = project / "testdata" / "missing_column.csv"

    # The located error first, through the loader, so the structured attributes are pinned rather
    # than only the rendering: `DataError` carries file, line and column (AAP 0.4.3).
    with pytest.raises(DataError) as excinfo:
        load_cases(data_file)
    assert excinfo.value.file == data_file
    assert excinfo.value.line == 3
    assert excinfo.value.column == "expected_total"

    # Then the same fault through a real collection: pytest reports it as a collection error, and
    # because nothing was collected the run ends with exit code 2 (AAP 0.4.7).
    result = _collect(project, run_pytest)
    assert result.returncode == 2, f"{result.stdout}\n{result.stderr}"
    combined = f"{result.stdout}\n{result.stderr}"
    resolved = data_file.resolve()
    assert f"{resolved}:3:" in combined
    assert "(column 'expected_total')" in combined
    assert "missing_column.csv" in combined
    # Nothing was collected, so no test outcome was invented for the item that never existed.
    assert _nodeids(result.stdout, module) == []


def test_header_only_file_skips_with_the_no_cases_reason(
    write_project: Callable[..., Path],
    run_pytest: RunPytest,
    fixtures_dir: Path,
    tmp_path: Path,
) -> None:
    """A file with a header and no rows is one skipped item, not a collection error (AAP 0.4.7)."""
    module = "test_empty.py"
    project = write_project(
        tmp_path / "project", files=_project_files(fixtures_dir, "header_only.csv", module)
    )

    result = _run(project, run_pytest, "-q", "-rs")
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"

    # The generated item is reported as skipped, with the reason AAP 0.4.7 fixes, spelled with the
    # file exactly as the loader resolved it.
    resolved = (project / "testdata" / "header_only.csv").resolve()
    assert "skipped" in result.stdout
    assert f"no cases in {resolved}" in result.stdout

    # And the machine-readable record agrees: one result, outcome "skipped", so a consumer of
    # results.json sees the same state the terminal does (AAP 0.4.6).
    document = json.loads((project / "results.json").read_text(encoding="utf-8"))
    results = [record for record in document if record.get("record_type") == "result"]
    assert [record["outcome"] for record in results] == ["skipped"]


def test_unsupported_extension_lists_the_supported_formats(
    fixtures_dir: Path, clean_foundry_env: None
) -> None:
    """A suffix no reader claims is a located error listing the formats that would work."""
    path = fixtures_dir / "unsupported_extension.txt"

    # AAP 0.3.2 / 0.7.2: the registry is the extension contract, and it reports its suffixes sorted
    # so the message reads identically in every run.
    assert tuple(sorted({"csv", "json", "jsonl", "yaml", "yml"})) == supported_extensions()
    assert supported_extensions() == SUPPORTED_EXTENSIONS

    # Asserted through the loader rather than a second pytester-style run: the pytest-level form of
    # this state belongs to tests/plugin/test_data_collection_errors.py.
    with pytest.raises(DataError) as excinfo:
        load_cases(path)
    message = str(excinfo.value)
    assert "unsupported data file extension 'txt'" in message
    for extension in SUPPORTED_EXTENSIONS:
        assert extension in message, f"the error does not list the supported suffix {extension!r}"
    assert excinfo.value.file == path
    assert excinfo.value.line is None
    assert excinfo.value.column is None

    # Routing refuses the suffix before any reader opens the file, which is what keeps the fault a
    # located data error rather than a parse error from the wrong reader.
    with pytest.raises(DataError):
        loader_for(path)
