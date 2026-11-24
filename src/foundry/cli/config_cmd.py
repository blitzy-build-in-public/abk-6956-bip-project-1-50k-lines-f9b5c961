"""The ``foundry config show`` and ``foundry config validate`` subcommands.

AAP 0.4.4 fixes what each command accepts and prints, AAP 0.4.5 the per-key source reporting and the
two machine-readable documents, and AAP 0.7.3 that a ``--format`` run emits exactly one document on
stdout. Both are presenters -- resolution is :func:`foundry.config.merge.resolve`, the rows and
documents are :mod:`foundry.config.render`, the output is :mod:`foundry.cli.render` -- and a
:class:`foundry.core.exceptions.ConfigError` propagates to :func:`foundry.cli.main.main`, which
prints it on stderr and exits ``2`` (AAP 0.4.4). The override options come from
:data:`foundry.config.keys.KEY_SPECS`.
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

    The option set is a read-only table of these rather than a literal list of spellings, so the
    three facts click needs -- the spelling an operator types, the ``dest`` its value is read back
    through, and the one-line help -- are derived from :data:`foundry.config.keys.KEY_SPECS` and
    cannot disagree with the pytest option group :mod:`foundry.plugin` registers.

    Attributes:
        spelling: The option as an operator writes it, for example ``--foundry-http.timeout``.
            It is also the ``cli:<option>`` detail :func:`foundry.config.render.rows` attributes
            the value to (AAP 0.4.4).
        dest: The click parameter name the value is stored under, for example
            ``foundry__http__timeout``: :func:`foundry.config.keys.option_dest` for a generic
            option, :func:`foundry.config.cli_layer.alias_dest` for a named one. A name of dots
            is no parameter name, which is why a generic option cannot use its spelling.
        help_text: The one line ``--help`` prints for the option, naming the key and the values it
            accepts (AAP 0.7.4).
    """

    spelling: str
    dest: str
    help_text: str


def _override_option_table() -> tuple[_OverrideOption, ...]:
    """Build the override set of AAP 0.4.4 from the key registry.

    One option for every *scalar* key -- the generic ``--foundry-<dotted.key>`` form -- plus every
    named alias a :class:`foundry.config.keys.KeySpec` declares (``--foundry-profile``,
    ``--foundry-retries``, ``--foundry-timeout``) plus the file-selecting ``--foundry-config``.
    A key of a container kind is skipped: a header table or a list has no faithful single-token
    command-line form, so it is reachable only through the file and environment layers, and
    :func:`foundry.config.cli_layer.from_mapping` raises the documented error for it.

    A named alias that spells the same string as a key's generic option -- ``profile``'s, both of
    them ``--foundry-profile`` -- is registered once, through the generic destination, so click
    sees one parameter per spelling.

    Returns:
        One entry per option spelling, sorted by spelling so two runs print the same ``--help``.
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
    :func:`foundry.config.cli_layer.from_mapping` through
    :func:`foundry.config.validation.coerce_cli_value`, which is also what the pytest option group
    goes through, so a rejected value names the option and the accepted values in one wording for
    both surfaces (AAP 0.4.5).

    Args:
        option: The option to declare.

    Returns:
        A fresh :func:`click.option` decorator for it, so no two commands share one parameter
        object.
    """
    return click.option(
        option.spelling,
        option.dest,
        type=str,
        default=None,
        help=option.help_text,
    )


def override_options(command: _F) -> _F:
    """Declare every ``--foundry-*`` override option AAP 0.4.4 puts on a config subcommand.

    AAP 0.4.4 requires the same override set on every subcommand that resolves project
    configuration, so it is applied as one decorator rather than listed per command. The
    decorator is defined here, built from :data:`foundry.config.keys.KEY_SPECS`, because
    :mod:`foundry.cli.main` builds the group and imports every subcommand module: importing the
    decorator back from it would close that cycle and leave this module unimportable on its own,
    while the registry both surfaces already share is the one thing that keeps the two option
    groups in step. It adds no import this module did not already have, so AAP 0.4.8's one-way
    direction is unchanged.

    This module is therefore the one home of the override set: every subcommand that resolves
    project configuration -- ``run``, ``config show``, ``config validate`` and ``doctor`` --
    imports :func:`override_options` from here rather than declaring an option group of its own,
    so the spellings AAP 0.4.4 requires those commands to share exist once and cannot drift.

    Args:
        command: The command callback to decorate. It is returned unchanged, so the decorator is
            applied beneath :func:`click.command` and click sees the parameters first.

    Returns:
        The same callback, carrying one click parameter per override option.
    """
    decorated: _F = command
    for option in reversed(_OVERRIDE_OPTIONS):
        decorated = _option_decorator(option)(decorated)
    return decorated


def _supplied_input(supplied: Mapping[str, object]) -> cli_layer.CliInput:
    """Return the command line this invocation typed, as the configuration layer reads it.

    The mapping is rebuilt as ``spelling -> value`` because that is the vocabulary
    :func:`foundry.config.cli_layer.from_mapping` speaks: it owns the coercions, the spelling
    rules and the error wording, and handing it click's parameter names instead would make this
    module restate them. An option the operator did not type carries ``None`` and is skipped, and
    iterating :data:`_OVERRIDE_OPTIONS` -- rather than the mapping -- keeps the payload limited to
    the options the registry declares.

    Args:
        supplied: The values click parsed, keyed by the parameter name of each override option.

    Returns:
        The overrides, the spelling behind each of them and the configuration-file path, equal to
        :data:`foundry.config.cli_layer.EMPTY_CLI_INPUT` when the command typed no Foundry option.

    Raises:
        foundry.core.exceptions.ConfigError: From :func:`foundry.config.cli_layer.from_mapping`,
            for a value its key cannot parse or a range it is outside, which ends the run with
            exit code 2.
    """
    raw: dict[str, object] = {}
    for option in _OVERRIDE_OPTIONS:
        value = supplied.get(option.dest)
        if value is not None:
            raw[option.spelling] = value
    return cli_layer.from_mapping(raw)


def _deferred_input(supplied: cli_layer.CliInput) -> cli_layer.CliInput:
    """Return ``supplied`` without the key overrides a deferred ``config show`` does not apply.

    AAP 0.4.4 reports the resolution from defaults, file, profile and environment only when
    ``--resolved`` is absent, so the resolved configuration is built from an input that keeps the
    configuration-file path and drops the key overrides. The dropped overrides are not lost: the
    caller passes the original input as the not-applied section, which is where the operator reads
    them.

    Args:
        supplied: The command line as typed.

    Returns:
        The same input, carrying only the two path options.
    """
    return replace(supplied, overrides={}, spellings={})


def _not_applied_rows(
    config: FoundryConfig, deferred: cli_layer.CliInput
) -> tuple[config_render.ConfigRow, ...]:
    """Return the rows of the separate ``not applied`` section of a deferred ``config show``.

    :func:`foundry.config.render.rows` appends the deferred overrides after every resolved row and
    labels each of them ``not-applied:<--option>``, so the section reuses that rendering and
    selects the tail: the resolved rows are the first ``len(resolved)`` of the combined sequence,
    which keeps one implementation of a row's shape and of its value redaction.

    Args:
        config: The configuration the command reported.
        deferred: The overrides the command did not apply.

    Returns:
        One row per deferred override, in the sorted key order the renderer produces.
    """
    resolved_count = len(config_render.rows(config))
    return config_render.rows(config, not_applied=deferred)[resolved_count:]


@click.group(name="config", help="inspect and validate configuration")
def config_group() -> None:
    """Group the configuration subcommands: ``show`` and ``validate``.

    ``foundry --help`` lists the group with its one-line description and ``foundry config --help``
    lists the two commands (AAP 0.7.4). The group itself resolves nothing: every subcommand
    resolves the configuration once, so the report a command prints and the configuration that
    command validated are the same resolution.
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
    the overrides are applied and attributed to ``cli:<--option>`` (AAP 0.4.4). With
    ``--format json`` or ``--format toml`` the same values and their provenance leave as one
    document on stdout and nothing else (AAP 0.4.5, AAP 0.7.3); in that mode a deferred override
    rides the additive ``not_applied`` member the document already carries.

    Args:
        resolved: Whether this command's overrides are applied rather than deferred.
        output_format: ``"json"``, ``"toml"``, or ``None`` for the ``Key`` / ``Value`` / ``Source``
            table.
        supplied: The values click parsed for the override options, keyed by parameter name.

    Returns:
        :data:`foundry.cli.exit_codes.SUCCESS`; the command that publishes it is
        :func:`foundry.cli.main.main`.

    Raises:
        foundry.core.exceptions.ConfigError: Propagated unchanged from
            :func:`foundry.config.merge.resolve` for any fault AAP 0.4.7 makes fatal, which
            :func:`foundry.cli.main.main` reports on stderr before exiting ``2``.
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
    configuration-file option are applied (AAP 0.4.4). Success prints the one line
    :func:`foundry.config.errors.success_message` builds from the active profile; a fault is not
    caught here, which is what leaves stdout empty when the command fails.

    Args:
        supplied: The values click parsed for the override options, keyed by parameter name.

    Returns:
        :data:`foundry.cli.exit_codes.SUCCESS`; the command that publishes it is
        :func:`foundry.cli.main.main`.

    Raises:
        foundry.core.exceptions.ConfigError: Propagated unchanged from
            :func:`foundry.config.merge.resolve` for the first fault it meets, which
            :func:`foundry.cli.main.main` prints on stderr before exiting ``2``.
    """
    config = merge.resolve(cli_overrides=_supplied_input(supplied))
    cli_render.render_message(
        config_errors.success_message(config.profile),
        redactor=config_render.redactor_for(config),
    )
    return exit_codes.SUCCESS
