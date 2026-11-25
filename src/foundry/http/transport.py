"""The one socket-capable transport Foundry builds, and the one live-or-recorded choice.

Redirects are never followed, because a request is issued by calling
:meth:`httpx.BaseTransport.handle_request` and never through a client, and :func:`close_quietly`
reports a transport that fails to close as exactly one ``WARNING`` (AAP 0.1.2, AAP 0.14.3).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Final

import httpx

from foundry.core.exceptions import FoundryError
from foundry.obs.events import emit_plumbing_failure

if TYPE_CHECKING:
    from foundry.cassette.modes import CassetteSession

__all__: Final[tuple[str, ...]] = (
    "DEFAULT_VERIFY",
    "LiveTransport",
    "NETWORK_RETRIES",
    "build_live_transport",
    "close_quietly",
    "select_transport",
    "timeout_extension",
)

DEFAULT_VERIFY: Final[bool] = True
"""Whether the live transport verifies the peer's certificate.

A caller that must relax it passes ``verify=False`` to :func:`build_live_transport`; the choice is
a keyword argument rather than a configuration key, which would need ``foundry.config`` (AAP 0.4.8).
"""

NETWORK_RETRIES: Final[int] = 0
"""How often ``httpcore`` may retry a connection on its own: never.

:mod:`foundry.http.retries` owns the policy, so a transport-level retry would hide an attempt from
it and make ``attempts`` in ``results.json`` describe something else (AAP 0.3.1).
"""

_COMPONENT: Final[str] = "http.transport"
"""The component name :func:`close_quietly` reports a fault under, as a sibling module names itself
in its own plumbing-failure events."""

_CLOSE_ERRORS: Final[tuple[type[BaseException], ...]] = (FoundryError, OSError)
"""The faults a transport's ``close`` raises: a Foundry error or a socket-layer ``OSError``.

Nothing else is caught: a ``close`` raising outside this set is a defect that must stay visible.
"""


def timeout_extension(seconds: float) -> dict[str, float]:
    """Return the ``timeout`` request extension that gives a bare request a deadline.

    Mandatory on every request the HTTP layer builds:
    :meth:`httpx.BaseTransport.handle_request` hands :attr:`httpx.Request.extensions` straight to
    ``httpcore``, which reads exactly the four sub-keys :meth:`httpx.Timeout.as_dict` returns, and
    a request carrying no ``timeout`` extension gets no deadline at all -- an unresponsive peer
    would hold the call open indefinitely, which is a hang rather than a failure pytest reports.

    Returns:
        ``{"connect": seconds, "read": seconds, "write": seconds, "pool": seconds}``, the mapping
        ``httpx`` expects under ``request.extensions["timeout"]``.
    """
    values = httpx.Timeout(seconds).as_dict()
    # A single-argument Timeout sets all four phases to the same value, so nothing is dropped here;
    # the guard narrows the ``float | None`` that ``as_dict`` is declared to return.
    return {key: float(value) for key, value in values.items() if value is not None}


class LiveTransport(httpx.BaseTransport):
    """The one transport Foundry builds that can open a socket, and nothing beyond that.

    A named class rather than a bare ``HTTPTransport``, for three reasons: it is the single
    greppable construction site the offline guarantee is audited at, it is the seam a test
    subclasses to prove a replay never reaches the network, and it sets no cassette source tag, so a
    reply it returns reports ``source == "live"`` (AAP 0.4.3).
    """

    _transport: httpx.HTTPTransport
    """The wrapped ``httpx`` transport; the only object in the package that reaches ``httpcore``."""

    def __init__(self, *, verify: bool = DEFAULT_VERIFY) -> None:
        """Build the wrapped transport with network retries disabled.

        Args:
            verify: Whether the peer's certificate is verified; :data:`DEFAULT_VERIFY` unless
                relaxed.
        """
        self._transport = httpx.HTTPTransport(retries=NETWORK_RETRIES, verify=verify)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        """Send ``request`` to the peer and return what came back, whatever its status.

        Args:
            request: The request the HTTP layer built. It must carry the ``timeout`` extension
                :func:`timeout_extension` returns, or the call has no deadline.

        Returns:
            The peer's reply for every status code, redirects included: a ``3xx`` is returned to the
            caller rather than followed.

        Raises:
            httpx.HTTPError: Whatever ``httpcore`` raised -- a timeout, a connection fault, a
                protocol error -- unchanged, so :mod:`foundry.http.retries` can classify it.
        """
        return self._transport.handle_request(request)

    def close(self) -> None:
        """Close the wrapped transport and the connection pool behind it.

        Teardown reaches this through :func:`close_quietly`, so a fault while closing is reported as
        one ``WARNING`` and never becomes a test result (AAP 0.1.2, AAP 0.14.3).
        """
        self._transport.close()


def build_live_transport(*, verify: bool = DEFAULT_VERIFY) -> httpx.BaseTransport:
    """Return a fresh live transport for a session that may reach a peer.

    The annotation is :class:`httpx.BaseTransport` on purpose: this is what
    :func:`select_transport` answers with, so a caller must not depend on the concrete
    :class:`LiveTransport`. At most one per session, and it is the object that session sends on,
    so its ``close`` releases it through :func:`close_quietly`.

    Returns:
        A :class:`LiveTransport` wrapping one :class:`httpx.HTTPTransport`, which opens no socket
        until a request is sent on it.
    """
    return LiveTransport(verify=verify)


def select_transport(
    live: Callable[[], httpx.BaseTransport], cassette: CassetteSession | None = None
) -> httpx.BaseTransport:
    """Return the transport a request is sent on: the live one, or the cassette's.

    Nothing here looks at the mode, so the mode table, the capture sink and the save at teardown
    stay in :mod:`foundry.cassette.modes`. ``live`` is a factory because a session that sends on a
    cassette never builds a transport that can reach a peer.
    """
    if cassette is None:
        return live()
    return cassette.transport()


def close_quietly(transport: httpx.BaseTransport | None) -> None:
    """Close ``transport``, reporting a fault as one ``WARNING`` instead of raising it.

    The teardown path for every transport Foundry builds: a socket that lingers or a pool that
    refuses to shut down is a fault in Foundry's own plumbing, so it is reported on the ``foundry``
    logger and the run keeps its outcomes and its exit code (AAP 0.1.2, AAP 0.14.3). The message
    names the transport's class and nothing else, since a fault's own text can quote a URL or a
    header.
    """
    if transport is None:
        return
    try:
        transport.close()
    except _CLOSE_ERRORS as exc:
        emit_plumbing_failure(
            _COMPONENT, f"could not close the {type(transport).__name__}", exc=exc
        )
