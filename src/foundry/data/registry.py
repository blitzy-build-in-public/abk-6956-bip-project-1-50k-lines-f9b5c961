"""The extension table that routes a data file to its reader.

A data file reaches the package as a path, and this module is the single place that decides
which reader parses it: :func:`loader_for` maps a suffix onto one of the shipped readers
(``csv``, ``json``, ``yaml``/``yml``, ``jsonl``), and the loading functions compose the steps
into a document, a case set or an expanded matrix. The composition lives here rather than in
each of those modules because it is what keeps ``cases``, ``matrix`` and ``schema`` free of
imports between one another: the direction stays ``loaders`` <- ``registry`` ->
{``cases``, ``matrix``, ``schema``} (AAP 0.4.8).

The table is one of only two import-time registration tables the tree permits (AAP 0.4.8): the
four shipped readers are registered at import from the ``LOADER`` singleton each of them
publishes, and :func:`register` is the additive extension point a new format uses -- implement
:class:`~foundry.data.loaders.base.Loader` and call it (AAP 0.4.9). Nothing else happens at
import: no file is opened, no directory is scanned, no environment variable is read and no
logger is configured.

Two behaviours are states rather than faults. A file that parses but holds no data rows yields
an empty case set, which :func:`foundry.data.cases.build_parameters` turns into one skipped item
whose reason reads ``no cases in <file>``. A path whose suffix no reader claims is a
:class:`~foundry.core.exceptions.DataError` raised at collection through
:func:`foundry.data.loaders.base.data_error`, naming the extension and listing every supported
one (AAP 0.4.7).

Containment of a data file name is not decided here: :func:`resolve_data_file` delegates it to
:func:`foundry.core.paths.resolve_under`, the tree's single check that raises
:class:`~foundry.core.exceptions.ArtifactError` when a resolved path escapes its root, including
through a symlink (AAP 0.14.3). That error and the ``DataError`` are the only exceptions this
module lets out.

``cases``, ``matrix`` and ``schema`` are imported inside the functions that use them rather than
at module scope: ``foundry.data.cases`` imports pytest (it is the parametrisation adapter) and
the schema engine reaches PyYAML and jsonschema on demand, so this module stays importable
without any of the three and the plugin's startup budget pays nothing for a run that reads no
data file (implicit requirement I5, AAP 0.14.2).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from foundry.core.paths import resolve_under
from foundry.data.loaders.base import LoadedMatrix, LoadedTable, Loader, data_error
from foundry.data.loaders.csv import LOADER as CSV_LOADER
from foundry.data.loaders.json import LOADER as JSON_LOADER
from foundry.data.loaders.jsonl import LOADER as JSONL_LOADER
from foundry.data.loaders.yaml import LOADER as YAML_LOADER

if TYPE_CHECKING:
    from foundry.data.cases import CaseSet
    from foundry.data.matrix import Matrix

__all__ = [
    "SUPPORTED_EXTENSIONS",
    "load_cases",
    "load_document",
    "load_matrix",
    "loader_for",
    "register",
    "resolve_data_file",
    "supported_extensions",
]

SUPPORTED_EXTENSIONS: tuple[str, ...] = ("csv", "json", "jsonl", "yaml", "yml")
"""The suffixes the shipped table registers, sorted.

This is the set :func:`supported_extensions` reports until a caller registers a reader of its
own; it is a fact about this release rather than a closed list, because :func:`register` adds to
the live table (AAP 0.4.9).
"""

# The one mutable module state the package holds (AAP 0.4.8): suffix -> the reader that claims
# it. Insertion order is never relied on -- only :func:`supported_extensions` reads the table
# back out, and it sorts -- so registering the same suffix twice cannot perturb collection.
_LOADERS: dict[str, Loader] = {}


def register(loader: Loader) -> None:
    """Register a reader under every suffix it claims.

    This is the extension point AAP 0.4.9 promises: a new format -- a new delimited-text
    dialect, a Rules table, a spreadsheet export -- is added by implementing
    :class:`~foundry.data.loaders.base.Loader` and calling this function with an instance, after
    which ``.myext`` files reach that reader with no change to the loading functions below.

    Each entry of ``loader.extensions`` is normalised before it is stored: a leading dot is
    removed and the name is lowercased, so a reader that declares ``".CSV"`` and one that
    declares ``"csv"`` address the same files. The last registration for a suffix wins, and a
    re-registration raises nothing: overriding a shipped reader for a suffix is a supported
    thing to do in a project's own test support module, and a raise here would turn it into a
    startup failure (AAP 0.14.3 lists the exceptions this package may produce).

    An entry that normalises to the empty string is ignored rather than stored. A suffix-less
    file is exactly the case :func:`loader_for` reports as unsupported, and a reader claiming
    ``""`` would silently take every such path instead.

    Args:
        loader: The reader to register, whose ``extensions`` names the suffixes it answers for.

    Returns:
        ``None``. The table is module state, and the caller reads it back through
        :func:`supported_extensions` rather than through a return value.
    """
    for extension in loader.extensions:
        normalised = extension.removeprefix(".").lower()
        if not normalised:
            continue
        _LOADERS[normalised] = loader


# The shipped table, built once at import: one line per reader, in the order the package
# documents its formats. This is the whole of the module's import-time work (AAP 0.4.8).
register(CSV_LOADER)
register(JSON_LOADER)
register(YAML_LOADER)
register(JSONL_LOADER)


def supported_extensions() -> tuple[str, ...]:
    """Return the suffixes the table currently answers for, sorted.

    Sorted rather than in registration order so that the value is deterministic across
    processes and independent of the order a project registered its own readers in -- the same
    property every other collection this package emits has (implicit requirement I6). It is also
    what an unsupported-extension error lists, so the message reads the same in every run.

    Returns:
        The live table's keys as a sorted tuple, for example
        ``("csv", "json", "jsonl", "yaml", "yml")``.
    """
    return tuple(sorted(_LOADERS))


def loader_for(path: Path) -> Loader:
    """Return the reader that handles the suffix of ``path``.

    The suffix is matched the way :func:`register` stored it: a leading dot removed and the name
    lowercased, so ``CART.CSV`` and ``cart.csv`` reach the same reader. Only the last suffix is
    considered, which is what lets a sidecar such as ``cart.csv.schema.json`` be addressed by
    its own extension; nothing here opens the file or checks that it exists.

    Args:
        path: The data file whose suffix selects a reader. It may be any path -- the error names
            it -- and containment belongs to :func:`resolve_data_file`.

    Returns:
        The registered reader for that suffix.

    Raises:
        DataError: No reader claims the suffix, including the suffix-less case where the
            extension is the empty string. The message names the extension and lists every
            supported one, because a collection error has to tell the author what would work
            (AAP 0.4.7).
    """
    extension = path.suffix.removeprefix(".").lower()
    loader = _LOADERS.get(extension)
    if loader is None:
        data_error(
            path,
            f"unsupported data file extension '{extension}' for {path}; "
            f"supported extensions: {', '.join(supported_extensions())}",
        )
    return loader


def resolve_data_file(name: str | Path, *, directory: Path) -> Path:
    """Resolve a data file name against ``directory`` and prove it stays inside it.

    A data-driven test names a file, not a path: the name is relative to ``data.dir`` and
    nothing outside it is addressable, so a name carrying ``..`` or an absolute path pointing
    elsewhere is refused rather than read. That containment is
    :func:`foundry.core.paths.resolve_under`'s job -- the tree's single check, which also
    rejects an escape reached through a symlink -- so this function neither joins, normalises
    nor ``..``-strips the name itself (AAP 0.14.3 "No traversal escape").

    Args:
        name: The file to locate, relative to ``directory``. ``cart.csv`` and
            ``nested/cart.csv`` both name a file inside the data directory.
        directory: The resolved ``data.dir`` value the name is resolved under.

    Returns:
        The resolved absolute path of an existing file inside ``directory``.

    Raises:
        ArtifactError: The resolved path is ``directory`` itself or outside it, which is the
            error :func:`foundry.core.paths.resolve_under` raises unchanged.
        DataError: The resolved path exists and is a directory, or does not exist at all.
    """
    resolved = resolve_under(directory, Path(name))
    if resolved.is_dir():
        data_error(resolved, f"data path is a directory, not a file: {resolved}")
    if not resolved.is_file():
        data_error(resolved, f"data file not found: {resolved}")
    return resolved


def load_document(path: Path) -> LoadedTable | LoadedMatrix:
    """Read ``path`` with the reader its suffix selects and return the document it holds.

    The format is decided by :func:`loader_for` and everything format-specific stays in the
    reader, so this function's result is one of the two neutral shapes regardless of whether the
    file was CSV, JSON, YAML or JSON Lines.

    Args:
        path: An already-resolved path, normally one :func:`resolve_data_file` returned. Nothing
            here resolves or contains it.

    Returns:
        A :class:`~foundry.data.loaders.base.LoadedTable` for a file that reads as rows of named
        columns, or a :class:`~foundry.data.loaders.base.LoadedMatrix` for a document whose every
        value is an axis of values.

    Raises:
        DataError: No reader claims the suffix, or the reader rejects the file -- unreadable,
            undecodable, syntactically invalid or breaking the document shape rule.
    """
    return loader_for(path).load(path)


def load_cases(path: Path, *, strict: bool = True, schema_dir: Path | None = None) -> CaseSet:
    """Read ``path`` and return the cases its document expands to.

    This is the composition the rest of the package calls, and the only place it happens: a
    tabular document has its sidecar JSON Schema resolved and enforced row by row before
    :func:`foundry.data.cases.build_case_set` converts the rows, so a rejected row never becomes
    a case; a matrix document expands straight to its cross-product through
    :func:`foundry.data.matrix.expand_matrix`.

    Row-level JSON Schema validation applies to tabular documents in this release. A matrix
    document's axes are expanded as written and are not validated against a schema, because a
    combination is not a row of the file.

    An empty file is a case set rather than a fault: a header-only CSV, an empty JSON array, a
    blank YAML document and a blank-only JSON Lines file all return a set whose ``is_empty`` is
    ``True``, which :func:`foundry.data.cases.build_parameters` turns into one skipped item
    reading ``no cases in <file>`` (AAP 0.4.7).

    Args:
        path: An already-resolved path, normally one :func:`resolve_data_file` returned.
        strict: Whether a column the sidecar schema does not declare is a fault, taken from
            ``data.schema_strict``. A data file that declares no schema is unaffected either way.
        schema_dir: The directory the sidecar schema must stay inside, normally the resolved
            ``data.dir``; ``None`` imposes no containment on the schema document.

    Returns:
        The cases the document expands to, in file order for a table and in expansion order for
        a matrix, together with the columns the document declared.

    Raises:
        DataError: The document cannot be read, breaks its shape rule, or has a row the schema
            rejects -- the last naming the file, the row's line and the column.
        ContractError: The sidecar schema document is unreadable, is not a schema, or references
            a location outside itself.
    """
    from foundry.data.cases import build_case_set
    from foundry.data.matrix import expand_matrix
    from foundry.data.schema import enforce_table, load_row_schema

    document = load_document(path)
    if isinstance(document, LoadedTable):
        row_schema = load_row_schema(path, base_dir=schema_dir)
        enforced = enforce_table(document, strict=strict, schema=row_schema)
        return build_case_set(enforced)
    return expand_matrix(document).to_case_set()


def load_matrix(path: Path) -> Matrix:
    """Read ``path`` as a matrix document and return its expanded cross-product.

    A caller asks for a matrix when the data is axes rather than rows -- a pricing grid, a set of
    credential pairs -- so a document that reads as a table is a modelling mistake and is
    reported as one instead of being expanded into single-value cases.

    Args:
        path: An already-resolved path, normally one :func:`resolve_data_file` returned.

    Returns:
        The expanded matrix: its axes sorted by name, and one case per combination in the order
        :func:`foundry.data.matrix.expand_matrix` establishes.

    Raises:
        DataError: The document is tabular, so no axis expansion exists for it, or the reader
            rejects the file.
    """
    from foundry.data.matrix import expand_matrix

    document = load_document(path)
    if not isinstance(document, LoadedMatrix):
        data_error(
            path,
            f"not a matrix document: {path}; every top-level value must be a list of axis values",
        )
    return expand_matrix(document)
