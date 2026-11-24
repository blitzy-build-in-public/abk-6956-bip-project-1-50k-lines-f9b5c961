"""The ``foundry run`` command: pytest's own run, wrapped by Foundry's option layer.

``run`` translates the ``--foundry-*`` options this command parsed into the canonical spellings
:mod:`foundry.plugin` registers, appends the caller's pytest arguments unchanged and in order, and
hands the list to :func:`pytest.main`, so a bare ``pytest`` invocation and ``foundry run`` end
identically (AAP 0.4.4, AAP 0.7.1, AAP 0.4.9). The one code Foundry produces itself is ``2``: the
configuration is resolved once here, ahead of :func:`pytest.main`, so a usage or configuration fault
reaches :func:`foundry.cli.main.main` before a test is collected or a report written (AAP 0.4.7).
Nothing here activates the plugin, no argument the caller did not supply is added, and ``pytest`` is
imported inside :func:`run` so the other commands never pay for it (AAP 0.4.2, AAP 0.4.8).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import Enum
from types import MappingProxyType
from typing import Final

import click

from foundry.cli import exit_codes
from foundry.cli.config_cmd import override_options
from foundry.config import cli_layer, keys, merge
from foundry.core.exceptions import ConfigError

__all__: Final[tuple[str, ...]] = ("build_pytest_argv", "run")

_FLAG_SPELLINGS: Final[frozenset[str]] = frozenset(
    option.spelling
    for option in cli_layer.SPECIAL_OPTIONS
    if option.kind in {"record-flag", "replay-flag"}
)
"""The two special options that are switches, so no value follows them (AAP 0.4.4).

Derived from :data:`foundry.config.cli_layer.SPECIAL_OPTIONS` rather than respelled, so a third
switch added there is emitted as a switch here without a second list to keep in step.
"""

_OFF_WORDS: Final[frozenset[str]] = frozenset({"", "false", "0", "no", "off"})
"""The spellings of a supplied-but-disabled switch, read exactly as the command-line layer does."""

_OPTION_WORD: Final[str] = keys.GENERIC_OPTION_PREFIX.removeprefix("--").replace("-", "_")
"""``foundry_``: the leading part of a parser parameter name, derived from the option prefix."""


def build_pytest_argv(
    foundry_options: Mapping[str, object],
    pytest_args: Sequence[str],
) -> list[str]:
    """Return the argument list that carries ``foundry_options`` and ``pytest_args`` to pytest.

    The translation this command exists for: every supplied Foundry option becomes the canonical
    ``--foundry-<dotted.key>`` spelling :mod:`foundry.plugin` registers (AAP 0.4.4), followed by
    its value unless it is one of the two switches, and the caller's own pytest arguments are
    appended afterwards, unchanged and in the order given. The result therefore contains no
    plugin selector and no argument the caller did not supply.

    The order is fixed rather than incidental, so two runs and two xdist workers build identical
    argument lists (AAP 0.13.1): options are emitted in the registry order of
    :data:`foundry.config.keys.KEY_SPECS`, then the four
    :data:`foundry.config.cli_layer.SPECIAL_OPTIONS` in their documented order, then
    ``pytest_args``. The function is pure -- no environment, no filesystem, no clock, no mutation
    of its arguments -- which is what lets a unit test assert the whole list.

    A supplied option is recognised by its dotted key (``http.timeout``), by a named alias word
    (``timeout``, ``retries``), or by an option spelling (``--foundry-http.timeout``,
    ``--foundry-config``); whichever form arrived is emitted in the one canonical spelling, so
    the argument list does not depend on how the caller named the option. A value of ``None``
    means the option was not supplied and is skipped -- the same reading
    :func:`foundry.config.cli_layer.from_mapping` applies.

    Args:
        foundry_options: The supplied Foundry options, keyed by option spelling, alias word or
            dotted key. Values may already be typed (``bool``, ``int``, ``float``, ``Path``, an
            enum member) or still be the text a parser produced.
        pytest_args: The arguments the caller passed through, in the order they were typed. They
            are appended verbatim: never reordered, rewritten, deduplicated or shell-quoted.

    Returns:
        The complete argument list for :func:`pytest.main`.

    Raises:
        foundry.core.exceptions.ConfigError: When an entry names no documented option or key, in
            which case the caller has a spelling to fix. Exit code 2 (AAP 0.4.4).
    """
    values: dict[str, str] = {}
    switches: set[str] = set()
    for name in sorted(foundry_options):
        supplied = foundry_options[name]
        if supplied is None:
            continue
        spelling = _option_spelling(name)
        if spelling is None:
            raise _unmappable(name)
        if spelling in _FLAG_SPELLINGS:
            if _switch_on(supplied):
                switches.add(spelling)
            continue
        values[spelling] = _render_value(supplied)

    argv: list[str] = []
    for spec in keys.KEY_SPECS:
        spelling = keys.cli_option(spec.dotted)
        rendered = values.get(spelling)
        if rendered is not None:
            argv.extend((spelling, rendered))
    for special in cli_layer.SPECIAL_OPTIONS:
        rendered = values.get(special.spelling)
        if rendered is not None:
            argv.extend((special.spelling, rendered))
        elif special.spelling in switches:
            argv.append(special.spelling)
    argv.extend(pytest_args)
    return argv


@click.command(
    name="run",
    context_settings={"ignore_unknown_options": True, "allow_interspersed_args": False},
    help="run the test suite under Foundry",
)
@click.argument("pytest_args", nargs=-1, type=click.UNPROCESSED)
@override_options
def run(pytest_args: tuple[str, ...], **foundry_options: object) -> int:
    """Run the suite under pytest and return pytest's own exit code.

    ``context_settings`` makes this command a faithful wrapper: every argument Foundry does not
    define is collected into ``pytest_args`` untouched and reaches pytest in the order it was
    typed (AAP 0.4.4). ``@override_options`` is the shared ``--foundry-*`` option group of
    :mod:`foundry.cli.config_cmd`, applied here exactly as on every other subcommand that
    resolves project configuration.

    The configuration is resolved before pytest starts, for the reason the module docstring
    gives: a usage or configuration fault must end the run with exit 2 and no collection and no
    report file (AAP 0.4.7). pytest itself is imported only at that point, so a command that
    never runs tests is not charged for it.

    Nothing is caught: pytest handles its own exceptions and ``KeyboardInterrupt``, while a
    :class:`~foundry.core.exceptions.ConfigError` raised here passes through to
    :func:`foundry.cli.main.main` unchanged, which is what maps it onto exit code 2.

    Args:
        pytest_args: The arguments to hand to pytest, unchanged and in order.
        **foundry_options: The ``--foundry-*`` options ``@override_options`` declared, with the
            value each was given, or ``None`` for one that was not supplied.

    Returns:
        The exit code to publish -- ``pytest.main``'s own code, with ``None`` normalised to ``0``
        by :func:`foundry.cli.exit_codes.passthrough`.

    Raises:
        foundry.core.exceptions.ConfigError: When this command line, the configuration file or
            the environment cannot be resolved into a valid configuration.
    """
    supplied = _supplied_options(foundry_options)
    # Resolving here is the pre-start validation of AAP 0.4.7, not a substitute for the session's
    # own resolution: foundry.plugin resolves again from the same arguments once pytest boots, so
    # the two cannot disagree, and a fault is reported before any test or report exists.
    merge.resolve(cli_overrides=cli_layer.from_mapping(supplied))

    import pytest

    return exit_codes.passthrough(pytest.main(build_pytest_argv(supplied, pytest_args)))


# The helpers come before the public surface because _PARAMETER_TABLE is built once at import
# from _parameter_table, and an index a call reads should be defined before the call that reads it.
def _supplied_options(parameters: Mapping[str, object]) -> dict[str, object]:
    """Translate the command's parsed parameters into canonical option spellings.

    A ``click`` command receives one parameter per declared option, named after the option
    spelling or after the dotted key it sets. Both the command-line layer and the argument
    builder work in the one canonical vocabulary of :func:`build_pytest_argv`, so the values are
    keyed by that spelling here and nowhere else: the record and replay switches keep their own
    spelling because they configure a mode rather than a value, and every other option is keyed
    by the ``--foundry-<dotted.key>`` form of the key it sets.

    Parameters whose value is ``None`` are dropped, which is how ``click`` reports an option that
    was not supplied, so nothing downstream has to distinguish absent from taken.

    Args:
        parameters: The parameter names and values ``click`` parsed.

    Returns:
        The supplied values keyed by canonical option spelling, in sorted parameter order so the
        mapping is the same on every run and every worker.

    Raises:
        foundry.core.exceptions.ConfigError: When a parameter names no documented option, rather
            than letting an override the operator typed disappear silently.
    """
    supplied: dict[str, object] = {}
    for name in sorted(parameters):
        value = parameters[name]
        if value is None:
            continue
        spelling = _option_spelling(name)
        if spelling is None:
            raise _unmappable(name)
        supplied[spelling] = value
    return supplied


def _option_spelling(name: str) -> str | None:
    """Return the canonical ``--foundry-*`` spelling a parameter name or mapping key stands for.

    One name can arrive in several forms and all of them mean the same option: the spelling an
    operator typed (``--foundry-http.timeout``), the name ``click`` derives from it, the dotted
    key the configuration registry publishes (``http.timeout``), a named alias word
    (``timeout``), or the ``dest`` :mod:`foundry.plugin` gives the option
    (``foundry__http__timeout``). :data:`_PARAMETER_TABLE` holds each of them, so this function
    is a lookup with a fallback for a spelling the table does not carry.

    Args:
        name: The parameter name or mapping key to translate.

    Returns:
        The spelling :mod:`foundry.plugin` registers -- ``--foundry-http.timeout`` for a key, and
        the option's own spelling for one of the four
        :data:`foundry.config.cli_layer.SPECIAL_OPTIONS` -- or ``None`` when the name configures no
        documented option.
    """
    trimmed = name.strip()
    if not trimmed:
        return None
    direct = _PARAMETER_TABLE.get(trimmed)
    if direct is not None:
        return direct
    word = trimmed.lstrip("-")
    for candidate in (word, "--" + word.replace("_", "-")):
        spelling = _PARAMETER_TABLE.get(candidate)
        if spelling is not None:
            return spelling
    if trimmed.startswith("-"):
        dotted = keys.option_to_key(trimmed)
        if dotted is not None:
            return keys.cli_option(dotted)
    return None


def _render_value(value: object) -> str:
    """Return the text of one option's value for the argument list.

    The value reaches pytest as one token, so it is rendered the way the key's own constraint
    describes it (AAP 0.4.5): a boolean as ``true`` or ``false``, an enum member as its value,
    and everything else as its string form -- ``1.0`` for a float, ``2`` for an int, the spelled
    path for a :class:`pathlib.Path`. :func:`foundry.config.validation.coerce_cli_value` parses
    each of those back, so the round trip through the command line cannot change a value.

    Args:
        value: The supplied value, already typed by ``click`` or still textual.

    Returns:
        The single token pytest's parser will read.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Enum):
        return str(value.value)
    return str(value)


def _switch_on(value: object) -> bool:
    """Report whether a supplied switch was turned on.

    ``click`` hands a switch over as a real ``bool``, while a caller building a mapping by hand
    may write the word instead; reading ``"false"`` as "supplied" would silently turn recording
    on. The words are the ones :mod:`foundry.config.cli_layer` reads, so both surfaces agree on
    what an operator's ``--foundry-record false`` means.

    Args:
        value: The value supplied for the switch.

    Returns:
        ``True`` unless the value is ``False``, an empty string or one of the words that spell
        "off".
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in _OFF_WORDS
    return bool(value)


def _unmappable(name: str) -> ConfigError:
    """Return the error for a parameter or mapping key that names no documented option.

    Reported rather than skipped: an override that quietly disappears is the failure mode this
    guard exists to prevent, and exit code 2 with the name in it is something the caller can act
    on (AAP 0.4.4).

    Args:
        name: The parameter name or mapping key that could not be translated.

    Returns:
        The :class:`~foundry.core.exceptions.ConfigError` the caller raises.
    """
    return ConfigError(
        f"the foundry run command cannot pass {name} on to pytest",
        key=name,
        hint="supply the documented --foundry-* option instead",
    )


def _parameter_table() -> Mapping[str, str]:
    """Build the index from every way an option can be named to its canonical spelling.

    Both vocabularies of AAP 0.4.4 are covered without respelling either of them: the generic
    ``--foundry-<dotted.key>`` of every scalar key in
    :data:`foundry.config.keys.KEY_SPECS`, its named aliases, the dotted key itself, and the
    ``dest`` conventions the plugin and the command line already share
    (:func:`foundry.config.keys.option_dest` and
    :func:`foundry.config.cli_layer.alias_dest`), plus the four special options. A name is
    indexed only once -- the first spelling that claims it wins -- so a lookup cannot depend on
    the order the table was built in.

    Returns:
        A read-only mapping from each accepted name to the option spelling that is emitted.
    """

    def remember(entries: dict[str, str], names: tuple[str, ...], spelling: str) -> None:
        """Record ``spelling`` under each of ``names`` that no earlier entry claimed."""
        for name in names:
            entries.setdefault(name, spelling)

    entries: dict[str, str] = {}
    for spec in keys.KEY_SPECS:
        spelling = keys.cli_option(spec.dotted)
        word = spelling.removeprefix(keys.GENERIC_OPTION_PREFIX)
        remember(
            entries,
            (
                spelling,
                word,
                word.replace(".", "_"),
                word.replace(".", "__"),
                _OPTION_WORD + word.replace(".", "_"),
                keys.option_dest(spec.dotted),
                spec.dotted,
            ),
            spelling,
        )
        for alias in spec.aliases:
            alias_word = alias.removeprefix(keys.GENERIC_OPTION_PREFIX)
            remember(
                entries,
                (alias, alias_word, alias_word.replace("-", "_")),
                spelling,
            )
    for special in cli_layer.SPECIAL_OPTIONS:
        word = special.spelling.removeprefix(keys.GENERIC_OPTION_PREFIX)
        remember(
            entries,
            (
                special.spelling,
                word,
                word.replace("-", "_"),
                cli_layer.alias_dest(special.spelling),
            ),
            special.spelling,
        )
    return MappingProxyType(entries)


_PARAMETER_TABLE: Final[Mapping[str, str]] = _parameter_table()
"""Every accepted name for an option, mapped to the spelling that is emitted for it.

Read-only and built once at import, like the registry it derives from: it is an index, not
module state, and two imports observe the same table (AAP 0.4.8).
"""
