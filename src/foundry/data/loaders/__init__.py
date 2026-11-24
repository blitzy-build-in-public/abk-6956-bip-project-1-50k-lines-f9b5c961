"""The data format readers, and the neutral document shapes they hand upward.

Every reader in this sub-package turns one data file into a neutral,
line-annotated document -- a :class:`LoadedTable` for anything that reads as rows
of named columns, a :class:`LoadedMatrix` for a mapping whose values are axes --
so that the rest of :mod:`foundry.data` converts those documents into ``Case``
objects without knowing which format produced them.

The dependency direction is acyclic: ``base`` <- {``csv``, ``json``, ``yaml``,
``jsonl``} <- ``foundry.data.registry`` (AAP 0.4.8). No reader imports a sibling
reader; each imports only :mod:`foundry.data.loaders.base`, which itself imports
nothing of Foundry except ``DataError``.

This barrel imports no concrete loader, so ``foundry.data.registry`` remains the
single wiring point for the shipped formats (``csv``, ``json``, ``yaml``,
``yml``, ``jsonl``): each concrete module exposes a module-level ``LOADER``
singleton, and the registry -- not this module -- imports those four objects and
seeds its table from the suffixes they publish. Keeping them out of here holds
the import cost of the sub-package to the document shapes and the error helper,
which is what the plugin's startup budget depends on (AAP 0.14.2).

Every failure in the sub-package is a :class:`foundry.core.exceptions.DataError`
raised through :func:`foundry.data.loaders.base.data_error`, the package's only
construction site for it, so a collection error always names the file and, where
the format provides them, the line and the column (AAP 0.4.7).
"""

# One statement per re-export: the ``X as X`` alias is the explicit re-export
# idiom, and the project's isort configuration does not combine aliased names, so
# a single parenthesized block is reported as unsorted (I001).
from foundry.data.loaders.base import LoadedMatrix as LoadedMatrix
from foundry.data.loaders.base import LoadedTable as LoadedTable
from foundry.data.loaders.base import Loader as Loader
from foundry.data.loaders.base import RawRow as RawRow
from foundry.data.loaders.base import classify_document as classify_document
from foundry.data.loaders.base import data_error as data_error

__all__: list[str] = [
    "LoadedMatrix",
    "LoadedTable",
    "Loader",
    "RawRow",
    "classify_document",
    "data_error",
]
