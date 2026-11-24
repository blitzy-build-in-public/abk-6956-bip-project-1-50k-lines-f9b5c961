"""The command-line layer: the ``--foundry-*`` overrides an operator typed.

Foundry resolves one configuration through four layers, and this module is the highest of
them (AAP 0.4.5). It does three things: it reads the values an operator supplied, it turns
each into the value its key declares, and it remembers which option spelling supplied it,
because ``foundry config show --resolved`` has to attribute the value to ``cli:<option>``
rather than to the layer as a whole (AAP 0.4.4, criterion 4).

:class:`CliInput` is the result and the only shape :mod:`foundry.config.merge` needs:
:attr:`CliInput.overrides` maps a dotted key to its coerced value,
:attr:`CliInput.spellings` maps the same key to the option the operator wrote, and the two
path-valued options that configure no key -- ``--foundry-config`` and
``--foundry-json-report`` -- travel beside them. The carrier is frozen and its mappings are
read-only, so the not-applied section ``foundry config show`` prints without ``--resolved``
describes exactly the input a resolved run would apply (AAP 0.4.5).

Two vocabularies of option exist, and neither is restated here (AAP 0.4.4):

- the *generic* ``--foundry-<dotted.key>``, registered for every scalar key in
  :data:`foundry.config.keys.KEY_SPECS` and mapped back to its key by
  :func:`foundry.config.keys.option_to_key`;
- the *named* options: the aliases a :class:`~foundry.config.keys.KeySpec` declares
  (``--foundry-profile``, ``--foundry-retries``, ``--foundry-timeout``), which resolve
  through that same registry, and the four :data:`SPECIAL_OPTIONS` below, which configure
  no key at all and are therefore listed here.

Options are *registered* elsewhere -- :mod:`foundry.plugin` builds the pytest option group,
the ``click`` decorators in :mod:`foundry.cli` build theirs -- so this module imports
neither ``click`` at runtime nor ``pytest``: ``pytest`` appears only as an annotation for
:func:`collect_from_pytest` under :data:`typing.TYPE_CHECKING`, and the command line hands
its parsed values in as a plain mapping (AAP 0.4.8). The one cross-module detail both sides
must agree on is the ``dest`` an option stores its value under; :func:`alias_dest` writes it
down for a named option exactly as :func:`foundry.config.keys.option_dest` does for a
generic one.

Nothing here reads ``os.environ`` -- ``FOUNDRY_CONFIG`` is the file and environment layers'
business -- and nothing here performs I/O: a path option is recorded as the operator spelled
it and resolved later, against the invocation directory, by
:func:`foundry.config.files.discover_file`. The module writes no file, configures no logger
and holds no mutable module state: :data:`SPECIAL_OPTIONS` and the map derived from it are
built once and are read-only, which is the import-time discipline AAP 0.4.8 requires.

Every failure is a :class:`foundry.core.exceptions.ConfigError` (AAP 0.14.3), which the
command line reports as exit code 2 and the plugin turns into
``pytest.exit(returncode=2)`` before collection (assumption A4). No ``ValueError``, no
``click.UsageError`` and no bare exception leaves this module, and the wording of a message
belongs to :mod:`foundry.config.errors`, which this module calls rather than composes.

Importing this module imports :mod:`foundry.config.validation` and therefore ``pydantic``.
That cost is deliberate and bounded: it is paid when a configuration is resolved -- inside
``pytest_configure`` and inside a ``foundry`` subcommand -- and never by
:mod:`foundry.plugin` at import time, which is what keeps the 150 ms startup budget
(AAP 0.14.2, implicit requirement I5).
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
"""Prefix of the option generated for every scalar key (AAP 0.4.4).

Re-exported from :mod:`foundry.config.keys` rather than respelled, so the generic option a
caller builds here and the one :mod:`foundry.plugin` registers are the same string.
"""

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

``click`` and ``pytest`` hand a flag over as a real ``bool``; a caller building a mapping by
hand may write ``"false"``, and reading that as "supplied" would silently turn recording on.
"""


@dataclass(frozen=True, slots=True)
class SpecialOption:
    """A named ``--foundry-*`` option that configures no configuration key.

    Four documented options act on the run rather than on a key: one selects the
    configuration file, one relocates ``results.json``, and two select a cassette mode
    (AAP 0.4.4). They are described here instead of in :data:`foundry.config.keys.KEY_SPECS`
    because that registry is the table of the *configuration surface*, and
    ``--foundry-json-report`` in particular is explicitly not a key: it moves a file
    without changing a resolved value (AAP 0.1.2).

    Attributes:
        spelling: The option as an operator writes it, for example ``--foundry-record``.
            It is also the ``cli:<option>`` detail reported for the value it supplies.
        kind: The job the option does, which decides where :func:`from_mapping` records it:
            ``config-path`` selects the configuration file, ``json-report`` relocates
            ``results.json``, and ``record-flag`` / ``replay-flag`` select a cassette mode.
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

The three key-bound aliases are deliberately absent: ``--foundry-profile``,
``--foundry-retries`` and ``--foundry-timeout`` are declared by the
:class:`~foundry.config.keys.KeySpec` they set, so the registry stays the single table of
what configures a key and this tuple the single table of what does not.
"""

_SPECIAL_BY_SPELLING: Final[Mapping[str, SpecialOption]] = types.MappingProxyType(
    {option.spelling: option for option in SPECIAL_OPTIONS}
)
"""The special options by spelling, for the lookup :func:`from_mapping` performs per entry."""


def _frozen_by_key(values: Mapping[str, _ValueT]) -> Mapping[str, _ValueT]:
    """Return a read-only copy of ``values``, ordered by sorted key.

    Defined ahead of :class:`CliInput` -- the one place in this module where a helper
    precedes the public surface -- because :data:`EMPTY_CLI_INPUT` is constructed at import
    time by :meth:`CliInput.empty`, whose ``__post_init__`` calls this function.

    Args:
        values: The mapping to copy.

    Returns:
        A :class:`types.MappingProxyType` over a fresh dict holding the entries in sorted key
        order, which is what makes two inputs built from the same entries in different
        insertion orders equal and iterable alike.
    """
    return types.MappingProxyType({key: values[key] for key in sorted(values)})


@dataclass(frozen=True, slots=True)
class CliInput:
    """Everything the command line supplied, coerced, with the spelling behind each value.

    The carrier :mod:`foundry.config.merge` applies as its highest layer. A value alone is
    not enough: ``foundry config show --resolved`` reports ``cli:--foundry-http.timeout``,
    so the option spelling an operator typed travels beside the value it supplied
    (AAP 0.4.4, criterion 4), and the two path-valued options travel beside both because
    they configure no key (AAP 0.1.2).

    The instance is frozen and slotted, and its mappings are read-only views over
    key-sorted copies. That is what makes it safe to hold in a resolved configuration and
    to inspect twice -- once for the not-applied section of ``foundry config show`` and once
    when the overrides are applied -- without the second reader observing anything the
    first did (AAP 0.4.5). Two inputs built from the same entries in different insertion
    orders are therefore equal.

    Attributes:
        overrides: The coerced value of each overridden key, by dotted key.
        spellings: The option spelling that supplied each of those values, by dotted key.
            Every key of :attr:`overrides` appears here; the reverse is not guaranteed to a
            caller and is in practice the same set.
        config_path: The configuration file ``--foundry-config`` named, exactly as it was
            typed -- a relative path stays relative and is resolved by
            :func:`foundry.config.files.discover_file` -- or ``None`` when it was not given.
        json_report_path: The ``results.json`` output path ``--foundry-json-report`` named,
            as typed, or ``None`` when it was not given.
    """

    overrides: Mapping[str, object] = field(default_factory=dict)
    spellings: Mapping[str, str] = field(default_factory=dict)
    config_path: Path | None = None
    json_report_path: Path | None = None

    def __post_init__(self) -> None:
        """Freeze both mappings into key-sorted read-only views.

        The dataclass is already frozen, but a ``dict`` handed to it would stay mutable and
        a caller could edit a resolved configuration after the fact. Copying the entries in
        sorted key order closes that hole and makes iteration order -- which reaches the
        source table and the debug log line -- independent of the order the caller built its
        mapping in (AAP 0.13.1).
        """
        object.__setattr__(self, "overrides", _frozen_by_key(self.overrides))
        object.__setattr__(self, "spellings", _frozen_by_key(self.spellings))

    @classmethod
    def empty(cls) -> CliInput:
        """Return the input of a command line that supplied no ``--foundry-*`` option.

        Returns:
            A :class:`CliInput` with no overrides and no paths, equal to
            :data:`EMPTY_CLI_INPUT`.
        """
        return cls()

    def is_empty(self) -> bool:
        """Return whether the command line supplied nothing at all.

        A caller uses this to skip the layer before iterating it, so a debug line, a
        not-applied section or a payload is not produced for an invocation that typed no
        Foundry option.

        Returns:
            ``True`` when no key was overridden and neither path was named.
        """
        return not self.overrides and self.config_path is None and self.json_report_path is None

    def dotted_keys(self) -> tuple[str, ...]:
        """Return the overridden keys in sorted order.

        Sorted rather than insertion-ordered because the order reaches a message, a source
        table and a debug log line, and the insertion order depends on how a caller built
        its mapping (AAP 0.13.1).

        Returns:
            One dotted key per override, sorted.
        """
        return tuple(sorted(self.overrides))

    def spelling_for(self, dotted: str) -> str | None:
        """Return the option spelling that supplied ``dotted``.

        Args:
            dotted: The dotted configuration key, for example ``http.timeout``.

        Returns:
            The option the operator wrote, for example ``--foundry-http.timeout``, or
            ``None`` when the key was not overridden on this command line.
        """
        return self.spellings.get(dotted)


EMPTY_CLI_INPUT: Final[CliInput] = CliInput.empty()
"""The immutable "nothing was supplied" input :func:`foundry.config.merge.resolve` defaults to.

It is a shared constant rather than a fresh instance per call because it can never change:
a caller that reads it and a caller that compares against it observe the same object.
"""


def alias_dest(option: str) -> str:
    """Return the pytest ``dest`` a named ``--foundry-*`` option stores its value under.

    pytest needs one attribute name per option, and this function is the single statement
    of the convention for the *named* options of this layer: ``--foundry-retries`` becomes
    ``foundry_retries``, ``--foundry-config`` becomes ``foundry_config`` and
    ``--foundry-json-report`` becomes ``foundry_json_report``. Because it is the one detail
    two modules must agree on, it is written down once: :mod:`foundry.plugin` must register
    each named option with exactly this ``dest`` -- which is also what argparse derives by
    itself for a single-word option -- and :func:`collect_from_pytest` reads the value back
    through it. A mismatch raises nothing: the option is still parsed, its value is simply
    read as ``None`` and the override disappears, and that silent degradation is why the
    rule lives in a function rather than at the two call sites.

    A generic ``--foundry-<dotted.key>`` option does **not** use this function. Its key
    contains a dot, which is no attribute name, so the generic ``dest`` comes from
    :func:`foundry.config.keys.option_dest`, which joins the segments with ``__`` behind a
    ``foundry__`` prefix.

    Args:
        option: The option spelling, with or without its leading dashes and with or without
            an inline value, for example ``--foundry-json-report`` or
            ``--foundry-json-report=out.json``.

    Returns:
        The ``dest`` the option's value is stored under, for example ``foundry_json_report``.
    """
    spelling = option.split(_OPTION_VALUE_SEPARATOR, 1)[0].strip().lstrip("-")
    if spelling.startswith(_OPTION_WORD):
        spelling = spelling[len(_OPTION_WORD) :]
    return _ALIAS_DEST_PREFIX + spelling.replace("-", "_")


def from_mapping(raw: Mapping[str, object], *, cwd: Path | None = None) -> CliInput:
    """Collect the ``--foundry-*`` overrides a command line supplied.

    The entry point both surfaces share: :mod:`foundry.cli` hands over the mapping its
    ``click`` command built and :func:`collect_from_pytest` hands over the one it read from a
    :class:`pytest.Config`, so the spelling rules, the coercions and the error wording exist
    exactly once.

    An entry is keyed by either an option spelling -- ``--foundry-http.timeout``,
    ``--foundry-retries``, ``--foundry-config``, optionally carrying its value inline as
    ``--foundry-http.timeout=1.0`` -- or a dotted key (``http.timeout``), which keeps the
    mapping convenient to write by hand in a test as well as to build from a parser. Entries
    are visited in sorted order, so the fault a payload with two of them reports is the same
    on every run and on every xdist worker (AAP 0.13.1).

    What each kind of entry does:

    * an entry whose value is ``None`` is skipped, which is how ``click`` and ``pytest`` say
      an option was not supplied, and so is a path option given as an empty string;
    * ``--foundry-config`` is recorded as :attr:`CliInput.config_path` and
      ``--foundry-json-report`` as :attr:`CliInput.json_report_path`, both exactly as typed:
      a relative path is resolved later, against the invocation directory, by
      :func:`foundry.config.files.discover_file`, and resolving it here as well would let the
      two answers disagree (AAP 0.4.5, A5);
    * ``--foundry-record`` together with ``--foundry-replay`` is a usage error (exit 2, AAP
      0.4.4); either one alone sets ``cassette.mode`` to ``record`` or ``replay`` and is
      attributed to the switch the operator typed. The switch wins over a ``cassette.mode``
      supplied in the same mapping through the generic spelling or a dotted key, because it
      is the documented way to select that mode;
    * a named alias -- ``--foundry-retries``, ``--foundry-timeout``, ``--foundry-profile`` --
      resolves to the key its :class:`~foundry.config.keys.KeySpec` declares, so
      ``--foundry-retries`` sets ``execution.retries`` and never ``http.timeout``
      (AAP 0.1.2);
    * a generic spelling resolves through :func:`foundry.config.keys.option_to_key` and a
      dotted key through :func:`foundry.config.keys.spec_for`; an option or key the registry
      does not declare raises with the closest registered spelling in the hint (AAP 0.4.5).

    Every value is coerced by :func:`foundry.config.validation.coerce_cli_value` -- whether it
    arrives as the text of a generic option or already typed by a ``click`` parser -- and the
    spelling that supplied it is recorded beside it, which is what lets
    ``foundry config show --resolved`` report ``cli:--foundry-http.timeout`` (criterion 4).

    A key whose value is a table or a list -- ``http.headers``, ``secrets.redact_keys`` --
    raises instead of being coerced. No generic option is registered for such a key, because
    a header table or a list has no faithful single-token form, so the error names the option
    and the shape the key accepts. It is built by
    :func:`foundry.config.errors.bad_option_value` rather than
    :func:`foundry.config.errors.unknown_key`, because the key does exist: it is the option
    that cannot carry it, and ``bad_option_value``'s hint sends the operator to the file
    layer that can.

    Args:
        raw: The supplied values, keyed by option spelling or by dotted key.
        cwd: The directory the operator typed the options in, or ``None``. It is accepted so
            a caller holding it needs no second collector, and it deliberately changes
            nothing that is recorded: both paths stay exactly as typed, for the reason above.

    Returns:
        The overrides, the spelling behind each of them and the two path options; an input
        equal to :data:`EMPTY_CLI_INPUT` when nothing was supplied.

    Raises:
        ConfigError: For a key or option the registry does not declare, a value its key
            cannot parse, a ``--foundry-record`` / ``--foundry-replay`` combination, or a
            non-scalar key named on the command line. Each of them ends a run with exit
            code 2.
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
            raise errors.bad_option_value(spelling, spec.constraint, key=dotted)
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

    The plugin's entry to this layer. The values are read back through the two ``dest``
    conventions -- :func:`foundry.config.keys.option_dest` for the generic option of every
    scalar key and :func:`alias_dest` for the named ones -- and handed to
    :func:`from_mapping` as a ``spelling -> value`` mapping, so there is one implementation
    of the coercion rules and no second copy to drift (AAP 0.4.4).

    Every read passes ``default=None``, so a build of the plugin that registered fewer
    options than this module knows about degrades to "no override supplied" instead of
    raising ``ValueError`` inside ``pytest_configure``. A key reachable through both a
    generic and a named ``dest`` is recorded once per supplied value and the named spelling
    wins, because it is the one the operator typed.

    Args:
        config: The pytest configuration whose option namespace holds the parsed values.

    Returns:
        The overrides, their spellings and the two path options, equal to what
        :func:`from_mapping` returns for the same values.

    Raises:
        ConfigError: From :func:`from_mapping`, for a value the key cannot parse and for a
            ``--foundry-record`` / ``--foundry-replay`` combination.
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

    A generic spelling and a named alias both resolve through
    :func:`foundry.config.keys.option_to_key`, which only ever answers with a registered key;
    a spelling that configures none is the unknown option this layer reports.

    Args:
        spelling: The option as the operator wrote it, for example
            ``--foundry-http.timeout``.

    Returns:
        The dotted key and its :class:`~foundry.config.keys.KeySpec`.

    Raises:
        ConfigError: When no key is bound to the spelling, naming the closest registered
            spelling in the hint and the option in the detail (AAP 0.4.5).
    """
    dotted = keys.option_to_key(spelling)
    spec = keys.spec_for(dotted) if dotted is not None else None
    if spec is None:
        raise _unknown_option(spelling)
    return spec.dotted, spec


def _unknown_option(spelling: str) -> ConfigError:
    """Build the error for an option spelling that configures no registered key.

    Args:
        spelling: The option as the operator wrote it, for example
            ``--foundry-http.timout``.

    Returns:
        The :class:`foundry.core.exceptions.ConfigError` the caller raises, naming the key
        part of the spelling, the closest registered key and the ``cli`` layer.
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

    ``--foundry-http.timeout=1.0`` is one mapping key when a caller builds the mapping from
    a raw argument list, and the value still belongs to the option.

    Args:
        entry: The entry, for example ``--foundry-http.timeout=1.0``.

    Returns:
        The spelling with its leading dashes, for example ``--foundry-http.timeout``, and the
        inline value, or ``None`` when the entry carries none.
    """
    spelling, separator, inline = entry.partition(_OPTION_VALUE_SEPARATOR)
    return errors.cli_detail(spelling), inline if separator else None


def _path_option(spelling: str, value: object) -> Path | None:
    """Return the path a path-valued option carries.

    Args:
        spelling: The option's spelling, quoted in every error raised here.
        value: The value the parser produced, which is text or a
            :class:`pathlib.Path`; an empty string means the option was supplied with
            nothing and is read as "not supplied".

    Returns:
        The path exactly as typed, so a relative path stays relative for
        :func:`foundry.config.files.discover_file` to resolve, or ``None`` for an empty
        value.

    Raises:
        ConfigError: When the value is of neither kind, which only a caller hand-building the
            mapping can produce. The message names the option and expects a path, and the
            error carries the option as its key because these two options configure none.
    """
    if isinstance(value, Path):
        return value
    if isinstance(value, str):
        text = value.strip()
        return Path(text) if text else None
    raise errors.bad_option_value(spelling, _PATH_EXPECTED, key=spelling)


def _flag_enabled(value: object) -> bool:
    """Return whether a supplied switch was turned on.

    Args:
        value: The value the parser produced: ``click`` and ``pytest`` hand over a real
            ``bool``, and a caller hand-building a mapping may write the word instead.

    Returns:
        ``True`` for ``True`` and for any word outside :data:`_FLAG_OFF_WORDS`, and for any
        other truthy object; ``False`` for ``False``, for an empty string and for the words
        that spell "off", so a flag written as ``"false"`` does not silently turn recording
        on.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in _FLAG_OFF_WORDS
    return bool(value)


def _selected_mode(flags: Mapping[str, bool]) -> tuple[str, CassetteMode] | None:
    """Return the cassette mode the supplied switches select, with the one to attribute it to.

    Args:
        flags: The switch values read from the payload, by option spelling.

    Returns:
        ``("--foundry-record", CassetteMode.RECORD)``, ``("--foundry-replay",
        CassetteMode.REPLAY)``, or ``None`` when neither switch was turned on.

    Raises:
        ConfigError: When both switches were turned on, which AAP 0.4.4 makes a usage error.
            The conflict is decided here, after every entry has been read, so it is reported
            whatever order the two arrived in.
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
