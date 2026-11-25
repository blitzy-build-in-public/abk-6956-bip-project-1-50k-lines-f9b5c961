"""The command-line layer: the ``--foundry-*`` overrides an operator typed.

:class:`CliInput` is the only shape :mod:`foundry.config.merge` needs: a dotted key's coerced value
and the option spelling that supplied it, which ``foundry config show --resolved`` attributes to
``cli:<option>``. :data:`SPECIAL_OPTIONS` configure no key.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal, TypeVar

from foundry.config import errors, keys, validation
from foundry.config.enums import CassetteMode
from foundry.core.exceptions import ConfigError
from foundry.obs import events

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
    "fold_option_values",
    "from_mapping",
    "register_unregistered_options",
    "restore_preparse_state",
    "unregistered_spellings",
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

_CONFIG_PLUGIN_NAME: Final[str] = "pytestconfig"
"""The name pytest registers its own :class:`pytest.Config` under, which carries the argument list.

``pytest_addoption`` receives the parser and the plugin manager but no arguments, so the one
placeholder registration reads the list the ``Config`` already holds.
"""

_UNREGISTERED_HELP_TEMPLATE: Final[str] = "{spelling} configures no Foundry key and is rejected"
"""The help text of a placeholder option, which only a run's own ``--help`` could ever show."""

_FLAG_OFF_WORDS: Final[frozenset[str]] = frozenset({"", "false", "0", "no", "off"})
"""The spellings of a supplied-but-disabled flag.

A caller building a mapping by hand may write ``"false"``, which must not turn recording on.
"""

_OPTION_DASH: Final[str] = "-"
"""The character that marks a token as an option, and so never as a collection argument."""

_VALUELESS_SPECIAL_KINDS: Final[frozenset[str]] = frozenset({"record-flag", "replay-flag"})
"""The two switches: no token after one of them belongs to the option (AAP 0.4.4)."""

_REPAIR_FAULT: Final[str] = (
    "could not re-derive the root directory after the pre-parse read a Foundry option value as a "
    "path, so the run keeps pytest's reading; no outcome or exit code changes (AAP 0.1.2)"
)
"""Logged at ``WARNING`` when pytest's pre-parse internals answer differently than expected."""


@dataclass(frozen=True, slots=True)
class SpecialOption:
    """A named ``--foundry-*`` option that configures no configuration key.

    They are described here rather than in :data:`foundry.config.keys.KEY_SPECS`, the registry of
    the configuration surface, because ``--foundry-json-report`` changes no resolved value.

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


def _is_registered_option(spelling: str) -> bool:
    """Report whether :mod:`foundry.plugin` registers an ``argparse`` option for ``spelling``.

    The plugin registers three families: the four :data:`SPECIAL_OPTIONS`, the generic spelling of
    every scalar key and each declared alias. A list- or table-valued key therefore has no option of
    its own -- ``--foundry-http.headers`` is carried by the file layer alone -- and
    :func:`from_mapping` refuses it with :func:`foundry.config.errors.option_not_settable` once the
    placeholder below has made the token parseable.
    """
    if spelling in _SPECIAL_BY_SPELLING:
        return True
    dotted = keys.option_to_key(spelling)
    spec = keys.spec_for(dotted) if dotted is not None else None
    return spec is not None and keys.is_scalar(spec)


def unregistered_spellings(args: Iterable[str]) -> tuple[str, ...]:
    """Return the ``--foundry-*`` spellings in ``args`` that no registered option answers.

    Every token starting with :data:`GENERIC_OPTION_PREFIX` is examined, in the order it was typed
    and once each, with an inline value dropped, so what comes back is the spelling alone.
    Two consumers, one scan: :func:`register_unregistered_options` registers a placeholder for each
    spelling before pytest parses the command line, and :func:`collect_from_pytest` reads the value
    back through it so the configuration layer can reject the key.
    """
    found: list[str] = []
    for token in args:
        if not token.startswith(GENERIC_OPTION_PREFIX):
            continue
        spelling = token.partition(_OPTION_VALUE_SEPARATOR)[0]
        if _is_registered_option(spelling) or spelling in found:
            continue
        found.append(spelling)
    return tuple(found)


def _placeholder_dest(spelling: str) -> str:
    """Return the pytest ``dest`` a placeholder option stores its value under.

    The generic convention again: the dotted key the spelling names with each ``.`` folded to
    ``__``, so :func:`collect_from_pytest` reads the value back by the rule the option was
    registered with rather than by a second one.
    """
    dotted = spelling.partition(_OPTION_VALUE_SEPARATOR)[0].removeprefix(GENERIC_OPTION_PREFIX)
    return keys.option_dest(dotted)


def register_unregistered_options(
    parser: pytest.Parser, pluginmanager: pytest.PytestPluginManager
) -> None:
    """Register a placeholder ``argparse`` option for each misspelt ``--foundry-*`` spelling.

    pytest refuses an option it was never told about with its usage-error code 4, before
    ``pytest_configure`` runs, while AAP 0.4.5 and AAP 0.4.7 publish exit 2 for an unknown key in
    *any* layer, the command line included. Registering the spelling exactly as it was typed keeps
    the token parseable; :func:`collect_from_pytest` then hands its value to :func:`from_mapping`,
    which rejects the key naming ``difflib``'s nearest valid spelling and ends the session at exit
    2 through the ordinary configuration-error path.
    """
    config = pluginmanager.get_plugin(_CONFIG_PLUGIN_NAME)
    if config is None:
        return
    typed = getattr(getattr(config, "invocation_params", None), "args", ())
    for spelling in unregistered_spellings(typed):
        parser.addoption(
            spelling,
            dest=_placeholder_dest(spelling),
            default=None,
            type=str,
            metavar="VALUE",
            help=_UNREGISTERED_HELP_TEMPLATE.format(spelling=spelling),
        )


def from_mapping(raw: Mapping[str, object], *, cwd: Path | None = None) -> CliInput:
    """Collect the ``--foundry-*`` overrides a command line supplied.

    The entry point both surfaces share -- click's mapping and the one :func:`collect_from_pytest`
    reads from a :class:`pytest.Config` -- so the spelling rules, the coercions and the error
    wording exist once. An entry whose value is ``None`` is skipped, and a path option given as an
    empty string.
    ``--foundry-config`` and ``--foundry-json-report`` are recorded exactly as typed, ``cwd``
    changing nothing that is recorded; ``--foundry-record`` with ``--foundry-replay`` is a usage
    error, and either alone sets ``cassette.mode``, winning over a mode in the same mapping. Every
    value is coerced by :func:`foundry.config.validation.coerce_cli_value`, its spelling recorded
    beside it; a table- or list-valued key raises, the generic option taking scalars only.

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
    for spelling in unregistered_spellings(config.invocation_params.args):
        _remember(raw, spelling, config.getoption(_placeholder_dest(spelling), default=None))
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


def _takes_separate_value(token: str) -> bool:
    """Report whether ``token`` is a Foundry option that consumes the argument after it."""
    if not token.startswith(GENERIC_OPTION_PREFIX) or _OPTION_VALUE_SEPARATOR in token:
        return False
    special = _SPECIAL_BY_SPELLING.get(token)
    return special is None or special.kind not in _VALUELESS_SPECIAL_KINDS


def fold_option_values(args: Sequence[str]) -> list[str] | None:
    """Return ``args`` with each Foundry option's separately typed value folded onto its token.

    ``--foundry-artifacts.dir PATH`` becomes ``--foundry-artifacts.dir=PATH``, which ``argparse``
    reads identically and pytest's early scan cannot mistake for a path on disk
    (:func:`restore_preparse_state` states why that matters). ``None`` when nothing folded, so a
    command line that needs no repair costs one pass over the arguments.

    Returns:
        A new list when at least one value was folded, otherwise ``None``.
    """
    folded: list[str] = []
    index = 0
    while index < len(args):
        token = args[index]
        following = args[index + 1] if index + 1 < len(args) else ""
        if following and _takes_separate_value(token) and not following.startswith(_OPTION_DASH):
            folded.append(token + _OPTION_VALUE_SEPARATOR + following)
            index += 2
            continue
        folded.append(token)
        index += 1
    return folded if folded != list(args) else None


def restore_preparse_state(config: pytest.Config, args: list[str]) -> bool:
    """Re-derive what pytest's pre-parse read before a Foundry option could be registered.

    ``Config._initini`` fixes the root directory and the configuration file before a ``pytest11``
    entry-point plugin is loaded, so the value of a ``--foundry-*`` option typed as its own token is
    unknown to ``argparse`` there and falls through to the ``file_or_dir`` positional. An existing
    directory named that way moves the root directory to its common ancestor with the invocation
    directory and loses the configuration file, ``testpaths`` with it, so collection falls back to
    the invocation directory -- where AAP 0.2.1 requires a worker's collection to be the serial one.
    Called from :func:`foundry.plugin.pytest_load_initial_conftests`, when the option group is
    registered and collection is not decided yet: the values are folded, the state is derived again
    through pytest's own ``_initini``, and the conftest cutoff, ``addopts``, ``pythonpath`` and the
    two ini checks are restored.

    Returns:
        ``True`` when the state was re-derived, ``False`` when the command line needed no repair.

    Raises:
        UsageError: From either ini check, when the repaired file fails it; a run that never lost
            the file reports the same fault at the same point.
    """
    folded = fold_option_values(args)
    if folded is None:
        return False
    previous_root, previous_inipath = config.rootpath, config.inipath
    confcutdir = str(previous_root) if previous_inipath is None else str(previous_inipath.parent)
    try:
        applied_addopts = tuple(str(token) for token in config.getini("addopts"))
        applied_pythonpath = tuple(str(path) for path in config.getini("pythonpath"))
        config._inicache.clear()
        config._initini(folded)
        if (config.rootpath, config.inipath) == (previous_root, previous_inipath):
            return False
        _restore_confcutdir(config, confcutdir)
        _restore_addopts(config, args, applied_addopts)
        _restore_pythonpath(config, applied_pythonpath)
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        # AAP 0.1.2: Foundry's own fault is one WARNING, and the run keeps pytest's reading.
        events.emit_plumbing_failure("config.cli_layer", _REPAIR_FAULT, exc=exc)
        return False
    _recheck_ini_requirements(config)
    return True


def _restore_confcutdir(config: pytest.Config, previous: str) -> None:
    """Re-apply the derived conftest cutoff, leaving an explicit ``--confcutdir`` as typed."""
    namespace = config.known_args_namespace
    if getattr(namespace, "confcutdir", None) != previous:
        return
    inipath = config.inipath
    namespace.confcutdir = str(config.rootpath) if inipath is None else str(inipath.parent)


def _restore_addopts(config: pytest.Config, args: list[str], applied: tuple[str, ...]) -> None:
    """Prepend the repaired file's ``addopts``, replacing exactly the tokens that were applied."""
    needed = tuple(str(token) for token in config.getini("addopts"))
    if needed == applied:
        return
    if tuple(args[: len(applied)]) == applied:
        del args[: len(applied)]
    args[:0] = list(needed)


def _restore_pythonpath(config: pytest.Config, applied: tuple[str, ...]) -> None:
    """Insert the repaired file's ``pythonpath`` entries, which the pre-parse never applied."""
    needed = [str(path) for path in config.getini("pythonpath")]
    if needed == list(applied):
        return
    for entry in reversed(needed):
        if entry not in sys.path:
            sys.path.insert(0, entry)


def _recheck_ini_requirements(config: pytest.Config) -> None:
    """Re-run the ``minversion`` and ``required_plugins`` checks the lost file carried."""
    config._checkversion()
    config._validate_plugins()
