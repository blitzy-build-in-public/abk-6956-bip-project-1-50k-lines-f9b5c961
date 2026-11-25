"""The ``foundry`` console script: the click application and the one exit-code mapping.

This module is the exact target of the ``foundry`` console entry point --
``[project.scripts] foundry = "foundry.cli.main:main"`` -- so it owns the three things the command
line surface needs and nothing a subcommand implements (AAP 0.4.2, 0.4.4, 0.7.1): the click group
with its ``--version`` banner and its five documented subcommands, the shared ``--foundry-*``
override decorator every configuration-aware subcommand is decorated with, and the single place a
Foundry error becomes a process exit status.

**The exit-code table** (AAP 0.4.4, 0.4.9, 0.7.3): ``0`` success, ``1`` failed tests or a newly
failing comparison, ``2`` a usage or configuration error Foundry itself raised. pytest's own ``3``
(``INTERNALERROR``), ``4`` (usage error) and ``5`` (no tests collected) are returned unchanged, and
:func:`foundry.cli.exit_codes.passthrough` is what carries them: Foundry never rewrites a code
pytest produced, and this module never invents one of its own. ``KeyboardInterrupt`` keeps pytest's
interrupt code, so a ``^C`` is published as an interrupt rather than reclassified (AAP 0.4.7).

**The layer boundary** (AAP 0.4.8): ``foundry.cli`` sits on the interface rung and may import
``core``, ``obs``, ``config``, ``execution``, ``diagnostics`` and ``reporting``. This module must
never import ``foundry.plugin``: the plugin activates through the ``pytest11`` entry point, and the
command line reaching into it would make the two surfaces inseparable. Importing this module parses
no argument, reads no environment, configures no logger and imports no ``pytest``; the group is
built per call, so an import is inert.

The subcommand modules are imported inside :func:`build_group` rather than at module scope, and the
reason is the one cycle this package would otherwise close: every subcommand module imports the
override decorator, so a module-scope import of them from here -- the module the decorator's home
would have to import back -- leaves that home unimportable on its own. The decorator therefore lives
in :mod:`foundry.cli.config_cmd`, which owns the single override set, and this module re-exports it
as part of its public surface so the entry point still publishes the name the plan requires.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

import click

import foundry
from foundry.cli import exit_codes
from foundry.cli import render as cli_render
from foundry.cli.config_cmd import override_options
from foundry.core.exceptions import FoundryError
from foundry.obs import events

__all__: Final[tuple[str, ...]] = ("build_group", "main", "override_options")

_PROG_NAME: Final[str] = "foundry"
"""The name the banner, the usage lines and the help headings print, whatever argv[0] happens to be.

Pinned rather than detected so two runs from two directories print identical text (AAP 0.13.1).
"""

_VERSION_MESSAGE: Final[str] = "%(prog)s, version %(version)s"
"""The one-line format AAP 0.1.2 pins, so the banner reads ``foundry, version 0.1.0``."""

_GROUP_HELP: Final[str] = (
    "Foundry is a test-automation framework for pytest suites: layered configuration, HTTP and API "
    "helpers, contract validation, data-driven cases, record and replay, retries with quarantine, "
    "sharding, artifact capture and reporting. Run 'foundry run' to execute a suite under Foundry, "
    "'foundry init' to scaffold a project, and 'foundry doctor' to check the environment."
)
"""The one paragraph ``foundry --help`` prints above the subcommand list (AAP 0.7.4)."""

_INTERRUPT_MESSAGE: Final[str] = "aborted"
"""The single line an interrupt prints on stderr, the word click itself uses for the same event."""

_COMPONENT: Final[str] = "cli.main"
"""The component name reported when this module's own error rendering fails (AAP 0.1.2)."""


class _DocumentedOrderGroup(click.Group):
    """A click group that lists its subcommands in the order this module registered them.

    click sorts a group's commands alphabetically, which would print ``config`` first and scatter
    the five subcommands away from the order AAP 0.4.4 documents them in. Registration order is
    equally deterministic and is the order the plan publishes, so the help lists ``run``, ``init``,
    ``config``, ``report`` and ``doctor``.
    """

    def list_commands(self, ctx: click.Context) -> list[str]:
        """Return the registered subcommand names in registration order.

        Args:
            ctx: The group's context, unused here because the order is fixed at build time.

        Returns:
            One name per registered subcommand, in the order :func:`build_group` added them.
        """
        return list(self.commands)


def build_group() -> click.Group:
    """Build and return the ``foundry`` click application.

    Every call constructs a fresh group, so importing this module leaves nothing behind and two
    calls cannot share a mutable command registry. The group declares ``--version`` from
    :data:`foundry.__version__` and nothing else of its own: each subcommand declares the options
    AAP 0.4.4 documents for it, which is what keeps ``foundry init --help`` free of the
    configuration overrides that only a configuration-aware command accepts.

    The five subcommands come from their own modules, imported here rather than at module scope for
    the circular-import reason the module docstring gives. ``config`` and ``report`` are themselves
    groups -- ``show``/``validate`` and ``compare`` -- published under the ``<verb>_command`` name
    their modules also expose so the import spelling cannot drift. ``--version`` and ``--help`` both
    exit ``0``; an invocation that names no subcommand prints the help and is a usage error.

    Returns:
        The application object, ready for :func:`main` or a ``CliRunner`` test.
    """
    from foundry.cli import config_cmd, doctor_cmd, init, report_cmd, run

    @click.group(
        name=_PROG_NAME,
        cls=_DocumentedOrderGroup,
        invoke_without_command=False,
        no_args_is_help=True,
        help=_GROUP_HELP,
    )
    @click.version_option(
        version=foundry.__version__,
        prog_name=_PROG_NAME,
        message=_VERSION_MESSAGE,
    )
    def application() -> None:
        """Publish the five subcommands that make up the ``foundry`` command line."""

    for command in (
        run.run,
        init.init_command,
        config_cmd.config_group,
        report_cmd.report_group,
        doctor_cmd.doctor_command,
    ):
        application.add_command(command)
    return application


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command line and return the process exit status.

    The declared entry target, called by the installed console script's wrapper, which hands the
    value to :func:`sys.exit`: this function therefore returns an ``int`` and never exits the
    process itself. The group is invoked with ``standalone_mode=False`` so click surfaces its
    exceptions here instead of ending the interpreter, which is what makes one documented mapping
    possible.

    The mapping, and the only exception types this function catches -- an unexpected exception is
    deliberately left to propagate, so a real defect stays visible instead of becoming exit ``2``:

    * a command's own ``int`` result is returned unchanged, and a command that returned nothing
      publishes :data:`foundry.cli.exit_codes.SUCCESS`;
    * :class:`foundry.core.exceptions.ConfigError` and :class:`foundry.core.exceptions.DataError`
      -- and every other :class:`foundry.core.exceptions.FoundryError` -- publish
      :data:`foundry.cli.exit_codes.USAGE_OR_CONFIG`;
    * :class:`click.UsageError` and :class:`click.ClickException` publish the same code, which is
      the number click itself publishes in standalone mode;
    * :class:`KeyboardInterrupt` -- which click re-raises as :class:`click.Abort` in this mode --
      publishes :data:`foundry.cli.exit_codes.INTERRUPT`, pytest's interrupt code, preserved as
      AAP 0.4.7 requires.

    A caught error's message is written to stderr by :func:`foundry.cli.render.render_error`, which
    puts the hint on an indented second line and passes both through the redaction engine, so a
    value the configuration masks can never reach the terminal (AAP 0.3.2, 0.7.4).

    Args:
        argv: The arguments to parse, without the program name; ``None`` reads ``sys.argv[1:]``, the
            console script's own behaviour.

    Returns:
        The exit status to publish, as one of :mod:`foundry.cli.exit_codes`' documented values or a
        code a command produced -- pytest's own ``3``, ``4`` and ``5`` included.
    """
    command = build_group()
    arguments: list[str] | None = None if argv is None else list(argv)
    try:
        result = command.main(args=arguments, prog_name=_PROG_NAME, standalone_mode=False)
    except (click.Abort, KeyboardInterrupt):
        # click converts a ^C into Abort once standalone mode is off, so both spellings of one
        # interrupt land here and keep pytest's interrupt code rather than becoming a usage error.
        _render_failure(_INTERRUPT_MESSAGE, None)
        return exit_codes.INTERRUPT
    except click.ClickException as error:
        _render_failure(error.format_message(), None)
        return exit_codes.USAGE_OR_CONFIG
    except FoundryError as error:
        _render_failure(str(error), error.hint)
        return exit_codes.USAGE_OR_CONFIG
    return exit_codes.passthrough(result if isinstance(result, int) else None)


def _render_failure(message: str, hint: str | None) -> None:
    """Write one failure to stderr, absorbing a rendering fault so the exit code still stands.

    Failure isolation (AAP 0.1.2): a fault inside Foundry's own plumbing -- here the console, the
    redaction engine or a closed stream -- is reported at ``WARNING`` on the ``foundry`` logger and
    changes neither the outcome nor the code :func:`main` is about to publish. The broad handler is
    the point of this function, and it covers only the write, never the command that failed.

    Args:
        message: The text of the failure, redacted before it reaches the console.
        hint: An actionable next step printed on the line below, or ``None`` for none.

    Returns:
        ``None``; the lines go to stderr.
    """
    try:
        cli_render.render_error(message, hint)
    except Exception as error:
        # Deliberately broad: it covers only this write, and the one thing that must not happen is a
        # console or redaction fault replacing the exit status the command already produced.
        events.emit_plumbing_failure(_COMPONENT, "could not render an error message", exc=error)
