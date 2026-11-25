"""The ``foundry`` console application: the ``foundry.cli`` package of AAP 0.3.2.

The five subcommands -- ``run``, ``init``, ``config``, ``report`` and ``doctor`` (AAP 0.4.4) -- live
in their own modules, and the console entry point ``[project.scripts] foundry =
"foundry.cli.main:main"`` (AAP 0.4.2) reaches them, so this module is the package marker and the
barrel of the public surface, holding no logic of its own. Its import budget is ``core``, ``obs``,
``config``, ``execution``, ``diagnostics`` and ``reporting``, and never ``plugin`` (AAP 0.4.8): the
plugin activates through the ``pytest11`` entry point, and a command line that imported it would
make the two surfaces inseparable. Both published names come from :mod:`foundry.cli.main`,
re-exported lazily through the PEP 562 :func:`__getattr__` below, so ``import foundry.cli`` imports
nothing from ``click``, ``rich``, ``pytest`` or ``pydantic``, while ``from foundry.cli import main``
still resolves on first use.
"""

from __future__ import annotations

import importlib
from collections.abc import Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

__all__: list[str] = ["build_group", "main"]

_LAZY_IMPORTS: Final[Mapping[str, str]] = MappingProxyType(
    {"build_group": "foundry.cli.main", "main": "foundry.cli.main"}
)
"""Which module defines each name of :data:`__all__`, for :func:`__getattr__` to import.

Read-only through :class:`~types.MappingProxyType` and keyed by exactly the published names, so this
is a lookup for those two names rather than a registration table: the tree's only import-time
registration tables are ``foundry.data.registry`` and ``foundry.reporting.renderers`` (AAP 0.4.8).

``main`` and the submodule ``foundry.cli.main`` share a spelling, so importing that submodule binds
the module object to this attribute before :func:`__getattr__` is consulted; the console entry point
resolves ``foundry.cli.main:main`` through ``importlib`` and is indifferent to which one it finds.
"""


def __getattr__(name: str) -> object:
    """Resolve one published name on first access, importing its defining module at that point.

    A name outside :data:`_LAZY_IMPORTS` raises the ``AttributeError`` PEP 562 expects, naming the
    sorted surface so the caller sees the valid spellings. A resolved name is memoised in the module
    globals, so a second access costs one dictionary lookup: the value it stores is the very object
    ``importlib`` already returns out of ``sys.modules``, which makes the entry a cache of an
    imported name rather than module-level state the run depends on (AAP 0.4.8).
    """
    module_name = _LAZY_IMPORTS.get(name)
    if module_name is None:
        surface = ", ".join(sorted(__all__))
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}; its published surface is {surface}"
        )
    value = getattr(importlib.import_module(module_name), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """List the published surface, sorted, without importing the modules behind it."""
    return sorted(__all__)


if TYPE_CHECKING:
    # A static checker cannot follow __getattr__, so the two published names are bound here under
    # their real types. The ``as``-form is the explicit re-export spelling ``mypy --strict``
    # requires, which is also what keeps each import from reading as unused.
    from foundry.cli.main import build_group as build_group
    from foundry.cli.main import main as main
