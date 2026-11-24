"""Deterministic JSON Schema validation of HTTP response bodies and data files.

A contract failure is reported as the single first violation together with its JSON path, so a
test fails on one locatable defect rather than on a list whose order the caller cannot rely on.
That determinism is why this package exists separately: :mod:`jsonschema` documents no ordering
for :meth:`~jsonschema.protocols.Validator.iter_errors` and the relevance ranking it offers is an
explicit heuristic, so "the first violation" is a rule Foundry fixes itself -- shallowest JSON
path first, then the path structurally, then the message -- in :mod:`foundry.contract.violations`,
and the same instance and schema always report the same violation.

Two safety properties come with every name published here. YAML schema files are reachable only
through ``yaml.safe_load``, and a ``$ref`` that is not a fragment of the same document is refused
before a validator sees it, so no validation can retrieve a document over the network. Both hold
on every path a caller takes, because loading belongs to :mod:`foundry.contract.schema` and
validation to :mod:`foundry.contract.validate`, and neither delegates either rule anywhere else.

The package imports :mod:`foundry.core` -- and :mod:`foundry.obs.redaction` through the single lazy
import in :mod:`foundry.contract.violations` -- and nothing above it. That obs edge is the
package's one sanctioned exception to the permitted-import column of AAP 0.4.8, which lists
``core`` alone: the capability row of AAP 0.3.2 lets a capability package import ``obs`` as well,
and AAP 0.14.3 makes :mod:`foundry.obs.redaction` the only redaction engine every sink calls.
Importing this module therefore defines names only: no file is read, no environment variable is
consulted, no logger is configured and nothing heavy (``jsonschema``, ``yaml``, ``pydantic``) is
pulled in by the import itself.

Only the ten names in :data:`__all__` form the public surface. The ordering and rendering
internals of :mod:`foundry.contract.violations` -- ``choose_first``, ``sort_violations``,
``order_key``, ``violation_from_error``, ``to_contract_error``, ``json_path_from_parts`` and
``format_schema_path`` -- and the schema and validator internals -- ``check_schema``,
``select_validator`` from :mod:`foundry.contract.validate`, and ``declared_draft``,
``resolve_schema_path``, ``SUPPORTED_SCHEMA_SUFFIXES`` from :mod:`foundry.contract.schema` -- are
deliberately not re-exported: they stay reachable from the module that owns them, which keeps this
surface small enough to hold stable while the rules behind it change freely.
"""

from foundry.contract.schema import SchemaSource, load_schema
from foundry.contract.validate import (
    find_violation,
    first_violation,
    iter_violations,
    validate_instance,
)
from foundry.contract.violations import Violation, excerpt, render_violation
from foundry.core.exceptions import ContractError

__all__ = (
    "ContractError",
    "SchemaSource",
    "Violation",
    "excerpt",
    "find_violation",
    "first_violation",
    "iter_violations",
    "load_schema",
    "render_violation",
    "validate_instance",
)
