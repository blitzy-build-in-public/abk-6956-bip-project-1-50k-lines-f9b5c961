"""``foundry report compare``: which tests regressed between two result documents.

``foundry report compare BASE.json NEW.json`` answers the one question a team asks of two
``results.json`` documents -- did this run regress against the baseline? -- by printing the three
node-id sets AAP 0.4.4 has the command publish: Newly failing, Newly passing and Still failing, in
that fixed order (AAP 0.7.4). ``--json`` replaces those three groups with exactly one document on
stdout and nothing else -- ``{"newly_failing": [...], "newly_passing": [...], "still_failing":
[...]}``, every list in ascending order (AAP 0.7.3) -- so a CI job reads the answer without parsing
prose.

Exit codes (AAP 0.4.4)
    ``0`` (:data:`foundry.cli.exit_codes.SUCCESS`) when no test newly fails, ``1``
    (:data:`foundry.cli.exit_codes.FAILED`) when at least one does, and ``2``
    (:data:`foundry.cli.exit_codes.USAGE_OR_CONFIG`) when either document cannot be read or is not a
    Foundry result document. The third code is not produced here, and deliberately so: an unreadable
    document raises :class:`foundry.core.exceptions.DataError` from
    :func:`foundry.reporting.store.read`, this module lets it through, and
    :func:`foundry.cli.main.main` maps it to ``2`` with the message on stderr and nothing on stdout.
    Catching it here would state that mapping a second time, and the two statements could disagree.

No comparison logic
    The three sets come from :func:`foundry.reporting.compare.diff_files`, the comparator the
    report and the JUnit writer already agree with. This module performs no set arithmetic,
    re-classifies no outcome and keeps no quarantine special case of its own: quarantined outcomes
    are excluded from the comparison there because a known-flaky failure does not fail its run
    (AAP 0.4.7), and deciding that again here could make the command line and the report disagree
    about one pair of documents.

No project configuration
    ``report compare`` consumes two documents and nothing else. It declares no ``--foundry-*``
    option, imports no configuration module and reads no environment variable, so no
    ``foundry.toml``, profile or environment override can change its answer -- AAP 0.4.4 states that
    ``init`` and ``report compare`` resolve no project configuration and take none of the options
    that do.

Where the output goes
    Nothing here prints directly. The readable form is printed by
    :func:`foundry.cli.render.render_comparison`, which passes the three labels and every node id
    through the one redaction engine (AAP 0.3.2), and the machine-readable form is emitted by
    :func:`foundry.cli.render.render_document`, which writes the bytes verbatim so a JSON document
    is never wrapped at the terminal width. ``rich`` therefore belongs to that module and is never
    imported here.

Determinism
    The comparator hands over ascending, de-duplicated tuples, the renderer prints them in the order
    it receives them, and the JSON document is rendered with sorted keys and one trailing newline.
    Two identical pairs of documents therefore produce byte-identical output, and nothing here reads
    a clock, sleeps, or iterates a set or a bare dict on its way to the console (AAP 0.13.1).

Import direction and import-time discipline (AAP 0.4.8)
    The command line may import ``core``, ``obs``, ``config``, ``execution``, ``diagnostics`` and
    ``reporting``, and this module needs the exit-code vocabulary, the renderer and the comparator.
    Importing it defines names and three constants only: no I/O, no environment read, no logger
    configuration, no argument parsing and no module-level mutable state.
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
"""Indent width of the ``--json`` document: two spaces per nesting depth, the width
``results.json`` uses, so two documents a person reads side by side are formatted the same way."""

_JSON_UNESCAPED: Final[bool] = False
"""``ensure_ascii`` of the ``--json`` document: a non-ASCII node id is written as itself, so a
parametrised case id stays readable and two runs of one case produce identical bytes."""

_NEWLINE: Final[str] = "\n"
"""The document's single trailing newline, so its last line is a complete line to a shell reader."""


class _ExitCodeGroup(click.Group):
    """A click group that publishes the exit status its subcommand returned.

    Click discards a command callback's return value, so a command that computes its own exit status
    -- as ``report compare`` does, because newly failing comparisons are exit ``1`` (AAP 0.4.4) --
    needs one place that turns that value into the process's status. Doing it here rather than in
    the callback keeps :func:`compare_command` a plain function returning the documented code, and
    publishes the code through :meth:`click.Context.exit`, which is the route click itself honours:
    ``CliRunner`` reports it as the invocation's ``exit_code``, and a group run with
    ``standalone_mode=False`` returns it to its caller. Both surfaces therefore observe the same
    number, which is what the command's contract promises (AAP 0.4.9).
    """

    def invoke(self, ctx: click.Context) -> object:
        """Invoke the selected subcommand, then exit with the status it returned.

        Args:
            ctx: The group's click context, as click built it for this invocation.

        Returns:
            The subcommand's return value when it is not an ``int``, which click's own machinery
            ignores. A returned exit status does not return at all: :meth:`click.Context.exit` ends
            the invocation with it.
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

    Args:
        diff: The comparison between a baseline and a newer document, as
            :func:`foundry.reporting.compare.diff_files` returned it.

    Returns:
        :data:`foundry.cli.exit_codes.FAILED` when at least one test newly fails, which is what
        makes a regression break a build that already measured the baseline; otherwise
        :data:`foundry.cli.exit_codes.SUCCESS`. A standing failure and a recovery both leave the
        status at ``0``, because neither is news about the newer run.
    """
    return FAILED if diff.has_new_failures else SUCCESS


def _document(diff: ComparisonDiff) -> str:
    """Return the one JSON document the ``--json`` flag prints, as text.

    The keys come from :meth:`foundry.reporting.compare.ComparisonDiff.as_dict`, which already
    yields lists, so the document holds exactly the three keys AAP 0.7.3 fixes and no other. Sorted
    keys, two-space indentation and a single trailing newline are what make two identical inputs
    produce byte-identical output (AAP 0.13.1).

    Args:
        diff: The comparison to render.

    Returns:
        The document as text, ending with exactly one newline.
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

    BASE is the baseline ``results.json`` and NEW is the newer one, both written by a Foundry run.
    The three groups print in the fixed order Newly failing, Newly passing, Still failing (AAP
    0.7.4), each with its size and its node ids in ascending order, and an empty group prints
    ``(none)`` instead of disappearing so that all three are always visible.

    Args:
        base: Path of the baseline result document.
        new: Path of the newer result document.
        as_json: Whether to print one JSON document instead of the three readable groups.

    Returns:
        :data:`foundry.cli.exit_codes.FAILED` when at least one test newly fails, otherwise
        :data:`foundry.cli.exit_codes.SUCCESS`.

    Raises:
        foundry.core.exceptions.DataError: Either path is missing, is a directory, cannot be decoded
            as UTF-8, or does not hold a Foundry result document. It is deliberately not caught
            here: the command line maps it to exit code ``2`` with the message on stderr and
            nothing on stdout (AAP 0.4.4), and :func:`foundry.cli.main.main` is the one place that
            mapping is stated.
    """
    diff = diff_files(base, new)
    if as_json:
        render_document(_document(diff))
    else:
        render_comparison(diff)
    return comparison_exit_code(diff)
