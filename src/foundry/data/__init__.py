"""The data-driven testing engine: the loaders, the case model and the stable case ids.

``foundry.data`` turns one data file -- CSV, JSON, YAML or JSONL -- or a matrix of named axes into
the :class:`Case` objects that parametrise a test, enforcing the file's JSON Schema sidecar and
reporting a defective row at collection with the file, the line and the column that produced it
(AAP 0.4.7). ``foundry.plugin`` calls :func:`parametrize` from the ``pytest_generate_tests``
delegation it owns, and the ``foundry_data`` fixture calls :func:`load_cases`, :func:`load_matrix`
and :func:`resolve_data_file`; both import this package lazily inside the hook or the fixture body,
which is what keeps the plugin inside its startup budget (AAP 0.14.2).

Two rules shape every id this package builds. A case id derives from the file's stem and the row's
position alone -- never from a worker id, a host, a wall clock or a directory listing -- so every
``pytest-xdist`` worker collects the same ids in the same order and the controller's collection
parity check cannot abort a sharded run (implicit requirement I6). A data file that parses to no
cases yields one parameter carrying a skip whose reason reads ``no cases in <file>`` rather than a
collection error, so a file kept for reference with its header alone leaves the session green
(AAP 0.4.7).

**Import rule** (AAP 0.4.8). The package imports :mod:`foundry.core`, :mod:`foundry.obs`,
:mod:`foundry.contract` and its own submodules, and nothing above them: no HTTP, cassette, database,
configuration, execution, reporting, CLI or plugin module. This barrel defines nothing of its own
-- it re-exports names and adds no import cost -- and the loaders reach ``yaml`` and ``jsonschema``
only inside the call that parses, so importing the package pulls neither. ``foundry.data.cases``
does import ``pytest`` at module scope, because it is the parametrisation adapter that builds
``pytest.param`` objects at runtime; that import only ever runs inside a pytest process, where the
host module is already loaded.
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
