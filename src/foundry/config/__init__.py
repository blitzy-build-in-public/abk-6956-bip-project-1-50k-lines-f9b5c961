"""The ``foundry.config`` package: the four-layer configuration engine of AAP 0.4.5.

Settings resolve in one fixed precedence -- field default, then the TOML file with the selected
``[profiles.<name>]`` table over it, then the ``FOUNDRY_*`` environment, then the ``--foundry-*``
command line -- and every resolved key carries the :class:`~foundry.config.sources.ConfigSource`
naming the layer that supplied it. :mod:`foundry.config.merge` owns that resolution,
:mod:`foundry.config.models` the validated settings model, :mod:`foundry.config.keys` the key
registry, :mod:`foundry.config.files` the five-step discovery order, :mod:`foundry.config.env`
the environment source, :mod:`foundry.config.profiles` the profile tables and
:mod:`foundry.config.render` the table ``foundry config show`` prints. This module is the barrel
publishing the surface of them all.

**Published surface.** Exactly the nine names in :data:`__all__`, and nothing else (AAP 0.4.3,
AAP 0.4.9): the six enum-typed key vocabularies, :class:`~foundry.config.sources.ConfigSource`,
:class:`~foundry.config.models.FoundryConfig` and :func:`foundry.config.merge.resolve`. The
exception a resolution raises is deliberately not re-exported here: :class:`ConfigError` is
published once, from :mod:`foundry.core.exceptions` and :data:`foundry.__all__`, and a second
import site would blur that boundary.

**Why the re-exports are lazy** (PEP 562). :mod:`foundry.plugin` imports ``foundry.config.keys``
at module scope on every pytest startup to build the ``--foundry-*`` option group, and importing
a submodule executes its package ``__init__`` first. Eager imports here would therefore load
``pydantic`` and ``pydantic-settings``, through :mod:`foundry.config.models`, on the
``pytest_addoption`` path, and the 150 ms plugin-startup budget of AAP 0.14.2 would fail. The
nine names are hence resolved by the module-level :func:`__getattr__` on first access and typed
for static checkers by the :data:`typing.TYPE_CHECKING` block at the end of this file. Keep it
that way: an eager import here costs the whole budget.

**Layering and import-time discipline** (AAP 0.4.8). The package imports :mod:`foundry.core` and
:mod:`foundry.obs` and nothing above them; this barrel adds no edge of its own beyond its own
submodules. Importing it defines names only -- no file is read, no argument is parsed and no
logger is configured. ``os.environ`` is read in exactly one module of this package,
:mod:`foundry.config.env`, and a caller may pass its own mapping to
:func:`~foundry.config.merge.resolve` rather than let that module read the process environment.
"""

from __future__ import annotations

import importlib
from collections.abc import Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

__all__: list[str] = [
    "AuthStrategy",
    "CassetteMode",
    "ConfigSource",
    "FoundryConfig",
    "LogLevel",
    "ProfileName",
    "QuarantineMode",
    "RetryPredicate",
    "resolve",
]

_LAZY_IMPORTS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "AuthStrategy": "foundry.config.enums",
        "CassetteMode": "foundry.config.enums",
        "ConfigSource": "foundry.config.sources",
        "FoundryConfig": "foundry.config.models",
        "LogLevel": "foundry.config.enums",
        "ProfileName": "foundry.config.enums",
        "QuarantineMode": "foundry.config.enums",
        "RetryPredicate": "foundry.config.enums",
        "resolve": "foundry.config.merge",
    }
)
"""Which module defines each name of :data:`__all__`, for :func:`__getattr__` to import.

The table is an immutable intent record, not module state: it is never written after import and
its keys are exactly the published names, so a name and the module behind it cannot drift apart.
It is read-only through :class:`~types.MappingProxyType`, which is what keeps this module free of
the mutable module-level state AAP 0.4.8 prohibits. Accessing it imports nothing; only reading a
name through :func:`__getattr__` imports the module a name maps to.
"""


def __getattr__(name: str) -> object:
    """Resolve one published name on first access, importing its module at that point.

    ``name`` is looked up in :data:`_LAZY_IMPORTS`. A name outside that table is not published
    here, so the ``AttributeError`` PEP 562 expects for a missing module attribute is raised --
    which is also what lets a ``from foundry.config import errors`` statement fall through to
    importing that submodule instead. A published name is read off its defining module, whose
    import ``sys.modules`` already caches; nothing is written back into this module's globals, so
    the module stays free of post-import mutation and a second access costs one dictionary
    lookup.
    """
    module_name = _LAZY_IMPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module(module_name), name)


def __dir__() -> list[str]:
    """List the published surface, sorted, without importing the modules behind it."""
    return sorted(__all__)


if TYPE_CHECKING:
    # The runtime resolves these names through __getattr__, which a static checker cannot follow,
    # so the same nine names are bound here under their real types. The ``as``-form is the
    # explicit re-export spelling, which keeps them from reading as unused imports.
    from foundry.config.enums import AuthStrategy as AuthStrategy
    from foundry.config.enums import CassetteMode as CassetteMode
    from foundry.config.enums import LogLevel as LogLevel
    from foundry.config.enums import ProfileName as ProfileName
    from foundry.config.enums import QuarantineMode as QuarantineMode
    from foundry.config.enums import RetryPredicate as RetryPredicate
    from foundry.config.merge import resolve as resolve
    from foundry.config.models import FoundryConfig as FoundryConfig
    from foundry.config.sources import ConfigSource as ConfigSource
