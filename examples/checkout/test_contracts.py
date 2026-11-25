"""The order contract in three cases: one matrix axis, three helpers, one response body.

AAP 0.8.1 places this module in the example project as three contract and subset assertions, and
these are 3 of the suite's 24 collected tests (6 + 4 + 3 + 2 + 2 + 2 + 2 + 3) -- the count the
end-to-end test asserts and the set the sharding and determinism tests compare across.

**Three cases rather than one test per helper.** The brief names three helpers
(``assert_status``, ``assert_json_subset``, ``assert_json_schema``) and exactly three collected
tests, so the count is met without giving each helper an item of its own: the single currency axis
of ``testdata/checkout_matrix.yaml`` drives the three cases, and every case runs all three helpers.
That also exercises the YAML matrix loader from a real entry point -- the cross-product, the axes
sorted by name and the stable case ids -- which no other example module covers.

Each case posts one order whose body is a function of the case's own currency value and of nothing
else: no clock, no generated identifier, no counter. A cassette is matched later on method, path,
order-insensitive query and body hash with the host left out (assumption A7), so the same three
cases pass against the live echo server ``conftest.py`` starts and against ``cassettes/`` with no
server running.

Per case, in order: ``assert_status`` for the created status, carrying a redacted body excerpt when
it fails; ``assert_json_subset`` for the fields the order rule fixes; ``assert_json_schema`` against
``testdata/order.schema.json``, which is where a field the schema forbids or a value it rejects
surfaces as a ``ContractError`` carrying the offending JSON path. The same body then proves the
subset helper's failure contract at its own JSON path, so the message shape is observed rather than
assumed, without a fourth collected test.

Each test also spends one call of the example's deterministic work budget (AAP 0.14.2).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Final

import _work
import pytest

from foundry.assertions import assert_json_schema, assert_json_subset, assert_status
from foundry.data.registry import load_matrix

if TYPE_CHECKING:
    from foundry.data.cases import Case
    from foundry.fixtures.http import FoundrySession

_TESTDATA: Final[Path] = Path(__file__).with_name("testdata")
"""This example's data directory: the matrix is read from it and the schema lives beside it."""

_MATRIX: Final[Path] = _TESTDATA / "checkout_matrix.yaml"
"""The matrix whose one currency axis expands to the three cases of this module."""

_CASES: Final[tuple[Case, ...]] = load_matrix(_MATRIX).cases
"""One case per currency, read at import so a serial run and a worker collect the same three."""

_ORDER_SCHEMA: Final[Path] = _TESTDATA / "order.schema.json"
"""The schema every ``POST /orders`` response body is validated against."""

_ORDERS_PATH: Final[str] = "/orders"
"""The create-order route: one fixed path, so a recording replays against any host (A7)."""

_CREATED: Final[int] = 201
"""The only status a successfully created order is answered with."""

_ITEM: Final[dict[str, object]] = {"sku": "SKU-1001", "quantity": 1, "unit_price_cents": 2500}
"""The one line item every order here is made of, so the reply is fully determined."""

_TOTAL_CENTS: Final[int] = 2500
"""``quantity * unit_price_cents`` over that line: what the server sums into ``total_cents``."""


def _order_body(currency: str) -> dict[str, object]:
    """Return the create-order body for one case: the case's currency and the fixed line item."""
    return {"currency": currency, "items": [_ITEM]}


@pytest.mark.parametrize("case", _CASES, ids=[case.id for case in _CASES])
def test_checkout_contract(foundry_api: FoundrySession, case: Case) -> None:
    """Create one order per currency, then check its status, its fields and its schema.

    The id is asserted rather than left to the parametrisation: it derives from the matrix file's
    stem and the case's own axis value and from nothing else, which is what lets a serial run and a
    sharded run agree on the collection (implicit requirement I6), so a drift in the format shows up
    here rather than as a cassette that no longer matches.
    """
    currency = str(case.values["currency"])
    assert case.source_file == _MATRIX
    assert case.id == f'checkout_matrix-currency="{currency}"'

    response = foundry_api.post(_ORDERS_PATH, json=_order_body(currency))
    assert_status(response, _CREATED)

    order = response.json()
    assert_json_subset(
        order, {"status": "created", "currency": currency, "total_cents": _TOTAL_CENTS}
    )
    assert_json_schema(order, _ORDER_SCHEMA)

    # The helpers' failure contract, observed rather than assumed: a fragment the reply does not
    # contain is refused at its own JSON path, so a contract failure names the field it came from.
    with pytest.raises(AssertionError) as excinfo:
        assert_json_subset(order, {"status": "cancelled"}, path="$")
    assert "$.status" in str(excinfo.value)

    assert _work.spend() >= 0
