"""The JSON reader: stdlib ``json`` over array documents and object documents.

A JSON data file is one of the four formats the data engine reads, and this module
turns one into the neutral document shapes of :mod:`foundry.data.loaders.base`. The
document-shape rule is deliberately not re-implemented here: an array of objects is
a table of records, an object whose every value is an array is a matrix of axes, an
object of scalar fields is a single record, and an empty array or object is the
empty document -- all of that is decided once, by
:func:`foundry.data.loaders.base.classify_document`, so JSON and YAML cannot drift
apart.

What is specific to this reader is turning bytes into a payload. The file is read
as UTF-8, ``json.loads`` parses it, and each of the three ways that can fail -- an
unreadable file, a file that is not valid UTF-8, a document that is not valid JSON
-- is translated into exactly one ``DataError`` naming the file. A JSON syntax
error therefore reaches a user as a located data fault rather than as a builtin
``JSONDecodeError`` (AAP 0.4.7, AAP 0.14.3), and that translation is the whole of
this module's error handling: :meth:`JsonLoader.load` raises ``DataError`` and
nothing else.

Two consequences of the format matter to a caller. A parsed JSON document carries
no per-element line notion, so every row of a table produced here holds ``line=None``;
and the payload's own order is preserved throughout -- no key is sorted, renamed or
defaulted -- so two runs and two sharding workers derive the same columns in the
same order (implicit requirement I6).

JSON Lines is a different format with its own reader
(:mod:`foundry.data.loaders.jsonl`), and schema strictness belongs to
``foundry.data.schema``; neither is this module's concern. Importing it defines
names only: no file is opened, no logger is configured and no state is created
(AAP 0.4.8).
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
                JSON, or holds a document the shape rule rejects. A syntax error
                reports its 1-based line in ``DataError.line`` and its 1-based
                character offset in the message, because ``DataError.column`` is
                reserved for the name of a column rather than for a position.
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

        return classify_document(payload, path)


# registry.py imports this singleton to seed its loader table from `extensions`.
LOADER: Loader = JsonLoader()
