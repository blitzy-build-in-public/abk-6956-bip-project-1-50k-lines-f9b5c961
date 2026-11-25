"""The checkout API through the two HTTP fixtures: four tests, one session contract.

These are 4 of the suite's 24 collected tests (6 + 4 + 3 + 2 + 2 + 2 + 2 + 3), the count the
end-to-end test asserts and the set the sharding and determinism tests compare across. They run
against the loopback echo server ``conftest.py`` starts on an ephemeral ``127.0.0.1`` port.

Per test: ``test_health_reports_ok`` the response surface and its ``source`` tag;
``test_create_order_returns_created`` a ``POST`` with a JSON body checked by the fragment helper;
``test_get_order_roundtrip`` two requests, so its cassette holds two interactions;
``test_unauthenticated_session_returns_401`` a ``4xx`` *returned* rather than raised, plus the
fixture difference -- ``foundry_api`` resolves relative paths, ``foundry_http`` holds no base URL.

Every request is reproducible from this module alone -- fixed paths, one fixed body, no clock, no
generated identifier, no counter -- because a recording is matched later on method, path,
order-insensitive query and body hash, with the host left out (assumption A7), so the same four
tests pass against a live server and against ``cassettes/`` with no server running. Only the
fixtures are used, never ``httpx``, so what is proved is the published fixture surface.
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
"""The liveness route: no body, no query, identical on every call."""

_ORDERS_PATH: Final[str] = "/orders"
"""The collection route a ``POST`` creates an order on and a ``GET`` reads one back from."""

_PROFILE_PATH: Final[str] = "/private/profile"
"""The example's only unauthenticated surface, answered with ``401``."""

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

# The three statuses asserted on: a healthy read, a created order and the unauthenticated route.
_OK: Final[int] = 200
_CREATED: Final[int] = 201
_UNAUTHORIZED: Final[int] = 401

_TOTAL_CENTS: Final[int] = 5000
"""``quantity * unit_price_cents`` over :data:`_ITEM`, which the server sums for the reply."""

_JSON_CONTENT_TYPE: Final[str] = "application/json"
"""The media type every route answers with, checked as a prefix because a charset may follow it."""

_SOURCES: Final[frozenset[str]] = frozenset({"live", "cassette"})
"""Where a reply may come from, so a test passes under a live run and a replayed one alike."""


def test_health_reports_ok(foundry_api: FoundrySession) -> None:
    """Read ``/health`` through the configured client and check the whole response surface.

    ``assert_status`` turns a status into an assertion failure with a redacted, bounded message, so
    the raw status is never compared inline. ``source`` is checked against both values rather than
    ``"live"`` alone: the same test has to pass when the reply is replayed from a cassette.
    """
    response = foundry_api.get(_HEALTH_PATH)
    assert_status(response, _OK)

    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["service"] == "checkout"
    assert response.headers["content-type"].startswith(_JSON_CONTENT_TYPE)
    assert response.elapsed_ms >= 0
    assert response.source in _SOURCES

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


def test_unauthenticated_session_returns_401(
    foundry_api: FoundrySession,
    foundry_http: FoundrySession,
    checkout_base_url: str,
) -> None:
    """Check that a ``401`` arrives as a value, and that the two fixtures differ where they claim.

    The example configures no auth strategy, so ``foundry_api`` sends no credential and the server
    answers ``/private/profile`` with ``401``. That reply is *returned*: every status is, ``4xx``
    included, and only a transport fault raises. The request travels through the cassette-backed
    fixture, which records this interaction and replays it with no server running; ``foundry_http``
    sends live in every run and keeps no cassette. The difference is pinned behaviourally instead:
    ``foundry_http`` holds no base URL, and a relative target is refused with ``ConfigError``
    naming ``http.base_url`` before any socket opens, which is safe under replay too.
    """
    response = foundry_api.get(_PROFILE_PATH)
    assert_status(response, _UNAUTHORIZED)

    body = response.json()
    assert body["error"] == "unauthorized"
    assert response.source in _SOURCES

    assert foundry_api.base_url == checkout_base_url
    assert foundry_http.base_url is None
    with pytest.raises(ConfigError):
        foundry_http.get(_PROFILE_PATH)

    assert _work.spend() >= 0
