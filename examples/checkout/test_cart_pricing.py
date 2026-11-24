"""The pricing rule, checked against every row of ``testdata/cart_pricing.csv``.

AAP 0.8.1 places this module in the example project as six data-driven cases read from a CSV, and
the CSV's sidecar ``testdata/cart_pricing.schema.json`` states the two claims each row makes: the
total its discount produces and the tier its quantity earns. The rows are read at collection time
through the engine the plugin itself uses, :func:`foundry.data.registry.load_cases`, with
``strict=True`` and the testdata directory as the schema root, so the sidecar is enforced on every
row before it becomes a case: a cell the schema stops accepting fails collection instead of passing
quietly (AAP 0.4.7).

Each test asserts the row's own ``expected_*`` values against the arithmetic those columns imply,
so no figure the data already carries is restated here. A CSV cell arrives as text, so every
numeric column is coerced with :func:`int` first.

Each test also spends one call of the example's deterministic work budget, because the suite's 12 s
of aggregate CPU work is what the sharding budget reasons about (AAP 0.14.2).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import _work
import pytest

from foundry.data.registry import load_cases

if TYPE_CHECKING:
    from foundry.data.cases import Case

_TESTDATA = Path(__file__).with_name("testdata")
"""The example's data directory, which the sidecar schema must stay inside."""

_BULK_QUANTITY = 4
"""The quantity a row earns the bulk tier at, as the sidecar schema states it."""

_CASES = load_cases(_TESTDATA / "cart_pricing.csv", strict=True, schema_dir=_TESTDATA).cases
"""One case per data row, read once at collection time, in file order."""


def _cell(case: Case, column: str) -> int:
    """Return one CSV cell of ``case`` as an integer, the cell itself being text."""
    return int(str(case.values[column]))


@pytest.mark.parametrize("case", _CASES, ids=[case.id for case in _CASES])
def test_cart_pricing_row(case: Case) -> None:
    """Assert the row's total and tier, the two claims its ``expected_*`` columns make."""
    quantity = _cell(case, "quantity")
    unit_price_cents = _cell(case, "unit_price_cents")
    discount_percent = _cell(case, "discount_percent")

    total_cents = (quantity * unit_price_cents * (100 - discount_percent)) // 100
    tier = "bulk" if quantity >= _BULK_QUANTITY else "standard"

    assert total_cents == _cell(case, "expected_total_cents")
    assert tier == case.values["expected_tier"]

    assert _work.spend() >= 0
