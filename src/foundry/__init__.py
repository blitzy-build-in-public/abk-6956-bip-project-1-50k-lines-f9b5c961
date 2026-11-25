"""The ``foundry`` import package: version string and exception names, nothing else.

The version resolves on first access, so importing does no I/O (AAP 0.4.1, 0.4.8).

``PYTEST_DONT_REWRITE`` is pytest's own opt-out marker for assertion rewriting (AAP 0.4.2,
0.7.4). The pytest11 entry point makes this a plugin package, so the console script's
``foundry run`` has imported it by the time its in-process ``pytest.main`` installs the rewriting
hook, and without the marker pytest opens every such run with ``PytestAssertRewriteWarning:
Module already imported so cannot be rewritten; foundry``. Nothing here holds an assert, so no
rewritten form of this module is lost."""

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

__version__: str  # Resolved by the __getattr__ below on first access, never at import.


def __getattr__(name: str) -> object:
    """Resolve ``__version__`` on first access, reading the distribution metadata at that point."""
    if name != "__version__":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import metadata  # Deferred: importing it pulls email.message and inspect.

    try:
        # Metadata is keyed by the distribution name: version("foundry") raises even when installed.
        version = metadata.version("pytest-foundry")
    except metadata.PackageNotFoundError:
        version = "0.1.0"  # A source checkout carries no distribution metadata to read.
    globals().setdefault(name, version)  # Cache the read, so a later access is one lookup.
    return version
