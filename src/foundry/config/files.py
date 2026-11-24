"""Configuration-file discovery and the TOML loader behind the file layer.

AAP 0.4.5 fixes one discovery order, and nothing searches beyond it: ``--foundry-config
PATH``, then ``FOUNDRY_CONFIG``, then ``foundry.toml`` in the invocation directory, then
``[tool.foundry]`` in that directory's ``pyproject.toml``, then the field defaults. The first
match wins, so this module answers exactly two questions -- which file applies, and what does
it hold:

* :func:`discover_file` walks the five steps once and returns a :class:`DiscoveredFile`, or
  ``None`` when none of them matched and the caller resolves from the defaults alone;
* :func:`load_file` turns that descriptor into a :class:`FilePayload` -- the base
  configuration as flat dotted keys, the ``[profiles.<name>]`` tables beside it, and the path
  ``foundry config show`` prints as ``file:<path>``.

**Discovery is anchored to the invocation directory, never to the test file.** That is what
AAP 0.4.5 means by "``foundry.toml`` in the invocation directory", it is why the shipped
example project carries no ``foundry.toml`` of its own (assumption A9), and it is why this
module takes the directory as an argument instead of reading one.

**A file the operator named must be usable; a file Foundry merely found must not stop the
run.** A missing explicitly named file is the AAP 0.4.7 row "Explicit config path missing":
:func:`foundry.config.errors.missing_file`, the message ``config file not found: <path>`` and
exit 2 at the caller. A malformed ``pyproject.toml`` that nobody named is skipped instead -- a
working directory may carry one Foundry was never asked to read -- while a malformed
``foundry.toml`` is always a fault, because that name is Foundry's own.

**Profiles are extracted here and ordered elsewhere.** ``[profiles.<name>]`` of a
``foundry.toml`` and ``[tool.foundry.profiles.<name>]`` of a ``pyproject.toml`` become the one
:attr:`FilePayload.profiles` mapping, so the two spellings are one feature. Merging a selected
profile over the base file belongs to :mod:`foundry.config.profiles` and
:mod:`foundry.config.merge`: this module states what a file holds, never what wins.

**Every key is checked against the registry while the file that supplied it is still known.**
:func:`foundry.config.validation.validate_unknown_keys` runs per layer -- ``file`` for the base
table and ``profile:<name>`` for each profile table -- with the file's path as the detail, so a
rejection names the closest valid key and the file to edit (AAP 0.4.5).

**Deserialisation is TOML and nothing else** -- the standard library's :mod:`tomllib` over a
UTF-8 decode, with no ``eval``, ``exec``, ``pickle`` or YAML/JSON configuration format
(AAP 0.3.2, AAP 0.9.2, AAP 0.14.3). Nothing is written, so no path needs a traversal guard.

Importing this module performs no I/O, reads no environment variable, configures no logger and
holds no mutable state: the environment mapping is passed in because
:mod:`foundry.config.env` owns the only environment read (AAP 0.4.8). It imports
:mod:`foundry.config.errors`, :mod:`foundry.config.layers` and
:mod:`foundry.config.validation`, the last of which pulls in ``pydantic`` -- a cost paid when a
configuration is resolved and never by :mod:`foundry.plugin` at import time (AAP 0.14.2,
implicit requirement I5).
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import Final, Literal

from foundry.config import errors, layers, validation
from foundry.config.env import ENV_CONFIG_VAR

__all__: Final[tuple[str, ...]] = (
    "DISCOVERY_ORDER",
    "DiscoveredFile",
    "FOUNDRY_TOML",
    "FileKind",
    "FileOrigin",
    "FilePayload",
    "PROFILES_KEY",
    "PROFILE_KEY",
    "PYPROJECT_TOML",
    "TOOL_FOUNDRY",
    "TOOL_TABLE",
    "default_payload",
    "discover_file",
    "load_file",
)


FOUNDRY_TOML: Final[str] = "foundry.toml"
"""Foundry's own configuration file, read from the invocation directory (AAP 0.4.5)."""

PYPROJECT_TOML: Final[str] = "pyproject.toml"
"""The file whose ``[tool.foundry]`` table configures Foundry when no foundry.toml applies."""

TOOL_TABLE: Final[str] = "tool"
"""The table a packaging tool's ``pyproject.toml`` section lives in."""

TOOL_FOUNDRY: Final[str] = "foundry"
"""Foundry's own table name inside ``[tool]``."""

PROFILES_KEY: Final[str] = "profiles"
"""The table holding one profile table per name (AAP 0.4.5); never part of the base table."""

PROFILE_KEY: Final[str] = "profile"
"""The key that selects the active profile (AAP 0.4.5); a profile table may not set it."""

DISCOVERY_ORDER: Final[tuple[str, ...]] = (
    "--foundry-config",
    ENV_CONFIG_VAR,
    FOUNDRY_TOML,
    f"{PYPROJECT_TOML} [{TOOL_TABLE}.{TOOL_FOUNDRY}]",
    "defaults",
)
"""The five discovery steps in the order they are tried, first match wins (AAP 0.4.5).

Published behaviour, stated once as data: ``foundry doctor`` reports it and the other modules
point at it rather than paraphrasing it.
"""


FileKind = Literal["foundry-toml", "pyproject"]
"""How a discovered file is read.

``foundry-toml`` is a file whose top-level keys are the base configuration -- a ``foundry.toml``
and also any file an operator named explicitly; ``pyproject`` is a ``pyproject.toml`` whose
configuration sits under ``[tool.foundry]``.
"""

FileOrigin = Literal["cli", "env", "cwd", "pyproject"]
"""Which discovery step selected a file.

``cli`` and ``env`` are the two steps that name a file explicitly; ``cwd`` is the
``foundry.toml`` beside the invocation directory and ``pyproject`` the ``[tool.foundry]``
table in that directory's ``pyproject.toml`` (AAP 0.4.5).
"""

_LINE_COLUMN_RE: Final[re.Pattern[str]] = re.compile(r"\s*\(at line (\d+), column (\d+)\)$")
"""The location :mod:`tomllib` appends to a parse fault's message.

CPython 3.11 through 3.13 expose no ``lineno``/``colno`` attribute on
:class:`tomllib.TOMLDecodeError`, so the location AAP 0.4.5 requires in the message is read
back out of the message itself rather than off the exception.
"""

_END_OF_DOCUMENT: Final[str] = "(at end of document)"
"""What :mod:`tomllib` appends instead of a location when a document ends too early."""

_NOT_UTF8: Final[str] = "not valid UTF-8 text"
"""The fault reported for a configuration file that does not decode as UTF-8."""

_PROFILE_TABLES_EXPECTED: Final[str] = "a table of profile tables"
"""Expected shape of a ``profiles`` key, quoted by :func:`foundry.config.errors.invalid_shape`."""

_CONFIG_TABLE_EXPECTED: Final[str] = "a table of configuration keys"
"""Expected shape of a profile table or of ``[tool.foundry]``."""

_NO_PROFILE_NESTING_EXPECTED: Final[str] = "configuration keys rather than a nested profiles table"
"""Expected shape of a profile table that carries a ``profiles`` key of its own."""


@dataclass(frozen=True, slots=True)
class DiscoveredFile:
    """One configuration file that discovery selected, and how it was selected.

    The record is what :func:`load_file` reads and what
    :func:`foundry.config.merge.resolve` turns into a ``ConfigSource`` for the ``file``
    layer. It is frozen and slotted: a descriptor is built once by discovery and read by the
    loader and the source recorder, neither of which may see it change under them.

    Attributes:
        path: The file, absolute and normalised. A relative path an operator typed is
            resolved against the invocation directory that was passed to
            :func:`discover_file`, never against the process working directory, so a
            resolution is a pure function of its arguments.
        kind: How the file is read -- as a Foundry TOML document or through
            ``[tool.foundry]``.
        origin: The discovery step that selected it.
        explicit: Whether the operator named the file (``--foundry-config`` or
            ``FOUNDRY_CONFIG``) rather than Foundry finding it. Only an explicit file turns
            a read or parse fault into an error.
        document: The parsed document, when discovery had to parse the file to decide
            whether it configures Foundry -- the ``pyproject.toml`` step. :func:`load_file`
            reads it instead of parsing the same file a second time; it is ``None`` for
            every other file and for a descriptor a caller builds by hand. It is left out of
            comparison and of ``repr`` because it is a cache of the file's content, not part
            of what identifies the descriptor.
    """

    path: Path
    kind: FileKind
    origin: FileOrigin
    explicit: bool
    document: Mapping[str, object] | None = field(default=None, compare=False, repr=False)

    def label(self) -> str:
        """Return the path as the text a ``file`` layer source records as its detail.

        ``foundry config show`` prints this string after ``file:``, so it is the answer to
        "which file supplied this value" and must be the resolved path rather than the
        spelling the operator happened to type (AAP 0.4.5, AAP 0.7.4).

        Returns:
            The path as text, absolute because :func:`discover_file` resolves it.
        """
        return str(self.path)


def discover_file(
    *,
    cli_path: Path | None = None,
    environ: Mapping[str, str],
    cwd: Path,
) -> DiscoveredFile | None:
    """Select the configuration file that applies, walking the five steps of AAP 0.4.5.

    First match wins, and a step that finds no file is not a fault: resolution continues to the
    next step and, when none matches, the caller resolves from the field defaults and logs
    ``foundry: no config file found``. A relative path the operator named is resolved against
    ``cwd``, so ``--foundry-config data/suite.toml`` means the invocation directory rather than
    the process working directory or the test file's directory.

    Args:
        cli_path: The value of ``--foundry-config`` as the command line recorded it -- a
            relative path arrives relative and is resolved here -- or ``None`` when it was not
            given (:class:`foundry.config.cli_layer.CliInput`).
        environ: The environment to look ``FOUNDRY_CONFIG`` up in, matched case-insensitively
            as AAP 0.4.5 requires; passed in because :mod:`foundry.config.env` owns the only
            environment read (AAP 0.4.8).
        cwd: The invocation directory the local steps and a relative explicit path resolve
            against.

    Returns:
        The selected file, or ``None`` when no step matched and the defaults apply.

    Raises:
        foundry.core.exceptions.ConfigError: When an explicitly named file does not exist or is
            not a regular file -- message ``config file not found: <path>``, AAP 0.4.7, exit 2
            at the caller -- or when a ``pyproject.toml`` in the invocation directory carries a
            ``[tool.foundry]`` that is not a table.
    """
    named = _named_path(cli_path)
    if named is not None:
        return _explicit_file(named, origin="cli", cwd=cwd)

    from_environment = _environment_path(environ)
    if from_environment is not None:
        return _explicit_file(from_environment, origin="env", cwd=cwd)

    local = _absolute(Path(FOUNDRY_TOML), cwd)
    if local.is_file():
        return DiscoveredFile(path=local, kind="foundry-toml", origin="cwd", explicit=False)

    return _local_pyproject(cwd)


def _named_path(cli_path: Path | None) -> Path | None:
    """Return the path ``--foundry-config`` supplied, or ``None`` when it supplied none.

    An option given as an empty string is already read as "not supplied" by
    :mod:`foundry.config.cli_layer`, and the whitespace test here closes the same hole for a
    caller that hands a path over directly.

    Args:
        cli_path: The recorded option value, or ``None``.

    Returns:
        The path to resolve, or ``None`` when the option carried nothing.
    """
    if cli_path is None or not str(cli_path).strip():
        return None
    return cli_path


def _environment_path(environ: Mapping[str, str]) -> Path | None:
    """Return the path ``FOUNDRY_CONFIG`` names, matched case-insensitively.

    AAP 0.4.5 makes every ``FOUNDRY_*`` variable case-insensitive, and POSIX lets both
    spellings stand at once: the canonical upper-case spelling wins when both are present and
    otherwise the candidates are visited in sorted order, so the file selected never depends on
    the order the mapping happened to hold.

    Args:
        environ: The environment to read, as given.

    Returns:
        The path the variable names, or ``None`` when no spelling of it is set or the value
        is empty or whitespace -- an empty variable selects no file rather than the current
        directory.
    """
    spellings = sorted(name for name in environ if name.upper() == ENV_CONFIG_VAR)
    if not spellings:
        return None
    chosen = ENV_CONFIG_VAR if ENV_CONFIG_VAR in environ else spellings[0]
    value = environ[chosen].strip()
    return Path(value) if value else None


def _explicit_file(named: Path, *, origin: FileOrigin, cwd: Path) -> DiscoveredFile:
    """Resolve a file the operator named and refuse it when it is not there.

    An explicitly named file is read as a full Foundry TOML document: its top-level keys are
    the base configuration, exactly as in a ``foundry.toml``. Naming a ``pyproject.toml`` this
    way therefore does not switch to the ``[tool.foundry]`` reading -- its ``[tool]`` table is
    then an ordinary table whose keys the registry rejects.

    Args:
        named: The path as the command line or the environment spelled it.
        origin: The step that named it, ``cli`` or ``env``.
        cwd: The invocation directory a relative path is resolved against.

    Returns:
        The descriptor, marked explicit so that a read or parse fault becomes an error.

    Raises:
        foundry.core.exceptions.ConfigError: When the path does not exist or is not a
            regular file; the message is the AAP 0.4.7 sentence
            ``config file not found: <path>``.
    """
    path = _absolute(named, cwd)
    if not path.is_file():
        raise errors.missing_file(path)
    return DiscoveredFile(path=path, kind="foundry-toml", origin=origin, explicit=True)


def _local_pyproject(cwd: Path) -> DiscoveredFile | None:
    """Return the invocation directory's ``pyproject.toml`` when it configures Foundry.

    The fourth step of AAP 0.4.5 matches only a ``pyproject.toml`` that actually carries a
    ``[tool.foundry]`` table, and deciding that requires reading the file -- so the parsed
    document is kept on the descriptor and :func:`load_file` reuses it rather than parsing
    the same file twice. A file that cannot be read or parsed is not a match: it was not
    named by anyone, so it may belong to another tool entirely (see the module docstring).

    Args:
        cwd: The invocation directory.

    Returns:
        The descriptor for a ``pyproject.toml`` carrying ``[tool.foundry]``, or ``None`` when
        there is no such file, it cannot be parsed, or it carries no Foundry table.

    Raises:
        foundry.core.exceptions.ConfigError: When the file carries a ``[tool.foundry]`` that
            is not a table, which is an attempt to configure Foundry in the wrong shape.
    """
    path = _absolute(Path(PYPROJECT_TOML), cwd)
    if not path.is_file():
        return None
    probe = DiscoveredFile(path=path, kind="pyproject", origin="pyproject", explicit=False)
    document = _read_document(probe)
    if document is None or _foundry_table(document) is None:
        return None
    return replace(probe, document=document)


def _absolute(path: Path, cwd: Path) -> Path:
    """Resolve a path against the invocation directory and normalise it.

    Args:
        path: The path as given, absolute or relative.
        cwd: The invocation directory a relative path is resolved against.

    Returns:
        The absolute, symlink-resolved path. A path that cannot be resolved -- a symlink
        loop, which no configuration file can have -- is returned as its absolute spelling
        so that the existence check reports it instead of an unrelated fault.
    """
    candidate = path if path.is_absolute() else cwd / path
    try:
        return candidate.resolve()
    except OSError:
        return candidate


@dataclass(frozen=True, slots=True)
class FilePayload:
    """What the resolved configuration file contributes, in the form the layers merge.

    :mod:`foundry.config.merge` applies this payload as the ``file`` layer and, for each
    profile, as a ``profile:<name>`` layer above it (AAP 0.4.5). Everything is already flat and
    key-sorted, so a payload can be attributed key by key and two runs over one file iterate
    identically (AAP 0.13.1). The mappings are read-only views: a caller that could edit one
    could change a resolution after the fact.

    Attributes:
        source: The descriptor the values were read through, or ``None`` when no file applied.
        base: The base configuration as ``dotted key -> value``, sorted: what a
            ``foundry.toml`` states at top level, or what ``[tool.foundry]`` holds, minus the
            ``profiles`` table.
        profiles: One entry per profile table, keyed by profile name and sorted; each value is
            that table as ``dotted key -> value``, also sorted.
        label: The path as text for :attr:`source`, or ``""`` when there is none -- the detail
            a ``file`` layer :class:`~foundry.config.sources.ConfigSource` records.
    """

    source: DiscoveredFile | None
    base: Mapping[str, object]
    profiles: Mapping[str, Mapping[str, object]]
    label: str

    def is_empty(self) -> bool:
        """Report whether the file contributed no configuration value at all.

        A file that only declares profile tables is not empty: those are values a selected
        profile contributes later.

        Returns:
            ``True`` when the payload holds neither a base key nor a profile table.
        """
        return len(self.base) == 0 and len(self.profiles) == 0

    def dotted_keys(self) -> tuple[str, ...]:
        """Return the base configuration's keys in sorted order.

        Returns:
            One dotted key per base value, sorted, so two loads of one file agree.
        """
        return tuple(sorted(self.base))

    def profile_names(self) -> tuple[str, ...]:
        """Return the declared profile names in sorted order.

        The caller decides whether a name is one of the four AAP 0.4.5 accepts and what a
        selected profile with no table means; this states only what the file declares.

        Returns:
            One profile name per table in :attr:`profiles`, sorted.
        """
        return tuple(sorted(self.profiles))


def default_payload() -> FilePayload:
    """Return the payload of a run with no configuration file.

    The caller that received ``None`` from :func:`discover_file` resolves from the field
    defaults and logs ``foundry: no config file found`` (AAP 0.4.7); this gives it the empty
    payload to merge, so no caller builds a :class:`FilePayload` by hand.

    Returns:
        A payload with no source, no values, no profiles and an empty label.
    """
    return _payload(source=None, base=MappingProxyType({}), profiles=MappingProxyType({}))


def load_file(discovered: DiscoveredFile | None) -> FilePayload:
    """Read the discovered file and return the values it contributes.

    The payload is validated key by key against the registry before it is returned -- the base
    table under the ``file`` layer and each profile table under its ``profile:<name>`` layer,
    both with the file's path as the detail, so a rejection names the file to edit as well as
    the closest valid key (AAP 0.4.5).

    Args:
        discovered: The descriptor :func:`discover_file` returned, or ``None`` when no file
            applies.

    Returns:
        The payload for that file; the empty payload from :func:`default_payload` when
        ``discovered`` is ``None``, and a payload carrying the descriptor but no values when a
        ``pyproject.toml`` nobody named turned out to be unreadable or malformed.

    Raises:
        foundry.core.exceptions.ConfigError: When a file named explicitly cannot be read or
            parsed, when a ``profiles`` table or a profile table has the wrong shape, when a
            profile table tries to set ``profile`` itself, or when the registry does not
            declare a key the file sets.
    """
    if discovered is None:
        return default_payload()
    document = discovered.document
    if document is None:
        document = _read_document(discovered)
    if document is None:
        return _payload(source=discovered, base=MappingProxyType({}), profiles=MappingProxyType({}))

    base, profiles = _split_document(document, discovered)
    detail = errors.file_detail(discovered.path)
    validation.validate_unknown_keys(base, layer=errors.LAYER_FILE, detail=detail)
    for name in sorted(profiles):
        validation.validate_unknown_keys(
            profiles[name], layer=errors.profile_layer(name), detail=detail
        )
    return _payload(source=discovered, base=base, profiles=profiles)


def _split_document(
    document: Mapping[str, object],
    discovered: DiscoveredFile,
) -> tuple[Mapping[str, object], Mapping[str, Mapping[str, object]]]:
    """Split a parsed document into its base configuration and its profile tables.

    The two kinds differ only in where the Foundry table sits: at the document's top level for
    a Foundry TOML document, under ``[tool.foundry]`` for a ``pyproject.toml``. From there both
    read the same way, which is what makes ``[profiles.ci]`` and
    ``[tool.foundry.profiles.ci]`` one feature with two spellings.

    Args:
        document: The parsed file.
        discovered: The descriptor being loaded, which supplies the kind.

    Returns:
        The base configuration and the profile tables, both flat and key-sorted.

    Raises:
        foundry.core.exceptions.ConfigError: When a table has the wrong shape, when a profile
            table sets ``profile`` itself, or when one nests another ``profiles`` table.
    """
    table = document if discovered.kind == "foundry-toml" else _foundry_table(document)
    if table is None:
        # Discovery selects only a pyproject.toml carrying the table, so this descriptor was
        # built by hand for a file that has none: it contributes nothing.
        return MappingProxyType({}), MappingProxyType({})
    base_source = {key: value for key, value in table.items() if key != PROFILES_KEY}
    return _flatten(base_source, layer=layers.ConfigLayer.FILE), _profile_tables(
        table.get(PROFILES_KEY)
    )


def _profile_tables(raw: object) -> Mapping[str, Mapping[str, object]]:
    """Return the profile tables of a document as flat, sorted mappings by name.

    Args:
        raw: The value the ``profiles`` key held, or ``None`` when the file declares none.

    Returns:
        One entry per profile, sorted by name.

    Raises:
        foundry.core.exceptions.ConfigError: When ``profiles`` is not a table of tables, when
            a profile's value is not a table, when that table sets ``profile`` itself, or when
            it nests a further ``profiles`` table.
    """
    if raw is None:
        return MappingProxyType({})
    declared = _as_table(raw)
    if declared is None:
        raise errors.invalid_shape(PROFILES_KEY, _PROFILE_TABLES_EXPECTED, layer=errors.LAYER_FILE)
    profiles: dict[str, Mapping[str, object]] = {}
    for name in sorted(declared):
        profile_table = _as_table(declared[name])
        if profile_table is None:
            raise errors.invalid_shape(
                f"{PROFILES_KEY}.{name}", _CONFIG_TABLE_EXPECTED, layer=errors.LAYER_FILE
            )
        if PROFILE_KEY in profile_table:
            raise errors.profile_override_attempt(name)
        if PROFILES_KEY in profile_table:
            raise errors.invalid_shape(
                f"{PROFILES_KEY}.{name}.{PROFILES_KEY}",
                _NO_PROFILE_NESTING_EXPECTED,
                layer=errors.profile_layer(name),
            )
        profiles[name] = _flatten(profile_table, layer=layers.ConfigLayer.PROFILE)
    return MappingProxyType(profiles)


def _flatten(table: Mapping[str, object], *, layer: layers.ConfigLayer) -> Mapping[str, object]:
    """Flatten one table into its dotted keys, sorted and read-only.

    :func:`foundry.config.layers.flatten` already visits keys in sorted order and keeps a
    table-valued key such as ``http.headers`` whole; the sort here makes that guarantee local
    to whatever the tree's own ordering becomes.

    Args:
        table: The nested table to flatten.
        layer: The layer the table belongs to, so a shape fault names it.

    Returns:
        One entry per leaf, keyed by its dotted path, sorted.

    Raises:
        foundry.core.exceptions.ConfigError: When a key is not a string, or when one key is
            defined both as a value and as the prefix of another.
    """
    return MappingProxyType(dict(sorted(layers.flatten(table, layer=layer).items())))


def _read_document(discovered: DiscoveredFile) -> Mapping[str, object] | None:
    """Parse the descriptor's file with :mod:`tomllib`, or report that it is unreadable.

    A read or parse fault is an error for the two kinds of file Foundry must be able to trust:
    one an operator named, and a ``foundry.toml`` -- a name only Foundry uses. It is not an
    error for a ``pyproject.toml`` discovery merely found, which may belong to another tool and
    whose breakage must not stop a run that never asked Foundry to read it; the caller logs
    that skip at ``DEBUG``.

    Args:
        discovered: The descriptor to read.

    Returns:
        The parsed document as a read-only view, or ``None`` when the file is one Foundry may
        skip and cannot be read or parsed.

    Raises:
        foundry.core.exceptions.ConfigError: ``unreadable_file`` when a file Foundry must trust
            cannot be read, ``malformed_toml`` when it is not UTF-8 text or not valid TOML,
            carrying the parser's location when it reported one.
    """
    tolerant = not discovered.explicit and discovered.kind == "pyproject"
    path = discovered.path
    try:
        text = path.read_bytes().decode("utf-8")
    except OSError as exc:
        if tolerant:
            return None
        raise errors.unreadable_file(path, str(exc)) from exc
    except UnicodeDecodeError as exc:
        if tolerant:
            return None
        raise errors.malformed_toml(path, f"{_NOT_UTF8} ({exc.reason})") from exc
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        if tolerant:
            return None
        detail, line, column = _parse_fault(exc)
        raise errors.malformed_toml(path, detail, line=line, column=column) from exc
    return MappingProxyType(document)


def _parse_fault(exc: tomllib.TOMLDecodeError) -> tuple[str, int | None, int | None]:
    """Return a parse fault's description with the location it already carries split off.

    The location is returned separately because
    :func:`foundry.config.errors.malformed_toml` appends it in AAP 0.4.5's format, so leaving it
    in the description would print it twice.

    Args:
        exc: The parser's exception.

    Returns:
        The description, then the 1-based line and column, each ``None`` when the parser
        reported no location and said ``(at end of document)`` instead.
    """
    message = str(exc)
    match = _LINE_COLUMN_RE.search(message)
    if match is not None:
        return message[: match.start()].rstrip(), int(match.group(1)), int(match.group(2))
    if message.endswith(_END_OF_DOCUMENT):
        return message[: -len(_END_OF_DOCUMENT)].rstrip(" :"), None, None
    return message, None, None


def _foundry_table(document: Mapping[str, object]) -> Mapping[str, object] | None:
    """Return the ``[tool.foundry]`` table of a ``pyproject.toml``, when it has one.

    Args:
        document: The parsed ``pyproject.toml``.

    Returns:
        The Foundry table, or ``None`` when the document has no ``[tool]`` table, no
        ``[tool.foundry]`` entry, or a ``[tool]`` that is not a table.

    Raises:
        foundry.core.exceptions.ConfigError: When ``tool.foundry`` is present but is not a
            table: that entry is an attempt to configure Foundry in a shape Foundry cannot
            read, which is worth reporting rather than passing over.
    """
    tool = _as_table(document.get(TOOL_TABLE))
    if tool is None:
        return None
    candidate = tool.get(TOOL_FOUNDRY)
    if candidate is None:
        return None
    table = _as_table(candidate)
    if table is None:
        raise errors.invalid_shape(
            f"{TOOL_TABLE}.{TOOL_FOUNDRY}", _CONFIG_TABLE_EXPECTED, layer=errors.LAYER_FILE
        )
    return table


def _as_table(value: object) -> Mapping[str, object] | None:
    """Return ``value`` as a table, or ``None`` when it is not one.

    Args:
        value: Any value read out of a parsed document.

    Returns:
        The value itself when it is a mapping, so a nested table keeps the ordering
        :func:`_flatten` sorts, and ``None`` for a scalar, a list or an absent entry.
    """
    if isinstance(value, Mapping):
        return value
    return None


def _payload(
    *,
    source: DiscoveredFile | None,
    base: Mapping[str, object],
    profiles: Mapping[str, Mapping[str, object]],
) -> FilePayload:
    """Build a payload, deriving the label from the descriptor that supplied it.

    Args:
        source: The descriptor the values came from, or ``None`` when no file applied.
        base: The base configuration, already flat and sorted.
        profiles: The profile tables, already flat and sorted.

    Returns:
        The payload, labelled with the source's path or with ``""`` when there is no source.
    """
    label = "" if source is None else source.label()
    return FilePayload(source=source, base=base, profiles=profiles, label=label)
