"""The ``foundry_data`` fixture: the data files one test reads by name.

``foundry_data`` is one of Foundry's twelve public fixtures (AAP 0.4.3). It is function scoped and
returns a :class:`FoundryDataLoader` whose two published methods each read one data file:
``foundry_data.cases("cart.csv")`` returns every data row of ``<data.dir>/cart.csv`` as a
:class:`foundry.data.cases.Case`, and ``foundry_data.matrix("pricing.yaml")`` returns the cases a
matrix document's cross-product expands to. Both hand back immutable tuples in the order the loader
produced them -- file order for a table, sorted-axis expansion order for a matrix -- so a test
parametrises over a deterministic sequence instead of a set or a dictionary view (implicit
requirement I6).

**Names, not paths.** A test names a file relative to ``data.dir`` (default ``testdata``,
AAP 0.4.5), and nothing outside that directory is addressable:
:func:`foundry.data.registry.resolve_data_file` refuses an escaping name through
:func:`foundry.core.paths.resolve_under`, the tree's single containment check, so a ``..`` segment
or an absolute path pointing elsewhere is rejected rather than read (AAP 0.14.3). A relative
``data.dir`` is resolved once, here, against pytest's invocation directory -- the directory the
command was run from, and the one an author means by ``testdata``.

**Strictness and the located ``DataError``.** ``data.schema_strict`` is passed to the loader as
given: switched on (the default) a column the file's sidecar JSON Schema does not declare is a
fault, switched off the column is tolerated and the row still becomes a case (AAP 0.4.5). Every
fault a data file can produce surfaces as one :class:`foundry.core.exceptions.DataError` naming the
file and, where they are known, the line and the column (AAP 0.4.7): a name that resolves outside
``data.dir``, a file that does not exist, an extension no reader claims, a row missing a required
column, and a row the schema rejects. The error propagates from here unchanged, so a single typed
exception reaches the collection error pytest reports.

**An empty file is not a fault.** A header-only CSV, an empty JSON array, a blank YAML document and
a blank-only JSON Lines file all yield ``()``, which is what lets a parametrisation skip the item
with the reason ``no cases in <file>`` rather than fail collection (AAP 0.4.7). An extension no
reader claims is the opposite case and does raise, listing the extensions that would work.

**This module parses nothing.** Reading a format, expanding a matrix, deriving a case id and
enforcing a schema belong to :mod:`foundry.data`, and a second implementation here would let the two
drift: the ids this fixture returns have to match the ones the plugin's own
``pytest_generate_tests`` delegation produces for the same file, because both are the same
collection seen from two entry points (AAP 0.7.2). The facade supplies the resolved directory and
the strictness flag, returns what the engine produced, and writes no file of its own.

**A conftest override reaches it.** ``foundry_config`` is requested as a fixture argument rather
than read from a session stash, so a project that overrides the public fixture -- as
``examples/checkout`` does to point ``data.dir`` at its own directory -- changes the directory this
fixture reads from. Nothing is cached at module or session scope and the loader is frozen, so one
test's settings cannot reach another's.

**Imported lazily** (implicit requirement I5, AAP 0.14.2). This module is imported during
``pytest_configure``, at every pytest start, inside a 150 ms budget, while only the tests that
request the fixture read a data file. Module scope therefore imports ``dataclasses``, ``pathlib``,
``typing`` and ``pytest`` and no Foundry module at all: :mod:`foundry.data` and
:mod:`foundry.core.exceptions` are imported inside the method that first needs them, and the names
used in annotations are imported under ``TYPE_CHECKING``. ``yaml`` and ``jsonschema`` stay behind
:mod:`foundry.data`'s own function-local imports, so neither is on this module's import path.

**Layering** (AAP 0.4.8). This module imports :mod:`foundry.data` and, on a misuse of the facade,
:mod:`foundry.core`. It imports no ``foundry.http``, ``foundry.execution``, ``foundry.reporting``,
``foundry.cli`` or ``foundry.plugin``, and it never resolves configuration itself: the
:class:`foundry.config.models.FoundryConfig` arrives as the ``foundry_config`` fixture's value, and
that fixture is the one upward edge the fixture package is allowed.

Importing this module defines names only: the facade, its one private helper and the fixture. It
performs no I/O, reads no environment variable, configures no logger and holds no module-level
state (AAP 0.4.8).

Example:
    Reading the rows of a CSV, skipping the way an empty data file is meant to be skipped::

        def test_every_row_is_priced(foundry_data: FoundryDataLoader) -> None:
            cases = foundry_data.cases("cart.csv")
            if not cases:
                pytest.skip("no cases in cart.csv")
            assert all(case.values["quantity"] > 0 for case in cases)
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
    (AAP 0.14.3).

    Frozen, because a loader captured at collection must not be re-pointed at another directory
    while the run planned around it executes, and free of shared state, because the fixture builds
    one instance per test from that test's own configuration (AAP 0.4.8).

    Attributes:
        directory: The resolved ``data.dir`` the names are resolved against. Absolute, because the
            fixture resolves a relative setting once, against pytest's invocation directory.
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

        The format is decided by the file's suffix, so ``cart.csv``, ``cart.json``,
        ``cart.yaml`` and ``cart.jsonl`` are all read here and each reaches the cases through the
        same conversion: a tabular document row by row in file order, a matrix document by its
        cross-product with the axes sorted by name (AAP 0.7.2). A file that parses but holds no
        data rows yields ``()`` rather than raising, so the caller can skip the generated item with
        the reason ``no cases in <file>`` (AAP 0.4.7).

        Args:
            name: The data file's name relative to :attr:`directory`, for example ``cart.csv``. A
                nested name such as ``pricing/cart.csv`` addresses a file inside a subdirectory of
                the data directory.

        Returns:
            One case per data row, in the loader's order, as an immutable tuple: ``f"{stem}-{row}"``
            ids for a table and the matrix id of each combination for a matrix document. Empty when
            the document holds no data.

        Raises:
            DataError: The name is empty, resolves outside :attr:`directory`, names a directory or
                a file that does not exist, carries an extension no reader claims, or produces a row
                the sidecar schema rejects. The message names the file and, where they are known,
                the line and the column (AAP 0.4.7).
            ArtifactError: The resolved name escapes :attr:`directory` -- raised by
                :func:`foundry.core.paths.resolve_under`, which owns that check (AAP 0.14.3).
            ContractError: The sidecar schema document is unreadable, is not a schema, or
                references a location outside itself.
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

        A matrix document is a mapping whose every value is a list of axis values, so it becomes
        one case per combination with the axes sorted by name and the alphabetically first axis
        varying slowest -- the one order that makes a serial run and every xdist worker collect the
        same ids (AAP 0.7.2, implicit requirement I6). A document that reads as a table is a
        modelling mistake and is reported as one rather than expanded, and an axis holding no
        values yields ``()`` so the caller can skip with ``no cases in <file>`` (AAP 0.4.7).

        Args:
            name: The matrix document's name relative to :attr:`directory`, for example
                ``pricing.yaml``.

        Returns:
            One case per combination, in expansion order, as an immutable tuple. Empty when the
            document declares no axis or an axis holds no value.

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
        directory normally follows the first, and a representation listing both would suggest the
        two can diverge when a caller has not made them.
        """
        return (
            f"{type(self).__name__}"
            f"(directory={self.directory!r}, schema_strict={self.schema_strict!r})"
        )

    def _resolve(self, name: str) -> Path:
        """Return the path ``name`` addresses inside :attr:`directory`.

        Both published methods resolve their name here, so the containment check and the one
        message an unusable name produces are decided in a single place. Nothing is joined,
        normalised or stripped of ``..`` segments in this module: that is
        :func:`foundry.data.registry.resolve_data_file`'s work, which delegates containment to
        :func:`foundry.core.paths.resolve_under` (AAP 0.14.3).

        Args:
            name: The data file's name relative to :attr:`directory`.

        Returns:
            The resolved absolute path of an existing file inside :attr:`directory`.

        Raises:
            DataError: ``name`` is empty or whitespace-only, or the resolved path is a directory or
                does not exist -- the last two reported by :mod:`foundry.data` itself.
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
    happens to have changed its working directory to" -- a test that changes directory would
    otherwise silently read a different tree. Resolution therefore happens once per fixture request,
    against :attr:`pytest.Config.invocation_params`, and an absolute setting is only normalised.

    Args:
        configured: The ``data.dir`` value the resolved configuration carries.
        request: The fixture request, read for the invocation directory alone.

    Returns:
        The normalised absolute directory. It is not required to exist: a data directory that is
        missing makes each name fail with ``data file not found`` naming the file, which is a more
        useful message than a fault about the directory itself.
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
    cannot name a schema document anywhere else on the disk.

    ``foundry_config`` is requested as an argument rather than read from a session stash, which is
    what lets a project override the public fixture -- ``examples/checkout`` does, to point
    ``data.dir`` at the example's own directory -- and have this fixture read the overridden value.
    Nothing is cached, so the override reaches the very next test.

    Args:
        request: The fixture request, read for the invocation directory a relative ``data.dir``
            resolves against.
        foundry_config: The resolved configuration, read for ``data.dir`` and
            ``data.schema_strict`` only.

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
