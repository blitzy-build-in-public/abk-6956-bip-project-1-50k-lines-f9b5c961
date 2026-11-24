"""The one socket-capable transport Foundry builds, and the one live-or-recorded choice.

Every request :mod:`foundry.http` issues goes through an :class:`httpx.BaseTransport`, and this
module holds the two things that decision needs: the construction site of the transport that can
reach a peer, and the selection between it and the transport a cassette session hands out. Four
statements of the plan put it in scope.

* AAP 0.7.1 component table names it for "the live ``httpx.BaseTransport`` implementation and the
  selection between it and ``foundry.cassette.transport.ReplayTransport``".
* AAP 0.3.3 integration points pairs it with :mod:`foundry.cassette.transport`: "both are
  ``httpx.BaseTransport`` implementations, so replay never reaches ``httpcore``". That seam is what
  makes the zero-socket replay of acceptance criterion 8 provable rather than believed.
* AAP 0.3.1 technology selection picks ``httpx`` because "a custom ``httpx.BaseTransport`` is the
  only clean seam that makes replay provably socket-free"; redirects are never followed and the
  timeout comes from configuration.
* AAP 0.2.1 socket-guard research with the AAP 0.14.3 offline constraint means the example project's
  guard -- which replaces ``socket.socket.connect`` and ``connect_ex`` and allows only ``127.0.0.1``
  and ``::1`` -- sees every non-loopback attempt Foundry could ever make. Concentrating transport
  construction in one class is what gives that observation its meaning: one greppable site to
  audit instead of one per call site.

The mode policy lives in :mod:`foundry.cassette.modes`, not here
    :class:`~foundry.cassette.modes.CassetteSession` decides which transport each ``cassette.mode``
    calls for -- the live transport itself in ``off``, a
    :class:`~foundry.cassette.transport.RecordingTransport` in ``record``, a
    :class:`~foundry.cassette.transport.ReplayTransport` in ``replay``, and a replay with a live
    fallback in ``passthrough`` (AAP 0.7.3) -- and writes the capture at teardown. This module only
    decides *whether* a session's transport is used at all (:func:`select_transport`), so the mode
    table exists once. Branching on a mode name here, or accepting one as an argument, would be a
    second copy of that policy that could disagree with the first.

Redirects are never followed (AAP 0.3.1)
    A request is issued by calling :meth:`httpx.BaseTransport.handle_request`, and
    :class:`httpx.Client` -- the only object in ``httpx`` that follows a redirect -- is deliberately
    never built. A hand-built :class:`httpx.Request` carries no redirect logic of its own, so a
    ``3xx`` comes back as an ordinary response, the caller sees the status the peer sent, and
    ``FoundryResponse.history`` is always empty.

Import direction and import-time discipline (AAP 0.4.8)
    ``httpx``, :mod:`foundry.core.exceptions` and :mod:`foundry.obs.events`, with
    :class:`~foundry.cassette.modes.CassetteSession` imported under :data:`typing.TYPE_CHECKING`
    because it arrives as an argument and is never built here; never :mod:`foundry.config`,
    :mod:`foundry.reporting`, :mod:`foundry.cli`, :mod:`foundry.plugin` or
    :mod:`foundry.execution`. ``httpx`` may be imported at module scope because nothing on
    ``foundry.plugin``'s import path reaches this module. Importing it builds no transport, reads no
    environment variable, opens no file, configures no logger and creates no module-level mutable
    state.

Closing never changes a test's outcome (AAP 0.1.2, AAP 0.14.3)
    :func:`close_quietly` is the teardown path for a transport, and it reports a transport that
    fails to close as exactly one ``WARNING`` on the ``foundry`` logger: the run keeps its outcomes
    and its exit code, because a socket that lingered is a diagnostic and never a test result.
"""

from __future__ import annotations

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

``True`` is the only default Foundry ships: a suite that reaches a peer over TLS should notice a
certificate it cannot verify. A caller that must relax it -- a staging peer with a private CA --
passes ``verify=False`` to :func:`build_live_transport`, which is why the choice is a keyword
argument rather than a configuration key this module would have to import ``foundry.config`` to read
(AAP 0.4.8).
"""

NETWORK_RETRIES: Final[int] = 0
"""How often ``httpcore`` may retry a connection on its own: never.

:mod:`foundry.http.retries` owns the policy, the predicates, the backoff, the attempt records and
the :class:`~foundry.core.exceptions.RetryExhaustedError`. A transport-level retry would hide an
attempt from it, multiply the configured attempt count, and make ``attempts`` in ``results.json``
describe something other than what Foundry did. Passing ``0`` explicitly records that retries are
Foundry's alone (AAP 0.3.1).
"""

_COMPONENT: Final[str] = "http.transport"
"""The component name :func:`close_quietly` reports a fault under, as a sibling module names itself
in its own plumbing-failure events."""

_CLOSE_ERRORS: Final[tuple[type[BaseException], ...]] = (FoundryError, OSError)
"""The faults a transport's ``close`` raises: ``OSError`` from the socket layer it is closing,
:class:`~foundry.core.exceptions.FoundryError` from a Foundry transport that refuses to close.

Nothing else is caught. A ``close`` that raised anything outside this set is a defect in the
transport rather than a plumbing fault, and it must stay visible instead of being absorbed here.
"""


def timeout_extension(seconds: float) -> dict[str, float]:
    """Return the ``timeout`` request extension that gives a bare request a deadline.

    Mandatory on every request the HTTP layer builds. :meth:`httpx.BaseTransport.handle_request`
    hands :attr:`httpx.Request.extensions` straight to ``httpcore``, which reads exactly the four
    sub-keys :meth:`httpx.Timeout.as_dict` returns -- ``pool`` in the connection pool, then
    ``connect``, ``write`` and ``read`` along the connection -- and a request carrying no
    ``timeout`` extension is given no deadline at all. An unresponsive peer would hold the call open
    indefinitely, which is a hang rather than the failure pytest reports, so this exists as a named
    function with its own docstring: an edit that inlined or dropped it would turn a slow peer into
    a suite that never finishes.

    Args:
        seconds: The deadline for every phase of the request, from ``http.timeout`` or from a
            caller's per-request override.

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

    It wraps :class:`httpx.HTTPTransport` configured with :data:`NETWORK_RETRIES` and the caller's
    certificate-verification choice, and delegates :meth:`handle_request` and :meth:`close` to it.
    Requests reach it by calling :meth:`handle_request` directly, so no redirect is ever followed: a
    ``3xx`` is returned as an ordinary response and ``FoundryResponse.history`` is always empty
    (AAP 0.3.1).

    Why a named class rather than a bare ``HTTPTransport``
        Three reasons, each of which a bare instance would lose. It is the single, greppable
        construction site for a socket-capable transport, which is what makes the offline guarantee
        of AAP 0.14.3 auditable rather than asserted. It is the seam a test subclasses or wraps to
        prove that a replay never reaches the network: a ``replay`` run has to show zero calls here,
        not merely zero recorded interactions. And it deliberately sets no cassette source tag -- a
        reply it returns carries no ``extensions["foundry.cassette.source"]`` -- so
        :func:`foundry.http.response.from_httpx` reports ``source == "live"`` for it, which is what
        ``FoundryResponse.source`` promises (AAP 0.4.3). Tagging a reply here would make
        ``FoundryResponse.source`` lie about which side answered it.

    A reply is never patched, rewritten or re-read
        What comes back is the peer's own :class:`httpx.Response`, so a retry policy above this
        class classifies exactly what ``httpx`` raised or returned, and
        :mod:`foundry.http.response` decodes the body once.
    """

    _transport: httpx.HTTPTransport
    """The wrapped ``httpx`` transport; the only object in the package that reaches ``httpcore``."""

    def __init__(self, *, verify: bool = DEFAULT_VERIFY) -> None:
        """Build the wrapped transport with network retries disabled.

        Args:
            verify: Whether the peer's certificate is verified; :data:`DEFAULT_VERIFY` unless a
                caller deliberately relaxes it.
        """
        self._transport = httpx.HTTPTransport(retries=NETWORK_RETRIES, verify=verify)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        """Send ``request`` to the peer and return what came back, whatever its status.

        Args:
            request: The request the HTTP layer built. It must carry the ``timeout`` extension
                :func:`timeout_extension` returns, or the call has no deadline.

        Returns:
            The peer's reply for every status code, redirects included: a ``3xx`` is returned to the
            caller rather than followed, so the caller decides what an unfollowed redirect means.

        Raises:
            httpx.HTTPError: Whatever ``httpcore`` raised -- a timeout, a connection fault, a
                protocol error. It propagates unchanged so :mod:`foundry.http.retries` can classify
                it and :class:`~foundry.core.exceptions.TransportError` can name its cause.
        """
        return self._transport.handle_request(request)

    def close(self) -> None:
        """Close the wrapped transport and the connection pool behind it.

        Teardown reaches this through :func:`close_quietly` rather than calling it directly, so a
        fault while closing is reported as one ``WARNING`` and never becomes a test result
        (AAP 0.1.2, AAP 0.14.3).
        """
        self._transport.close()


def build_live_transport(*, verify: bool = DEFAULT_VERIFY) -> httpx.BaseTransport:
    """Return a fresh live transport for a session that may reach a peer.

    The annotation is :class:`httpx.BaseTransport` on purpose. This object is handed to
    :func:`select_transport`, which may answer with a cassette transport instead, so a caller that
    depended on the concrete :class:`LiveTransport` would break the moment the cassettes in use
    changed. One transport per session, closed by the fixture that built it through
    :func:`close_quietly`.

    Args:
        verify: Whether the peer's certificate is verified; :data:`DEFAULT_VERIFY` unless a caller
            deliberately relaxes it.

    Returns:
        A :class:`LiveTransport` wrapping one :class:`httpx.HTTPTransport`, which opens no socket
        until a request is sent on it.
    """
    return LiveTransport(verify=verify)


def select_transport(
    live: httpx.BaseTransport, cassette: CassetteSession | None = None
) -> httpx.BaseTransport:
    """Return the transport a request is sent on: the live one, or the cassette's.

    The whole of this module's part in record/replay: a session is asked for the transport its mode
    calls for and nothing here looks at the mode (AAP 0.7.3). The mode table, the capture sink and
    the save at teardown all stay in :mod:`foundry.cassette.modes`, so there is one copy of that
    policy.

    Args:
        live: The transport :func:`build_live_transport` returned. It is handed back unchanged when
            no session is given, and it is the very object a session in ``off`` mode answers with.
        cassette: The test's cassette session, or ``None`` when the run keeps no cassette. Its
            :meth:`~foundry.cassette.modes.CassetteSession.transport` answers which transport its
            mode calls for, building it on first use.

    Returns:
        ``live`` itself when ``cassette`` is ``None``; otherwise the transport that session's mode
        calls for -- the caller's own object in ``off``, a recording wrapper in ``record``, and a
        replay in ``replay`` and ``passthrough``. No second live transport is ever built here.
    """
    if cassette is None:
        return live
    return cassette.transport()


def close_quietly(transport: httpx.BaseTransport | None) -> None:
    """Close ``transport``, reporting a fault as one ``WARNING`` instead of raising it.

    The teardown path for every transport Foundry builds. A socket that lingers, or a pool that
    refuses to shut down, is a fault in Foundry's own plumbing: it is reported on the ``foundry``
    logger and the run keeps its outcomes and its exit code, because the alternative -- a teardown
    error against a passing test -- turns a diagnostic into a failure the user cannot act on
    (AAP 0.1.2, AAP 0.14.3). The message names the transport's class and nothing else: the fault's
    own text can quote a URL or a header, and this line reaches a terminal and a captured log.

    Args:
        transport: The transport to close, or ``None`` for a session that built none. Closing
            nothing is a normal teardown, not an error, so it returns quietly.
    """
    if transport is None:
        return
    try:
        transport.close()
    except _CLOSE_ERRORS as exc:
        emit_plumbing_failure(
            _COMPONENT,
            f"could not close the {type(transport).__name__}",
            exc=exc,
        )
