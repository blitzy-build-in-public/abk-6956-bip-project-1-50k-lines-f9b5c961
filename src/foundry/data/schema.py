"""JSON Schema enforcement for the rows of a data file.

A data file may carry a *sidecar* schema: a JSON Schema document beside it, discovered under either
of two spellings -- the data file's own full name plus ``.schema.json``, or its bare stem plus the
same suffix -- so ``testdata/cart.csv`` is described by ``testdata/cart.csv.schema.json`` or by
``testdata/cart.schema.json``, and ``testdata/pricing.json`` by
``testdata/pricing.json.schema.json`` or by ``testdata/pricing.schema.json``. The full-name form
is tried first and wins when both documents exist, which is what lets ``cart.csv`` and
``cart.json`` sit in one directory with a schema each; the stem form is there for the sidecar an
author writes beside the bare stem, and a stem keeps every dot it holds, so ``cart.pricing.csv``
is described by ``cart.pricing.schema.json``. Nothing else declares a schema -- there is no schema
key in a data file and none in the configuration -- so the sidecar's presence is the whole
declaration, and a data file without one is enforced against nothing beyond the shape rules the
readers apply.

Two checks run for a row, in this order, and both report through
:func:`foundry.data.loaders.base.data_error`, the package's single ``DataError`` construction
site, so every fault names the file and, where they are known, the line and the column
(AAP 0.4.7, AAP 0.14.3):

1. **Structure, before values.** A column the schema lists in ``required`` that the row does not
   carry is reported by name, and a column the schema's ``properties`` does not declare is
   reported by name when ``data.schema_strict`` is on (AAP 0.4.5); a schema that declares no
   ``properties`` fixes no column set, so it has no undeclared column to report. Running this
   first is what keeps a missing column nameable: the ``required`` complaint a JSON Schema walk
   produces is located at the document root, where there is no column to report.
2. **Values, through the contract layer.** The row's values are validated with
   :func:`foundry.contract.validate.find_violation`, which returns the first violation in
   Foundry's own order instead of raising, so this module turns it into a located
   :class:`~foundry.core.exceptions.DataError` whose ``column`` comes from the violation's
   ``path_parts``. ``jsonschema`` is never imported here: the contract layer owns the walk, the
   ordering and the redaction, and this module reads only the resulting violation (implicit
   requirement I5).

``strict=False`` tolerates extra columns: they stay in the row the caller carries forward but are
left out of the projection the schema validates, so a schema that says nothing about
``additionalProperties`` does not fail on a column an author added for another consumer.

Every check is order-stable, which is what makes the reported fault reproducible: the
``required`` names, the declared property set and the extra columns are each sorted before the
first of them is reported, and :func:`find_violation` owns the choice of violation. The same file
therefore reports the same message on every run and on every ``pytest-xdist`` worker (implicit
requirement I6).

Enforcement precedes case construction: :func:`enforce_table` returns the table that
:func:`foundry.data.cases.build_case_set` converts, so no case exists for a row the schema
rejects, and the fault surfaces at collection time, where pytest reports it as a collection error
rather than inside a test.

Importing the module defines names and does nothing else (AAP 0.4.8): no schema is loaded at
import, no file is opened, no environment variable is read and no logger is configured. Its whole
import list is the standard library, three modules of :mod:`foundry.contract` and
:mod:`foundry.data.loaders.base`; it never reaches ``http``, ``reporting``, ``cli``, ``plugin``,
``execution``, ``config``, ``fixtures``, ``db``, ``assertions`` or ``cassette`` (AAP 0.4.8).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Final

# The defining modules are imported rather than the package barrel, so the import does not depend
# on a re-export that may or may not exist; the layering is the same either way (AAP 0.4.8).
from foundry.contract.schema import load_schema
from foundry.contract.validate import find_violation
from foundry.contract.violations import RedactorCallable
from foundry.data.loaders.base import LoadedTable, RawRow, data_error

__all__ = [
    "SCHEMA_SUFFIX",
    "enforce_row",
    "enforce_table",
    "load_row_schema",
    "schema_path_for",
]

# --- Locating the sidecar --------------------------------------------------

SCHEMA_SUFFIX: Final[str] = ".schema.json"
"""The suffix appended to a data file's full name, or to its bare stem, to name its schema.

``cart.csv`` keeps its extension in the preferred spelling, ``cart.csv.schema.json``, and the bare
stem gives the fallback spelling, ``cart.schema.json``; when both documents exist, the full-name
one is the one enforced, so two data files sharing a stem in different formats never collide.
"""


def schema_path_for(data_path: Path) -> Path:
    """Return the sidecar schema path that belongs to one data file.

    This is the only place the sidecar's name is decided, and the two spellings are tried in a
    fixed order: the full-name candidate ``<name>.schema.json`` when that file exists, else the
    stem candidate ``<stem>.schema.json`` when that one exists, else the full-name candidate. The
    existence tests stop here so that every caller -- including :func:`load_row_schema` -- reads
    one decision instead of repeating it, and the final fallback keeps the canonical full-name
    path as the answer for a data file that declares no schema. A data file whose stem is its own
    name, one carrying no suffix or a dotfile, has a single candidate rather than two.

    Args:
        data_path: The data file's path, for example ``testdata/cart.csv``.

    Returns:
        The path of the schema document that exists, the full-name form winning when both do --
        ``testdata/cart.csv.schema.json`` for ``testdata/cart.csv`` and
        ``testdata/pricing.json.schema.json`` for ``testdata/pricing.json`` -- otherwise the
        full-name candidate, ``testdata/cart.csv.schema.json`` for a data file carrying neither.
    """
    full_name = data_path.with_name(data_path.name + SCHEMA_SUFFIX)
    if data_path.stem == data_path.name:
        return full_name
    stem_name = data_path.with_name(data_path.stem + SCHEMA_SUFFIX)
    if full_name.exists():
        return full_name
    if stem_name.exists():
        return stem_name
    return full_name


def load_row_schema(
    data_path: Path, *, base_dir: Path | None = None
) -> Mapping[str, object] | None:
    """Load the sidecar schema of a data file, or report that it has none.

    A missing sidecar is the ordinary state of a data file that declares no schema, so it returns
    ``None`` rather than raising; every other fault in the document -- unreadable, not valid JSON
    or YAML, not a schema object, a reference outside the document -- is a
    :class:`~foundry.core.exceptions.ContractError` raised by
    :func:`foundry.contract.schema.load_schema`, which is the single loader for a schema document.

    Args:
        data_path: The data file whose sidecar is wanted, for example ``testdata/cart.csv``.
        base_dir: The data directory the sidecar must stay inside, normally ``data.dir``; ``None``
            imposes no containment. It is what stops a data file from naming a schema anywhere on
            the disk.

    Returns:
        The schema document, or ``None`` when :func:`schema_path_for` names no existing file.
    """
    schema_path = schema_path_for(data_path)
    if not schema_path.exists():
        return None
    return load_schema(schema_path, base_dir=base_dir)


# --- Reading the keywords this module enforces -----------------------------


def _declared_names(value: object) -> tuple[str, ...]:
    """Return the column names a schema keyword lists, sorted.

    The keyword's value is whatever the document held, so the shape is checked here rather than
    assumed: a ``required`` that is not a list of strings declares no column this module can
    compare against, and the JSON Schema walk reports the malformed keyword itself.

    Args:
        value: The keyword's value as the document holds it, such as ``["sku", "qty"]``.

    Returns:
        The names in sorted order, or an empty tuple when the value is not a list or tuple of
        strings. Sorting here is what makes the first missing column the same one on every run.
    """
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(sorted(item for item in value if isinstance(item, str)))


def _declared_properties(schema: Mapping[str, object]) -> Mapping[str, object] | None:
    """Return the column names a schema declares under ``properties``, with their subschemas.

    Args:
        schema: The schema document being enforced.

    Returns:
        The ``(name, subschema)`` pairs, or ``None`` when the document declares no ``properties``
        mapping -- a schema that declares no property constrains no column set, so there is
        nothing to compare a row's columns against. A key that is not a string is dropped, because
        a data column name is always a ``str`` and such a key can never name one.
    """
    declared = schema.get("properties")
    if not isinstance(declared, Mapping):
        return None
    return {key: value for key, value in declared.items() if isinstance(key, str)}


# --- Per-row enforcement ---------------------------------------------------


def enforce_row(
    values: Mapping[str, object],
    *,
    path: Path,
    line: int | None,
    schema: Mapping[str, object],
    strict: bool = True,
    redact: RedactorCallable | None = None,
) -> Mapping[str, object]:
    """Check one row against a schema, reporting the first fault with its column.

    The checks run in this fixed order, because each one exists to make the next one reportable:

    1. every column the schema lists in ``required`` and the row lacks, alphabetically first;
    2. under ``strict``, every column the schema's ``properties`` does not declare, alphabetically
       first; under ``strict=False`` those columns are instead dropped from the projection that
       step 3 validates, so a schema silent about ``additionalProperties`` passes;
    3. the remaining values, in the violation order
       :func:`foundry.contract.validate.find_violation` establishes, reported with the offending
       property as the column.

    The violation's own rendering is what reaches the message: it is redacted through ``redact`` --
    the run's engine, built from ``secrets.redact_keys`` -- and flattened to one line by
    :mod:`foundry.contract.violations`, which is the only party that may print a validator message
    because such a message quotes the instance value. A cell value and a whole row are never
    interpolated here.

    Args:
        values: The row's column names and values, in the table's column order.
        path: The data file the row came from; named by every error.
        line: The 1-based line the row is on, or ``None`` for a format with no per-row line
            notion; carried on the error.
        schema: The schema document to enforce, as :func:`load_row_schema` returns it.
        strict: Whether an undeclared column is a fault (``data.schema_strict``) or is tolerated
            and excluded from validation.
        redact: The callable that scrubs secrets out of the rendered violation, normally
            ``foundry.obs.redaction.Redactor.text`` of the run's engine, so a configured key is
            redacted as well as the built-in deny-lists. ``None`` applies those deny-lists, which
            is what a caller holding no configured keys relies on.

    Returns:
        The mapping the caller carries forward: ``values`` itself under ``strict``, and the
        declared-column projection of it when ``strict`` is ``False``.

    Raises:
        DataError: When a required column is absent, when an undeclared column is present and
            ``strict`` is on, or when a value violates the schema.
    """
    for name in _declared_names(schema.get("required")):
        if name not in values:
            data_error(path, f"missing required column '{name}'", line=line, column=name)

    properties = _declared_properties(schema)
    projected: Mapping[str, object] = values
    if properties is not None:
        extras = sorted(key for key in values if key not in properties)
        if extras and strict:
            data_error(path, f"unknown column '{extras[0]}'", line=line, column=extras[0])
        if not strict:
            projected = {key: value for key, value in values.items() if key in properties}

    violation = find_violation(dict(projected), schema)
    if violation is not None:
        # The property a violation is inside is the column it is about; a complaint at the
        # document root, such as `additionalProperties`, names no column of its own.
        column = next((part for part in violation.path_parts if isinstance(part, str)), None)
        data_error(path, violation.render(redact=redact), line=line, column=column)
    return projected


# --- Table enforcement, the registry's single entry point ------------------


def enforce_table(
    table: LoadedTable,
    *,
    strict: bool = True,
    schema: Mapping[str, object] | None = None,
    redact: RedactorCallable | None = None,
) -> LoadedTable:
    """Enforce a table's rows against its schema, when the data file declares one.

    This is the one entry point the data registry calls, and it returns the table that
    :func:`foundry.data.cases.build_case_set` converts, so schema enforcement always precedes case
    construction and a rejected row never becomes a case.

    One check applies here. When ``schema`` is not ``None``, every row goes through
    :func:`enforce_row`, which is the only party that compares a row's columns with the schema's
    ``required`` list and its ``properties`` mapping, and the validated rows are rebuilt so the
    caller receives what was actually checked: the row's own values under ``strict``, and the
    declared columns alone when an author tolerates extras. A data file that declares no schema is
    enforced against nothing, so the table comes back unchanged without a row being opened.

    Args:
        table: The table a reader produced, with its path, column order and rows.
        strict: Whether an undeclared column is a fault (``data.schema_strict``).
        schema: The sidecar schema document, or ``None`` for a data file that declares none.
        redact: The callable that scrubs secrets out of a rendered violation, passed to every
            :func:`enforce_row` call. ``None`` applies the built-in deny-lists.

    Returns:
        ``table`` itself when no schema was given, otherwise a table with the same path and
        columns and one rebuilt :class:`~foundry.data.loaders.base.RawRow` per input row, in the
        same order and carrying the same lines and row indexes.

    Raises:
        DataError: For any fault :func:`enforce_row` reports -- a required column the row lacks, an
            undeclared column present under ``strict``, or a value the schema rejects -- naming the
            file, the row's line and the column.
    """
    if schema is None:
        return table

    validated_rows: list[RawRow] = []
    for row in table.rows:
        validated = enforce_row(
            row.as_mapping(),
            path=table.path,
            line=row.line,
            schema=schema,
            strict=strict,
            redact=redact,
        )
        validated_rows.append(
            RawRow(
                values=tuple(validated.items()),
                line=row.line,
                row_index=row.row_index,
            )
        )
    return LoadedTable(path=table.path, columns=table.columns, rows=tuple(validated_rows))
