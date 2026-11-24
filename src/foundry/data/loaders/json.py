"""The JSON reader: stdlib ``json`` over array documents and object documents (AAP 0.4.7).

Shapes come from ``classify_document``; a parsed document has no per-element line notion.
"""

from __future__ import annotations

import json
from pathlib import Path

from foundry.data.loaders.base import (
    LoadedMatrix,
    LoadedTable,
    Loader,
    classify_document,
    data_error,
)

__all__: list[str] = ["LOADER", "JsonLoader"]


class JsonLoader:
    """Read a JSON data file into a table of records or a matrix of axes.

    One instance is published as :data:`LOADER`, which ``foundry.data.registry``
    seeds its loader table from using :attr:`extensions`. The class carries no state
    beyond that suffix, so the shared instance is safe to reuse and a caller may
    build another without consequence.

    Attributes:
        extensions: The single suffix this reader claims, lowercase and without a
            leading dot, so ``cases.json`` is routed here while ``cases.jsonl`` is
            left to the JSON Lines reader.
    """

    extensions: tuple[str, ...] = ("json",)

    def load(self, path: Path) -> LoadedTable | LoadedMatrix:
        """Parse the JSON file at ``path`` and return the document it holds.

        The return type is the union rather than the ``LoadedDocument`` alias so
        that a caller can branch on the shape it received: a table for an array of
        records or for an object of scalar fields, a matrix for an object whose
        every value is an array. Which of the two the payload becomes is
        :func:`foundry.data.loaders.base.classify_document`'s decision and is not
        repeated here.

        Args:
            path: An already-resolved path inside ``data.dir``. Locating the file
                within that directory is the registry's responsibility, so this
                reader neither resolves nor contains the path.

        Returns:
            The :class:`~foundry.data.loaders.base.LoadedTable` or
            :class:`~foundry.data.loaders.base.LoadedMatrix` the document describes.
            A table produced here holds ``line=None`` on every row, because a parsed
            JSON document has no per-element line to report.

        Raises:
            DataError: When the file cannot be read, is not valid UTF-8, is not valid
                JSON, is nested too deeply to parse, or holds a document the shape rule
                rejects. A syntax error reports its 1-based line in ``DataError.line``
                and its 1-based character offset in the message, because
                ``DataError.column`` is reserved for the name of a column rather than
                for a position; a document nested past the parser's stack carries no
                position at all, so the file name alone locates it.
        """
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            data_error(path, f"file is not valid UTF-8: {exc}")
        except OSError as exc:
            data_error(path, f"cannot read data file: {exc.strerror or exc}")

        # Typed ``object`` so no ``Any`` from the parser reaches ``classify_document``.
        payload: object
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            data_error(
                path,
                f"invalid JSON at line {exc.lineno} column {exc.colno}: {exc.msg}",
                line=exc.lineno,
            )
        except RecursionError:
            # A document nested past the interpreter's stack limit fails inside the
            # parser, which reaches no position to report; the file alone locates the
            # fault, and raising here keeps the original error chained as its context.
            data_error(path, "document is nested too deeply to parse")

        return classify_document(payload, path)


# registry.py imports this singleton to seed its loader table from `extensions`.
LOADER: Loader = JsonLoader()
