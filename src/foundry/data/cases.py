"""The immutable case model, and the adapter that turns cases into pytest parameters.

A data file reaches this module as a :class:`~foundry.data.loaders.base.LoadedTable` and
leaves it as a :class:`CaseSet` of :class:`Case` objects -- or, for a file that parses but
holds no data rows, as the single skipped parameter :func:`build_parameters` returns. The
field names :class:`Case` and :class:`CaseSet` publish are the AAP 0.7.2 contract: a later
release may add to them but may never rename or remove one (AAP 0.4.9).

Three rules shape what sits here.

* **Immutability.** Both models are frozen dataclasses with ``slots``, and a case's
  ``values`` are held in a :class:`types.MappingProxyType`, so a case captured at
  collection cannot be edited once the run has been planned around it.
* **Determinism.** Case order is the loader's file order, and an id comes from the file's
  stem plus a row position -- never from a cell value, the clock, the host or a worker id
  -- so a serial run and every xdist worker collect byte-identical parametrisation ids
  (implicit requirement I6).
* **An empty file is not a fault.** An empty ``LoadedTable.rows`` means the file parses and
  holds no data, which AAP 0.4.7 reports as one skipped item whose reason reads
  ``no cases in <file>``: the session exits 0 instead of failing collection.

This module converts and formats only; it validates nothing and raises nothing of its own.
Schema enforcement belongs to ``foundry.data.schema``, which ``foundry.data.registry`` runs
before calling :func:`build_case_set`. Importing the module defines names and does nothing
else -- no I/O, no environment read, no logger configuration, no mutable module state
(AAP 0.4.8).

``pytest`` is imported at module scope here because this module is the parametrisation
adapter: ``pytest.param``, ``Metafunc`` and the ``ParameterSet`` a parameter is built from
are values and types rather than hookspecs, so the rule that only ``foundry.plugin``
implements pytest hooks (AAP 0.4.8) still holds.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

# ``pytest`` 8.4 has no top-level ``ParameterSet``, so the annotation below names the very
# class ``pytest.param`` returns. The leading underscore keeps the import out of this
# module's public surface, which is exactly the five names in ``__all__``.
import pytest
from _pytest.mark.structures import ParameterSet as _ParameterSet

from foundry.data.ids import case_stem, tabular_case_id
from foundry.data.loaders.base import LoadedTable

__all__ = [
    "Case",
    "CaseSet",
    "build_case_set",
    "build_parameters",
    "parametrize",
]

# The data column whose value may label a case. The label is display-only: ids are built
# from the row position so that relabelling a row cannot perturb collection.
_NAME_COLUMN = "name"

# Suffix of the id of the single skipped parameter an empty data file expands to.
_NO_CASES_ID_SUFFIX = "-no-cases"

# The fixture name cases are bound to unless the caller names another one.
_DEFAULT_ARGNAME = "case"

# --- The case model --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Case:
    """One data-driven case: the values of a single data row, and the id naming it.

    A case is what a parametrised test receives. It carries the row's values in the data
    file's column order, the line the row came from so a failure can be traced back to the
    file, and the id pytest renders as part of the node id.

    Attributes:
        id: The parametrisation id. For a tabular row it is ``f"{stem}-{row_index}"``, for
            example ``cart-3`` for the fourth data row of ``cart.csv``. It derives from the
            file's stem and the row's position and from nothing else, so editing a cell
            renames no unrelated test and every worker of a sharded run agrees on the
            collection (implicit requirement I6).
        name: The display label -- the row's ``name`` cell when it holds a non-empty
            string, otherwise ``id``. The label never reaches the id, so a test author may
            relabel a row without perturbing collection.
        values: The row's ``column name -> value`` pairs in the file's column order, frozen
            to a read-only mapping by ``__post_init__``.
        source_file: The data file the row was read from; a failure names it.
        line: The 1-based line the row starts on, or ``None`` for a format with no per-row
            line notion. It is the real line in the file, never the row offset.
        row_index: The 0-based position of the row among the file's data rows, with the
            header excluded.
    """

    id: str
    name: str
    values: Mapping[str, object]
    source_file: Path
    line: int | None = None
    row_index: int | None = None

    def __post_init__(self) -> None:
        """Freeze ``values`` as a read-only mapping in the loader's column order.

        ``frozen=True`` stops the attribute being rebound, not the mapping it refers to
        being mutated, so without this a case captured at collection could be changed under
        the run that already planned around it. The proxy wraps a fresh ``dict``, which
        preserves the loader's column order and leaves the caller's own mapping untouched.
        """
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))


# --- The set of cases one file expands to ----------------------------------


@dataclass(frozen=True, slots=True)
class CaseSet:
    """Every case one data file expands to, with the columns that file declared.

    The set is what moves between the pieces of the data engine: a loader produces the
    table, :func:`build_case_set` produces this, ``foundry.data.schema`` checks a set's
    rows against a JSON Schema, and :func:`parametrize` turns it into pytest parameters.

    Attributes:
        source_file: The data file the cases came from; every message names it.
        columns: The column names in the order a reader first met them -- the header order
            for CSV, the union of the row keys in first-appearance order for JSON, YAML and
            JSON Lines. It is what the schema check tests unknown columns against, and it
            survives an empty file, whose header is still known.
        cases: One :class:`Case` per data row, in file order, or an empty tuple for a file
            that held no data rows -- the state :meth:`is_empty` reports.
    """

    source_file: Path
    columns: tuple[str, ...]
    cases: tuple[Case, ...]

    def __len__(self) -> int:
        """Return how many cases the file expanded to.

        Returns:
            The number of cases, which is ``0`` for a file with no data rows.
        """
        return len(self.cases)

    def __iter__(self) -> Iterator[Case]:
        """Iterate the cases in file order, which is the order pytest collects them in.

        Yields:
            Each :class:`Case` of the set, in the order the data file listed its rows.
        """
        return iter(self.cases)

    @property
    def is_empty(self) -> bool:
        """Whether the file held no data rows.

        Returns:
            ``True`` when the file parsed but carried no data row -- a header-only CSV or
            an empty JSON array -- which :func:`build_parameters` turns into one skipped
            item rather than a collection error (AAP 0.4.7).
        """
        return not self.cases


# --- Conversion ------------------------------------------------------------


def build_case_set(table: LoadedTable) -> CaseSet:
    """Convert one loaded table into the cases its rows become, in file order.

    Pure conversion: no validation and no schema logic happens here, because
    ``foundry.data.registry`` runs ``foundry.data.schema.enforce_table`` on the table before
    calling this. Each row is taken as-is, so a value keeps whatever type the reader gave it
    -- a string for CSV, the parsed scalar for JSON, YAML and JSON Lines.

    A row's display label is its ``name`` cell when that cell is a non-empty string, and
    its id otherwise. The id itself is never derived from a cell, which is what lets an
    author edit data or relabel a row without renaming tests (implicit requirement I6).

    Args:
        table: The table a reader loaded, carrying its columns, rows and path.

    Returns:
        The set of cases, one per row of ``table.rows`` and in the same order, with the
        table's columns and path. A table with no rows yields an empty set whose
        ``columns`` are still the ones the file declared.

    Raises:
        FoundryError: The file's stem is blank or a row position is negative; that check
            lives in :func:`foundry.data.ids.tabular_case_id` and its core helper, which own
            the id format.
    """
    stem = case_stem(table.path)
    cases: list[Case] = []
    for row in table.rows:
        values = row.as_mapping()
        identifier = tabular_case_id(stem, row.row_index)
        cases.append(
            Case(
                id=identifier,
                name=_display_name(values, identifier),
                values=values,
                source_file=table.path,
                line=row.line,
                row_index=row.row_index,
            )
        )
    return CaseSet(source_file=table.path, columns=table.columns, cases=tuple(cases))


def _display_name(values: Mapping[str, object], fallback: str) -> str:
    """Return the row's ``name`` cell when it is a non-empty string, else ``fallback``.

    Args:
        values: The row's values, in the file's column order.
        fallback: The case id, which labels a row that carries no usable ``name`` cell.

    Returns:
        The row's own label when ``values["name"]`` is a non-empty ``str``, otherwise
        ``fallback``. A missing, empty or non-string cell is treated as no label at all.
    """
    candidate = values.get(_NAME_COLUMN)
    if isinstance(candidate, str) and candidate:
        return candidate
    return fallback


# --- The pytest adapter ----------------------------------------------------


def build_parameters(case_set: CaseSet) -> list[_ParameterSet]:
    """Build the ``pytest.param`` objects that parametrise a test over one case set.

    This is the primitive :func:`parametrize` hands to pytest, published separately so a
    caller that parametrises a fixture keyed on something other than a case can reuse it.

    A non-empty set yields one parameter per case, in file order, whose id is exactly the
    case's id and which carries no marks of its own -- the test's own markers apply as
    usual. An empty set yields one skipped parameter instead of an empty list, because
    pytest rejects a parametrisation with no values: the reason reads
    ``no cases in <file>`` and the session exits 0 (AAP 0.4.7).

    Ids contain no timestamps, host names or worker ids, so two collections of the same
    file -- on one machine or across a sharded run -- produce identical parameters
    (implicit requirement I6).

    Args:
        case_set: The cases to parametrise over, as :func:`build_case_set` returned them.

    Returns:
        A non-empty list of parameters in the case set's order: one per case, or the single
        skip parameter when the data file held no rows.
    """
    if case_set.is_empty:
        return [_no_cases_parameter(case_set.source_file)]
    return [pytest.param(case, id=case.id) for case in case_set.cases]


def _no_cases_parameter(source_file: Path) -> _ParameterSet:
    """Build the single skipped parameter an empty data file expands to.

    The skip reason is the message AAP 0.4.7 fixes, ``no cases in <file>``, with the file
    spelled exactly as the case set holds it, so a test author sees which file contributed
    no cases without reading the configuration.

    Args:
        source_file: The data file that held no data rows.

    Returns:
        One parameter whose value is ``None``, whose id is ``f"{stem}-no-cases"`` and which
        carries a ``skip`` mark with the reason ``no cases in <file>``.
    """
    return pytest.param(
        None,
        id=f"{case_stem(source_file)}{_NO_CASES_ID_SUFFIX}",
        marks=pytest.mark.skip(reason=f"no cases in {source_file}"),
    )


def parametrize(
    metafunc: pytest.Metafunc,
    case_set: CaseSet,
    *,
    argname: str = _DEFAULT_ARGNAME,
) -> None:
    """Parametrise ``metafunc`` over the cases of one data file.

    This is the single entry point ``foundry.plugin`` calls from its ``pytest_generate_tests``
    delegation, and it does only what that delegation needs: bind one argument name to the
    parameters :func:`build_parameters` built, in case order.

    A data file that parsed to no cases produces a skip rather than a collection error, so
    a file kept for reference with its header alone does not fail the session (AAP 0.4.7).

    Args:
        metafunc: The metafunc pytest passed to ``pytest_generate_tests``, which owns the
            collected test function or fixture being parametrised.
        case_set: The cases to parametrise over.
        argname: The name of the test argument or fixture the cases are bound to. Defaults
            to ``"case"``; a caller parametrising a differently named argument passes it.

    Returns:
        ``None``. The effect is the parametrisation pytest records on ``metafunc``.
    """
    metafunc.parametrize(argname, build_parameters(case_set))
