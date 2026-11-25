"""The neutral document shapes the four format readers share (AAP 0.4.7, AAP 0.4.8).

:func:`data_error` is the package's only ``DataError`` site, so every fault is located.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn, Protocol, TypeAlias

from foundry.core.exceptions import DataError

__all__: list[str] = [
    "LoadedDocument",
    "LoadedMatrix",
    "LoadedTable",
    "Loader",
    "RawRow",
    "build_table",
    "check_row_width",
    "classify_document",
    "data_error",
]

# --- Document shapes -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RawRow:
    """One data row, carrying every column of its table in the table's order.

    The row is what sits between a reader and the case builder: a reader produces
    it, ``cases.py`` turns it into a frozen ``Case`` and ``schema.py`` checks its
    values. Because the table fills a missing key with ``None``, every row of one
    table holds the same columns in the same order, which is what lets a case
    freeze its values as one mapping.

    Attributes:
        values: The ``(column_name, value)`` pairs in the document's column order.
            A tuple rather than a mapping so that the order is explicit and the
            immutability of the row is real.
        line: The 1-based line the row starts on when the format provides one, or
            ``None`` for a format that has no per-row line notion. It is the real
            line in the file, never the row offset.
        row_index: The 0-based index of the row among the data rows, which is
            what a case id is built from (AAP 0.7.2).
    """

    values: tuple[tuple[str, object], ...]
    line: int | None
    row_index: int

    def as_mapping(self) -> dict[str, object]:
        """Return the row's values as a fresh mapping, in the row's column order.

        A case freezes its values as a mapping, so the conversion happens here
        once instead of at every call site. The dict is new on each call: a caller
        may keep or freeze it without affecting the row it came from.

        Returns:
            A dict whose keys are the column names in the order the row holds
            them, and whose values are the row's values.
        """
        return dict(self.values)


@dataclass(frozen=True, slots=True)
class LoadedTable:
    """The rows of a tabular document, with the column order they all share.

    Columns are the ordered union of every row's keys in first-appearance order
    (see :func:`build_table`), so the table is rectangular and both downstream
    consumers can rely on ``columns`` to name each row's values.

    Attributes:
        path: The data file the table was read from; every error names it.
        columns: The column names, in the order a reader first met them: the
            header order for CSV, the union of the row keys in first-appearance
            order for JSON, YAML and JSON Lines. A header-only CSV still carries
            its column names while ``rows`` is empty.
        rows: One :class:`RawRow` per data row, in file order, or an empty tuple
            for a document with no data rows -- the state a caller reports as
            ``no cases in <file>`` rather than as a fault (AAP 0.4.7).
    """

    path: Path
    columns: tuple[str, ...]
    rows: tuple[RawRow, ...]


@dataclass(frozen=True, slots=True)
class LoadedMatrix:
    """A matrix document: a mapping whose every value is one axis of values.

    Expansion belongs to ``matrix.py``; this shape only carries what a reader
    established. Axes arrive sorted by name so that the expander never
    sorts again and every worker of a sharded run expands the same cross-product
    in the same order (implicit requirement I6).

    Attributes:
        path: The data file the matrix was read from; every error names it.
        axes: The ``(axis_name, values)`` pairs sorted by axis name. Each axis
            keeps the order the document wrote, and its values are neither
            de-duplicated nor sorted, because a matrix is the cross-product of
            exactly what the author listed.
    """

    path: Path
    axes: tuple[tuple[str, tuple[object, ...]], ...]


LoadedDocument: TypeAlias = LoadedTable | LoadedMatrix
"""The document a reader returns, for a caller that only needs "a data document".

:meth:`Loader.load`, ``registry.load_document`` and a reader's own return
annotation share this one alias, so a function that accepts a table and a matrix
without caring which it got does not repeat ``LoadedTable | LoadedMatrix``.
"""

# --- The single error site -------------------------------------------------


def data_error(
    path: Path,
    message: str,
    *,
    line: int | None = None,
    column: str | None = None,
) -> NoReturn:
    """Raise the data package's only ``DataError``, naming the file and the location.

    The message takes one of four shapes -- ``<path>:<line>: <message> (column '<column>')`` -- with
    the line or the column part omitted when it is unknown; ``file``, ``line`` and ``column`` carry
    the same context in structured form, so a handler branches on an attribute, not on the text.

    Raises:
        DataError: Always, carrying the composed message and the location.
    """
    if line is not None and column is not None:
        composed = f"{path}:{line}: {message} (column {column!r})"
    elif line is not None:
        composed = f"{path}:{line}: {message}"
    elif column is not None:
        composed = f"{path}: {message} (column {column!r})"
    else:
        composed = f"{path}: {message}"
    raise DataError(composed, file=path, line=line, column=column)


# --- Shared construction rules ---------------------------------------------


def build_table(
    path: Path,
    rows: Sequence[tuple[Mapping[str, object], int | None]],
) -> LoadedTable:
    """Build a table from row mappings, taking the column order from first appearance.

    Columns are the union of every row's keys in first-appearance order, never a set, and every row
    carries every column with ``None`` for a key its mapping lacks, so the table is rectangular.

    Raises:
        DataError: When a mapping has a non-string key, which cannot become a column name.
    """
    columns: list[str] = []
    seen: set[str] = set()
    for row_index, (mapping, line) in enumerate(rows):
        for key in mapping:
            if not isinstance(key, str):
                data_error(
                    path,
                    f"row {row_index} has a non-string key {key!r}; data columns must be strings",
                    line=line,
                )
            if key not in seen:
                seen.add(key)
                columns.append(key)
    raw_rows = [
        RawRow(
            values=tuple((name, mapping.get(name)) for name in columns),
            line=line,
            row_index=row_index,
        )
        for row_index, (mapping, line) in enumerate(rows)
    ]
    return LoadedTable(path=path, columns=tuple(columns), rows=tuple(raw_rows))


def _axis_name(axis: tuple[str, tuple[object, ...]]) -> str:
    """Return the name of one matrix axis, which is the key axes are sorted by.

    Args:
        axis: One ``(axis_name, values)`` pair of a matrix document.

    Returns:
        The axis name, so that ``sorted`` orders the axes by name rather than by
        the order the document happened to write them in.
    """
    return axis[0]


def classify_document(payload: object, path: Path) -> LoadedDocument:
    """Build the document a parsed data file holds, applying the shape rule once.

    A list of mappings is a table of records; an empty list or mapping is the empty-document state a
    caller reports as ``no cases in <file>`` (AAP 0.4.7); a mapping whose every value is a list is a
    matrix, its axes sorted by name here; a mapping mixing list and non-list values, and any other
    payload, are faults.

    Returns:
        A :class:`LoadedTable`, or a :class:`LoadedMatrix` for the all-lists mapping.

    Raises:
        DataError: For a payload no rule covers, for a list element that is not a mapping, and for a
            matrix key that is not a ``str``.
    """
    if isinstance(payload, list):
        if not payload:
            return LoadedTable(path=path, columns=(), rows=())
        table_rows: list[tuple[Mapping[str, object], int | None]] = []
        for index, element in enumerate(payload):
            if not isinstance(element, Mapping):
                data_error(path, f"element {index} is {type(element).__name__}, expected a mapping")
            table_rows.append((element, None))
        return build_table(path, table_rows)

    if isinstance(payload, Mapping):
        if not payload:
            return LoadedTable(path=path, columns=(), rows=())
        list_axes: list[tuple[str, tuple[object, ...]]] = []
        single_values: list[tuple[str, object]] = []
        for key, value in payload.items():
            if not isinstance(key, str):
                data_error(path, f"non-string key {key!r}; data columns must be strings")
            if isinstance(value, list):
                list_axes.append((key, tuple(value)))
            else:
                single_values.append((key, value))
        if not single_values:
            return LoadedMatrix(path=path, axes=tuple(sorted(list_axes, key=_axis_name)))
        if list_axes:
            axis_name = list_axes[0][0]
            other_value = single_values[0][1]
            data_error(
                path,
                f"mapping mixes list axis {axis_name!r} with a non-list value "
                f"of type {type(other_value).__name__}; a matrix needs every value to be a list",
            )
        return build_table(path, [(payload, None)])

    data_error(path, f"expected a list of mappings or a mapping, got {type(payload).__name__}")


def check_row_width(
    path: Path,
    columns: tuple[str, ...],
    values: Sequence[object],
    *,
    line: int,
    row_index: int,
) -> None:
    """Check a parsed row's width against the column count its header declares.

    A tabular reader knows the header before it knows any row, and a row with the wrong number of
    values cannot be zipped into named columns without silently dropping or inventing one. The
    check names the row by its line and by its 0-based index, because a reader that skips blank or
    comment lines cannot point at the offending row with a line number alone. A row short of the
    header names the first header column it never filled -- the column the author left out -- which
    is the "data row missing a required column" state AAP 0.4.7 reports as a collection error
    naming the file, the line number and the column. A row longer than the header has no absent
    column to name, so its error carries the line and leaves ``DataError.column`` unset.

    Args:
        path: The data file; named by the error.
        columns: The header's column names, whose length is the expected width and
            whose entry at the row's own length is the column a short row omitted.
        values: The parsed values of the current row; fewer of them than the header
            declares means the header's trailing names are the missing ones.
        line: The 1-based line the row starts on; the error carries it.
        row_index: The 0-based index of the row among the data rows; the message
            carries it.

    Returns:
        ``None`` when the row's width matches the header's.

    Raises:
        DataError: When the row holds a different number of values than the header
            declares, carrying the line in every case and naming the first column
            it did not fill when the row is short.
    """
    if len(values) != len(columns):
        # A short row has one identifiable fault location: the first header column it
        # never filled. An over-long row has no single absent column, so it names none.
        column = columns[len(values)] if len(values) < len(columns) else None
        data_error(
            path,
            f"row {row_index} has {len(values)} values but the header declares "
            f"{len(columns)} columns",
            line=line,
            column=column,
        )


# --- The reader protocol ---------------------------------------------------


class Loader(Protocol):
    """The structural contract every format reader in this package satisfies.

    ``registry.py`` holds one implementation per suffix and calls :meth:`load`,
    which is why a reader needs no common base class: the registry imports the four
    ``LOADER`` singletons and seeds its table from the suffixes each one publishes.
    The protocol is structural only -- nothing performs an ``isinstance`` check --
    so it is deliberately not marked ``runtime_checkable``.

    Attributes:
        extensions: The suffixes the reader accepts, lowercase and without a
            leading dot, such as ``("csv",)`` for CSV, and more than one entry
            when a format has several suffixes.
    """

    extensions: tuple[str, ...]

    def load(self, path: Path) -> LoadedDocument:
        """Read the file and return the document it holds.

        Args:
            path: An already-resolved path inside ``data.dir``. Containment is
                ``registry.resolve_data_file``'s responsibility, so a reader never
                resolves, contains or writes anything.

        Returns:
            A :class:`LoadedTable` for a tabular file, or a :class:`LoadedMatrix`
            for a matrix document.

        Raises:
            DataError: When the file cannot be parsed or breaks the shape rule.
        """
        ...
