"""Deterministic contract validation of HTTP response bodies and data files (AAP 0.4.3, AAP 0.4.7).

One ordered first violation; no schema load reaches the network (AAP 0.14.3).
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
