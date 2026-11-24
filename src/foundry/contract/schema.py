"""Schema documents: where they live, how they are parsed, what they may reference.

:func:`load_schema` turns any of the three :data:`SchemaSource` shapes into a JSON Schema
document: ``.json`` through :mod:`json`, ``.yaml``/``.yml`` through ``yaml.safe_load``, and every
other extension refused with :data:`SUPPORTED_SCHEMA_SUFFIXES` named in the failure.

Two safety rules hold here rather than in the validator, on every path. YAML is reachable only
through ``yaml.safe_load``, imported inside that branch, so importing this module costs the
standard library alone. A ``$ref``, ``$dynamicRef`` or ``$recursiveRef`` must be a fragment of
the same document: anything else would make :mod:`jsonschema` retrieve a document, which means
opening a socket, and Foundry runs offline.

Every fault is a :class:`foundry.core.exceptions.ContractError` carrying ``json_path="$"``: that
attribute locates a violation inside an *instance* and a schema fault has none, so the summary goes
in the message, the actionable text in ``detail`` and the next step in ``hint``. No builtin or
third-party exception crosses this module's boundary, and ``$id`` is not inspected, since it names
the document and triggers no retrieval. Importing this module reads no file, consults no
environment variable, caches no document and configures no logger, so a second load reflects the
bytes on disk; its one ``DEBUG`` record, naming path and suffix before the read, goes through
:func:`foundry.obs.events.log_event`, imported inside :func:`_parse_by_suffix` so that module-scope
imports stay inside the set AAP 0.4.8 lists for ``foundry.contract``.
"""

from __future__ import annotations

import json
import keyword
from collections.abc import Mapping
from pathlib import Path
from typing import Final, TypeAlias, cast

from foundry.core.exceptions import ContractError

__all__: Final[tuple[str, ...]] = (
    "SUPPORTED_SCHEMA_SUFFIXES",
    "SchemaSource",
    "declared_draft",
    "load_schema",
    "resolve_schema_path",
)

SchemaSource: TypeAlias = Mapping[str, object] | str | Path
"""Every shape :func:`load_schema` accepts: a document in memory, or a path to one."""

SUPPORTED_SCHEMA_SUFFIXES: Final[frozenset[str]] = frozenset({".json", ".yaml", ".yml"})
"""The lower-cased file extensions :func:`load_schema` can parse, and the whole accepted set."""

_DOCUMENT_ROOT: Final[str] = "$"
"""JSON path of a document root; the ``json_path`` every schema fault reports."""

_REFERENCE_KEYWORDS: Final[frozenset[str]] = frozenset({"$ref", "$dynamicRef", "$recursiveRef"})
"""The three keywords through which a validator can be made to retrieve another document."""


def _error(detail: str, *, summary: str | None = None, hint: str | None = None) -> ContractError:
    """Build the :class:`~foundry.core.exceptions.ContractError` this module reports faults with.

    ``json_path`` is always the document root and ``summary`` defaults to ``detail``, so one rule
    holds for every schema fault. The exception is returned, never raised here: each caller raises
    it inside its own ``except`` clause so the original cause stays chained.
    """
    return ContractError(summary or detail, json_path=_DOCUMENT_ROOT, detail=detail, hint=hint)


def resolve_schema_path(source: str | Path, *, base_dir: Path | None = None) -> Path:
    """Resolve a schema path to an absolute one, refusing a path that escapes ``base_dir``.

    A relative path is joined onto ``base_dir``, or onto :func:`pathlib.Path.cwd` when ``base_dir``
    is ``None``, and an absolute one is used as given; the result passes through
    :meth:`pathlib.Path.resolve`, so ``..`` segments and symlinks are gone before the containment
    check -- which is what stops a data file from naming a schema anywhere on the disk. ``~`` is
    deliberately not expanded: it would name a different file on every machine. Nothing is opened
    here; this function is path arithmetic.

    Args:
        source: A path to a schema file, absolute or relative.
        base_dir: The directory a relative path is resolved against and must stay inside, or
            ``None`` to resolve against the working directory with no containment.

    Returns:
        The resolved, absolute path.

    Raises:
        ContractError: When ``base_dir`` is given and the resolved path is not inside it.
    """
    candidate = Path(source)
    if base_dir is None:
        root = Path.cwd()
        return candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()

    root = base_dir.resolve()
    resolved = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    if not resolved.is_relative_to(root):
        raise _error(
            f"schema file {resolved} is outside the schema directory {root}",
            hint="move the schema beside the data it describes, or name it with an absolute path",
        )
    return resolved


def load_schema(source: SchemaSource, *, base_dir: Path | None = None) -> Mapping[str, object]:
    """Load a JSON Schema document from a mapping in memory or from a ``.json``/``.yaml`` file.

    A mapping is returned by identity rather than copied, because the caller owns it and a
    validator treats a schema as read-only. Both safety rules of this module apply on every path,
    and an empty document is a valid schema that matches everything.

    Args:
        source: The schema document itself, or the path to the file that holds it.
        base_dir: The directory a relative path is resolved against and must stay inside, or
            ``None`` to resolve against the working directory with no containment.

    Returns:
        The schema document: the caller's own mapping, or the parsed file.

    Raises:
        ContractError: When the file does not exist, cannot be read or decoded, is not a mapping,
            has an extension outside :data:`SUPPORTED_SCHEMA_SUFFIXES`, escapes ``base_dir``,
            holds a reference that is not a ``#`` fragment, or when ``source`` is none of the
            three accepted shapes. Each reports ``json_path="$"`` with the specifics in ``detail``.
    """
    try:
        return _load_schema(source, base_dir=base_dir)
    except RecursionError as exc:
        # A pathologically nested document, or a cyclic mapping passed in memory, exhausts the
        # interpreter's stack while references are walked. A builtin must not cross this
        # module's boundary, so the fault is reported as the contract violation it is.
        raise _error(
            "schema document is nested too deeply to inspect",
            hint="flatten the document, or move repeated rules into $defs and reference them",
        ) from exc


def declared_draft(document: Mapping[str, object]) -> str | None:
    """Return the draft a schema declares in its ``$schema`` keyword.

    The URI is read, never validated: choosing a validator and rejecting a draft it cannot support
    belongs to :mod:`foundry.contract.validate`, which reports the draft it refused.

    Returns:
        The declared draft URI, or ``None`` when the document declares none or a non-string value.
    """
    declared = document.get("$schema")
    return declared if isinstance(declared, str) else None


def _load_schema(source: SchemaSource, *, base_dir: Path | None) -> Mapping[str, object]:
    """Dispatch on the shape of ``source`` and return the schema document.

    Raises:
        ContractError: For every fault listed on :func:`load_schema`.
    """
    if isinstance(source, (str, Path)):
        path = resolve_schema_path(source, base_dir=base_dir)
        document = _ensure_object(_parse_by_suffix(path), origin=path)
        _reject_remote_refs(document, origin=path)
        return document

    if isinstance(source, Mapping):
        # The caller's mapping is checked and returned as it stands: copying it would hide a
        # later mutation behind a snapshot, and rewriting it would edit a document this module
        # does not own.
        document = _ensure_object(source, origin=None)
        _reject_remote_refs(document, origin=None)
        return document

    raise _error(
        "schema source must be a mapping or a path to a .json, .yaml or .yml file",
        hint="pass the schema document itself, or the path to the file that holds it",
    )


def _ensure_object(document: object, *, origin: Path | None) -> Mapping[str, object]:
    """Return ``document`` typed as a schema object, or raise.

    ``origin`` is the file the document came from and is named in the failure, or ``None`` for a
    document passed in memory. An empty mapping is a schema that matches everything.

    Raises:
        ContractError: When ``document`` is not a mapping -- a list, a scalar, or ``None`` from an
            empty YAML file.
    """
    if isinstance(document, Mapping):
        return cast("Mapping[str, object]", document)

    detail = f"schema document must be a JSON Schema object (got {type(document).__name__})"
    if origin is not None:
        detail = f"{detail} in {origin}"
    raise _error(
        detail,
        summary=f"could not load schema: {origin}" if origin is not None else detail,
        hint="a schema document is a JSON object; wrap the rules in braces",
    )


def _parse_by_suffix(path: Path) -> object:
    """Parse the file at ``path`` according to its lower-cased extension.

    Returns:
        Whatever the parser produced; its shape is settled by :func:`_ensure_object`.

    Raises:
        ContractError: When the extension is not one of :data:`SUPPORTED_SCHEMA_SUFFIXES`, the
            file cannot be read, or its text is not the format the extension declares.
    """
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_SCHEMA_SUFFIXES:
        supported = ", ".join(sorted(SUPPORTED_SCHEMA_SUFFIXES))
        raise _error(
            f"schema file must have one of these extensions: {supported} (got {suffix!r})",
            summary=f"could not load schema: {path}",
            hint=f"rename {path} to .json, .yaml or .yml",
        )

    from foundry.obs.events import LEVEL_DEBUG, LifecycleEvent, log_event

    log_event(
        LifecycleEvent(
            name="schema_load",
            level=LEVEL_DEBUG,
            fields=(("path", str(path)), ("suffix", suffix)),
        )
    )
    text = _read_text(path)
    if suffix == ".json":
        return _parse_json(text, path)
    return _parse_yaml(text, path)


def _read_text(path: Path) -> str:
    """Return the UTF-8 text of ``path``, reporting every read failure as a contract violation.

    Raises:
        ContractError: When the file does not exist, is a directory or is otherwise unreadable,
            or when its bytes are not valid UTF-8.
    """
    summary = f"could not load schema: {path}"
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise _error(
            f"schema file not found: {path}",
            summary=summary,
            hint="check the path, or create the file",
        ) from exc
    except UnicodeDecodeError as exc:
        raise _error(
            f"schema file could not be read: {path} (not valid UTF-8 at byte {exc.start})",
            summary=summary,
            hint="save the schema as UTF-8",
        ) from exc
    except OSError as exc:
        reason = exc.strerror or type(exc).__name__
        raise _error(
            f"schema file could not be read: {path} ({reason})",
            summary=summary,
            hint="check that the file is readable and is not a directory",
        ) from exc


def _parse_json(text: str, path: Path) -> object:
    """Decode a JSON schema document.

    Raises:
        ContractError: When the text is not one valid JSON document, naming the line and column
            the decoder stopped at.
    """
    try:
        decoded: object = json.loads(text)
    except json.JSONDecodeError as exc:
        location = f"line {exc.lineno}, column {exc.colno}"
        raise _error(
            f"schema file is not valid JSON: {path} ({location}: {exc.msg})",
            summary=f"could not load schema: {path}",
            hint="fix the JSON syntax at the reported line and column",
        ) from exc
    return decoded


def _parse_yaml(text: str, path: Path) -> object:
    """Decode a YAML schema document with ``yaml.safe_load`` and nothing else.

    ``yaml`` is imported here rather than at module scope, because :mod:`foundry.http` imports this
    module and the YAML branch runs only for a document that actually is YAML text. No ``Loader``
    argument is passed and no constructor is registered, so a document constructs only the
    primitives, lists and mappings a safe load builds -- never a Python object or a callable.

    Raises:
        ContractError: When the text is not valid YAML, naming the line and column of the mark the
            parser stopped at when it recorded one.
    """
    import yaml

    try:
        decoded: object = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise _error(
            f"schema file is not valid YAML: {path} ({_yaml_location(exc)})",
            summary=f"could not load schema: {path}",
            hint="fix the YAML syntax at the reported line and column",
        ) from exc
    return decoded


def _yaml_location(error: object) -> str:
    """Render the line and column a YAML parser fault points at.

    A ``MarkedYAMLError`` carries a zero-based ``problem_mark`` and a plain ``YAMLError`` carries
    none, so the mark is read with :func:`getattr`: naming the type would need ``yaml`` imported
    when this module loads, which the startup budget forbids.

    Returns:
        ``line 3, column 9: <problem>`` where the marks exist, and the parser's problem text -- or
        the exception's class name -- when they do not. Counts are 1-based, as an editor shows.
    """
    problem = getattr(error, "problem", None)
    mark = getattr(error, "problem_mark", None)
    line = getattr(mark, "line", None)
    column = getattr(mark, "column", None)
    text = problem if isinstance(problem, str) else type(error).__name__
    if isinstance(line, int) and isinstance(column, int):
        return f"line {line + 1}, column {column + 1}: {text}"
    return text


def _reject_remote_refs(document: object, *, origin: Path | None) -> None:
    """Refuse every reference in ``document`` that is not a fragment of the same document.

    Raises:
        ContractError: When a ``$ref``, ``$dynamicRef`` or ``$recursiveRef`` value would make a
            validator retrieve another document.
    """
    _walk(document, path=_DOCUMENT_ROOT, origin=origin)


def _walk(node: object, *, path: str, origin: Path | None) -> None:
    """Check every reference inside ``node``, descending through mappings and lists.

    ``path`` is the JSON path of ``node``, so a rejected reference is located in the message.

    Raises:
        ContractError: When a reference in ``node`` is not a fragment.
    """
    if isinstance(node, Mapping):
        for key, value in node.items():
            child = _child(path, key)
            if key in _REFERENCE_KEYWORDS:
                _check_reference(value, path=child, origin=origin)
            _walk(value, path=child, origin=origin)
    elif isinstance(node, (list, tuple)):
        for index, item in enumerate(node):
            _walk(item, path=f"{path}[{index}]", origin=origin)


def _check_reference(value: object, *, path: str, origin: Path | None) -> None:
    """Raise unless ``value`` names a fragment of the document holding it.

    ``path`` is the JSON path of the reference keyword, rendered into the message so the reader
    can find it in a document of any size.

    Raises:
        ContractError: When the reference is a URL, a bare file name, a non-string, or anything
            else that is not a fragment of this document.
    """
    if isinstance(value, str) and value.startswith("#"):
        return

    rendered = value if isinstance(value, str) else repr(value)
    hint = "inline the referenced schema, or point at a fragment such as '#/$defs/Price'"
    if origin is not None:
        hint = f"{hint} instead of the reference in {origin}"
    raise _error(
        f"remote schema reference is not supported: {rendered} at {path}",
        hint=hint,
    )


def _child(path: str, key: object) -> str:
    """Return the JSON path of ``key`` inside the object at ``path``.

    Returns:
        The child path: ``$.properties.items`` for a plain name, ``$.items.$ref`` for a schema
        keyword, ``$["a.b"]`` for a key a dot would make ambiguous, and ``$[3]`` for a non-string
        key.
    """
    if isinstance(key, str) and _is_dotted_key(key):
        return f"{path}.{key}"
    return f"{path}[{json.dumps(key, ensure_ascii=False, default=repr)}]"


def _is_dotted_key(key: str) -> bool:
    """Return whether ``key`` renders in dotted form rather than as a quoted subscript.

    Returns:
        ``True`` for a name that reads as a Python identifier and is not a keyword, and for a
        schema keyword such as ``$ref`` or ``@context`` whose leading punctuation is not part of
        the name; ``False`` for anything a dot would make ambiguous, such as an empty key or
        ``a.b``.
    """
    if not key:
        return False
    if key[0] in {"$", "@"}:
        return key[1:].isidentifier()
    return key.isidentifier() and not keyword.iskeyword(key)
