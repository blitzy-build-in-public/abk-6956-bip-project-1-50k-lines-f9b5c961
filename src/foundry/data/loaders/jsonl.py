"""The JSON Lines reader: one JSON object per line, one table row per object.

JSON Lines is the one data format whose every record has a real line in the file, so
this is the reader that can point a collection error at the exact line that broke a
case (AAP 0.4.7). A file holds one JSON object per physical line, and every non-blank
line contributes exactly one row to the table this module returns.

Two rules follow from that, and they are why the reader lives apart from ``json.py``:

* A line that is empty or whitespace-only is skipped without consuming a record, and
  skipping never renumbers: the record after a blank line keeps its true 1-based
  physical line, which is what makes ``DataError.line`` name the line a person sees in
  an editor.
* A JSON Lines file is always tabular. A line that parses to a list, a scalar or
  ``null`` is a fault naming that line rather than a matrix or a single record, because
  a JSON Lines file that needs either shape is a modelling error its author should meet
  at collection time and not as a surprising case set.

The column order comes from :func:`build_table` in the loader base -- the same
implementation the CSV, JSON and YAML readers reach -- so the first record's keys come
first, a key a later line introduces is appended, a missing key becomes ``None``, and
two runs or two shards derive the same columns without iterating a set (implicit
requirement I6).

Only :class:`foundry.core.exceptions.DataError` escapes :meth:`JsonlLoader.load`, and
it is raised exclusively through :func:`data_error` in the loader base: the read, the
decode and the per-line parse each translate their own builtin exception once, at the
boundary of ``load`` (AAP 0.14.3). That includes a line nested too deeply to parse,
which is reported against its own physical line rather than as an escaping builtin.

Importing this module defines names and does nothing else -- the standard library plus
the loader base, no I/O, no environment read, no logger configuration, and no state
beyond the ``LOADER`` singleton (AAP 0.4.8, implicit requirement I5).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from foundry.data.loaders.base import (
    LoadedTable,
    Loader,
    build_table,
    data_error,
)

__all__: list[str] = ["LOADER", "JsonlLoader"]


class JsonlLoader:
    """Read a JSON Lines file as a table whose rows keep their real line numbers.

    The class satisfies :class:`Loader` structurally, which is all ``registry.py``
    needs: it holds one instance per suffix and calls :meth:`load` through that
    protocol.

    Attributes:
        extensions: The suffixes this reader answers for, lowercase and without a
            leading dot, which is what the registry seeds its lookup table from.
    """

    extensions: tuple[str, ...] = ("jsonl",)

    def load(self, path: Path) -> LoadedTable:
        """Read the file and return one row per non-blank line, in file order.

        Args:
            path: The data file to read. ``registry.resolve_data_file`` has already
                resolved it and proved it sits inside ``data.dir``, so this method
                neither resolves nor contains anything.

        Returns:
            A :class:`LoadedTable` holding one :class:`RawRow` per non-blank line, in
            file order: its ``line`` is that line's true 1-based physical line and its
            ``row_index`` its 0-based position among the emitted rows. ``columns`` is
            the union of the documents' keys in first-appearance order. A file with no
            non-blank line yields the empty table, which a caller reports as
            ``no cases in <file>`` rather than as a fault (AAP 0.4.7).

        Raises:
            DataError: When the file cannot be read, when its bytes are not valid
                UTF-8, when a line is not valid JSON, when a line is nested too deeply
                to parse, or when a line's value is not a JSON object. Every case names
                the file; the per-line cases name the line as well.
        """
        try:
            # The whole file is decoded in one call instead of through a line iterator:
            # an iterator raises UnicodeDecodeError from inside the loop, where the
            # translation below could not reach it, so one read keeps the translation at
            # a single boundary.
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            data_error(path, f"file is not valid UTF-8: {exc}")
        except OSError as exc:
            data_error(path, f"cannot read data file: {exc.strerror or exc}")

        rows: list[tuple[Mapping[str, object], int | None]] = []
        # splitlines() honours "\n", "\r\n" and "\r" alike, so a file written on another
        # platform reports the line numbers a POSIX reader would, and the counter starts
        # at 1 because a line number is what a person reads in an editor.
        for offset, raw_line in enumerate(text.splitlines(), start=1):
            stripped = raw_line.strip()
            if not stripped:
                # A blank or whitespace-only line carries no record, and skipping it
                # does not shift the numbering of the records that follow.
                continue
            try:
                document = json.loads(stripped)
            except json.JSONDecodeError as exc:
                # exc.lineno is always 1 here, because the line was parsed on its own,
                # so the physical offset is the line to report. The parser's character
                # position stays in the message rather than moving to DataError.column,
                # which carries the name of a column and never a character offset.
                data_error(
                    path,
                    f"invalid JSON at line {offset} column {exc.colno}: {exc.msg}",
                    line=offset,
                )
            except RecursionError:
                # A line nested past the interpreter's stack limit fails inside the parser
                # with no position of its own, so the physical line this loop is on is the
                # one to report; raising here keeps the original error chained as context.
                data_error(path, f"line {offset} is nested too deeply to parse", line=offset)
            if not isinstance(document, Mapping):
                # Always a table, never a matrix: a list on a line is not an axis set and
                # a scalar is not a single record, so each reports the type it found.
                data_error(
                    path,
                    f"line {offset} is {type(document).__name__}, expected a JSON object",
                    line=offset,
                )
            rows.append((document, offset))

        # build_table owns the column-union rule, and an empty sequence produces the
        # empty table, so a blank-only or zero-byte file needs no separate path here.
        return build_table(path, rows)


# registry.py seeds its suffix table from this singleton.
LOADER: Loader = JsonlLoader()
