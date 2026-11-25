"""The checkout API through the two HTTP fixtures: four tests, one session contract.

These are 4 of the suite's 24 collected tests (6 + 4 + 3 + 2 + 2 + 2 + 2 + 3), the count the
end-to-end test asserts. ``test_health_reports_ok`` covers the response surface, its ``source`` tag
and one query sent in two orders; ``test_create_order_returns_created`` a ``POST`` checked by the
fragment helper; ``test_get_order_roundtrip`` two requests; and
``test_private_profile_requires_the_credential`` both ``4xx`` answers *returned* -- no credential,
then a wrong one -- the ``200`` the configured strategy earns, plus the two fixtures' difference.

Every request is reproducible from this module alone -- fixed paths, one fixed body, no clock, no
generated identifier -- so the four tests pass live and replayed, and only the fixtures are used.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final

import _work
import pytest

from foundry import ConfigError
from foundry.assertions import assert_json_subset, assert_status

if TYPE_CHECKING:
    from foundry.fixtures.http import FoundrySession

_HEALTH_PATH: Final[str] = "/health"
"""The liveness route: no body, and one answer per set of query parameters, whatever their order."""

_ORDERS_PATH: Final[str] = "/orders"
"""The collection route a ``POST`` creates an order on and a ``GET`` reads one back from."""

_PROFILE_PATH: Final[str] = "/private/profile"
"""The credential-bearing route: ``401`` with none presented, ``403`` with a wrong one."""

_ITEM: Final[dict[str, object]] = {"sku": "SKU-1001", "quantity": 2, "unit_price_cents": 2500}
"""The one line item every order here is made of, so the reply is fully determined."""

_ORDER_BODY: Final[dict[str, object]] = {"currency": "EUR", "items": [_ITEM]}
"""The fixed create-order body: the same bytes on the recording run and on the replaying one."""

_ORDER_FRAGMENT: Final[dict[str, object]] = {
    "status": "created",
    "currency": "EUR",
    "items": [_ITEM],
}
"""The part of the created order this module insists on, leaving the server's own id out of it."""

_ORDER_ID: Final[re.Pattern[str]] = re.compile(r"ord-[0-9a-f]{12}")
"""The id shape the echo server derives from the canonical body's SHA-256, never a counter."""

# The four statuses asserted on: a healthy read, a created order and the two credential refusals.
_OK: Final[int] = 200
_CREATED: Final[int] = 201
_UNAUTHORIZED: Final[int] = 401
_FORBIDDEN: Final[int] = 403

_AUTHORIZATION: Final[str] = "Authorization"
"""The credential header: blanked, then given a wrong value, then left to the strategy."""
_WRONG_CREDENTIAL: Final[str] = "Bearer not-the-credential"
"""A presented credential the server must refuse: a scheme and a value the demo does not accept."""
_HEALTH_QUERY: Final[list[list[str]]] = [["page", "2"], ["sort", "asc"]]
"""The parameters the health requests carry, as the server echoes them back: sorted by name."""

_TOTAL_CENTS: Final[int] = 5000
"""``quantity * unit_price_cents`` over :data:`_ITEM`, which the server sums for the reply."""

_JSON_CONTENT_TYPE: Final[str] = "application/json"
"""The media type every route answers with, checked as a prefix because a charset may follow it."""

_SOURCES: Final[frozenset[str]] = frozenset({"live", "cassette"})
"""Where a reply may come from, so a test passes under a live run and a replayed one alike."""


def test_health_reports_ok(foundry_api: FoundrySession) -> None:
    """Read ``/health`` through the configured client and check the whole response surface.

    ``assert_status`` turns a status into an assertion failure with a redacted, bounded message, and
    ``source`` is checked against both values: the same test has to pass when the reply replays.
    The two extra requests carry one set of parameters in two different orders, the first also
    passing a per-request header: together they prove the encoding stage runs and that the answer
    does not depend on that order, the property the cassette matcher's query comparison relies on.
    """
    response = foundry_api.get(_HEALTH_PATH)
    assert_status(response, _OK)

    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["service"] == "checkout"
    assert response.headers["content-type"].startswith(_JSON_CONTENT_TYPE)
    assert response.elapsed_ms >= 0
    assert response.source in _SOURCES

    ordered = foundry_api.get(
        _HEALTH_PATH, params={"page": "2", "sort": "asc"}, headers={"X-Example-Trace": "health"}
    )
    reversed_ = foundry_api.get(_HEALTH_PATH, params=[("sort", "asc"), ("page", "2")])
    assert_status(ordered, _OK)
    assert_status(reversed_, _OK)
    assert ordered.json() == reversed_.json()
    assert ordered.json()["query"] == _HEALTH_QUERY

    assert _work.spend() >= 0


def test_create_order_returns_created(foundry_api: FoundrySession) -> None:
    """Create one order, then check the subset fragment, the id shape and the summed total.

    ``assert_json_subset`` asserts that the reply *contains* the fragment, so a field the server
    adds later cannot break this test, and a field it drops or changes fails at that JSON path.
    """
    response = foundry_api.post(_ORDERS_PATH, json=_ORDER_BODY)
    assert_status(response, _CREATED)

    order = response.json()
    assert_json_subset(order, _ORDER_FRAGMENT)
    assert isinstance(order["order_id"], str)
    assert _ORDER_ID.fullmatch(order["order_id"]) is not None
    assert order["total_cents"] == _TOTAL_CENTS

    assert _work.spend() >= 0


def test_get_order_roundtrip(foundry_api: FoundrySession) -> None:
    """Create an order, then read it back by the id the server returned.

    Two requests in one test, which is the point: the test's cassette holds two interactions, so
    record and replay exercise the ordered-interaction model rather than a one-entry document.
    """
    created = foundry_api.post(_ORDERS_PATH, json=_ORDER_BODY)
    assert_status(created, _CREATED)
    order_id = created.json()["order_id"]
    assert isinstance(order_id, str)

    fetched = foundry_api.get(f"{_ORDERS_PATH}/{order_id}")
    assert_status(fetched, _OK)

    stored = fetched.json()
    assert stored["order_id"] == order_id
    assert stored["status"] == "created"

    assert _work.spend() >= 0


def test_private_profile_requires_the_credential(
    foundry_api: FoundrySession,
    foundry_http: FoundrySession,
    checkout_base_url: str,
) -> None:
    """Check both ``4xx`` answers to the one request, then the ``200`` the strategy earns.

    A blank per-request ``Authorization`` replaces the header the configured strategy applied, so
    the server sees no credential and answers ``401``; a wrong value in the same place is a
    credential presented and refused, so it answers ``403`` -- two answers the route could not tell
    apart while it answered ``401`` unconditionally. The third request overrides nothing, so the
    token the strategy resolved at session start is what the server accepts: that ``200`` is the
    proof the auth stage of the header merge ran. Every reply arrives as a value, ``4xx`` included.
    """
    blank = foundry_api.get(_PROFILE_PATH, headers={_AUTHORIZATION: ""})
    assert_status(blank, _UNAUTHORIZED)
    assert blank.json()["error"] == "unauthorized"

    wrong = foundry_api.get(_PROFILE_PATH, headers={_AUTHORIZATION: _WRONG_CREDENTIAL})
    assert_status(wrong, _FORBIDDEN)
    assert wrong.json()["error"] == "forbidden"

    accepted = foundry_api.get(_PROFILE_PATH)
    assert_status(accepted, _OK)
    profile = accepted.json()
    assert profile["realm"] == "checkout"
    assert profile["user"] == "checkout-demo"

    assert foundry_api.base_url == checkout_base_url
    assert foundry_http.base_url is None
    with pytest.raises(ConfigError):
        foundry_http.get(_PROFILE_PATH)

    assert _work.spend() >= 0
