"""The data format readers, and the neutral document shapes they hand upward (AAP 0.4.8).

Each reader yields a table or a matrix; every fault is a ``DataError`` naming the file and line.
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
