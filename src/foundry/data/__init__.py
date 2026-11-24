"""The data-driven engine: the loaders, the case model and the stable case ids (AAP 0.4.7).

Ids derive from a stem and a row position alone, so serial and sharded collection agree (AAP 0.7.2).
"""

# One statement per re-export: the ``X as X`` alias is the explicit re-export idiom mypy --strict
# requires, and with this isort configuration a single parenthesised block of aliased names is
# reported as unsorted (I001), so each name keeps its own statement.
from foundry.data.cases import Case as Case
from foundry.data.cases import CaseSet as CaseSet
from foundry.data.cases import build_case_set as build_case_set
from foundry.data.cases import build_parameters as build_parameters
from foundry.data.cases import parametrize as parametrize
from foundry.data.ids import case_stem as case_stem
from foundry.data.ids import matrix_case_id as matrix_case_id
from foundry.data.ids import tabular_case_id as tabular_case_id
from foundry.data.loaders.base import LoadedMatrix as LoadedMatrix
from foundry.data.loaders.base import LoadedTable as LoadedTable
from foundry.data.loaders.base import Loader as Loader
from foundry.data.loaders.base import RawRow as RawRow
from foundry.data.matrix import Matrix as Matrix
from foundry.data.matrix import expand_axes as expand_axes
from foundry.data.matrix import expand_matrix as expand_matrix
from foundry.data.registry import SUPPORTED_EXTENSIONS as SUPPORTED_EXTENSIONS
from foundry.data.registry import load_cases as load_cases
from foundry.data.registry import load_document as load_document
from foundry.data.registry import load_matrix as load_matrix
from foundry.data.registry import loader_for as loader_for
from foundry.data.registry import register as register
from foundry.data.registry import resolve_data_file as resolve_data_file
from foundry.data.registry import supported_extensions as supported_extensions
from foundry.data.schema import SCHEMA_SUFFIX as SCHEMA_SUFFIX
from foundry.data.schema import enforce_row as enforce_row
from foundry.data.schema import enforce_table as enforce_table
from foundry.data.schema import load_row_schema as load_row_schema
from foundry.data.schema import schema_path_for as schema_path_for

__all__: list[str] = [
    "Case",
    "CaseSet",
    "LoadedMatrix",
    "LoadedTable",
    "Loader",
    "Matrix",
    "RawRow",
    "SCHEMA_SUFFIX",
    "SUPPORTED_EXTENSIONS",
    "build_case_set",
    "build_parameters",
    "case_stem",
    "enforce_row",
    "enforce_table",
    "expand_axes",
    "expand_matrix",
    "load_cases",
    "load_document",
    "load_matrix",
    "load_row_schema",
    "loader_for",
    "matrix_case_id",
    "parametrize",
    "register",
    "resolve_data_file",
    "schema_path_for",
    "supported_extensions",
    "tabular_case_id",
]
