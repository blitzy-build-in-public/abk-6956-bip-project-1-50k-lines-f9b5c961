"""The ``foundry.config`` package: the four-layer configuration engine of AAP 0.4.5.

Each key carries the :class:`~foundry.config.sources.ConfigSource` naming the layer that won it, and
the nine published names re-export lazily through :func:`__getattr__`, which keeps ``pydantic`` off
the ``pytest_addoption`` path.
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

Its keys are exactly the published names and it is read-only, so this module holds no mutable
module-level state and keeps the promise AAP 0.4.8 makes about the package.
"""


def __getattr__(name: str) -> object:
    """Resolve one published name on first access, importing its module at that point.

    A name outside :data:`_LAZY_IMPORTS` raises the ``AttributeError`` PEP 562 expects, so a ``from
    foundry.config import errors`` statement falls through to importing that submodule. Nothing is
    written back into the module globals, so a second access costs one dictionary lookup.
    """
    module_name = _LAZY_IMPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module(module_name), name)


def __dir__() -> list[str]:
    """List the published surface, sorted, without importing the modules behind it."""
    return sorted(__all__)


if TYPE_CHECKING:
    # A static checker cannot follow __getattr__, so the same nine names are bound here under their
    # real types. The ``as``-form is the explicit re-export spelling, which marks them as used.
    from foundry.config.enums import AuthStrategy as AuthStrategy
    from foundry.config.enums import CassetteMode as CassetteMode
    from foundry.config.enums import LogLevel as LogLevel
    from foundry.config.enums import ProfileName as ProfileName
    from foundry.config.enums import QuarantineMode as QuarantineMode
    from foundry.config.enums import RetryPredicate as RetryPredicate
    from foundry.config.merge import resolve as resolve
    from foundry.config.models import FoundryConfig as FoundryConfig
    from foundry.config.sources import ConfigSource as ConfigSource
