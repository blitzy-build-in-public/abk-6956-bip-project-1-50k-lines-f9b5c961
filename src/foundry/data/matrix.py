"""Matrix expansion: the deterministic cross-product a matrix data file expands into (AAP 0.7.2).

Axes sort by name, the first varying slowest, and ``row_index`` counts combinations from zero.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from itertools import product
from pathlib import Path

from foundry.data.cases import Case, CaseSet
from foundry.data.ids import case_stem, matrix_case_id
from foundry.data.loaders.base import LoadedMatrix

__all__ = ["Matrix", "expand_axes", "expand_matrix"]

# --- The expanded matrix ---------------------------------------------------


@dataclass(frozen=True, slots=True)
class Matrix:
    """One expanded matrix: the axes it varies, and the cases their cross-product produced.

    Frozen because the cases are what the run is planned around; a matrix that could be edited
    after collection could disagree with the node ids pytest had already reported.

    Attributes:
        name: The matrix's name, which is the data file's stem, so ``matrix.yaml`` expands to cases
            whose ids start ``matrix-`` (AAP 0.7.2).
        source_file: The data file the axes were read from; every message about a case names it.
        axes: The ``(axis_name, values)`` pairs sorted by axis name -- the invariant the expansion
            establishes, restated here so a reader of a matrix can see the order its cases were
            produced in rather than re-deriving it.
        cases: One :class:`~foundry.data.cases.Case` per combination, in expansion order, or an
            empty tuple when an axis held no values.
    """

    name: str
    source_file: Path
    axes: tuple[tuple[str, tuple[object, ...]], ...]
    cases: tuple[Case, ...]

    def to_case_set(self) -> CaseSet:
        """Return the expanded cases as the case set the rest of the package consumes.

        Conversion rather than a second adapter: ``foundry.data.cases`` owns the one route from a
        case set to pytest parameters, so a matrix reaches a test through exactly the code a table
        does, and the dependency direction stays ``matrix -> cases`` on one side only (AAP 0.4.8).

        Returns:
            A case set over this matrix's source file, whose ``columns`` are the axis names in
            sorted order and whose ``cases`` are this matrix's cases in expansion order.
        """
        return CaseSet(
            source_file=self.source_file,
            columns=tuple(axis for axis, _ in self.axes),
            cases=self.cases,
        )


# --- Expansion -------------------------------------------------------------


def expand_axes(
    *,
    source_file: Path,
    axes: Mapping[str, Sequence[object]],
    name: str | None = None,
) -> Matrix:
    """Expand an axis mapping into its cross-product of cases, in one fixed order.

    The expansion is a total function of its arguments, which is what a sharded run needs: the axes
    are ordered by ``sorted()`` on their names, so the order the caller wrote them in cannot change
    the result, and ``itertools.product`` then varies the alphabetically first axis slowest.
    ``line`` is ``None`` on every case. A combination is not a line of the file, and a reader
    records axis positions rather than per-combination ones, so the field reports honestly that
    there is no line to give instead of inventing one (AAP 0.7.2).

    Args:
        source_file: The data file the axes were read from; carried on the matrix and on every case
            so a failure can name it.
        axes: The axis values to expand, as ``axis_name -> values``. An axis with an empty value
            sequence yields no combinations, hence no cases; a mapping with no axes at all yields no
            cases either, because a document that declares no axis carries no data.
        name: The name recorded on the matrix. It prefixes no id -- the ids come from
            :func:`foundry.data.ids.matrix_case_id` -- and defaults to the source file's stem.

    Returns:
        The expanded matrix, whose ``axes`` are the pairs sorted by axis name and whose ``cases``
        hold one case per combination, each named and identified by
        :func:`foundry.data.ids.matrix_case_id`, carrying its 0-based position as ``row_index``.

    Raises:
        FoundryError: The file's stem is blank, or an axis value cannot be rendered as JSON. Both
            checks live in :func:`foundry.core.ids.case_id_for_matrix`, which owns the id format.
    """
    stem = case_stem(source_file) if name is None else name
    sorted_axes: tuple[tuple[str, tuple[object, ...]], ...] = tuple(
        (axis, tuple(axes[axis])) for axis in sorted(axes)
    )
    # An empty axis makes the product empty on its own; a matrix with no axes has no dimension to
    # vary, so it is the empty-document state rather than one case fixing nothing.
    combinations: Iterator[tuple[object, ...]] = (
        product(*(values for _, values in sorted_axes)) if sorted_axes else iter(())
    )
    axis_names = tuple(axis for axis, _ in sorted_axes)
    cases: list[Case] = []
    for row_index, combination in enumerate(combinations):
        values = dict(zip(axis_names, combination, strict=True))
        identifier = matrix_case_id(stem, values)
        cases.append(
            Case(
                id=identifier,
                name=identifier,
                values=values,
                source_file=source_file,
                line=None,
                row_index=row_index,
            )
        )
    return Matrix(name=stem, source_file=source_file, axes=sorted_axes, cases=tuple(cases))


def expand_matrix(document: LoadedMatrix) -> Matrix:
    """Expand a matrix document that a format reader produced.

    The entry point the registry calls, so each document kind is expanded once, in one place: a
    table through :func:`foundry.data.cases.build_case_set`, a matrix through this function, and
    both produce what the parametrisation adapter consumes.

    Args:
        document: The matrix a reader loaded, carrying the file it came from and its axes, which
            the reader has already sorted by name.

    Returns:
        The expanded matrix, named after the stem of ``document.path``.

    Raises:
        FoundryError: The file's stem is blank, or an axis value cannot be rendered as JSON; see
            :func:`expand_axes`, which delegates the format to
            :func:`foundry.core.ids.case_id_for_matrix`.
    """
    return expand_axes(
        source_file=document.path,
        axes=dict(document.axes),
        name=case_stem(document.path),
    )
