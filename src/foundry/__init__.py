"""The ``foundry`` import package: one distribution, four surfaces.

Installing the ``pytest-foundry`` distribution makes this package importable as
``foundry`` and activates all four surfaces at once: the pytest plugin loaded through its
``pytest11`` entry point, the fixture and assertion library a suite imports, the
``foundry`` console application, and the scaffolder behind ``foundry init``.

This module is the one file every ``import foundry`` pays for, so it resolves the version
and re-exports the exception names and does nothing else: no capability package and none
of the third-party libraries behind them is imported here, which is what keeps the pytest
plugin inside its startup budget. The exception classes are defined once in
:mod:`foundry.core.exceptions`, and the re-exports below carry the names, bases and
attributes the compatibility promise fixes.
"""

from importlib import metadata
from typing import Final

from .core.exceptions import (
    ArtifactError,
    CassetteMissError,
    ConfigError,
    ContractError,
    DataError,
    FoundryError,
    RetryExhaustedError,
    TransportError,
)

__all__: Final[tuple[str, ...]] = (
    "__version__",
    "ArtifactError",
    "CassetteMissError",
    "ConfigError",
    "ContractError",
    "DataError",
    "FoundryError",
    "RetryExhaustedError",
    "TransportError",
)

try:
    # Distribution metadata is keyed by the distribution name, not the import name:
    # version("foundry") raises PackageNotFoundError even with the wheel installed.
    __version__: str = metadata.version("pytest-foundry")
except metadata.PackageNotFoundError:
    # A source checkout carries no installed distribution metadata to read.
    __version__ = "0.1.0"
