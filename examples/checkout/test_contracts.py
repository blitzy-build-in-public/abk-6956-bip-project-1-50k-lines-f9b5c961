"""The checkout contract: cart rows against their schema, and a summary priced per currency.

AAP 0.8.1 places this module in the example project as three contract and subset assertions, one
per case of ``testdata/checkout_matrix.yaml``. The matrix is read at collection time through
:func:`foundry.data.registry.load_matrix`, so its single currency axis expands to the three cases
``checkout_matrix-currency="EUR"``, ``"GBP"`` and ``"USD"``, whose ids come from the file's own
stem (AAP 0.7.2).

Each test reads ``testdata/cart_pricing.csv`` through the public ``foundry_data`` fixture, which
resolves that bare name against ``data.dir`` only if the conftest's override took effect, and then
makes the two assertion kinds the data exists for: every row must satisfy
``testdata/cart_pricing.schema.json`` (:func:`foundry.assertions.assert_json_schema`, whose string
forms accept a CSV cell), and every priced line must contain the tier its quantity earns
(:func:`foundry.assertions.assert_json_subset`, a fragment of the line rather than an equality, so
the row's own ``expected_tier`` cell is what the rule is compared against and a tier the data states
wrongly is reported at its JSON path).

Each test also spends one call of the example's deterministic work budget (AAP 0.14.2).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import _work
import pytest

from foundry.assertions import assert_json_schema, assert_json_subset
from foundry.data.registry import load_matrix

if TYPE_CHECKING:
    from foundry.data.cases import Case
    from foundry.fixtures.data import FoundryDataLoader

_TESTDATA = Path(__file__).with_name("testdata")
"""The example's data directory, which every name here resolves against."""

_ROW_SCHEMA = _TESTDATA / "cart_pricing.schema.json"
"""The sidecar document every row of the pricing CSV is checked against."""

_BULK_QUANTITY = 4
"""The quantity a row earns the bulk tier at, as the sidecar schema describes that column."""

_CASES = load_matrix(_TESTDATA / "checkout_matrix.yaml").cases
"""One case per currency the matrix declares, read once at collection time."""


@pytest.mark.parametrize("case", _CASES, ids=[case.id for case in _CASES])
def test_checkout_contract(foundry_data: FoundryDataLoader, case: Case) -> None:
    """Check every cart row against its schema, and every priced line against the tier rule."""
    currency = str(case.values["currency"])
    rows = foundry_data.cases("cart_pricing.csv")
    assert rows, "cart_pricing.csv must carry the rows this contract is checked on"

    for row in rows:
        # The row is copied into a plain dict: a case holds a read-only mapping, and the schema's
        # "object" type is a dict check, so a mapping proxy would fail the row on its container
        # rather than on its values.
        line = dict(row.values)
        assert_json_schema(line, _ROW_SCHEMA)

        priced = {
            "currency": currency,
            "sku": line["sku"],
            "total_cents": int(str(line["expected_total_cents"])),
            "tier": line["expected_tier"],
        }
        # The document carries the tier the row claims and the expectation is the rule itself, so
        # a tier cell contradicting "bulk when quantity >= 4" fails here at its JSON path.
        quantity = int(str(line["quantity"]))
        expected = {"tier": "bulk" if quantity >= _BULK_QUANTITY else "standard"}
        assert_json_subset(priced, expected, path="$.cart")

    assert _work.spend() >= 0
