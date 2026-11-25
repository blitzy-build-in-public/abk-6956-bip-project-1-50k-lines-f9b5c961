"""``foundry doctor``: the pre-flight probe of the environment Foundry runs in.

The command has two renderings -- the ``Probe`` / ``Status`` / ``Detail`` table and the single JSON
document -- and two exit codes: ``0`` when no required probe failed and ``2`` when one did. A
``warn`` never changes the code, which is why a project that installs no socket guard still
reports a usable environment.

The probes belong to :mod:`foundry.diagnostics`, which owns their order, isolates a probe that
raises into a ``fail`` row and guarantees that the table and the document list the same rows in the
same sequence. This module is a presenter and nothing else: it turns the override options into a
:class:`~foundry.config.cli_layer.CliInput`, picks a rendering and maps ``ok`` onto a code.

Resolution.  :func:`foundry.diagnostics.checks.run_checks` takes a resolved
:class:`~foundry.config.models.FoundryConfig` rather than a ``CliInput``, so this command resolves
the four configuration layers itself, exactly once, with
``merge.resolve(cli_overrides=<this command's CliInput>)``, and hands the model to ``run_checks``.
Resolving here is what makes the options this command accepts genuinely affect the configuration the
doctor probes. A :class:`~foundry.core.exceptions.ConfigError` raised by that call propagates
unchanged, so the fault is not folded into the configuration probe's row -- which is what a
resolution attempted inside ``run_checks`` would do.

No probe opens a socket: the plugin probe reads installed distribution metadata and the directory
probes touch the filesystem.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

import click

from foundry.cli import exit_codes
from foundry.cli import render as cli_render

# ``override_options`` is imported from :mod:`foundry.cli.config_cmd` -- the single owner of the
# ``--foundry-*`` set, whose module docstring states that ``run``, ``config show``,
# ``config validate`` and ``doctor`` all take the decorator from there -- rather than from
# :mod:`foundry.cli.main`. The group builder imports every subcommand module, so a decorator read
# back from it would close an import cycle and leave this module unimportable on its own.
from foundry.cli.config_cmd import override_options
from foundry.config import cli_layer, keys, merge
from foundry.diagnostics.checks import run_checks
from foundry.diagnostics.report import DoctorReport

__all__: Final[tuple[str, ...]] = ("doctor", "doctor_command")

_JSON_INDENT: Final[int] = 2
"""Indent width of the ``--json`` document, the width ``results.json`` uses (AAP 0.7.3)."""

_JSON_UNESCAPED: Final[bool] = False
"""``ensure_ascii`` of the document: a non-ASCII path or detail is written as itself, so two runs on
one machine produce identical bytes (AAP 0.13.1)."""

_NEWLINE: Final[str] = "\n"
"""The document's single trailing newline, so its last line is a complete line to a shell reader."""


def _dest_spellings() -> Mapping[str, str]:
    """Return the option spelling behind every parameter name a Foundry option is stored under.

    ``click`` hands a command its parsed options keyed by the ``dest`` of each option, while
    :func:`foundry.config.cli_layer.from_mapping` speaks the vocabulary an operator types, so the
    two are joined here from the registries both sides already derive from:
    :data:`foundry.config.keys.KEY_SPECS` for the generic ``--foundry-<dotted.key>`` option and each
    named alias, and :data:`foundry.config.cli_layer.SPECIAL_OPTIONS` for the options that
    configure no key. Nothing is respelled by hand, so this table cannot drift from the set
    :func:`foundry.cli.config_cmd.override_options` declares on the command.

    Returns:
        A read-only ``dest -> spelling`` mapping, built once at import and never mutated.
    """
    spellings: dict[str, str] = {}
    for spec in keys.KEY_SPECS:
        if not keys.is_scalar(spec):
            continue
        spellings.setdefault(keys.option_dest(spec.dotted), keys.cli_option(spec.dotted))
        for alias in spec.aliases:
            spellings.setdefault(cli_layer.alias_dest(alias), alias)
    for special in cli_layer.SPECIAL_OPTIONS:
        spellings.setdefault(cli_layer.alias_dest(special.spelling), special.spelling)
    return MappingProxyType(spellings)


_DEST_SPELLINGS: Final[Mapping[str, str]] = _dest_spellings()
"""Every ``--foundry-*`` parameter name this command declares, mapped to its option spelling."""


def _supplied_input(parameters: Mapping[str, object]) -> cli_layer.CliInput:
    """Return the ``--foundry-*`` options click parsed, as the configuration layer reads them.

    The coercions, the spelling rules and the error wording belong to
    :func:`foundry.config.cli_layer.from_mapping`, so this function only renames: each parameter is
    translated through :data:`_DEST_SPELLINGS` and an option that was not supplied -- which click
    reports as ``None`` -- is dropped. The command's own ``--json`` and any unknown name are skipped
    for the same reason, instead of silently becoming an override.

    Raises:
        foundry.core.exceptions.ConfigError: From :func:`foundry.config.cli_layer.from_mapping` for
            a value its key cannot parse or a range it is outside, which ends the command with exit
            code 2.
    """
    supplied: dict[str, object] = {}
    for name in sorted(parameters):
        value = parameters[name]
        if value is None:
            continue
        spelling = _DEST_SPELLINGS.get(name)
        if spelling is None:
            continue
        supplied[spelling] = value
    return cli_layer.from_mapping(supplied)


def _document(report: DoctorReport) -> str:
    """Return the one JSON document ``--json`` prints, as text.

    The members come from :meth:`foundry.diagnostics.report.DoctorReport.as_document`; sorted keys,
    two-space indentation and a single trailing newline make two invocations on an unchanged machine
    produce byte-identical output.
    """
    return (
        json.dumps(
            report.as_document(),
            indent=_JSON_INDENT,
            sort_keys=True,
            ensure_ascii=_JSON_UNESCAPED,
        )
        + _NEWLINE
    )


@click.command(name="doctor", help="probe the environment for Foundry readiness")
@override_options
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="print one JSON document instead of the probe table",
)
def doctor(*, as_json: bool = False, **supplied: object) -> int:
    """Probe the environment and publish the verdict.

    The configuration is resolved with the options this command typed and handed to
    :func:`foundry.diagnostics.checks.run_checks`, which runs the six probes in their published
    order: interpreter, plugin entry point, configuration, artifact directory, cassette directory
    and socket guard. With ``--json`` the report leaves as one document and nothing else reaches
    stdout; without it the ``Probe`` / ``Status`` / ``Detail`` table prints, followed by its count
    line.

    A ``warn`` is information, not a fault: only a ``fail`` of a required probe changes the code,
    which is what lets a project without a socket guard, or one that has not recorded a cassette
    yet, report a healthy environment.

    Returns:
        :data:`foundry.cli.exit_codes.SUCCESS` when no required probe failed, otherwise
        :data:`foundry.cli.exit_codes.USAGE_OR_CONFIG`, for the entry point to publish.

    Raises:
        foundry.core.exceptions.ConfigError: Propagated unchanged from
            :func:`foundry.config.merge.resolve` for any fatal configuration fault, which the entry
            point reports on stderr before exiting ``2`` with nothing printed on stdout.
    """
    cli_input = _supplied_input(supplied)
    # Resolved once, with this command's overrides applied, so the six probes all inspect the
    # configuration the operator asked about; the call is the one the module docstring records.
    config = merge.resolve(cli_overrides=cli_input)
    report = run_checks(config=config)

    if as_json:
        cli_render.render_document(_document(report))
    else:
        cli_render.render_probe_table(report.as_rows(), ok=report.ok)
    return exit_codes.SUCCESS if report.ok else exit_codes.USAGE_OR_CONFIG


doctor_command: Final[click.Command] = doctor
"""The same command under the ``<verb>_command`` name its sibling modules publish.

``foundry.cli.main`` may import either spelling; both name one :class:`click.Command`, so the
option set and the callback exist once.
"""
