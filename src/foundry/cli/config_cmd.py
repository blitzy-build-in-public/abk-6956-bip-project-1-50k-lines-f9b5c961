"""The ``foundry config show`` and ``foundry config validate`` subcommands.

Both commands are presenters -- resolution is :func:`foundry.config.merge.resolve`, the rows and
documents are :mod:`foundry.config.render`, the output is :mod:`foundry.cli.render` -- and a
:class:`foundry.core.exceptions.ConfigError` propagates to the command line entry point, which
prints it on stderr and exits ``2``. A ``--format`` run emits one document on stdout.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Final, TypeVar

import click

from foundry.cli import exit_codes
from foundry.cli import render as cli_render
from foundry.config import cli_layer, keys, merge
from foundry.config import errors as config_errors
from foundry.config import render as config_render

if TYPE_CHECKING:
    from foundry.config.models import FoundryConfig

__all__: Final[tuple[str, ...]] = ("config_group", "override_options")

_F = TypeVar("_F", bound=Callable[..., object])
"""The command callback :func:`override_options` decorates and returns unchanged in type."""


@dataclass(frozen=True, slots=True)
class _OverrideOption:
    """One ``--foundry-*`` option of the override set, as the registry declares it.

    A read-only table of these rather than a literal list of spellings, so the three facts click
    needs -- the spelling an operator types, the ``dest`` its value is read back through, and the
    one-line help -- are derived from :data:`foundry.config.keys.KEY_SPECS` and cannot disagree
    with the pytest option group :mod:`foundry.plugin` registers. The spelling is also the
    ``cli:<option>`` detail the value is attributed to, and a generic option cannot use its spelling
    as ``dest`` because a name of dots is no parameter name.
    """

    spelling: str
    dest: str
    help_text: str


def _override_option_table() -> tuple[_OverrideOption, ...]:
    """Build the override set from the key registry.

    One option for every scalar key -- the generic ``--foundry-<dotted.key>`` form -- plus every
    named alias a :class:`foundry.config.keys.KeySpec` declares, plus the file-selecting
    ``--foundry-config``. An alias that spells the same string as a key's generic option is
    registered once, through the generic destination, so click sees one parameter per spelling.
    Entries come back sorted by spelling, so two runs print the same ``--help``.
    """
    entries: dict[str, _OverrideOption] = {}
    for spec in keys.KEY_SPECS:
        if not keys.is_scalar(spec):
            continue
        help_text = f"override {spec.dotted} (accepted: {spec.constraint})"
        spelling = keys.cli_option(spec.dotted)
        entries[spelling] = _OverrideOption(
            spelling=spelling,
            dest=keys.option_dest(spec.dotted),
            help_text=help_text,
        )
        for alias in spec.aliases:
            entries.setdefault(
                alias,
                _OverrideOption(
                    spelling=alias,
                    dest=cli_layer.alias_dest(alias),
                    help_text=help_text,
                ),
            )
    for special in cli_layer.SPECIAL_OPTIONS:
        if special.kind != "config-path":
            continue
        entries[special.spelling] = _OverrideOption(
            spelling=special.spelling,
            dest=cli_layer.alias_dest(special.spelling),
            help_text="select the configuration file to resolve",
        )
    return tuple(entries[spelling] for spelling in sorted(entries))


_OVERRIDE_OPTIONS: Final[tuple[_OverrideOption, ...]] = _override_option_table()
"""The override set both subcommands accept, in the order ``--help`` prints it."""


def _option_decorator(option: _OverrideOption) -> Callable[[_F], _F]:
    """Return the decorator that declares one override option on a command.

    The option is typed as text and left un-coerced on purpose: every value is parsed by
    :func:`foundry.config.cli_layer.from_mapping`, which is also what the pytest option group goes
    through, so a rejected value carries one wording on both surfaces. Each call returns a fresh
    decorator, so no two commands share one parameter object.
    """
    return click.option(
        option.spelling,
        option.dest,
        type=str,
        default=None,
        help=option.help_text,
    )


def override_options(command: _F) -> _F:
    """Declare every ``--foundry-*`` override option on a config subcommand.

    The same override set is required on every subcommand that resolves project configuration, so
    it is applied as one decorator rather than listed per command, and it is built from
    :data:`foundry.config.keys.KEY_SPECS`. This module is therefore the one home of the override
    set, so the shared spellings exist once and cannot drift. The callback is returned unchanged,
    which is why the decorator is applied beneath :func:`click.command`.
    """
    decorated: _F = command
    for option in reversed(_OVERRIDE_OPTIONS):
        decorated = _option_decorator(option)(decorated)
    return decorated


def _supplied_input(supplied: Mapping[str, object]) -> cli_layer.CliInput:
    """Return the command line this invocation typed, as the configuration layer reads it.

    The mapping is rebuilt as ``spelling -> value`` because that is the vocabulary
    :func:`foundry.config.cli_layer.from_mapping` speaks: it owns the coercions, the spelling rules
    and the error wording. An option the operator did not type carries ``None`` and is skipped.

    Raises:
        foundry.core.exceptions.ConfigError: For a value its key cannot parse or a range it is
            outside, which ends the run with exit code 2.
    """
    raw: dict[str, object] = {}
    for option in _OVERRIDE_OPTIONS:
        value = supplied.get(option.dest)
        if value is not None:
            raw[option.spelling] = value
    return cli_layer.from_mapping(raw)


def _deferred_input(supplied: cli_layer.CliInput) -> cli_layer.CliInput:
    """Return ``supplied`` without the key overrides a deferred ``config show`` does not apply.

    Without ``--resolved`` the resolution covers defaults, file, profile and environment only, so
    the resolved configuration keeps the configuration-file path alone. The dropped overrides are
    not lost: the caller passes the original input as the not-applied section, where the operator
    reads them.
    """
    return replace(supplied, overrides={}, spellings={})


def _not_applied_rows(
    config: FoundryConfig, deferred: cli_layer.CliInput
) -> tuple[config_render.ConfigRow, ...]:
    """Return the rows of the separate ``not applied`` section of a deferred ``config show``.

    :func:`foundry.config.render.rows` appends the deferred overrides after every resolved row and
    labels each of them ``not-applied:<--option>``, so the section selects that tail, which keeps
    one implementation of a row's shape and of its value redaction.
    """
    resolved_count = len(config_render.rows(config))
    return config_render.rows(config, not_applied=deferred)[resolved_count:]


@click.group(name="config", help="inspect and validate configuration")
def config_group() -> None:
    """Group the configuration subcommands: ``show`` and ``validate``.

    The group itself resolves nothing: every subcommand resolves the configuration once, so the
    report a command prints and the configuration that command validated are the same resolution.
    """


@config_group.command(
    name="show",
    help="print the resolved configuration with the layer that supplied each value",
)
@override_options
@click.option(
    "--resolved",
    is_flag=True,
    default=False,
    help="apply this command's overrides and attribute them to cli:<option>",
)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["json", "toml"]),
    default=None,
    help="print one JSON or TOML document instead of the table",
)
def show_command(*, resolved: bool, output_format: str | None, **supplied: object) -> int:
    """Print the configuration one row per key, or one machine-readable document.

    Without ``--resolved`` the resolution covers defaults, file, profile and environment only and
    any override this command was given is listed in a separate ``not applied`` section; with it,
    the overrides are applied and attributed to ``cli:<--option>``. With ``--format json`` or
    ``--format toml`` the same values and their provenance leave as one document on stdout and
    nothing else; in that mode a deferred override rides the additive ``not_applied`` member.

    Returns:
        :data:`foundry.cli.exit_codes.SUCCESS`, for the entry point to publish.

    Raises:
        foundry.core.exceptions.ConfigError: Propagated unchanged from
            :func:`foundry.config.merge.resolve` for any fatal configuration fault, which the entry
            point reports on stderr before exiting ``2``.
    """
    cli_input = _supplied_input(supplied)
    applied = cli_input if resolved else _deferred_input(cli_input)
    config = merge.resolve(cli_overrides=applied)
    deferred = None if resolved else cli_input

    if output_format == "json":
        cli_render.render_document(config_render.to_json_text(config, not_applied=deferred))
        return exit_codes.SUCCESS
    if output_format == "toml":
        cli_render.render_document(config_render.to_toml_text(config, not_applied=deferred))
        return exit_codes.SUCCESS

    redactor = config_render.redactor_for(config)
    cli_render.render_config_table(config_render.rows(config), redactor=redactor)
    if deferred is not None:
        cli_render.render_not_applied(_not_applied_rows(config, deferred), redactor=redactor)
    return exit_codes.SUCCESS


@config_group.command(
    name="validate",
    help="resolve the configuration and report whether it is valid",
)
@override_options
def validate_command(**supplied: object) -> int:
    """Resolve the configuration with every override applied and report success on stdout.

    The command validates exactly what a run would use, so every override option and the
    configuration-file option are applied. Success prints the one line
    :func:`foundry.config.errors.success_message` builds from the active profile; a fault is not
    caught here, which is what leaves stdout empty when the command fails.

    Returns:
        :data:`foundry.cli.exit_codes.SUCCESS`, for the entry point to publish.

    Raises:
        foundry.core.exceptions.ConfigError: Propagated unchanged from
            :func:`foundry.config.merge.resolve` for the first fault it meets, which the entry point
            prints on stderr before exiting ``2``.
    """
    config = merge.resolve(cli_overrides=_supplied_input(supplied))
    cli_render.render_message(
        config_errors.success_message(config.profile),
        redactor=config_render.redactor_for(config),
    )
    return exit_codes.SUCCESS
