"""The CSV reader of the data engine, built on the standard library's ``csv`` module (AAP 0.4.7).

A row's ``line`` is the physical line its record starts on; every fault names file and line.
"""

from __future__ import annotations

import csv
from pathlib import Path

from foundry.data.loaders.base import LoadedTable, Loader, RawRow, check_row_width, data_error

__all__: list[str] = ["LOADER", "CsvLoader"]


def _validate_header(path: Path, header: list[str]) -> tuple[str, ...]:
    """Check a CSV header and return it as the table's column tuple.

    A CSV column is addressed by its header name, so a header that names no column or names one
    column twice cannot be turned into a table: a schema could not refer to the column and a case's
    values would silently lose the later duplicate. Both faults are reported as a located
    ``DataError`` at line 1 -- the only line a header can be on -- and the header is otherwise kept
    exactly as written, because stripping or normalising a name would hide an authoring mistake the
    error can name instead.

    Args:
        path: The data file the header was read from; named by the error.
        header: The header cells as the reader produced them, in file order.

    Returns:
        The column names, in header order, as the tuple a :class:`LoadedTable` carries.

    Raises:
        DataError: When a header cell is empty, or when a name repeats an earlier one.
    """
    seen: set[str] = set()
    for index, name in enumerate(header):
        if not name:
            data_error(
                path,
                f"header at line 1 has an empty column name at position {index + 1}",
                line=1,
                column=f"#{index + 1}",
            )
        if name in seen:
            data_error(path, f"duplicate header {name!r}", line=1, column=name)
        seen.add(name)
    return tuple(header)


class CsvLoader:
    """Read a comma-separated data file into a table of named columns.

    The class holds no state of its own: one instance serves every file of a run, which is why
    :data:`LOADER` can be a single module-level object rather than one per read.

    Attributes:
        extensions: The suffix this reader claims, lowercase and without a leading dot:
            ``("csv",)``. :mod:`foundry.data.registry` maps that suffix onto :data:`LOADER`, so
            the tuple belongs to the loader contract rather than to an implementation detail.
    """

    extensions: tuple[str, ...] = ("csv",)

    def load(self, path: Path) -> LoadedTable:
        """Read ``path`` and return its header and data rows as a loaded table.

        The header line supplies ``columns`` in file order. Every following non-blank line is one
        data row, in file order, whose ``row_index`` counts the *emitted* rows from 0 -- contiguous,
        so the ``<stem>-<row_index>`` case ids built by ``cases.py`` are stable across runs and
        workers (AAP 0.7.2) -- and whose ``line`` is the 1-based physical line the record starts on,
        matching ``RawRow.line`` and ``Case.line``. Three states are not faults: a file with no
        lines returns a table with no columns and no rows, a file whose only line is the header
        returns the columns with no rows -- both the ``no cases in <file>`` state a caller reports
        rather than an error (AAP 0.4.7) -- and a blank line between records is skipped without
        becoming a row, so a hand-edited file keeps working while the row indices stay contiguous. A
        row of empty strings is *not* skipped: its values can fail a schema.

        Args:
            path: An already-resolved path inside ``data.dir``. This reader resolves nothing,
                contains nothing and writes nothing -- a path escaping the data directory is
                the registry's concern, not this method's.

        Returns:
            The table holding the header's column names and one :class:`RawRow` per data row.

        Raises:
            DataError: When the file cannot be opened or read, is not valid UTF-8, is not valid
                CSV, has a header that is empty or duplicated, or has a row whose width differs
                from the header's.
        """
        rows: list[RawRow] = []
        columns: tuple[str, ...] = ()
        row_index = 0
        current_line: int | None = None
        try:
            # UTF-8 is stated rather than inherited from the locale, and newline="" is
            # mandatory: the csv module parses line endings itself, so the text layer's newline
            # translation would corrupt a CRLF embedded inside a quoted field.
            with path.open("r", encoding="utf-8", newline="") as handle:
                reader = csv.reader(handle)
                # ``line_num`` counts the physical lines consumed so far, so the record about to
                # be read starts on the line after it. Seeding the tracker from the header read
                # keeps the first data row right when the header itself spans several lines, and
                # refreshing it after every record keeps blank lines and quoted newlines aligned.
                try:
                    header = next(reader)
                except StopIteration:
                    return LoadedTable(path=path, columns=(), rows=())
                previous_line = reader.line_num
                columns = _validate_header(path, header)
                while True:
                    line = previous_line + 1
                    current_line = line
                    try:
                        row = next(reader)
                    except StopIteration:
                        break
                    previous_line = reader.line_num
                    if not row:
                        continue
                    check_row_width(path, columns, tuple(row), line=line, row_index=row_index)
                    rows.append(
                        RawRow(
                            values=tuple(zip(columns, row, strict=True)),
                            line=line,
                            row_index=row_index,
                        )
                    )
                    row_index += 1
        except UnicodeDecodeError as exc:
            data_error(path, f"file is not valid UTF-8: {exc}")
        except csv.Error as exc:
            data_error(path, f"invalid CSV: {exc}", line=current_line)
        except OSError as exc:
            data_error(path, f"cannot read data file: {exc.strerror or exc}")
        return LoadedTable(path=path, columns=columns, rows=tuple(rows))


# The registry imports this symbol to seed its loader table from the suffix it publishes.
LOADER: Loader = CsvLoader()
