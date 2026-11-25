"""The pricing rule, checked against every row of ``testdata/cart_pricing.csv``.

AAP 0.8.1 places this module in the example project as six data-driven cases read from
``testdata/cart_pricing.csv``, and those six are 6 of the suite's 24 collected tests
(6 + 4 + 3 + 2 + 2 + 2 + 2 + 3) -- the count ``tests/e2e/test_example_project.py`` asserts and the
set the sharding and determinism tests compare across.

Each row recomputes the pricing rule from its own input columns and checks the two claims its
``expected_*`` columns make::

    total = (quantity * unit_price_cents * (100 - discount_percent)) // 100
    tier  = "bulk" when quantity >= 4 else "standard"

The CSV's sidecar ``testdata/cart_pricing.schema.json`` states the range of every input column, and
the rows are read at collection time through the engine the plugin itself uses,
:func:`foundry.data.registry.load_cases`, with ``strict=True`` and the testdata directory as the
schema root, so the sidecar is enforced on every row before it becomes a case: a cell the schema
stops accepting fails collection instead of passing quietly (AAP 0.4.7).

The parameter list is built here, at import, rather than through a collection hook: the
``foundry_data`` fixture is function scoped, and the plugin's ``pytest_generate_tests`` carries no
data-file argument and registers no data marker, so this module is the place that knows which file
it reads. Building it once is also what keeps the six ids identical on every worker.

A CSV cell arrives as text, so each numeric column is asserted to be text and coerced with
:func:`int` exactly once, which makes a parse defect visible rather than silent. A case whose
``source_file``, ``row_index`` or ``line`` is not the one the file describes fails too, so the
``Case`` metadata contract is exercised rather than assumed.

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

_CSV_NAME = "cart_pricing.csv"
"""The data file every case of this module comes from."""

_BULK_QUANTITY = 4
"""The quantity a row earns the bulk tier at, as the sidecar schema states it."""

_FIRST_DATA_ROW_LINE = 2
"""The physical line the first data row starts on, the header occupying line 1."""

_CASE_ID_PREFIX = Path(_CSV_NAME).stem
"""The file stem every case id starts with, the row position completing it (AAP 0.7.2)."""

_CASES = load_cases(_TESTDATA / _CSV_NAME, strict=True, schema_dir=_TESTDATA).cases
"""One case per data row, read once at collection time, in file order."""


def _cell(case: Case, column: str) -> int:
    """Return one CSV cell of ``case`` as an integer, the cell itself being text."""
    raw = case.values[column]
    assert isinstance(raw, str), f"{column} of {case.id} is not CSV text: {type(raw).__name__}"
    return int(raw)


@pytest.mark.parametrize("case", _CASES, ids=[case.id for case in _CASES])
def test_cart_pricing_row(case: Case) -> None:
    """Check one CSV row: its metadata, its recomputed total and its recomputed tier.

    The id is asserted rather than left to the parametrisation: it derives from the file's stem and
    the row's position and from nothing else, which is what lets a serial run and a sharded run
    agree on the collection (implicit requirement I6), so a drift in the id format shows up here.
    """
    assert case.source_file.name == _CSV_NAME
    assert case.row_index is not None
    assert 0 <= case.row_index < len(_CASES)
    assert case.line == case.row_index + _FIRST_DATA_ROW_LINE
    assert case.id == f"{_CASE_ID_PREFIX}-{case.row_index}"

    quantity = _cell(case, "quantity")
    unit_price_cents = _cell(case, "unit_price_cents")
    discount_percent = _cell(case, "discount_percent")

    total_cents = (quantity * unit_price_cents * (100 - discount_percent)) // 100
    tier = "bulk" if quantity >= _BULK_QUANTITY else "standard"

    assert total_cents == _cell(case, "expected_total_cents")
    assert tier == case.values["expected_tier"]

    assert _work.spend() >= 0
