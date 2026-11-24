"""The neutral document shapes the four format readers share.

A data file reaches :mod:`foundry.data` as one of two in-memory documents: a
:class:`LoadedTable` for anything that reads as rows of named columns (CSV, JSON
records, YAML records, JSON Lines) and a :class:`LoadedMatrix` for a mapping
whose values are axes. Neither shape records which format produced it, so the
four readers stay independent of one another and the case builder, the matrix
expander and the schema checker downstream depend on these shapes rather than on
a parser.

The dependency direction is acyclic: this module imports nothing from the package
that contains it, the four readers import it, and ``foundry.data.registry``
imports the readers -- ``base`` <- {``csv``, ``json``, ``yaml``, ``jsonl``} <-
``foundry.data.registry`` (AAP 0.4.8). Each reader module exposes exactly one
module-level object named ``LOADER`` typed as :class:`Loader` and publishes the
suffixes it accepts, and the registry seeds its table from those four objects.
Everything the readers share lives here -- the two document shapes, the
column-union rule of :func:`build_table`, the shape rule of
:func:`classify_document`, the ragged-row check of :func:`check_row_width` and
the error helper -- so that no reader imports another.

:func:`data_error` is the package's only ``DataError`` construction site, which is
what gives every data fault its file, line and column on the way out
(AAP 0.14.3, AAP 0.4.7).

Importing this module does no work beyond defining names (AAP 0.4.8): the
standard library plus ``DataError`` are its whole import list, no state is
created, no file is opened and no logger is configured.
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
    """Raise the data package's only ``DataError``, naming the file it came from.

    Every data fault -- a non-string column, a ragged row, a mapping that is half
    matrix, an extension no reader handles -- is reported through this function,
    so the package constructs a ``DataError`` in exactly one place and every
    message names the file it is in. That single site is also the seam a caller
    adjusts if the exception's constructor ever changes shape; no caller passes
    the location keywords itself.

    The composed message takes one of four shapes, chosen by which parts of the
    location are known, and every one of them starts with the file:

    * line and column: ``<path>:<line>: <message> (column '<column>')``
    * line only: ``<path>:<line>: <message>``
    * column only: ``<path>: <message> (column '<column>')``
    * neither: ``<path>: <message>``

    ``DataError.file``, ``DataError.line`` and ``DataError.column`` carry the same
    context in structured form, so a handler branches on an attribute instead of
    parsing the text, while ``str()`` renders the message alone.

    Args:
        path: The data file the fault is in; it always appears in the message.
        message: What is wrong, phrased without the location, because this
            function supplies it.
        line: The 1-based line the fault is on, or ``None`` when it concerns the
            file as a whole.
        column: The *name* of the column at fault, or ``None``. It is never a
            character offset: when a parser knows a character position in a syntax
            error, that belongs in ``message``.

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

    The column set is the union of every row's keys, ordered by where each key is
    first seen: rows in document order, and the keys of one row in that row's own
    order. A later row that introduces a column appends it. No set is ever
    iterated, so two runs and two shards derive the same ``columns`` tuple
    (implicit requirement I6). This is the single implementation of that rule: the
    JSON and YAML readers reach it through :func:`classify_document`, and the JSON
    Lines reader calls it directly because it builds its rows one line at a time.

    Every produced row carries every column, with ``None`` for a key its own
    mapping lacks, so ``schema.py`` and ``cases.py`` see a rectangular shape
    instead of a ragged one and a schema can tell an absent column from a column
    whose value is null.

    Args:
        path: The data file the rows came from; carried on the table and named by
            every error.
        rows: One ``(mapping, line)`` pair per data row, in file order. Pairing a
            row with its line keeps the two aligned when a reader skips blank or
            comment lines. ``line`` is the 1-based line in the file, or ``None``
            for a format with no per-row line notion.

    Returns:
        The table, whose ``columns`` follow first appearance, whose ``rows`` hold
        one :class:`RawRow` per input mapping with ``row_index`` equal to that
        mapping's 0-based position, and whose ``rows`` is empty when the input
        holds no mappings.

    Raises:
        DataError: When a mapping has a non-string key. A data column is a column
            name, so a key that is not a ``str`` cannot become one.
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

    The rules below are applied in this order and no other, and every branch
    either returns a document or reports a fault through :func:`data_error`; no
    builtin exception escapes.

    1. A ``list`` is a table of records, one row per element. An empty list is the
       empty-document state -- a :class:`LoadedTable` with no columns and no rows
       -- which a caller reports as ``no cases in <file>`` rather than as a fault
       (AAP 0.4.7). Each element must be a mapping: a scalar element reports
       ``element <i> is <type>, expected a mapping``, and a mapping element
       contributes one row with ``line=None`` because a parsed JSON or YAML
       document carries no per-element line notion.
    2. An empty ``Mapping`` is the same empty-document state as ``[]``.
    3. A non-empty ``Mapping`` whose every value is a ``list`` is a matrix. Its
       keys are column-style names, so a key that is not a ``str`` is a fault, and
       its axes are sorted by name here, once, so that ``matrix.py`` expands a
       stable cross-product on every worker (implicit requirement I6). Axis values
       keep the document's order and are neither de-duplicated nor sorted.
    4. A non-empty ``Mapping`` whose no value is a ``list`` is a single record:
       one row, ``row_index`` 0, columns in key order.
    5. A ``Mapping`` that mixes list and non-list values is a fault. An author who
       wrote one list axis meant a matrix, so reading that mapping as a single row
       holding a list cell would hide the mistake; the message names the axis and
       the type of the non-list value that broke the rule instead, and never the
       value itself, which is ``foundry.data.schema``'s rule for the messages it
       reports.
    6. Anything else -- a scalar, ``None``, any type no rule above covers -- reports
       ``expected a list of mappings or a mapping, got <type>``.

    Args:
        payload: The value a reader produced by parsing the file: a JSON or YAML
            parse result, or a list of parsed rows.
        path: The data file the payload came from; carried on the returned
            document and named by every error.

    Returns:
        A :class:`LoadedTable` for rules 1, 2 and 4, or a :class:`LoadedMatrix` for
        rule 3.

    Raises:
        DataError: For rules 5 and 6, for a list element that is not a mapping, and
            for a matrix key that is not a ``str``.
    """
    if isinstance(payload, list):
        if not payload:
            return LoadedTable(path=path, columns=(), rows=())
        table_rows: list[tuple[Mapping[str, object], int | None]] = []
        for index, element in enumerate(payload):
            if not isinstance(element, Mapping):
                data_error(
                    path,
                    f"element {index} is {type(element).__name__}, expected a mapping",
                )
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

    data_error(
        path,
        f"expected a list of mappings or a mapping, got {type(payload).__name__}",
    )


def check_row_width(
    path: Path,
    columns: tuple[str, ...],
    values: Sequence[object],
    *,
    line: int,
    row_index: int,
) -> None:
    """Check a parsed row's width against the column count its header declares.

    A tabular reader knows the header before it knows any row, and a row with the
    wrong number of values cannot be zipped into named columns without silently
    dropping or inventing one. The check names the row by its line and by its
    0-based index, because a reader that skips blank or comment lines cannot point
    at the offending row with a line number alone.

    Args:
        path: The data file; named by the error.
        columns: The header's column names, whose length is the expected width.
        values: The parsed values of the current row.
        line: The 1-based line the row starts on; the error carries it.
        row_index: The 0-based index of the row among the data rows; the message
            carries it.

    Returns:
        ``None`` when the row's width matches the header's.

    Raises:
        DataError: When the row holds a different number of values than the header
            declares.
    """
    if len(values) != len(columns):
        data_error(
            path,
            f"row {row_index} has {len(values)} values but the header declares "
            f"{len(columns)} columns",
            line=line,
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
