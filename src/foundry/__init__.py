"""The ``foundry`` import package: version string and exception names, nothing else.

It re-exports only ``__version__`` and the exception names, so every ``import foundry`` stays
inside the startup budget (AAP 0.4.8, AAP 0.14.2); the classes are defined once in
:mod:`foundry.core.exceptions` and their names, bases and attributes are the compatibility
promise (AAP 0.4.9).
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
    # Metadata is keyed by the distribution name: version("foundry") raises even when installed.
    __version__: str = metadata.version("pytest-foundry")
except metadata.PackageNotFoundError:
    # A source checkout carries no installed distribution metadata to read.
    __version__ = "0.1.0"
