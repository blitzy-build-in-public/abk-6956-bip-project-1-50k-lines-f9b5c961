"""Configuration-file discovery and the TOML loader behind the file layer.

AAP 0.4.5 fixes one discovery order and nothing searches beyond it: ``--foundry-config PATH``, then
``FOUNDRY_CONFIG``, then ``foundry.toml`` in the invocation directory, then ``[tool.foundry]`` in
that directory's ``pyproject.toml``, then the field defaults -- first match wins. Discovery is
anchored to the invocation directory, never to the test file (assumption A9), and only a
``pyproject.toml`` that parses but carries no ``[tool.foundry]`` falls through: every other absent
or unreadable candidate is a fault (AAP 0.4.7). :func:`discover_file` answers which file applies and
:func:`load_file` what it holds, as the flat dotted keys plus profile tables the layers merge.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import Final, Literal

from foundry.config import errors, keys, layers, validation
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
"""The five discovery steps in the order they are tried, first match wins (AAP 0.4.5)."""


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

    What :func:`load_file` reads, and what :func:`foundry.config.merge.resolve` turns into a
    ``ConfigSource`` for the ``file`` layer. Frozen and slotted, being built once by discovery and
    read by the loader and the source recorder, neither of which may see it change under them.

    Attributes:
        path: The file, absolute and normalised. A relative path an operator typed is resolved
            against the invocation directory :func:`discover_file` was given, never against the
            process working directory.
        kind: How the file is read -- as a Foundry TOML document or through ``[tool.foundry]``.
        origin: The discovery step that selected it.
        document: The parsed document, kept only where discovery had to parse the file to decide
            whether it configures Foundry -- the ``pyproject.toml`` step -- so :func:`load_file`
            does not parse it twice; a cache of the file's content rather than part of what
            identifies the descriptor, so it is left out of comparison and ``repr``.
    """

    path: Path
    kind: FileKind
    origin: FileOrigin
    document: Mapping[str, object] | None = field(default=None, compare=False, repr=False)

    def label(self) -> str:
        """Return the path as the text a ``file`` layer source records as its detail.

        ``foundry config show`` prints it after ``file:``, so it is the resolved path and not the
        spelling the operator typed (AAP 0.4.5, AAP 0.7.4).
        """
        return str(self.path)


def discover_file(
    *,
    cli_path: Path | None = None,
    environ: Mapping[str, str],
    cwd: Path,
) -> DiscoveredFile | None:
    """Select the configuration file that applies, walking the five steps of AAP 0.4.5.

    First match wins, and a step that finds no file is not a fault: resolution continues to the next
    step and, when none matches, the caller resolves from the field defaults and logs
    ``foundry: no config file found``. A relative path the operator named is resolved against
    ``cwd``, so ``--foundry-config data/suite.toml`` means the invocation directory.

    Returns:
        The selected file, or ``None`` when no step matched and the defaults apply.

    Raises:
        foundry.core.exceptions.ConfigError: When an explicitly named file does not exist or is
            not a regular file -- ``config file not found: <path>``, AAP 0.4.7, exit 2 at the
            caller -- when the ``pyproject.toml`` candidate cannot be read or parsed, and when it
            carries a ``[tool.foundry]`` that is not a table.
    """
    named = _named_path(cli_path)
    if named is not None:
        return _explicit_file(named, origin="cli", cwd=cwd)

    from_environment = _environment_path(environ)
    if from_environment is not None:
        return _explicit_file(from_environment, origin="env", cwd=cwd)

    local = _absolute(Path(FOUNDRY_TOML), cwd)
    if local.is_file():
        return DiscoveredFile(path=local, kind="foundry-toml", origin="cwd")

    return _local_pyproject(cwd)


def _named_path(cli_path: Path | None) -> Path | None:
    """Return the path ``--foundry-config`` supplied, or ``None`` when it supplied none.

    ``cli_path`` is the recorded option value; an empty string is already read as "not supplied" by
    :mod:`foundry.config.cli_layer`, and the whitespace test here closes that hole for a caller
    handing a path over directly.
    """
    if cli_path is None or not str(cli_path).strip():
        return None
    return cli_path


def _environment_path(environ: Mapping[str, str]) -> Path | None:
    """Return the path ``FOUNDRY_CONFIG`` names in ``environ``, matched case-insensitively.

    AAP 0.4.5 makes every ``FOUNDRY_*`` variable case-insensitive, and POSIX lets both spellings
    stand at once: the canonical upper-case spelling wins when both are present, and otherwise the
    candidates are visited in sorted order, so the file selected never depends on the order the
    mapping happened to hold.

    Returns:
        The path the variable names, or ``None`` when no spelling of it is set or its value is
        empty or whitespace -- an empty variable selects no file rather than the current directory.
    """
    spellings = sorted(name for name in environ if name.upper() == ENV_CONFIG_VAR)
    if not spellings:
        return None
    chosen = ENV_CONFIG_VAR if ENV_CONFIG_VAR in environ else spellings[0]
    value = environ[chosen].strip()
    return Path(value) if value else None


def _explicit_file(named: Path, *, origin: FileOrigin, cwd: Path) -> DiscoveredFile:
    """Resolve a file the operator named and refuse it when it is not there.

    An explicitly named file is read as a full Foundry TOML document: its top-level keys are the
    base configuration, exactly as in a ``foundry.toml``, so naming a ``pyproject.toml`` this way
    does not switch to the ``[tool.foundry]`` reading.

    Raises:
        foundry.core.exceptions.ConfigError: When the path does not exist or is not a regular
            file; the message is the AAP 0.4.7 sentence ``config file not found: <path>``.
    """
    path = _absolute(named, cwd)
    if not path.is_file():
        raise errors.missing_file(path)
    return DiscoveredFile(path=path, kind="foundry-toml", origin=origin)


def _local_pyproject(cwd: Path) -> DiscoveredFile | None:
    """Return ``cwd``'s ``pyproject.toml`` when it configures Foundry.

    The fourth step of AAP 0.4.5 matches only a ``pyproject.toml`` that actually carries a
    ``[tool.foundry]`` table, and deciding that requires reading the file -- so the parsed document
    is kept on the descriptor and :func:`load_file` reuses it. Reading it is not a lenient step: a
    candidate that cannot be decoded or parsed faults exactly as a malformed ``foundry.toml`` does,
    because passing it over would resolve the run from the defaults while the operator's own table
    sat unread. A document that parses but carries no ``[tool.foundry]`` configures nothing, and
    that is the one case that falls through to the defaults (AAP 0.4.5, AAP 0.4.7).

    Raises:
        foundry.core.exceptions.ConfigError: ``unreadable_file`` when the file cannot be read,
            ``malformed_toml`` when it is not UTF-8 text or not valid TOML, and ``invalid_shape``
            when it carries a ``[tool.foundry]`` that is not a table.
    """
    path = _absolute(Path(PYPROJECT_TOML), cwd)
    if not path.is_file():
        return None
    probe = DiscoveredFile(path=path, kind="pyproject", origin="pyproject")
    document = _read_document(probe)
    if _foundry_table(document) is None:
        return None
    return replace(probe, document=document)


def _absolute(path: Path, cwd: Path) -> Path:
    """Resolve ``path`` against ``cwd`` and normalise it.

    Returns:
        The absolute, symlink-resolved path. A path that cannot be resolved -- a symlink loop,
        which no configuration file can have -- is returned as its absolute spelling so that the
        existence check reports it instead of an unrelated fault.
    """
    candidate = path if path.is_absolute() else cwd / path
    try:
        return candidate.resolve()
    except OSError:
        return candidate


@dataclass(frozen=True, slots=True)
class FilePayload:
    """What the resolved configuration file contributes, in the form the layers merge.

    :mod:`foundry.config.merge` applies this payload as the ``file`` layer and, for each profile, as
    a ``profile:<name>`` layer above it (AAP 0.4.5). Everything is already flat and key-sorted, so
    a payload can be attributed key by key (AAP 0.13.1), and the mappings are read-only views: a
    caller that could edit one could change a resolution after the fact.

    Attributes:
        source: The descriptor the values were read through, or ``None`` when no file applied.
        base: The base configuration as ``dotted key -> value``, sorted: what a ``foundry.toml``
            states at top level, or what ``[tool.foundry]`` holds, minus the ``profiles`` table.
        profiles: One entry per profile table, keyed by profile name and sorted; each value is
            that table as ``dotted key -> value``, also sorted.
        label: The path as text for :attr:`source`, or ``""`` when there is none.
    """

    source: DiscoveredFile | None
    base: Mapping[str, object]
    profiles: Mapping[str, Mapping[str, object]]
    label: str

    def is_empty(self) -> bool:
        """Report whether the file contributed no configuration value at all.

        A file that only declares profile tables is not empty: those are values a selected profile
        contributes later.
        """
        return len(self.base) == 0 and len(self.profiles) == 0

    def dotted_keys(self) -> tuple[str, ...]:
        """Return the base configuration's keys in sorted order, so two loads of one file agree."""
        return tuple(sorted(self.base))

    def profile_names(self) -> tuple[str, ...]:
        """Return the declared profile names in sorted order.

        The caller decides whether a name is one of the four AAP 0.4.5 accepts and what a selected
        profile with no table means; this states only what the file declares.
        """
        return tuple(sorted(self.profiles))


def default_payload() -> FilePayload:
    """Return the payload of a run with no configuration file.

    The caller that received ``None`` from :func:`discover_file` resolves from the field defaults
    and logs ``foundry: no config file found`` (AAP 0.4.7); this gives it the empty payload to
    merge, so no caller builds a :class:`FilePayload` by hand.
    """
    return _payload(source=None, base=MappingProxyType({}), profiles=MappingProxyType({}))


def load_file(discovered: DiscoveredFile | None) -> FilePayload:
    """Read the discovered file and return the values it contributes.

    The payload is validated key by key against the registry before it is returned -- the base
    table under the ``file`` layer and each profile table under its ``profile:<name>`` layer, both
    with the file's path as the detail, so a rejection names the file to edit as well as the
    closest valid key (AAP 0.4.5).

    Returns:
        The payload for that file, or the empty payload from :func:`default_payload` when
        ``discovered`` is ``None``.

    Raises:
        foundry.core.exceptions.ConfigError: When the file cannot be read or parsed, when a
            ``profiles`` table or a profile table has the wrong shape, when a profile table tries
            to set ``profile``, or when the registry does not declare a key or a table name the
            file sets.
    """
    if discovered is None:
        return default_payload()
    document = discovered.document
    if document is None:
        document = _read_document(discovered)

    detail = errors.file_detail(discovered.path)
    base, profiles = _split_document(document, discovered, detail=detail)
    validation.validate_unknown_keys(base, layer=errors.LAYER_FILE, detail=detail)
    for name in sorted(profiles):
        validation.validate_unknown_keys(
            profiles[name], layer=errors.profile_layer(name), detail=detail
        )
    return _payload(source=discovered, base=base, profiles=profiles)


def _split_document(
    document: Mapping[str, object],
    discovered: DiscoveredFile,
    *,
    detail: str,
) -> tuple[Mapping[str, object], Mapping[str, Mapping[str, object]]]:
    """Split a parsed document into its base configuration and its profile tables.

    The two kinds differ only in where the Foundry table sits -- at the document's top level for a
    Foundry TOML document, under ``[tool.foundry]`` for a ``pyproject.toml`` -- which is what makes
    ``[profiles.ci]`` and ``[tool.foundry.profiles.ci]`` one feature with two spellings. Flattening
    keeps one entry per leaf, so :func:`_reject_keyless_tables` reads the unflattened tree first
    (AAP 0.4.5).

    Returns:
        The base configuration and the profile tables, both flat and key-sorted.

    Raises:
        foundry.core.exceptions.ConfigError: When a table has the wrong shape, when a profile table
            sets ``profile``, when one nests another ``profiles`` table, or when a keyless table is
            not a registered table path.
    """
    table = document if discovered.kind == "foundry-toml" else _foundry_table(document)
    if table is None:
        # Discovery selects only a pyproject.toml carrying the table, so this descriptor was
        # built by hand for a file that has none: it contributes nothing.
        return MappingProxyType({}), MappingProxyType({})
    base_source = {key: value for key, value in table.items() if key != PROFILES_KEY}
    _reject_keyless_tables(base_source, layer=errors.LAYER_FILE, detail=detail)
    return _flatten(base_source, layer=layers.ConfigLayer.FILE), _profile_tables(
        table.get(PROFILES_KEY), detail=detail
    )


def _profile_tables(raw: object, *, detail: str) -> Mapping[str, Mapping[str, object]]:
    """Return the profile tables of a document as flat, sorted mappings by name.

    Each profile table is checked for keyless section headers before it is flattened, under its own
    ``profile:<name>`` layer, so a header that holds nothing is refused in the table that carries it
    (AAP 0.4.5).

    Raises:
        foundry.core.exceptions.ConfigError: When ``profiles`` is not a table of tables, when a
            profile's value is not a table, when that table sets ``profile``, when it nests a
            further ``profiles`` table, or when it holds a keyless table that is not a registered
            table path.
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
        _reject_keyless_tables(profile_table, layer=errors.profile_layer(name), detail=detail)
        profiles[name] = _flatten(profile_table, layer=layers.ConfigLayer.PROFILE)
    return MappingProxyType(profiles)


def _reject_keyless_tables(
    table: Mapping[str, object],
    *,
    prefix: str = "",
    layer: str,
    detail: str,
) -> None:
    """Refuse a section header that holds nothing and names no configuration key.

    Flattening keeps one entry per leaf, so ``[data.bogus]`` would reach the settings model as
    though the header were not there. A table name is a key in TOML terms, so this walk reads the
    unflattened tree and reports the fault a scalar of that spelling gets (AAP 0.4.5). Two shapes
    pass: a key the registry declares (``[http.headers]`` is table-valued, empty or not) and a
    path some published key sits under, such as ``[http.auth]``.

    Raises:
        foundry.core.exceptions.ConfigError: ``unknown_key`` for the first keyless table in sorted
            key order whose dotted path is neither a registered key nor a path some registered key
            sits under, carrying the closest registered spelling and the layer that supplied it.
    """
    for key in sorted(name for name in table if isinstance(name, str)):
        value = table[key]
        if not isinstance(value, Mapping):
            continue
        dotted = f"{prefix}.{key}" if prefix else key
        if keys.spec_for(dotted) is not None:
            continue
        if value:
            _reject_keyless_tables(value, prefix=dotted, layer=layer, detail=detail)
            continue
        if not any(leaf.startswith(f"{dotted}.") for leaf in keys.leaf_keys()):
            raise errors.unknown_key(
                dotted, closest=keys.closest_key(dotted), layer=layer, detail=detail
            )


def _flatten(table: Mapping[str, object], *, layer: layers.ConfigLayer) -> Mapping[str, object]:
    """Flatten one table into its dotted keys, sorted and read-only.

    :func:`foundry.config.layers.flatten` already visits keys in sorted order and keeps a
    table-valued key such as ``http.headers`` whole; the sort here makes that guarantee local.

    Raises:
        foundry.core.exceptions.ConfigError: When a key is not a string, or when one key is defined
            both as a value and as the prefix of another.
    """
    return MappingProxyType(dict(sorted(layers.flatten(table, layer=layer).items())))


def _read_document(discovered: DiscoveredFile) -> Mapping[str, object]:
    """Parse the descriptor's file with :mod:`tomllib`.

    Every candidate discovery can select is read the same way, whether an operator named it or
    Foundry found it: a file that cannot be decoded or parsed is a fault in both cases, because
    skipping it would resolve the run from the defaults while the table its operator wrote sat
    unread (AAP 0.4.5, AAP 0.4.7).

    Raises:
        foundry.core.exceptions.ConfigError: ``unreadable_file`` when the file cannot be read,
            ``malformed_toml`` when it is not UTF-8 text or not valid TOML, carrying the parser's
            location when it reported one.
    """
    path = discovered.path
    try:
        text = path.read_bytes().decode("utf-8")
    except OSError as exc:
        raise errors.unreadable_file(path, str(exc)) from exc
    except UnicodeDecodeError as exc:
        raise errors.malformed_toml(path, f"{_NOT_UTF8} ({exc.reason})") from exc
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        detail, line, column = _parse_fault(exc)
        raise errors.malformed_toml(path, detail, line=line, column=column) from exc
    return MappingProxyType(document)


def _parse_fault(exc: tomllib.TOMLDecodeError) -> tuple[str, int | None, int | None]:
    """Return a parse fault's description with the location it already carries split off.

    The location is returned separately because :func:`foundry.config.errors.malformed_toml` appends
    it in AAP 0.4.5's format, so leaving it in the description would print it twice.

    Returns:
        The description, then the 1-based line and column, each ``None`` when the parser reported
        no location and said ``(at end of document)`` instead.
    """
    message = str(exc)
    match = _LINE_COLUMN_RE.search(message)
    if match is not None:
        return message[: match.start()].rstrip(), int(match.group(1)), int(match.group(2))
    if message.endswith(_END_OF_DOCUMENT):
        return message[: -len(_END_OF_DOCUMENT)].rstrip(" :"), None, None
    return message, None, None


def _foundry_table(document: Mapping[str, object]) -> Mapping[str, object] | None:
    """Return the ``[tool.foundry]`` table of a parsed ``pyproject.toml``, when it has one.

    Returns:
        The Foundry table, or ``None`` when the document has no ``[tool]`` table, no
        ``[tool.foundry]`` entry, or a ``[tool]`` that is not a table.

    Raises:
        foundry.core.exceptions.ConfigError: When ``tool.foundry`` is present but is not a table:
            that entry attempts to configure Foundry in a shape Foundry cannot read, which is
            worth reporting rather than passing over.
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

    A nested table is returned as given, so it keeps the ordering :func:`_flatten` sorts.
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

    ``base`` and ``profiles`` arrive already flat and sorted.
    """
    label = "" if source is None else source.label()
    return FilePayload(source=source, base=base, profiles=profiles, label=label)
