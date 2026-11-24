"""The import closure of ``foundry.cli`` and the single owner of its override option set.

AAP 0.4.4 requires the same ``--foundry-*`` override set on every subcommand that resolves project
configuration -- ``run``, ``config show``, ``config validate`` and ``doctor`` -- so the option group
has to exist once, in a module all of those commands can import.
:mod:`foundry.cli.config_cmd` holds it, and this module is the proof of that: the group builder
imports every subcommand module, so a decorator taken back from the builder would close an import
cycle, while a second copy of the group would let the spellings the three commands publish drift
apart.

Two properties are guarded here. Every module ``pkgutil`` finds in ``foundry.cli`` is imported, so a
module naming something that does not exist fails as a test rather than as a gate outage, and the
option spellings ``run``, ``config show`` and ``config validate`` register are compared, so a second
implementation of the group cannot appear unnoticed.
"""

from __future__ import annotations

import importlib
import pkgutil

import click
import pytest

import foundry.cli
import foundry.cli.config_cmd as config_cmd
import foundry.cli.run as run_module

_OVERRIDE_PREFIX = "--foundry-"
"""The prefix AAP 0.4.4 gives every option of the override set."""

_OVERRIDE_COUNT = 28
"""Options in the set: one per scalar key, the named aliases, and ``--foundry-config``."""


def _cli_module_names() -> tuple[str, ...]:
    """Return the name of every module the ``foundry.cli`` package holds.

    Read from the package rather than listed, so the modules written after this test still run
    through it: a planned module such as ``foundry.cli.main`` needs no edit here to be covered.

    Returns:
        The fully qualified module names, sorted so a failure names the same module on every run.
    """
    return tuple(
        sorted(
            f"{foundry.cli.__name__}.{module.name}"
            for module in pkgutil.iter_modules(foundry.cli.__path__)
        )
    )


_CLI_MODULE_NAMES: tuple[str, ...] = _cli_module_names()
"""Every module of ``foundry.cli``, collected once at import."""


def _override_spellings(command: click.Command) -> set[str]:
    """Return the override option spellings ``command`` declares.

    Args:
        command: The click command to read; its parameters carry the spellings click stored from
            the option decorators.

    Returns:
        The spellings beginning with ``--foundry-``, leaving out a command's own options
        (``--resolved``, ``--format``, ``--json``) and its pass-through arguments.
    """
    return {
        spelling
        for parameter in command.params
        for spelling in parameter.opts
        if spelling.startswith(_OVERRIDE_PREFIX)
    }


def test_cli_module_discovery_finds_the_import_targets() -> None:
    """The collected package contents include the decorator's home and the command that uses it."""
    assert "foundry.cli.config_cmd" in _CLI_MODULE_NAMES
    assert "foundry.cli.run" in _CLI_MODULE_NAMES


@pytest.mark.parametrize("module_name", _CLI_MODULE_NAMES)
def test_every_cli_module_imports(module_name: str) -> None:
    """Each module of ``foundry.cli`` imports on its own, without the group builder."""
    assert importlib.import_module(module_name) is not None


def test_override_options_has_a_single_owner() -> None:
    """``run`` binds the decorator ``config_cmd`` defines, which that module also exports."""
    assert run_module.override_options is config_cmd.override_options
    assert "override_options" in config_cmd.__all__


def test_config_commands_register_the_same_override_set() -> None:
    """``run``, ``config show`` and ``config validate`` publish one identical option set."""
    run_set = _override_spellings(run_module.run)
    show_set = _override_spellings(config_cmd.config_group.commands["show"])
    validate_set = _override_spellings(config_cmd.config_group.commands["validate"])

    assert run_set == show_set
    assert show_set == validate_set
    assert len(run_set) == _OVERRIDE_COUNT
