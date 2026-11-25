"""The three collection-time data states AAP 0.4.7 makes observable through pytest itself.

Each row of the state table is asserted here together with the exit code that row fixes, because
Foundry preserves pytest's own codes and must not begin to rewrite them (AAP 0.4.4):

* an unsupported data-file extension is a ``DataError`` at collection that lists every extension
  which would work -- exit ``2`` when nothing else was collected, ``1`` when the rest of the
  session ran;
* a data row missing a required column is a collection error naming the file, the line and the
  column, with the same ``2``/``1`` split;
* an empty data file is one item skipped with the reason ``no cases in <file>`` -- exit ``0``.

The module also carries implicit requirement I6: case ids and collection order derive from file
content alone, so two collections of one project print the same node ids in the same order, which is
the property a sharded run's collection parity rests on.

**The two project-side seams.** The plugin owns the ``foundry_data`` marker seam, which
``tests/integration/test_data_expansion.py`` drives through a real collection; this module keeps the
project-side hook and the ``foundry_data`` fixture path, because the three collection-time error
states AAP 0.4.7 makes observable are asserted here through both. A consuming project's own
``pytest_generate_tests`` calls
``foundry.data.parametrize(metafunc, foundry.data.load_cases(path))``, and ``foundry_data`` is the
fixture-driven path beside it, so neither route is simulated.

**Nothing here touches the repository.** Every inner project is a throwaway directory in the
pytester temporary tree, into which the records under ``tests/fixtures`` are copied, so a run can
never mutate the fixtures it reads (AAP 0.14.3).

**Real entry point.** Every test drives pytest over a written project, which is the experience
``tests/plugin/`` exists to assert (AAP 0.8.1): a user meets this through collection, not through a
direct call to a loader. ``tests/integration/test_data_expansion.py`` owns the in-process expansion
and id-set comparison and ``tests/unit/test_data.py`` the loader units, so neither is duplicated.
"""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import Iterable, Mapping
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
"""The public surface this module reaches through a real pytester run (AAP 0.14.1).

``tests/plugin/test_reachability.py`` parses this tuple with :mod:`ast` and requires one test per
entry, so each name below is backed by a run in this module and by nothing else.
"""

_FIXTURE_TREE: Final[tuple[str, ...]] = ("tests", "fixtures")
"""The directory the data records are copied from, relative to the repository root."""

_DATA_RECORDS: Final[tuple[str, ...]] = (
    "cases.csv",
    "cases.json",
    "cases.jsonl",
    "cases.yaml",
    "header_only.csv",
    "matrix.yaml",
    "missing_column.csv",
    "unsupported_extension.txt",
)
"""Every record this module copies into an inner project, sorted."""

_LOADER_RECORDS: Final[tuple[str, ...]] = ("cases.csv", "cases.json", "cases.jsonl", "cases.yaml")
"""The four records holding the same twelve cases, one per shipped reader (AAP 0.7.1)."""

_SUPPORTED_EXTENSIONS: Final[frozenset[str]] = frozenset({"csv", "json", "jsonl", "yaml", "yml"})
"""The extensions an unsupported-file error must list (AAP 0.4.7)."""

_CASES_PER_RECORD: Final[int] = 12
"""How many cases each ``cases.*`` record holds, and the size of the matrix cross-product."""

_EXPECTED_IDS: Final[tuple[str, ...]] = tuple(
    f"cases-{index}" for index in range(_CASES_PER_RECORD)
)
"""The twelve ids the ``cases.*`` records expand to, one per row in file order."""

_UNSUPPORTED_RECORD: Final[str] = "unsupported_extension.txt"
"""The record no reader claims, whose error must name the formats that would work."""

_MISSING_COLUMN_RECORD: Final[str] = "missing_column.csv"
"""The record whose third line carries five values for a six-column header."""

_MISSING_COLUMN: Final[str] = "expected_total"
"""The column that short line never filled, and the one its error has to name."""

_EMPTY_RECORD: Final[str] = "header_only.csv"
"""The record holding a header and no data rows, which is a skip rather than a fault."""

_MATRIX_RECORD: Final[str] = "matrix.yaml"
"""The flat axis mapping whose 3 x 4 cross-product is twelve cases (AAP 0.7.2)."""

_SUPPORTED_LIST: Final[re.Pattern[str]] = re.compile(
    r"supported extensions: (?P<listed>[a-z]+(?:, [a-z]+)*)"
)
"""The clause an unsupported-extension error ends with, captured so the whole list is compared."""

_NODEID_LINE: Final[re.Pattern[str]] = re.compile(r"^(?P<nodeid>\S+\.py::\S+)$")
"""One line of ``--collect-only -q`` output: a node id and nothing else."""

_COLLECTING_CONFTEST: Final[str] = '''"""Collect cases through the published adapter."""

import pathlib

import foundry.data


def pytest_generate_tests(metafunc):
    """Bind a module's ``DATA_FILE`` to the ``case`` argument, as a consuming project does."""
    name = getattr(metafunc.module, "DATA_FILE", None)
    if name is None:
        return
    path = pathlib.Path(__file__).with_name(name)
    foundry.data.parametrize(metafunc, foundry.data.load_cases(path))
'''
"""The inner project's conftest: the seam a data-driven Foundry project writes for itself."""

_USING_FIXTURE_MODULE: Final[str] = f'''"""Read the records through the foundry_data fixture."""


def test_the_fixture_reads_the_records(foundry_data) -> None:
    """Twelve cases in row order, and twelve combinations from the flat axis mapping."""
    cases = foundry_data.cases("{_LOADER_RECORDS[0]}")
    expected = ["cases-" + str(index) for index in range({_CASES_PER_RECORD})]
    assert len(cases) == {_CASES_PER_RECORD}
    assert [case.id for case in cases] == expected
    assert [case.row_index for case in cases] == list(range({_CASES_PER_RECORD}))
    combinations = foundry_data.matrix("{_MATRIX_RECORD}")
    assert len(combinations) == {_CASES_PER_RECORD}
    assert len(set(combination.id for combination in combinations)) == {_CASES_PER_RECORD}
'''
"""The fixture-driven path: names resolved against ``data.dir``, not a path named by the test."""


def _case_module(data_file: str) -> str:
    """Return an inner test module whose single test is parametrised over ``data_file``.

    The module names its data file in ``DATA_FILE``, which the inner conftest reads, so the
    parametrisation is the project's own declaration rather than a name a hook guesses at.
    """
    return (
        f'"""One data-driven test over {data_file}."""\n'
        "\n"
        f"DATA_FILE = {data_file!r}\n"
        "\n"
        "\n"
        "def test_case(case) -> None:\n"
        '    """Every collected parameter is one case of the data file."""\n'
        "    assert case.id\n"
    )


def _loader_modules() -> dict[str, str]:
    """Return one inner test module per shipped reader, keyed by the module file name."""
    return {
        f"test_{Path(record).suffix.removeprefix('.')}.py": _case_module(record)
        for record in _LOADER_RECORDS
    }


def _build_project(
    pytester: pytest.Pytester,
    repo_root: Path,
    modules: Mapping[str, str],
) -> Path:
    """Write an inner project holding the copied records, the collecting conftest and ``modules``.

    The records are copied rather than referenced, which is what keeps the repository's fixtures
    read-only inputs of this module: no inner run can write back into them.
    """
    source = repo_root.joinpath(*_FIXTURE_TREE)
    for name in _DATA_RECORDS:
        shutil.copyfile(source / name, pytester.path / name)
    pytester.makeconftest(_COLLECTING_CONFTEST)
    for name, content in modules.items():
        (pytester.path / name).write_text(content, encoding="utf-8", newline="\n")
    return pytester.path


def _collected_nodeids(result: pytest.RunResult) -> list[str]:
    """Return the node ids a ``--collect-only -q`` run printed, in the order it printed them."""
    nodeids: list[str] = []
    for line in result.stdout.lines:
        match = _NODEID_LINE.match(line.strip())
        if match is not None:
            nodeids.append(match.group("nodeid"))
    return nodeids


def _ids_by_module(nodeids: Iterable[str]) -> dict[str, list[str]]:
    """Group the parametrisation id of each node id under the module that owns it.

    The parametrisation id is what a ``--collect-only`` line and a result record both carry after
    the ``[``, so the two spellings of one collection are compared through a single parse.
    """
    grouped: dict[str, list[str]] = {}
    for nodeid in nodeids:
        module, _, remainder = nodeid.partition("::")
        grouped.setdefault(module, []).append(remainder.rpartition("[")[2].rstrip("]"))
    return grouped


def _result_elements(project: Path) -> list[Mapping[str, object]]:
    """Return ``results.json``'s elements, or nothing when the run wrote no document at all."""
    document = project / "results.json"
    if not document.is_file():
        return []
    elements: object = json.loads(document.read_text(encoding="utf-8"))
    assert isinstance(elements, list), f"{document} does not hold a JSON array"
    return [element for element in elements if isinstance(element, dict)]


def _outcomes(project: Path) -> list[str]:
    """Return the outcome of every result record ``results.json`` holds, sorted."""
    return sorted(
        str(element["outcome"])
        for element in _result_elements(project)
        if element.get("record_type") == "result"
    )


def _listed_extensions(result: pytest.RunResult) -> set[str]:
    """Return the extensions the unsupported-extension error lists as the ones that would work."""
    output = result.stdout.str()
    match = _SUPPORTED_LIST.search(output)
    assert match is not None, f"the collection error lists no supported extensions:\n{output}"
    return {item.strip() for item in match.group("listed").split(",")}


# --- The three collection states, and the control ---------------------------


def test_unsupported_extension_errors_and_lists_supported(
    pytester: pytest.Pytester, repo_root: Path, clean_foundry_env: None
) -> None:
    """An extension no reader claims stops collection with exit 2 and names the formats that work.

    Nothing else is collected, so this is the state table's "2 when nothing was collected" branch:
    pytest interrupts the session instead of running anything.
    """
    _build_project(pytester, repo_root, {"test_unsupported.py": _case_module(_UNSUPPORTED_RECORD)})
    result = pytester.runpytest("-q")
    assert result.ret == 2
    output = result.stdout.str()
    assert _UNSUPPORTED_RECORD in output
    assert "'txt'" in output
    assert "DataError" in output
    assert _listed_extensions(result) == _SUPPORTED_EXTENSIONS
    assert "passed" not in _outcomes(pytester.path)


def test_unsupported_extension_with_other_tests_exits_1(
    pytester: pytest.Pytester, repo_root: Path, clean_foundry_env: None
) -> None:
    """Beside a valid module the session runs the rest, so the exit code is 1 and not 2.

    ``--continue-on-collection-errors`` is what reaches the row's second branch: without it pytest
    interrupts before running anything, which is the state the test above already asserts.
    """
    _build_project(
        pytester,
        repo_root,
        {
            "test_unsupported.py": _case_module(_UNSUPPORTED_RECORD),
            "test_valid.py": _case_module(_LOADER_RECORDS[0]),
        },
    )
    result = pytester.runpytest("-q", "--continue-on-collection-errors")
    assert result.ret == 1
    result.assert_outcomes(passed=len(_EXPECTED_IDS), errors=1)
    assert _UNSUPPORTED_RECORD in result.stdout.str()
    assert _outcomes(pytester.path) == ["passed"] * len(_EXPECTED_IDS)


def test_missing_column_reports_file_line_and_column(
    pytester: pytest.Pytester, repo_root: Path, clean_foundry_env: None
) -> None:
    """A short row is located at its own line and named by the column it never filled.

    A typed error alone would not satisfy AAP 0.7.1: the location is the part a reader acts on, so
    the file, the line the row starts on and the absent column are each asserted.
    """
    _build_project(
        pytester,
        repo_root,
        {"test_missing.py": _case_module(_MISSING_COLUMN_RECORD)},
    )
    result = pytester.runpytest("-q")
    assert result.ret == 2
    output = result.stdout.str()
    assert f"{_MISSING_COLUMN_RECORD}:3:" in output
    assert f"(column '{_MISSING_COLUMN}')" in output
    assert "row 1 has 5 values but the header declares 6 columns" in output


def test_header_only_file_skips_with_reason(
    pytester: pytest.Pytester, repo_root: Path, clean_foundry_env: None
) -> None:
    """A file with a header and no data rows is one skipped item, and the session still exits 0."""
    _build_project(pytester, repo_root, {"test_empty.py": _case_module(_EMPTY_RECORD)})
    result = pytester.runpytest("-q", "-rs")
    assert result.ret == 0
    result.assert_outcomes(skipped=1)
    reasons = [line for line in result.stdout.str().splitlines() if "no cases in " in line]
    assert len(reasons) == 1
    assert reasons[0].rstrip().endswith(_EMPTY_RECORD)
    assert _outcomes(pytester.path) == ["skipped"]


# --- The control: the same harness over a valid file ------------------------


def test_four_loaders_produce_equal_case_sets(
    pytester: pytest.Pytester, repo_root: Path, clean_foundry_env: None
) -> None:
    """The four shipped readers agree case for case on the same twelve rows, and all 48 run.

    This is the control the three states above rest on: a valid record collects twelve cases and
    exits 0, so a broken harness could not make an error assertion pass.
    """
    _build_project(pytester, repo_root, _loader_modules())
    result = pytester.runpytest("-q")
    assert result.ret == 0
    result.assert_outcomes(passed=len(_EXPECTED_IDS) * len(_LOADER_RECORDS))
    nodeids = [str(element["nodeid"]) for element in _result_elements(pytester.path)]
    per_module = _ids_by_module(nodeids)
    assert sorted(per_module) == sorted(_loader_modules())
    for module in sorted(per_module):
        assert sorted(per_module[module]) == sorted(_EXPECTED_IDS), module
    assert len({tuple(identifiers) for identifiers in per_module.values()}) == 1
    total = len(_EXPECTED_IDS) * len(_LOADER_RECORDS)
    assert sum(len(identifiers) for identifiers in per_module.values()) == total


def test_collection_is_deterministic_across_runs(
    pytester: pytest.Pytester, repo_root: Path, clean_foundry_env: None
) -> None:
    """Two collections of one project print the same node ids, in the same order (I6)."""
    _build_project(pytester, repo_root, _loader_modules())
    first = pytester.runpytest("-q", "--collect-only")
    second = pytester.runpytest("-q", "--collect-only")
    assert first.ret == 0
    assert second.ret == 0
    collected_first = _collected_nodeids(first)
    collected_second = _collected_nodeids(second)
    assert len(collected_first) == len(_EXPECTED_IDS) * len(_LOADER_RECORDS)
    assert sorted(collected_first) == sorted(collected_second)
    # Stronger than the sorted comparison: collection emits the cases in one settled order, so the
    # run-to-run sequences agree position for position and not only as sets.
    assert collected_first == collected_second


def test_foundry_data_fixture_reads_committed_files(
    pytester: pytest.Pytester, repo_root: Path, clean_foundry_env: None
) -> None:
    """The fixture-driven path reads the same records over the configured data directory.

    ``data.dir`` is what the fixture resolves a name against, so the override points it at this
    project; a name is never a path, which is the containment rule the fixture applies.
    """
    _build_project(pytester, repo_root, {"test_fixture_data.py": _USING_FIXTURE_MODULE})
    result = pytester.runpytest("-q", f"--foundry-data.dir={pytester.path}")
    assert result.ret == 0
    result.assert_outcomes(passed=1)
