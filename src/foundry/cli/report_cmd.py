"""``foundry report compare``: which tests regressed between two result documents.

The command prints the three node-id sets -- Newly failing, Newly passing and Still failing, in that
fixed order -- and ``--json`` prints one document instead. The exit codes are ``0``, ``1`` for a
newly failing test and ``2`` for an unreadable document, which raises
:class:`foundry.core.exceptions.DataError` here and is mapped to that code by the command line entry
point alone. The comparison itself comes from :func:`foundry.reporting.compare.diff_files`; no
project configuration is read.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final, cast

import click

from foundry.cli.exit_codes import FAILED, SUCCESS
from foundry.cli.render import render_comparison, render_document
from foundry.reporting.compare import ComparisonDiff, diff_files

__all__: Final[tuple[str, ...]] = ("compare_command", "comparison_exit_code", "report_group")

_JSON_INDENT: Final[int] = 2
"""Indent width of the ``--json`` document: two spaces, the width ``results.json`` uses."""

_JSON_UNESCAPED: Final[bool] = False
"""``ensure_ascii`` of the ``--json`` document: a non-ASCII node id is written as itself, so a
parametrised case id stays readable and two runs of one case produce identical bytes."""

_NEWLINE: Final[str] = "\n"
"""The document's single trailing newline, so its last line is a complete line to a shell reader."""


class _ExitCodeGroup(click.Group):
    """A click group that publishes the exit status its subcommand returned.

    Click discards a callback's return value, so the status ``report compare`` computes is published
    through :meth:`click.Context.exit`: :func:`compare_command` stays a plain function returning the
    documented code, and ``CliRunner`` observes the same number.
    """

    def invoke(self, ctx: click.Context) -> object:
        """Invoke the selected subcommand, then exit with the status it returned.

        A non-``int`` value is returned, which click's own machinery ignores; a returned exit
        status does not return at all, because :meth:`click.Context.exit` ends the invocation with
        it.
        """
        result = super().invoke(ctx)
        if isinstance(result, int):
            ctx.exit(result)
        return cast(object, result)


@click.group(name="report", cls=_ExitCodeGroup)
def report_group() -> None:
    """Compare Foundry result documents."""


def comparison_exit_code(diff: ComparisonDiff) -> int:
    """Return the exit status a comparison publishes.

    :data:`foundry.cli.exit_codes.FAILED` when at least one test newly fails, which is what makes a
    regression break a build that already measured the baseline; otherwise
    :data:`foundry.cli.exit_codes.SUCCESS`. A standing failure and a recovery both leave the status
    at ``0``, because neither is news about the newer run.
    """
    return FAILED if diff.has_new_failures else SUCCESS


def _document(diff: ComparisonDiff) -> str:
    """Return the one JSON document the ``--json`` flag prints, as text.

    The keys come from :meth:`foundry.reporting.compare.ComparisonDiff.as_dict`, which already
    yields lists, so the document holds exactly the three keys AAP 0.7.3 fixes. Sorted keys,
    two-space indentation and a single trailing newline make two identical inputs produce
    byte-identical output (AAP 0.13.1).
    """
    return (
        json.dumps(
            diff.as_dict(),
            indent=_JSON_INDENT,
            sort_keys=True,
            ensure_ascii=_JSON_UNESCAPED,
        )
        + _NEWLINE
    )


@report_group.command(name="compare")
@click.argument("base", type=click.Path(dir_okay=False, path_type=Path))
@click.argument("new", type=click.Path(dir_okay=False, path_type=Path))
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Print the comparison as one JSON document instead of three readable groups.",
)
def compare_command(base: Path, new: Path, *, as_json: bool = False) -> int:
    """Print the node-id sets that changed between two result documents.

    BASE is the baseline ``results.json`` and NEW the newer one, both written by a Foundry run. The
    three groups print in the fixed order Newly failing, Newly passing, Still failing (AAP 0.7.4),
    each with its size and its node ids in ascending order, and an empty group prints ``(none)`` so
    that all three are always visible. Raises: foundry.core.exceptions.DataError: Either path is
    missing, is a directory, cannot be decoded as UTF-8, or does not hold a Foundry result document.
    Deliberately not caught here: the command line maps it to exit code ``2`` with the message on
    stderr and nothing on stdout (AAP 0.4.4), and :func:`foundry.cli.main.main` is the one place
    that mapping is stated.
    """
    diff = diff_files(base, new)
    if as_json:
        render_document(_document(diff))
    else:
        render_comparison(diff)
    return comparison_exit_code(diff)
