"""The ``foundry_data`` fixture: the data files one test reads by name.

``foundry_data`` is one of Foundry's twelve public fixtures (AAP 0.4.3). It is function scoped and
returns a :class:`FoundryDataLoader` whose two published methods each read one data file:
``foundry_data.cases("cart.csv")`` returns every data row of ``<data.dir>/cart.csv`` as a
:class:`foundry.data.cases.Case`, and ``foundry_data.matrix("pricing.yaml")`` returns the cases a
matrix document's cross-product expands to. Both hand back immutable tuples in the order the loader
produced them -- file order for a table, sorted-axis expansion order for a matrix -- so a test
parametrises over a deterministic sequence (implicit requirement I6).

**Names, not paths.** A test names a file relative to ``data.dir`` (default ``testdata``,
AAP 0.4.5), and nothing outside that directory is addressable:
:func:`foundry.data.registry.resolve_data_file` refuses an escaping name through
:func:`foundry.core.paths.resolve_under`, the tree's single containment check (AAP 0.14.3), and a
relative ``data.dir`` is resolved once, here, against pytest's invocation directory.

**Strictness and the located ``DataError``.** ``data.schema_strict`` is passed to the loader as
given: switched on (the default) a column the file's sidecar JSON Schema does not declare is a
fault, switched off the column is tolerated (AAP 0.4.5). Every fault a data file can produce
surfaces as one :class:`foundry.core.exceptions.DataError` naming the file and, where they are
known, the line and the column (AAP 0.4.7), and propagates from here unchanged.

**An empty file is not a fault.** A header-only CSV, an empty JSON array, a blank YAML document and
a blank-only JSON Lines file all yield ``()``, which lets a parametrisation skip the item with the
reason ``no cases in <file>`` rather than fail collection (AAP 0.4.7). An extension no reader claims
is the opposite case and does raise, listing the extensions that would work.

**This module parses nothing.** Reading a format, expanding a matrix, deriving a case id and
enforcing a schema belong to :mod:`foundry.data`, because the ids this fixture returns have to match
the ones the plugin's own ``pytest_generate_tests`` delegation produces for the same file
(AAP 0.7.2). ``foundry_config`` is requested as a fixture argument rather than read from a session
stash, so a project that overrides it -- as ``examples/checkout`` does -- changes the directory this
fixture reads from.

**Imported lazily and layered** (AAP 0.4.8, AAP 0.14.2). This module is imported during
``pytest_configure``, at every pytest start, inside the 150 ms budget, so module scope imports
``dataclasses``, ``pathlib``, ``typing`` and ``pytest`` and no Foundry module at all:
:mod:`foundry.data` and :mod:`foundry.core.exceptions` are imported inside the method that first
needs them, and annotation names under ``TYPE_CHECKING``. Importing this module defines names only.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from foundry.config.models import FoundryConfig
    from foundry.data.cases import Case

__all__ = ["FoundryDataLoader", "foundry_data"]
"""The facade the fixture returns, and the fixture itself (AAP 0.4.3)."""


@dataclass(frozen=True, slots=True)
class FoundryDataLoader:
    """Read the data files of one test, resolving their names against ``data.dir``.

    AAP 0.4.3 publishes this object as the value of the ``foundry_data`` fixture: ``cases(name)``
    returns one :class:`foundry.data.cases.Case` per data row of the named file and
    ``matrix(name)`` returns one per combination of the named matrix document, both as immutable
    tuples. A name is relative to :attr:`directory` and nothing outside it is addressable
    (AAP 0.14.3). Frozen, because a loader captured at collection must not be re-pointed at another
    directory while the run planned around it executes (AAP 0.4.8).

    Attributes:
        directory: The resolved ``data.dir`` the names are resolved against, made absolute against
            pytest's invocation directory.
        schema_strict: ``data.schema_strict`` -- whether a column the file's sidecar schema does not
            declare is a fault (AAP 0.4.5).
        schema_dir: The directory a sidecar schema must stay inside, normally :attr:`directory`;
            ``None`` imposes no containment on the schema document.
    """

    directory: Path
    schema_strict: bool
    schema_dir: Path | None = None

    def cases(self, name: str) -> tuple[Case, ...]:
        """Return the cases one data file expands to, in the order the loader produced them.

        The format is decided by the file's suffix, so ``cart.csv``, ``cart.json``, ``cart.yaml``
        and ``cart.jsonl`` are all read here: a tabular document row by row in file order, a matrix
        document by its cross-product with the axes sorted by name (AAP 0.7.2). A file that parses
        but holds no data rows yields ``()`` rather than raising, so the caller can skip the
        generated item with the reason ``no cases in <file>`` (AAP 0.4.7).

        Args:
            name: The data file's name relative to :attr:`directory`, for example ``cart.csv``; a
                nested name such as ``pricing/cart.csv`` addresses a file in a subdirectory.

        Returns:
            One case per data row, in the loader's order, as an immutable tuple: ``f"{stem}-{row}"``
            ids for a table and the matrix id of each combination for a matrix document.

        Raises:
            DataError: The name is empty, resolves outside :attr:`directory`, names a directory or
                a file that does not exist, carries an extension no reader claims, or produces a row
                the sidecar schema rejects. The message names the file and, where they are known,
                the line and the column (AAP 0.4.7).
            ArtifactError: The resolved name escapes :attr:`directory` -- raised by
                :func:`foundry.core.paths.resolve_under`, which owns that check (AAP 0.14.3).
            ContractError: The sidecar schema document is unreadable, is not a schema, or references
                a location outside itself.
        """
        # Imported inside the method rather than at module scope: this module is imported during
        # `pytest_configure`, and `foundry.data` pulls the readers -- and, behind them, PyYAML and
        # jsonschema -- that only a test reading a data file needs (I5, AAP 0.14.2).
        from foundry.data.registry import load_cases

        loaded = load_cases(
            self._resolve(name),
            strict=self.schema_strict,
            schema_dir=self.schema_dir,
        )
        return tuple(loaded.cases)

    def matrix(self, name: str) -> tuple[Case, ...]:
        """Return the cases the named matrix document's cross-product expands to.

        A matrix document is a mapping whose every value is a list of axis values, so it becomes one
        case per combination with the axes sorted by name and the alphabetically first axis varying
        slowest -- the one order that makes a serial run and every xdist worker collect the same ids
        (AAP 0.7.2, implicit requirement I6). A document that reads as a table is reported as a
        modelling mistake rather than expanded, and an axis holding no values yields ``()``.

        Args:
            name: The matrix document's name relative to :attr:`directory`, for example
                ``pricing.yaml``.

        Returns:
            One case per combination, in expansion order, as an immutable tuple.

        Raises:
            DataError: The name is empty, resolves outside :attr:`directory`, names a directory or
                a file that does not exist, carries an extension no reader claims, or reads as a
                tabular document rather than a matrix. The message names the file (AAP 0.4.7).
            ArtifactError: The resolved name escapes :attr:`directory` (AAP 0.14.3).
        """
        from foundry.data.registry import load_matrix

        return tuple(load_matrix(self._resolve(name)).cases)

    def __repr__(self) -> str:
        """Return ``FoundryDataLoader(directory=..., schema_strict=...)``.

        Only the two settings that decide which files a name reaches are echoed: the schema
        directory normally follows the first, and listing both would suggest they can diverge when
        a caller has not made them.
        """
        return (
            f"{type(self).__name__}"
            f"(directory={self.directory!r}, schema_strict={self.schema_strict!r})"
        )

    def _resolve(self, name: str) -> Path:
        """Return the path ``name`` addresses inside :attr:`directory`.

        Both published methods resolve their name here, so the containment check and the one message
        an unusable name produces are decided in a single place; the joining and traversal checking
        belong to :func:`foundry.data.registry.resolve_data_file`, which delegates containment to
        :func:`foundry.core.paths.resolve_under` (AAP 0.14.3).

        Returns:
            The resolved absolute path of an existing file inside :attr:`directory`.

        Raises:
            DataError: ``name`` is empty or whitespace-only, or the resolved path is a directory or
                does not exist.
            ArtifactError: The resolved path escapes :attr:`directory`.
        """
        from foundry.data.loaders.base import data_error
        from foundry.data.registry import resolve_data_file

        if not name.strip():
            # A name the facade cannot use at all is reported as the data package's own located
            # error rather than as a builtin one, so a caller catches a single type for every
            # unusable name and the message still starts with the directory it was resolved under.
            data_error(self.directory, "a data file name is required, for example 'cart.csv'")
        return resolve_data_file(name, directory=self.directory)


def _base_directory(configured: Path, request: pytest.FixtureRequest) -> Path:
    """Return the absolute data directory a relative ``data.dir`` setting resolves against.

    ``data.dir`` is written by an author as the directory they keep their data in, so a relative
    value means "relative to where I ran pytest" rather than "relative to whatever the process
    happens to have changed its working directory to". Resolution therefore happens once per
    fixture request, against :attr:`pytest.Config.invocation_params`.

    Returns:
        The normalised absolute directory, which is not required to exist: a missing data directory
        makes each name fail with ``data file not found`` naming the file, which is a more useful
        message than a fault about the directory itself.
    """
    directory = Path(configured)
    if not directory.is_absolute():
        directory = Path(request.config.invocation_params.dir) / directory
    return directory.resolve()


@pytest.fixture
def foundry_data(
    request: pytest.FixtureRequest,
    foundry_config: FoundryConfig,
) -> FoundryDataLoader:
    """Return this test's loader over the configured data directory.

    Function scoped (AAP 0.4.3), so every test that requests the fixture gets a loader built from
    the configuration resolved for it, and nothing is shared with the next test or with another
    xdist worker.

    The loader reads two settings and nothing else: ``data.dir`` is the root every name resolves
    against -- resolved against pytest's invocation directory when it is relative -- and
    ``data.schema_strict`` decides whether a column the sidecar schema does not declare is a fault
    (AAP 0.4.5). The resolved directory doubles as the schema containment root, so a data file
    cannot name a schema document anywhere else on the disk. ``foundry_config`` is requested as an
    argument rather than read from a session stash, which is what lets a project override the public
    fixture and have this fixture read the overridden value.

    Returns:
        A :class:`FoundryDataLoader` whose ``cases`` and ``matrix`` methods read files under the
        configured directory.
    """
    directory = _base_directory(foundry_config.data.dir, request)
    return FoundryDataLoader(
        directory=directory,
        schema_strict=foundry_config.data.schema_strict,
        schema_dir=directory,
    )
