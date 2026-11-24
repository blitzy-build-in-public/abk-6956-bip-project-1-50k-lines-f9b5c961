"""Stable identifiers for the cases a data file expands into.

A data-driven case needs a name that a serial run and every xdist worker agree on, because
the controller aborts the run when two workers collect different collections (implicit
requirement I6). An id here comes from the data file's stem and either the row's 0-based
position or the case's axis values, and from nothing else: no clock, no worker id, no host.

The two id shapes belong to :mod:`foundry.core.ids`, which owns ``case_id_for_row`` and
``case_id_for_matrix``; the functions here delegate to it and restate neither the format nor
the axis sorting nor the canonical JSON rendering of an axis value. AAP 0.7.2 names one
function, ``case_id()``, published here as :func:`tabular_case_id` and
:func:`matrix_case_id` because the two shapes take disjoint inputs and one dispatcher could
not reject a malformed call without a bare builtin error (AAP 0.14.3).

Importing the module defines names and does nothing else (AAP 0.4.8).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from foundry.core.ids import case_id_for_matrix, case_id_for_row

__all__ = [
    "case_stem",
    "matrix_case_id",
    "tabular_case_id",
]

# --- The stem an id starts with --------------------------------------------


def case_stem(path: Path) -> str:
    """Return the stem that prefixes the case ids of one data file.

    Pure and file-agnostic: the caller resolves the path and decides whether it exists.

    Args:
        path: The data file's path, for example ``testdata/cart.csv``.

    Returns:
        The file's name without its final suffix: ``"cart"`` for ``testdata/cart.csv``,
        ``"matrix"`` for ``examples/checkout/testdata/matrix.yaml``.
    """
    return path.stem


# --- Case ids --------------------------------------------------------------


def tabular_case_id(stem: str, row_index: int) -> str:
    """Build the id of one data row, ``f"{stem}-{row_index}"``.

    Args:
        stem: The data file's stem, as :func:`case_stem` returns it.
        row_index: The 0-based position of the data row among the file's data rows, with
            the header excluded; not the line in the file (AAP 0.7.2).

    Returns:
        The case id, for example ``cart-3`` for the fourth data row of ``cart.csv``.

    Raises:
        FoundryError: The stem is blank or the row index is negative; the check lives in
            :func:`foundry.core.ids.case_id_for_row`, which owns the format.
    """
    return case_id_for_row(stem, row_index)


def matrix_case_id(stem: str, values: Mapping[str, object]) -> str:
    """Build the id of one expanded matrix case.

    Axes are sorted by name and each value rendered as canonical JSON by the core helper, so
    two collections agree however the axes were written down and ``1`` stays distinct from
    ``"1"``.

    Args:
        stem: The matrix file's stem, as :func:`case_stem` returns it.
        values: The axis values this case fixes, for example
            ``{"size": "L", "colour": "red"}``.

    Returns:
        The case id, ``f"{stem}-{axis1}={value1}-{axis2}={value2}"``, for example
        ``matrix-colour="red"-size="L"``, or the bare stem when no axis is fixed.

    Raises:
        FoundryError: The stem is blank, an axis name is not a string, or an axis value
            cannot be rendered as JSON.
    """
    return case_id_for_matrix(stem, values)
