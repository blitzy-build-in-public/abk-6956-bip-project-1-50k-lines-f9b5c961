"""The YAML data reader, which reaches PyYAML only through ``yaml.safe_load``.

A ``.yaml`` or ``.yml`` file inside ``data.dir`` reaches this module through
``foundry.data.registry``, which seeds its suffix table from :data:`LOADER`. The file is
read as UTF-8 text and handed to ``yaml.safe_load``, and the parsed value is classified
into one of the two neutral shapes :mod:`foundry.data.loaders.base` defines: a list of
mappings and a mapping of scalars become a :class:`LoadedTable`, a mapping whose every
value is a list becomes a :class:`LoadedMatrix`, and anything else is a fault. The shape
rules are not restated here -- :func:`base.classify_document` owns them, and this module
only hands it a parsed payload.

Two of this module's properties are security requirements rather than style choices
(AAP 0.3.2 "Deserialisation safety", AAP 0.14.3 "No unsafe deserialisation"):

* ``yaml.safe_load`` is the only YAML loader this module names. The tree's other
  ``safe_load`` site is ``foundry.contract.schema``, which decodes schema documents the
  same way; no unchecked loader is named in either, so a document can never construct a
  Python object, and an explicitly unsafe tag such as ``!!python/object:...`` raises a
  ``YAMLError``, which this module reports as a :class:`DataError` like any other syntax
  fault.
* ``import yaml`` sits inside :meth:`YamlLoader.load` rather than at module scope, because
  ``foundry.plugin`` imports :mod:`foundry.data` during ``pytest_configure`` and a suite
  that reads no data files must not pay for PyYAML (implicit requirement I5, AAP 0.14.2's
  150 ms startup budget). Importing this module therefore imports neither ``yaml`` nor any
  of PyYAML's dependencies.

The empty document is the one classification this module adds to the parser's output.
``yaml.safe_load`` returns ``None`` for a blank file, a comments-only file and a bare
``---``, and all three mean the file holds no cases: they map to a :class:`LoadedTable`
with no columns and no rows, which the caller reports as ``no cases in <file>`` rather
than as a fault, exactly as a header-only CSV does (AAP 0.4.7). Every other payload keeps
its own meaning, so a top-level scalar such as ``42`` is still a fault.

Only :class:`foundry.core.exceptions.DataError` escapes :meth:`YamlLoader.load`. The four
faults a reader can meet -- an unreadable file, a file that is not UTF-8, a document PyYAML
rejects, and a document nested too deeply to parse -- are each caught once at that boundary
and translated through :func:`base.data_error`, which names the file, the 1-based line and,
when the fault is about a column, the column's name. Importing this module has no effect
beyond defining names: no file is opened, no state is written and no logger is configured
(AAP 0.4.8).
"""

from __future__ import annotations

from pathlib import Path

from foundry.data.loaders.base import (
    LoadedMatrix,
    LoadedTable,
    Loader,
    classify_document,
    data_error,
)

__all__: list[str] = ["LOADER", "YamlLoader"]


class YamlLoader:
    """Read a YAML data file into one of the two neutral document shapes.

    The class holds no state beyond the two suffixes it accepts, which is what makes
    :data:`LOADER` a singleton the registry builds once at import: a reader is a pure
    function of the file it is given, so two workers reading the same file derive the same
    document and a sharded run collects the same cases as a serial one (implicit
    requirement I6).

    Both suffixes run through one code path, because PyYAML does not choose a parser by
    suffix. A record read from a YAML document carries ``line=None``: the parser reports a
    position for a syntax fault but not for each mapping it built, so the row's real line
    is unknown and :class:`base.RawRow` records that honestly instead of inventing one --
    the case id then comes from the row's index rather than from a line (AAP 0.7.2).

    Attributes:
        extensions: The suffixes this reader accepts, lowercase and without a leading dot,
            in the order the registry documents them.
    """

    extensions: tuple[str, ...] = ("yaml", "yml")

    def load(self, path: Path) -> LoadedTable | LoadedMatrix:
        """Read the file's YAML document and classify it into a document shape.

        The file is read as UTF-8 and parsed with ``yaml.safe_load``, so a document
        carrying a Python-object tag is a syntax fault rather than code execution. A
        ``None`` payload -- a blank file, a comments-only file or a bare ``---`` -- is the
        empty document and returns a table with no columns and no rows; every other
        payload is handed to :func:`base.classify_document` unchanged, because that
        function is the single place the list-of-records, matrix, single-record and
        mixed-mapping rules live.

        Args:
            path: The data file to read, already resolved and contained inside
                ``data.dir``. It is carried on the returned document and named by every
                error, and it is never resolved, created or written here.

        Returns:
            A :class:`LoadedTable` for a list of records, a mapping of scalars or the
            empty document, and a :class:`LoadedMatrix` for a mapping whose every value is
            a list, with its axes sorted by name.

        Raises:
            DataError: When the file cannot be read at all, when its bytes are not valid
                UTF-8, when PyYAML rejects the document -- a syntax fault, a multi-document
                stream and an unsafe tag all arrive that way -- when the document is nested
                too deeply to parse, which reports the file alone because the parser stops
                without a position, or when the parsed payload breaks the shape rule.
        """
        # Imported here, not at module scope: a suite that reads no data files must not
        # pay for PyYAML at pytest startup (implicit requirement I5, AAP 0.14.2).
        import yaml

        text: str
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            data_error(path, f"file is not valid UTF-8: {exc}")
        except OSError as exc:
            data_error(path, f"cannot read data file: {exc.strerror or exc}")

        payload: object
        try:
            payload = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            # A YAMLError carries no typed location, so the mark and the problem text are
            # read defensively: both are attributes of the concrete subclass rather than
            # of the base class, and a subclass may omit either.
            mark: object = getattr(exc, "problem_mark", None)
            line: int | None = None
            offset: int | None = None
            if isinstance(mark, yaml.Mark):
                # PyYAML counts from zero; a line in a message counts from one.
                line = mark.line + 1
                offset = mark.column + 1
            problem: object = getattr(exc, "problem", None)
            detail: str
            if isinstance(problem, str) and problem.strip():
                detail = problem.strip()
            else:
                detail = str(exc).strip() or "unknown error"
            if line is not None and offset is not None:
                message = f"invalid YAML at line {line} column {offset}: {detail}"
            elif line is not None:
                message = f"invalid YAML at line {line}: {detail}"
            else:
                message = f"invalid YAML: {detail}"
            # The character offset belongs in the message: DataError.column is a column
            # *name*, and a load fault has no column name to give it (AAP 0.4.7).
            data_error(path, message, line=line)
        except RecursionError:
            # A document nested past the interpreter's stack limit exhausts the stack
            # inside the parser, which builds no mark to report; the file alone locates
            # the fault, and raising here keeps the original error chained as its context.
            data_error(path, "document is nested too deeply to parse")

        if payload is None:
            return LoadedTable(path=path, columns=(), rows=())
        return classify_document(payload, path)


# The registry seeds its suffix table from this reader (foundry.data.registry).
LOADER: Loader = YamlLoader()
