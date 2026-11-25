"""The two ``httpx`` transports that make a cassette run possible: replay and record.

:class:`ReplayTransport` answers from a document and holds no live transport, so replay never
reaches ``httpcore`` (AAP 0.2.1), while :class:`RecordingTransport` sends live and tags the reply
(AAP 0.4.3).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Final

import httpx

from foundry.cassette.matcher import RequestFingerprint, describe, mask_query, match
from foundry.cassette.models import (
    CassetteDocument,
    Interaction,
    RecordedRequest,
    RecordedResponse,
    body_sha256_of,
    normalise_query,
)
from foundry.core.exceptions import CassetteMissError, FoundryError
from foundry.obs.events import emit_plumbing_failure
from foundry.obs.redaction import Redactor, default_redactor

__all__: Final[tuple[str, ...]] = (
    "SOURCE_CASSETTE",
    "SOURCE_EXTENSION_KEY",
    "SOURCE_LIVE",
    "VOLATILE_HEADERS",
    "RecordingTransport",
    "ReplayTransport",
    "fingerprint_from_request",
    "headers_subset",
    "recorded_response",
)

SOURCE_EXTENSION_KEY: Final[str] = "foundry.cassette.source"
"""The ``httpx.Response.extensions`` key that says which side answered a request.

:mod:`foundry.http.response` reads it to fill ``FoundryResponse.source`` (AAP 0.4.3), so the two
values below are the only legal contents. The key is namespaced with the distribution's own name so
it cannot collide with an extension ``httpcore`` sets on a live reply.
"""

SOURCE_CASSETTE: Final[str] = "cassette"
"""The value :data:`SOURCE_EXTENSION_KEY` carries for a reply served from a cassette document."""

SOURCE_LIVE: Final[str] = "live"
"""The value :data:`SOURCE_EXTENSION_KEY` carries for a reply the live transport produced.

A ``passthrough`` replay tags a delegated reply with this value rather than with
:data:`SOURCE_CASSETTE`, because the peer answered it (assumption A8).
"""

VOLATILE_HEADERS: Final[frozenset[str]] = frozenset(
    {
        "host",
        "content-length",
        "connection",
        "accept-encoding",
        "user-agent",
    }
)
"""The header names a capture drops, because they say nothing about the exchange.

Every one is set by the client or the connection rather than by the caller: a ``host`` and a
``content-length`` recorded on one machine would describe a different request on another. Denied
names such as ``authorization`` are deliberately not listed here: :func:`headers_subset` keeps them
and the store redacts their values on the way to disk.
"""

_ORIGIN_PATH: Final[str] = "/"
"""What a miss message shows when the fingerprint carries no path, mirroring the matcher's rule."""

_COMPONENT: Final[str] = "cassette.transport"
"""The component name :func:`~foundry.obs.events.emit_plumbing_failure` reports."""

_CAPTURE_ERRORS: Final[tuple[type[BaseException], ...]] = (
    FoundryError,
    httpx.HTTPError,
    TypeError,
    ValueError,
    KeyError,
    AttributeError,
    OSError,
)
"""Faults that end one capture attempt instead of the test that produced it.

A malformed recorded value (``FoundryError``), a body whose read failed (``httpx.HTTPError``), a
coercion the models refuse (``TypeError``, ``ValueError``, ``KeyError``, ``AttributeError``) and a
sink that cannot write (``OSError``). All are faults in Foundry's own capture, so they may not
change an outcome (AAP 0.1.2); a fault the live transport raises is raised before this band is
reached and is therefore untouched.
"""


# --- Request and response capture ------------------------------------------


def _request_body(request: httpx.Request) -> bytes:
    """Return the body of ``request``, or ``b""`` when it was never read into memory.

    AAP 0.9.2 excludes streaming, so an ordinary request has already been read by
    :class:`httpx.Request` and this returns its bytes. Capture and lookup both come through here,
    so they agree about that case and a request with a streamed body still matches itself.
    """
    try:
        return request.content
    except httpx.RequestNotRead:
        return b""


def _response_body(response: httpx.Response) -> bytes:
    """Return the body of ``response``, reading it first when the transport streamed it.

    A response built with ``content=`` is already read; a live transport's is not, and
    :attr:`httpx.Response.content` raises :class:`httpx.ResponseNotRead` until it is. Foundry never
    streams (AAP 0.9.2), so reading it here consumes the whole body and cannot wait on a producer
    with nothing more to send, and the client's own read afterwards is handed the buffered bytes.
    """
    try:
        return response.content
    except httpx.ResponseNotRead:
        return response.read()


def _kept_headers(headers: httpx.Headers) -> dict[str, str]:
    """Return ``headers`` without :data:`VOLATILE_HEADERS`, keyed by lower-cased name.

    ``httpx`` stores header names case-insensitively, so the mapping is unambiguous, and a repeated
    header arrives as its values joined by ``httpx`` itself. The result is ordered by name, so two
    runs that add their headers in a different order record identical text (AAP 0.13.1).
    """
    kept = {
        name.lower(): value
        for name, value in headers.items()
        if name.lower() not in VOLATILE_HEADERS
    }
    return {name: kept[name] for name in sorted(kept)}


def fingerprint_from_request(
    request: httpx.Request, *, redactor: Redactor | None = None
) -> RequestFingerprint:
    """Return the comparable part of ``request``, as a replay lookup decides a match.

    The four fields are the whole matching rule: the method, the path, the decoded query pairs and
    the body hash. The path carries no scheme, host or port, so a cassette recorded against one
    port replays on another, and the query goes through
    :func:`~foundry.cassette.models.normalise_query`, so parameter order and percent-encoding
    cannot separate two identical requests. Headers are absent by design.
    """
    engine = default_redactor() if redactor is None else redactor
    return RequestFingerprint(
        method=request.method,
        path=request.url.path,
        query=mask_query(normalise_query(request.url.query), engine),
        body_sha256=body_sha256_of(_request_body(request)),
    )


def headers_subset(request: httpx.Request) -> Mapping[str, str]:
    """Return the request headers a cassette keeps, by lower-cased name.

    :data:`VOLATILE_HEADERS` are dropped; every other header is kept exactly as received, denied
    names included, because :class:`~foundry.cassette.store.CassetteStore` redacts their values
    before the document is written and records what it scrubbed (implicit requirement I4). No header
    value is redacted here: a second redaction rule in this module would drift from the engine the
    artifact writer, the JSON store and the report all use.

    Args:
        request: The request being captured.

    Returns:
        A new mapping ordered by name, safe to hand to
        :class:`~foundry.cassette.models.RecordedRequest`, which freezes it.
    """
    return _kept_headers(request.headers)


def recorded_response(response: httpx.Response) -> RecordedResponse:
    """Return the pair a cassette stores for ``response``.

    The status is the peer's, so a recorded 500 replays as a 500 and no assertion is applied here.
    The headers are stored without :data:`VOLATILE_HEADERS`, and the body is decoded as UTF-8 with
    ``errors="replace"``: a cassette is a JSON document and the assertions it serves -- status, JSON
    subset, JSON Schema -- read text, while a byte sequence that is not UTF-8 still replays instead
    of failing the capture. A streamed body is read first (see :func:`_response_body`).

    Args:
        response: The response the live transport returned, already tagged or not.

    Returns:
        The immutable recorded response, ready to become half of an
        :class:`~foundry.cassette.models.Interaction`.
    """
    return RecordedResponse(
        status=response.status_code,
        headers=_kept_headers(response.headers),
        body=_response_body(response).decode("utf-8", errors="replace"),
    )


def _recorded_request(request: httpx.Request, redactor: Redactor) -> RecordedRequest:
    """Return the capture of ``request``, built from the fingerprint a lookup will compare.

    Going through :func:`fingerprint_from_request` rather than recomputing the fields is what keeps
    capture and matching in step: a request that records differently from the way it is looked up
    would produce a cassette that cannot replay the run which wrote it. The engine travels with it
    for the same reason, so a recorded query holds the marker the lookup will hold.
    """
    fingerprint = fingerprint_from_request(request, redactor=redactor)
    return RecordedRequest(
        method=fingerprint.method,
        path=fingerprint.path,
        query=fingerprint.query,
        body_sha256=fingerprint.body_sha256,
        headers_subset=headers_subset(request),
    )


def _request_target(fingerprint: RequestFingerprint) -> str:
    """Return the path and query of ``fingerprint`` as ``/checkout?page=2``, without the method.

    :func:`~foundry.cassette.matcher.describe` renders the whole request for a message; the
    structured ``path`` attribute of a miss carries the target alone, so a caller reporting it need
    not parse the prose back apart.
    """
    target = describe(fingerprint).partition(" ")[2]
    return target or _ORIGIN_PATH


def _tag(response: httpx.Response, source: str) -> None:
    """Record which side answered ``response``, for :mod:`foundry.http.response` to read."""
    response.extensions[SOURCE_EXTENSION_KEY] = source


# --- Replay ----------------------------------------------------------------


class ReplayTransport(httpx.BaseTransport):
    """An ``httpx`` transport that answers from one cassette document and opens no socket.

    It holds the interactions, the document's path, the run's redaction engine and a cursor per
    fingerprint, and nothing else: no client and no socket unless the caller supplies a live
    transport for ``passthrough``. The cursor is what serves a repeated request in recording order.
    """

    def __init__(
        self,
        cassette: CassetteDocument,
        cassette_path: Path,
        *,
        fallback: httpx.BaseTransport | None = None,
        on_served: Callable[[Interaction], None] | None = None,
        redactor: Redactor | None = None,
    ) -> None:
        """Bind the transport to one document and the file it came from.

        Args:
            cassette: The document the test recorded, as
                :meth:`~foundry.cassette.store.CassetteStore.load` returned it. A present but empty
                document is the recording of a test that made no request, so every request it then
                makes is a miss.
            cassette_path: The document's resolved path, used only to name it in a
                :class:`~foundry.core.exceptions.CassetteMissError`. The transport never reads it.
            fallback: The live transport a ``passthrough`` replay delegates an unmatched request to
                (assumption A8). ``None`` -- ``replay`` and ``off`` alike -- makes a miss raise, so
                nothing in this class can reach a socket of its own.
            on_served: Called with each interaction the cassette served, so the caller can count
                replays or report them. It is not called for a delegated reply: that exchange was
                not served by the cassette.
            redactor: The run's redaction engine, normally the one the store carries, so a lookup
                masks a query value exactly as the recording did. Defaults to
                :func:`~foundry.obs.redaction.default_redactor`.
        """
        self._interactions = cassette.interactions
        self._cassette_path = cassette_path
        self._fallback = fallback
        self._on_served = on_served
        self._redactor = default_redactor() if redactor is None else redactor
        self._consumed: dict[RequestFingerprint, int] = {}

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        """Answer ``request`` from the cassette, from the fallback, or with a typed miss.

        Args:
            request: The request ``httpx`` is sending.

        Returns:
            The recorded reply, tagged :data:`SOURCE_CASSETTE`, or the fallback's reply tagged
            :data:`SOURCE_LIVE`.

        Raises:
            CassetteMissError: No interaction matches and no fallback was given. The message and the
                structured attributes both name the method, the path and the cassette document, with
                every query value the engine redacts rendered as the marker, and no socket was
                touched on the way here.
        """
        fingerprint = fingerprint_from_request(request, redactor=self._redactor)
        found = match(fingerprint, self._interactions, consumed=self._consumed.get(fingerprint, 0))
        if found is None:
            return self._delegate_or_raise(request, fingerprint)
        _, interaction = found
        self._consumed[fingerprint] = self._consumed.get(fingerprint, 0) + 1
        if self._on_served is not None:
            self._on_served(interaction)
        # ``request=request`` is mandatory: httpx 0.27 raises RuntimeError from ``response.request``
        # on a response built without one, and every diagnostic in Foundry reads it.
        return httpx.Response(
            status_code=interaction.response.status,
            headers=dict(interaction.response.headers),
            content=interaction.response.body.encode("utf-8"),
            request=request,
            extensions={SOURCE_EXTENSION_KEY: SOURCE_CASSETTE},
        )

    def close(self) -> None:
        """Close the fallback transport when there is one; a document holds nothing to close."""
        if self._fallback is not None:
            self._fallback.close()

    def _delegate_or_raise(
        self, request: httpx.Request, fingerprint: RequestFingerprint
    ) -> httpx.Response:
        """Return the fallback's reply for an unmatched request, or raise the typed miss.

        The miss is rendered from the fingerprint, never from the request's own URL, so a query
        value the engine redacts reaches neither the message nor the ``path`` attribute while the
        method, the path and the cassette document stay named (AAP 0.4.3, AAP 0.4.7, AAP 0.4.9).
        """
        if self._fallback is not None:
            response = self._fallback.handle_request(request)
            _tag(response, SOURCE_LIVE)
            return response
        raise CassetteMissError(
            f"no recorded interaction for {describe(fingerprint)} in cassette"
            f" {self._cassette_path}",
            method=fingerprint.method,
            path=_request_target(fingerprint),
            cassette_path=self._cassette_path,
            hint="record it with --foundry-record, or run with cassette.mode=off",
        )


# --- Record ----------------------------------------------------------------


class RecordingTransport(httpx.BaseTransport):
    """An ``httpx`` transport that sends live and captures the exchange on the way back.

    Every reply is the live transport's own object, tagged :data:`SOURCE_LIVE` and otherwise
    untouched, so the caller's status, headers and body cannot differ because recording is on. The
    captured :class:`~foundry.cassette.models.Interaction` goes to the sink the caller supplied --
    normally a list the ``record`` path saves at teardown -- and a fault in that capture is
    reported as one ``WARNING`` and absorbed, because it is Foundry's plumbing rather than the
    test's (AAP 0.1.2, AAP 0.14.3).
    """

    def __init__(
        self,
        live: httpx.BaseTransport,
        on_exchange: Callable[[Interaction], None],
        *,
        redactor: Redactor | None = None,
    ) -> None:
        """Bind the transport to the live transport it wraps and the sink that keeps captures.

        Args:
            live: The transport that actually sends the request. ``httpx`` raises its faults through
                this class unchanged, so a retry policy above it sees exactly what it would see
                without recording.
            on_exchange: Called once per successful exchange with the captured interaction, in the
                order the requests were issued.
            redactor: The run's redaction engine, normally the one the store carries, so the capture
                of a query value carries the marker the store would write and a replay of it will
                compare against. Defaults to
                :func:`~foundry.obs.redaction.default_redactor`.
        """
        self._live = live
        self._on_exchange = on_exchange
        self._redactor = default_redactor() if redactor is None else redactor

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        """Send ``request`` live, capture the exchange and return the live response.

        Args:
            request: The request ``httpx`` is sending.

        Returns:
            The live transport's own response object, with
            ``extensions[SOURCE_EXTENSION_KEY]`` set to :data:`SOURCE_LIVE`.

        Raises:
            httpx.HTTPError: Whatever the live transport raised, unchanged -- a timeout or a
                connection fault belongs to the retry policy above this class, never here.
        """
        response = self._live.handle_request(request)
        self._capture(request, response)
        _tag(response, SOURCE_LIVE)
        return response

    def close(self) -> None:
        """Close the wrapped live transport."""
        self._live.close()

    def _capture(self, request: httpx.Request, response: httpx.Response) -> None:
        """Record the exchange, reporting a capture fault as one ``WARNING``.

        The message names the method and the path only: the query string and the headers can carry a
        value the redaction rules protect, and this line reaches a terminal and a captured log.
        """
        try:
            self._on_exchange(
                Interaction(
                    request=_recorded_request(request, self._redactor),
                    response=recorded_response(response),
                )
            )
        except _CAPTURE_ERRORS as exc:
            emit_plumbing_failure(
                _COMPONENT,
                f"could not record the exchange for {request.method} {request.url.path}",
                exc=exc,
            )
