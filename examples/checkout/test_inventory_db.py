"""The ``foundry_db`` contract against a real sqlite schema: rows are visible inside the
transaction, and nothing survives the rollback that ends it.

AAP 0.8.1 places this module in the example project as two sqlite transaction tests, and those two
are 2 of the suite's 24 collected tests (6 + 4 + 3 + 2 + 2 + 2 + 2 + 3) -- the count
``tests/e2e/test_example_project.py`` asserts and the set the sharding and determinism tests compare
across. What is proved here is the fixture's own promise (AAP 0.4.3): ``foundry_db`` yields a plain
:class:`sqlalchemy.orm.Session` over a fresh in-memory sqlite database holding the schema of the
models the test registered, with its outer transaction already open. A row a test adds and flushes
is found by that test's own ``select``; a ``rollback`` discards it; and because the database belongs
to one test, the next test starts empty (AAP 0.4.8, "Fixture lifecycle rule"). A real database
stands behind every assertion -- no mock -- which is what AAP 0.3.1's in-memory sqlite buys.

**How a test registers its model.** No table is created unless the test asks for one, and there is
no marker or configuration key to ask with, so pytest's indirect parametrisation delivers the model
below as ``request.param``, the path ``src/foundry/fixtures/db.py`` documents. Nothing is registered
at import: ``foundry.db.schema`` takes models as values and keeps no registry, so importing this
module creates no table, no engine and no connection.

No socket, no cassette and no file is touched, so the example's loopback guard stays at zero.
Nothing here reads a clock or draws a random number -- a "created at" column is deliberately absent,
because determinism forbids the clock in this tree (AAP 0.14.3) -- and each test spends one call of
the example's deterministic work budget (AAP 0.14.2).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import _work
import pytest
from sqlalchemy import select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

_Rows = tuple[str, int, int]
"""One inventory row as the assertions read it back: ``(sku, quantity, price_cents)``."""

_INSERTED_ROWS: Final[tuple[_Rows, ...]] = (
    ("SKU-2002", 5, 2000),
    ("SKU-3003", 1, 1250),
    ("SKU-1001", 3, 2500),
)
"""The three rows the first test inserts, in an order deliberately not sku order, so the select's
``order_by`` clause has work to do rather than the database being free to answer in rowid order.
"""

_EXPECTED_ROWS: Final[tuple[_Rows, ...]] = (
    ("SKU-1001", 3, 2500),
    ("SKU-2002", 5, 2000),
    ("SKU-3003", 1, 1250),
)
"""The same three rows in sku order, which is what a select ordered by ``sku`` must return."""

_DISCARDED_ROWS: Final[tuple[_Rows, ...]] = (
    ("SKU-4004", 2, 900),
    ("SKU-5005", 7, 1500),
)
"""The two rows the rollback test adds and then discards."""


class InventoryBase(DeclarativeBase):
    """This module's declarative base, so the schema is built from the test's own models."""


class InventoryItem(InventoryBase):
    """One stocked SKU: a unique code, a quantity on hand and a unit price in cents."""

    __tablename__ = "inventory_item"

    id: Mapped[int] = mapped_column(primary_key=True)
    sku: Mapped[str] = mapped_column(unique=True)
    quantity: Mapped[int]
    price_cents: Mapped[int]


def _insert(session: Session, rows: tuple[_Rows, ...]) -> None:
    """Add one :class:`InventoryItem` per row and flush, so the session's own select finds them.

    ``flush`` rather than ``commit``: the promise under test is what is visible inside the
    transaction the fixture opened, so nothing has to be committed for the assertion to hold.
    """
    for sku, quantity, price_cents in rows:
        session.add(InventoryItem(sku=sku, quantity=quantity, price_cents=price_cents))
    session.flush()


def _stored(session: Session) -> list[_Rows]:
    """Return ``(sku, quantity, price_cents)`` for every stored row, in sku order.

    The select orders by ``sku`` rather than trusting the database's own order, and the tuples carry
    no ``id``: the primary key is an autoincrement column whose value says nothing about the
    fixture's contract (AAP 0.4.3).
    """
    items = session.execute(select(InventoryItem).order_by(InventoryItem.sku)).scalars()
    return [(item.sku, item.quantity, item.price_cents) for item in items]


@pytest.mark.parametrize("foundry_db", [InventoryItem], indirect=True)
def test_rows_are_visible_inside_the_transaction(foundry_db: Session) -> None:
    """A test's own inserts are readable by its own select, in sku order, without a commit."""
    assert _stored(foundry_db) == [], "a fresh in-memory database starts empty (AAP 0.4.3)"

    _insert(foundry_db, _INSERTED_ROWS)

    assert _stored(foundry_db) == list(_EXPECTED_ROWS)

    assert _work.spend() >= 0


@pytest.mark.parametrize("foundry_db", [InventoryItem], indirect=True)
def test_rollback_discards_uncommitted_rows(foundry_db: Session) -> None:
    """A rollback discards the test's rows, and the teardown keeps them out of the next test.

    Contract test, not an accident (AAP 0.4.3, AAP 0.4.8): ``foundry_db`` promises nothing a test
    writes is committed, so the fixture's teardown rollback is what stops one test inheriting
    another's data -- and the empty database this test opens on is the observable proof of it.
    """
    assert _stored(foundry_db) == [], "no other test's rows are visible here (AAP 0.4.8)"

    _insert(foundry_db, _DISCARDED_ROWS)
    assert _stored(foundry_db) == list(_DISCARDED_ROWS)

    foundry_db.rollback()

    assert _stored(foundry_db) == [], "a rollback leaves no uncommitted row behind (AAP 0.4.3)"

    assert _work.spend() >= 0
