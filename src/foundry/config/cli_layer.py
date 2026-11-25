"""The command-line layer: the ``--foundry-*`` overrides an operator typed.

:class:`CliInput` is the only shape :mod:`foundry.config.merge` needs: a dotted key's coerced value
and the option spelling that supplied it, which ``foundry config show --resolved`` attributes to
``cli:<option>``. :data:`SPECIAL_OPTIONS` configure no key.
"""

from __future__ import annotations

import types
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal, TypeVar

from foundry.config import errors, keys, validation
from foundry.config.enums import CassetteMode
from foundry.core.exceptions import ConfigError

if TYPE_CHECKING:
    import pytest

__all__: Final[tuple[str, ...]] = (
    "CliInput",
    "EMPTY_CLI_INPUT",
    "GENERIC_OPTION_PREFIX",
    "SPECIAL_OPTIONS",
    "SpecialOption",
    "alias_dest",
    "collect_from_pytest",
    "from_mapping",
)

GENERIC_OPTION_PREFIX: Final[str] = keys.GENERIC_OPTION_PREFIX
"""Prefix of the option generated for every scalar key, re-exported from the registry."""

_ValueT = TypeVar("_ValueT")

_CONFIG_OPTION: Final[str] = "--foundry-config"
_JSON_REPORT_OPTION: Final[str] = "--foundry-json-report"
_RECORD_OPTION: Final[str] = "--foundry-record"
_REPLAY_OPTION: Final[str] = "--foundry-replay"

_ALIAS_DEST_PREFIX: Final[str] = "foundry_"
"""Leading part of the pytest ``dest`` a named option stores its value under."""

_OPTION_WORD: Final[str] = GENERIC_OPTION_PREFIX.removeprefix("--")
"""``foundry-``: the word every option of this layer starts with, once its dashes are gone."""

_OPTION_VALUE_SEPARATOR: Final[str] = "="
"""Separator between an option spelling and a value written inline, as in ``--opt=value``."""

_CASSETTE_MODE_KEY: Final[str] = "cassette.mode"
"""The key the record and replay switches select a mode on (AAP 0.4.4)."""

_PATH_EXPECTED: Final[str] = "a path"
"""Expected type named when a path-valued option carries something that is not a path."""

_FLAG_OFF_WORDS: Final[frozenset[str]] = frozenset({"", "false", "0", "no", "off"})
"""The spellings of a supplied-but-disabled flag.

A caller building a mapping by hand may write ``"false"``, which must not turn recording on.
"""


@dataclass(frozen=True, slots=True)
class SpecialOption:
    """A named ``--foundry-*`` option that configures no configuration key.

    They are described here rather than in :data:`foundry.config.keys.KEY_SPECS`, because that
    registry is the table of the configuration surface: ``--foundry-json-report`` in particular is
    not a key, moving a file without changing a resolved value.

    Attributes:
        spelling: The option as an operator writes it, also the ``cli:<option>`` detail reported.
        kind: The job the option does, which decides where :func:`from_mapping` records it.
    """

    spelling: str
    kind: Literal["config-path", "json-report", "record-flag", "replay-flag"]


SPECIAL_OPTIONS: Final[tuple[SpecialOption, ...]] = (
    SpecialOption(spelling=_CONFIG_OPTION, kind="config-path"),
    SpecialOption(spelling=_JSON_REPORT_OPTION, kind="json-report"),
    SpecialOption(spelling=_RECORD_OPTION, kind="record-flag"),
    SpecialOption(spelling=_REPLAY_OPTION, kind="replay-flag"),
)
"""The four named options that configure no key, in the order AAP 0.4.4 documents them.

The three key-bound aliases are absent: the :class:`~foundry.config.keys.KeySpec` they set declares
them, so the registry stays the single table of what configures a key.
"""

_SPECIAL_BY_SPELLING: Final[Mapping[str, SpecialOption]] = types.MappingProxyType(
    {option.spelling: option for option in SPECIAL_OPTIONS}
)
"""The special options by spelling, for the lookup :func:`from_mapping` performs per entry."""


def _frozen_by_key(values: Mapping[str, _ValueT]) -> Mapping[str, _ValueT]:
    """Return a read-only copy of ``values``, ordered by sorted key.

    Defined ahead of :class:`CliInput` because :data:`EMPTY_CLI_INPUT` is built at import time.
    """
    return types.MappingProxyType({key: values[key] for key in sorted(values)})


@dataclass(frozen=True, slots=True)
class CliInput:
    """Everything the command line supplied, coerced, with the spelling behind each value.

    The carrier :mod:`foundry.config.merge` applies as its highest layer: a value alone is not
    enough, because ``foundry config show --resolved`` reports ``cli:--foundry-http.timeout``, and
    the two path-valued options travel beside both because they configure no key.

    Attributes:
        overrides: The coerced value of each overridden key, by dotted key.
        spellings: The option spelling that supplied each of those values, by dotted key.
        config_path: The configuration file ``--foundry-config`` named, as typed, or ``None``.
        json_report_path: The ``results.json`` path ``--foundry-json-report`` named, or ``None``.
    """

    overrides: Mapping[str, object] = field(default_factory=dict)
    spellings: Mapping[str, str] = field(default_factory=dict)
    config_path: Path | None = None
    json_report_path: Path | None = None

    def __post_init__(self) -> None:
        """Freeze both mappings into key-sorted read-only views.

        A frozen dataclass still holds a mutable ``dict``, and the sorted order is what reaches the
        source table and the debug log line.
        """
        object.__setattr__(self, "overrides", _frozen_by_key(self.overrides))
        object.__setattr__(self, "spellings", _frozen_by_key(self.spellings))

    @classmethod
    def empty(cls) -> CliInput:
        """Return the input of a command line that supplied no ``--foundry-*`` option."""
        return cls()

    def is_empty(self) -> bool:
        """Return whether the command line supplied nothing at all.

        Returns:
            ``True`` when no key was overridden and neither path was named.
        """
        return not self.overrides and self.config_path is None and self.json_report_path is None

    def dotted_keys(self) -> tuple[str, ...]:
        """Return the overridden keys in sorted order.

        Returns:
            One dotted key per override, sorted.
        """
        return tuple(sorted(self.overrides))

    def spelling_for(self, dotted: str) -> str | None:
        """Return the option spelling that supplied ``dotted``, or ``None`` when it was not given.

        Args:
            dotted: The dotted configuration key, for example ``http.timeout``.
        """
        return self.spellings.get(dotted)


EMPTY_CLI_INPUT: Final[CliInput] = CliInput.empty()
"""The immutable "nothing was supplied" input :func:`foundry.config.merge.resolve` defaults to."""


def alias_dest(option: str) -> str:
    """Return the pytest ``dest`` a named ``--foundry-*`` option stores its value under.

    The single statement of the convention two modules must agree on: :mod:`foundry.plugin`
    registers each named option with exactly this ``dest`` and :func:`collect_from_pytest` reads
    it back through it, so a mismatch would silently drop the override. A generic
    ``--foundry-<dotted.key>`` option uses :func:`foundry.config.keys.option_dest` instead, a dot
    being no attribute name.

    Args:
        option: The spelling, with or without leading dashes or an inline value.

    Returns:
        The ``dest``, for example ``foundry_json_report``.
    """
    spelling = option.split(_OPTION_VALUE_SEPARATOR, 1)[0].strip().lstrip("-")
    if spelling.startswith(_OPTION_WORD):
        spelling = spelling[len(_OPTION_WORD) :]
    return _ALIAS_DEST_PREFIX + spelling.replace("-", "_")


def from_mapping(raw: Mapping[str, object], *, cwd: Path | None = None) -> CliInput:
    """Collect the ``--foundry-*`` overrides a command line supplied.

    The entry point both surfaces share -- click's mapping and the one :func:`collect_from_pytest`
    reads from a :class:`pytest.Config` -- so the spelling rules, the coercions and the error
    wording exist once. An entry is keyed by an option spelling, optionally carrying its value
    inline, or by a dotted key, and entries are visited in sorted order.

    An entry whose value is ``None`` is skipped, as is a path option given as an empty string.
    ``--foundry-config`` and ``--foundry-json-report`` are recorded exactly as typed, ``cwd``
    changing nothing that is recorded; ``--foundry-record`` with ``--foundry-replay`` is a usage
    error, and either alone sets ``cassette.mode``, winning over a mode in the same mapping.

    Every value is coerced by :func:`foundry.config.validation.coerce_cli_value` and the spelling
    that supplied it is recorded beside it. A table- or list-valued key raises instead, because the
    generic option is registered for the scalar keys only, and
    :func:`foundry.config.errors.option_not_settable` names the file layer that can carry it.

    Returns:
        The overrides, the spelling behind each of them and the two path options.

    Raises:
        ConfigError: For an undeclared key or option, an unparseable value, a
            ``--foundry-record`` / ``--foundry-replay`` combination, or a non-scalar key. Each
            ends a run with exit code 2.
    """
    overrides: dict[str, object] = {}
    spellings: dict[str, str] = {}
    config_path: Path | None = None
    json_report_path: Path | None = None
    flags: dict[str, bool] = {}

    for entry in sorted(raw):
        supplied = raw[entry]
        if entry.startswith("-"):
            spelling, inline = _split_option(entry)
            if supplied is None:
                supplied = inline
            if supplied is None:
                continue
            special = _SPECIAL_BY_SPELLING.get(spelling)
            if special is not None:
                if special.kind == "config-path":
                    config_path = _path_option(spelling, supplied)
                elif special.kind == "json-report":
                    json_report_path = _path_option(spelling, supplied)
                else:
                    flags[spelling] = _flag_enabled(supplied)
                continue
            dotted, spec = _registered_key(spelling)
        else:
            if supplied is None:
                continue
            dotted = entry
            spelling = keys.cli_option(dotted)
            declared = keys.spec_for(dotted)
            if declared is None:
                raise errors.unknown_key(
                    dotted,
                    closest=keys.closest_key(dotted),
                    layer=errors.LAYER_CLI,
                    detail=spelling,
                )
            spec = declared
        if not keys.is_scalar(spec):
            raise errors.option_not_settable(spelling, dotted)
        overrides[dotted] = validation.coerce_cli_value(spec, supplied, option=spelling)
        spellings[dotted] = spelling

    selected = _selected_mode(flags)
    if selected is not None:
        mode_spelling, mode = selected
        overrides[_CASSETTE_MODE_KEY] = mode
        spellings[_CASSETTE_MODE_KEY] = mode_spelling

    return CliInput(
        overrides=overrides,
        spellings=spellings,
        config_path=config_path,
        json_report_path=json_report_path,
    )


def collect_from_pytest(config: pytest.Config) -> CliInput:
    """Collect the ``--foundry-*`` overrides a :class:`pytest.Config` carries.

    Reads the values back through the two ``dest`` conventions and hands them to
    :func:`from_mapping`, so there is one implementation of the coercion rules. Every read passes
    ``default=None``, so a build that registered fewer options degrades to "no override supplied"
    instead of raising inside ``pytest_configure``.
    """
    raw: dict[str, object] = {}
    for spec in keys.KEY_SPECS:
        if keys.is_scalar(spec):
            _remember(
                raw,
                keys.cli_option(spec.dotted),
                config.getoption(keys.option_dest(spec.dotted), default=None),
            )
        for alias in spec.aliases:
            _remember(raw, alias, config.getoption(alias_dest(alias), default=None))
    for special in SPECIAL_OPTIONS:
        _remember(
            raw, special.spelling, config.getoption(alias_dest(special.spelling), default=None)
        )
    return from_mapping(raw)


def _registered_key(spelling: str) -> tuple[str, keys.KeySpec]:
    """Return the key and the registry spec an option spelling configures.

    Raises:
        ConfigError: When no key is bound to the spelling, naming the closest registered spelling
            in the hint and the option in the detail.
    """
    dotted = keys.option_to_key(spelling)
    spec = keys.spec_for(dotted) if dotted is not None else None
    if spec is None:
        raise _unknown_option(spelling)
    return spec.dotted, spec


def _unknown_option(spelling: str) -> ConfigError:
    """Build the error for an option spelling that configures no registered key.

    Returns:
        The :class:`ConfigError` the caller raises, naming the key, the closest one and the layer.
    """
    candidate = spelling.removeprefix(GENERIC_OPTION_PREFIX) or spelling
    return errors.unknown_key(
        candidate,
        closest=keys.closest_key(candidate),
        layer=errors.LAYER_CLI,
        detail=spelling,
    )


def _split_option(entry: str) -> tuple[str, str | None]:
    """Split an option entry into its spelling and any value written inline.

    ``--foundry-http.timeout=1.0`` is one mapping key when a caller builds the mapping from a raw
    argument list, and the value still belongs to the option.
    """
    spelling, separator, inline = entry.partition(_OPTION_VALUE_SEPARATOR)
    return errors.cli_detail(spelling), inline if separator else None


def _path_option(spelling: str, value: object) -> Path | None:
    """Return the path a path-valued option carries, exactly as typed, or ``None`` when empty.

    Raises:
        ConfigError: When the value is neither text nor a path, naming the option and expecting a
            path; these two options configure no key, so the option is also the error's key.
    """
    if isinstance(value, Path):
        return value
    if isinstance(value, str):
        text = value.strip()
        return Path(text) if text else None
    raise errors.bad_option_value(spelling, _PATH_EXPECTED, key=spelling)


def _flag_enabled(value: object) -> bool:
    """Return whether a supplied switch was turned on.

    ``click`` and ``pytest`` hand over a real ``bool``, and a hand-built mapping may write the word
    instead, so a flag written as ``"false"`` does not silently turn recording on.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in _FLAG_OFF_WORDS
    return bool(value)


def _selected_mode(flags: Mapping[str, bool]) -> tuple[str, CassetteMode] | None:
    """Return the cassette mode the supplied switches select, with the one to attribute it to.

    Returns:
        ``("--foundry-record", CassetteMode.RECORD)``, ``("--foundry-replay",
        CassetteMode.REPLAY)``, or ``None`` when neither switch was turned on.

    Raises:
        ConfigError: When both were turned on, which the command line makes a usage error. The
            conflict is decided after every entry has been read, so it is reported whatever order
            they came in.
    """
    recording = flags.get(_RECORD_OPTION, False)
    replaying = flags.get(_REPLAY_OPTION, False)
    if recording and replaying:
        raise errors.both_record_and_replay()
    if recording:
        return _RECORD_OPTION, CassetteMode.RECORD
    if replaying:
        return _REPLAY_OPTION, CassetteMode.REPLAY
    return None


def _remember(raw: dict[str, object], spelling: str, value: object) -> None:
    """Add a supplied value to the raw payload, ignoring an option that was not supplied.

    Args:
        raw: The payload being built, mutated through the caller's dict.
        spelling: The option spelling the value belongs to.
        value: The value read from the parser; ``None`` means the option was not supplied.
    """
    if value is not None:
        raw[spelling] = value
