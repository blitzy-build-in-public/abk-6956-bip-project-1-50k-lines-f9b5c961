"""Schema documents: where they live, how they are parsed, what they may reference.

Every schema Foundry validates against arrives in one of three shapes -- an in-memory
:class:`~collections.abc.Mapping`, a ``str`` path, or a :class:`~pathlib.Path` -- and
:func:`load_schema` turns all three into a JSON Schema document. A ``.json`` file is decoded with
the standard library's :mod:`json`, a ``.yaml`` or ``.yml`` file with ``yaml.safe_load``, and an
extension outside :data:`SUPPORTED_SCHEMA_SUFFIXES` is refused with the supported set spelled out
in the failure, so a caller learns what is accepted from the error itself.

Two safety rules are enforced here rather than left to the validator.

Safe deserialisation
    YAML is reachable only through ``yaml.safe_load``. ``yaml.load`` is never called and no
    loader or constructor is registered, so a schema file can build no Python object beyond the
    primitives, lists and mappings a safe load builds. ``yaml`` is imported inside that branch
    and never at module scope, so importing this module costs the standard library alone -- it
    sits on the import path the HTTP layer loads at every ``pytest`` startup.

Fragment-only references
    A document may reference itself through ``$ref``, ``$dynamicRef`` or ``$recursiveRef`` only
    with a fragment (``#/$defs/Price``). Anything else -- an ``https`` URL above all -- would
    make :mod:`jsonschema` retrieve a document, which means opening a socket; Foundry runs
    offline and its suite fails on a non-loopback connection, so such a reference is rejected
    here, before a validator ever sees the document. ``$id`` is not inspected: it names the
    document and triggers no retrieval.

Every fault is a :class:`foundry.core.exceptions.ContractError` carrying ``json_path="$"``. The
``json_path`` attribute locates a violation inside an *instance*, and a fault in the schema
document has no instance location, so the actionable text goes in ``detail``, a one-line summary
in the message, and the next step in ``hint``. No builtin and no third-party exception crosses
this module's boundary.

Importing this module defines names and does nothing else: no file is read, no environment
variable is consulted, no logger is configured and no document is cached, so a second load of the
same path always reflects the bytes on disk at that moment. The one record this module emits -- a
``DEBUG`` line naming the schema path and the suffix, written before the file is read -- goes
through :func:`foundry.obs.events.log_event`, imported inside :func:`_parse_by_suffix` so that
this module's module-scope imports stay inside the set AAP 0.4.8 lists for ``foundry.contract``.
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

    Centralising construction is what makes one rule hold for every schema fault: ``json_path``
    is the document root, because the fault is in the schema rather than in an instance, and the
    specifics a caller branches on live in ``detail``.

    Args:
        detail: What went wrong, in the terms of the schema document at fault.
        summary: The one-line message the exception renders; the detail when omitted.
        hint: An actionable next step, or ``None``.

    Returns:
        The exception to raise, never raised here: each caller raises it inside its own
        ``except`` clause so the original cause stays chained.
    """
    return ContractError(summary or detail, json_path=_DOCUMENT_ROOT, detail=detail, hint=hint)


def resolve_schema_path(source: str | Path, *, base_dir: Path | None = None) -> Path:
    """Resolve a schema path to an absolute one, refusing a path that escapes ``base_dir``.

    A relative path is joined onto ``base_dir`` when one is given and resolved against
    :func:`pathlib.Path.cwd` otherwise; an absolute path is used as given. ``~`` is *not*
    expanded: a leading ``~`` in a configuration value would mean a different file on every
    machine that ran the suite, so it stays a literal directory name here.

    The result is passed through :meth:`pathlib.Path.resolve`, so ``..`` segments and symlinks
    are eliminated before any check. When ``base_dir`` is supplied -- :mod:`foundry.data` passes
    ``data.dir`` -- the resolved path must stay inside it, which is what stops a data file from
    naming a schema anywhere on the disk. When ``base_dir`` is ``None`` no containment applies,
    because a caller may legitimately point at a schema it owns elsewhere.

    Nothing is opened, read or tested for writability here: this function is path arithmetic,
    and the caller decides when to touch the filesystem.

    Args:
        source: A path to a schema file, absolute or relative.
        base_dir: The directory a relative path is resolved against, and the directory the
            resolved path must stay inside; ``None`` resolves against the working directory and
            imposes no containment.

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

    The three accepted shapes are a :class:`~collections.abc.Mapping`, a ``str`` path and a
    :class:`~pathlib.Path`. A mapping is returned as the caller's own object -- not copied and
    not rewritten, because the caller owns it and a validator treats a schema as read-only -- so
    a caller that mutates its document between two validated calls sees the change take effect.

    Both safety rules of this module apply on every path: YAML is parsed with ``yaml.safe_load``
    only, and a reference that is not a fragment of the same document is refused, so no load can
    reach the network. An empty document (``{}``) is a valid schema that matches everything, and
    is returned rather than rejected.

    Args:
        source: The schema document itself, or the path to the file that holds it.
        base_dir: The directory a relative path is resolved against and must stay inside;
            ``None`` resolves against the working directory and imposes no containment.

    Returns:
        The schema document, as the mapping the caller passed or as the parsed file. It is never
        a copy of a caller's mapping.

    Raises:
        ContractError: When the file does not exist, cannot be read, is not valid UTF-8, is not
            valid JSON or YAML, or has an extension outside :data:`SUPPORTED_SCHEMA_SUFFIXES`;
            when the parsed document is not a mapping; when the path escapes ``base_dir``; when
            a ``$ref``, ``$dynamicRef`` or ``$recursiveRef`` is anything but a ``#`` fragment;
            and when the source is none of the three accepted shapes. Every one of them reports
            ``json_path="$"`` and carries the specifics in ``detail``.
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

    The URI is read, never validated: selecting a validator and rejecting a draft it cannot
    support belongs to :mod:`foundry.contract.validate`, which reports the draft it refused.

    Args:
        document: A schema document, as :func:`load_schema` returns it.

    Returns:
        The declared draft URI, or ``None`` when the document declares none or declares a value
        that is not a string.
    """
    declared = document.get("$schema")
    return declared if isinstance(declared, str) else None


def _load_schema(source: SchemaSource, *, base_dir: Path | None) -> Mapping[str, object]:
    """Dispatch on the shape of ``source`` and return the schema document.

    Args:
        source: The mapping or the path to load.
        base_dir: The directory a relative path is resolved against and must stay inside.

    Returns:
        The schema document; a caller's mapping is returned by identity.

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

    Args:
        document: The parsed document, or any value a loosely typed caller passed as one.
        origin: The file the document came from and is named in the failure, or ``None`` for a
            document that was passed in memory.

    Returns:
        The same object, unchanged. An empty mapping is a schema that matches everything and is
        returned like any other.

    Raises:
        ContractError: When ``document`` is not a mapping -- a list, a scalar, or ``None`` from
            an empty YAML file.
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

    Args:
        path: The resolved path of the schema file.

    Returns:
        Whatever the parser produced. It is typed ``object`` because its shape is settled by
        :func:`_ensure_object`, not by the extension.

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

    Args:
        path: The resolved path of the schema file.

    Returns:
        The file's contents as text.

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

    Args:
        text: The file's contents.
        path: The file the text came from, named in a failure.

    Returns:
        The decoded document.

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

    ``yaml`` is imported here rather than at module scope. :mod:`foundry.http` imports this
    module, so a module-scope import would be paid on every ``pytest`` startup, and the YAML
    branch runs only for a document that actually is YAML text.

    No ``Loader`` argument is passed and no constructor is registered, so a document can
    construct only the primitives, lists and mappings a safe load builds -- never a Python
    object, and never a callable.

    Args:
        text: The file's contents.
        path: The file the text came from, named in a failure.

    Returns:
        The decoded document, which is ``None`` for an empty document.

    Raises:
        ContractError: When the text is not valid YAML, naming the line and column of the mark
            the parser stopped at when it recorded one.
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

    A ``MarkedYAMLError`` carries a ``problem_mark`` whose ``line`` and ``column`` are
    zero-based; a plain ``YAMLError`` carries neither. The mark is read with :func:`getattr`
    instead of an annotation because naming the type would need ``yaml`` imported when this
    module loads, which the startup budget forbids.

    Args:
        error: The exception the safe loader raised.

    Returns:
        ``line 3, column 9: mapping values are not allowed here`` where the marks exist, and the
        parser's problem text -- or the exception's class name -- when they do not. Counts are
        1-based, as an editor shows them.
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

    Args:
        document: The schema document, in memory.
        origin: The file the document came from, or ``None`` for a document passed in memory.

    Returns:
        None when every reference is a ``#`` fragment. The function reports its result through
        the exception it raises.

    Raises:
        ContractError: When a ``$ref``, ``$dynamicRef`` or ``$recursiveRef`` value would make a
            validator retrieve another document.
    """
    _walk(document, path=_DOCUMENT_ROOT, origin=origin)


def _walk(node: object, *, path: str, origin: Path | None) -> None:
    """Check every reference inside ``node``, descending through mappings and lists.

    Args:
        node: The document, or any value nested inside it.
        path: The JSON path of ``node``, so a rejected reference is located in the message.
        origin: The file the document came from, or ``None``.

    Returns:
        None.

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

    Args:
        value: The value of a reference keyword.
        path: The JSON path of that keyword, rendered into the message so the reader can find
            it in a document of any size.
        origin: The file the document came from, named in the hint, or ``None``.

    Returns:
        None when the reference is a string beginning with ``#``.

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

    Args:
        path: The JSON path of the object holding ``key``.
        key: The key to descend to, which need not be a string in a document a caller passed in
            memory.

    Returns:
        The child path: ``$.properties.items`` for a plain name, ``$.items.$ref`` for a schema
        keyword, ``$["a.b"]`` for a key a dot would make ambiguous, and ``$[3]`` for a
        non-string key.
    """
    if isinstance(key, str) and _is_dotted_key(key):
        return f"{path}.{key}"
    return f"{path}[{json.dumps(key, ensure_ascii=False, default=repr)}]"


def _is_dotted_key(key: str) -> bool:
    """Return whether ``key`` renders in dotted form rather than as a quoted subscript.

    Args:
        key: A key of a JSON object.

    Returns:
        ``True`` for a name that reads as a Python identifier and is not a keyword, and for a
        schema keyword such as ``$ref`` or ``@context`` whose leading ``$`` or ``@`` is
        punctuation rather than part of the name; ``False`` for anything a dot would make
        ambiguous, such as an empty key or ``a.b``.
    """
    if not key:
        return False
    if key[0] in {"$", "@"}:
        return key[1:].isidentifier()
    return key.isidentifier() and not keyword.iskeyword(key)
