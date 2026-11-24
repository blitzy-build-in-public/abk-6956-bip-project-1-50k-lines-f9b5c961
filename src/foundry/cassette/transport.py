"""The two ``httpx`` transports that make a cassette run possible: replay and record.

``foundry.cassette`` owns record/replay (AAP 0.3.2), and this module is the seam where a cassette
meets the HTTP client. An :class:`httpx.BaseTransport` is the only place ``httpx`` consults for a
reply, so a transport that answers from a document -- and holds no live transport at all -- is
what makes replay provably offline rather than merely believed to be (AAP 0.2.1).
:class:`ReplayTransport` serves the recorded interactions of one cassette, and
:class:`RecordingTransport` wraps the live transport and captures what passed through it -- the
``record`` row of the mode table (AAP 0.7.3) that would otherwise have no seam. Both are ordinary
``httpx`` objects, so :mod:`foundry.http` hands one to :class:`httpx.Client` and learns which mode
it is in from the tag on each reply rather than from the transport's type.

The reply tag is the interface :mod:`foundry.http.response` reads
    Every reply either class returns carries ``extensions[SOURCE_EXTENSION_KEY]``, whose value is
    :data:`SOURCE_CASSETTE` for a recorded interaction and :data:`SOURCE_LIVE` for a reply the live
    transport produced -- including the one a ``passthrough`` replay delegates to its fallback. That
    is how ``FoundryResponse.source: Literal["live", "cassette"]`` (AAP 0.4.3) is populated without
    the HTTP layer reaching into this module, and a reply carrying neither value is a defect in the
    transport rather than a third state. Nothing else about a reply is rewritten: the status, the
    headers and the body are the peer's.

Replay opens no socket, by construction (AAP 0.9.2, AAP 0.14.3)
    :class:`ReplayTransport` is handed a document and a path, not a client: in ``off``, ``record``
    and ``replay`` mode it holds no live transport, so no path inside it can reach ``httpcore`` or
    ``socket.connect``. Only ``passthrough`` receives a fallback (assumption A8), and that is the
    caller's own transport, which the caller already owns. Nothing here patches a socket and nothing
    falls back silently: a request with no recorded counterpart raises
    :class:`~foundry.core.exceptions.CassetteMissError` naming the method, the path and the cassette
    document (AAP 0.4.3, AAP 0.4.9), and only a non-``None`` fallback changes that.

Matching, ordering and determinism
    Every lookup goes through :func:`foundry.cassette.matcher.match_interaction` with a cursor this
    instance holds, so a test that issues one request twice replays the first recording and then the
    second, and a third lookup replays the last one instead of reporting a miss (AAP 0.2.1,
    AAP 0.4.6). The cursor is an attribute of one transport, never module state (AAP 0.4.8), so two
    tests of one session each start from zero. A fingerprint carries the method, the path, the
    decoded query pairs and the body hash and nothing else (assumption A7), so one document and one
    sequence of requests always produce the same replies, on any host and any port (AAP 0.13.1).

Capture never changes a test's outcome (AAP 0.1.2, AAP 0.14.3)
    :class:`RecordingTransport` returns the live transport's own response object, tagged and
    otherwise untouched, and an ``httpx`` fault raised by the live transport propagates unchanged so
    :mod:`foundry.http.retries` can classify it. A fault while *capturing* that reply -- a body that
    cannot be read, a sink that raises -- is a fault in Foundry's own plumbing: it is reported as
    exactly one ``WARNING`` on the ``foundry`` logger and the reply is still returned, because a
    cassette that could not be recorded must never turn a passing test into an error.

Redaction belongs to the store, not to this module (implicit requirement I4)
    :func:`headers_subset` deliberately keeps ``authorization``, ``cookie`` and any other denied
    name: :class:`~foundry.cassette.store.CassetteStore` replaces those values with the redaction
    marker on the way to disk and records the names in the document's ``redacted_keys``, which it
    could not do for a header removed before it was ever offered. This module therefore stores no
    secret and scrubs nothing; there is one redaction engine and one write funnel, and neither is
    here.

Import direction and import-time discipline (AAP 0.4.8)
    ``httpx``, :mod:`foundry.cassette.models`, :mod:`foundry.cassette.matcher`,
    :mod:`foundry.cassette.paths`, :mod:`foundry.core.exceptions` and :mod:`foundry.obs.events` --
    never :mod:`foundry.http`, which imports this package, and never :mod:`foundry.config`,
    :mod:`foundry.reporting` or :mod:`foundry.execution`. ``httpx`` may be imported at module scope
    because nothing on ``foundry.plugin``'s import path reaches this package. Importing the module
    reads no environment variable, opens no file, configures no logger and builds no mutable
    module-level state: the only mutable containers are the cursors on a replay instance.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Final

import httpx

from foundry.cassette.matcher import RequestFingerprint, describe, match_interaction
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
values below are the only legal contents and every reply this module returns carries one of them.
The key is namespaced with the distribution's own name so it cannot collide with an extension
``httpcore`` sets on a live reply.
"""

SOURCE_CASSETTE: Final[str] = "cassette"
"""The value :data:`SOURCE_EXTENSION_KEY` carries for a reply served from a cassette document."""

SOURCE_LIVE: Final[str] = "live"
"""The value :data:`SOURCE_EXTENSION_KEY` carries for a reply the live transport produced.

A ``passthrough`` replay tags a delegated reply with this value rather than with
:data:`SOURCE_CASSETTE`, because the peer answered it and a failure diagnosed against it belongs to
the service under test (assumption A8).
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

Every one of them is set by the client or the connection rather than by the caller: a ``host`` and a
``content-length`` recorded on one machine would describe a different request on another, and an
``accept-encoding`` or ``user-agent`` describes the client rather than the intent. Denied names --
``authorization``, ``cookie``, ``x-api-key`` and the rest of ``secrets.redact_keys`` -- are
deliberately *not* listed here and are kept by :func:`headers_subset`, because the store redacts
their values on the way to disk and records the scrubbed names in the document's ``redacted_keys``.
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

A malformed recorded value (``FoundryError``), a body whose read failed (``httpx.HTTPError``,
which covers ``StreamConsumed`` and the read faults), a coercion the models refuse
(``TypeError``, ``ValueError``, ``KeyError``, ``AttributeError``) and a sink that cannot write
(``OSError``). All of them are faults in Foundry's own capture, so they may not change an outcome
(AAP 0.1.2). A fault the *live* transport raises is raised before this band is reached and is
therefore untouched.
"""


# --- Request and response capture ------------------------------------------


def _request_body(request: httpx.Request) -> bytes:
    """Return the body of ``request``, or ``b""`` when it was never read into memory.

    AAP 0.9.2 excludes streaming, so an ordinary request has already been read by
    :class:`httpx.Request` and this returns its bytes. A request built around a caller's own stream
    has no content until it is sent, and a fingerprint is taken before that; rather than refusing
    the request, such a body fingerprints as empty. Capture and lookup both come through here, so
    they agree about that case and a request with a streamed body still matches itself.
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


def fingerprint_from_request(request: httpx.Request) -> RequestFingerprint:
    """Return the comparable part of ``request``, as a replay lookup decides a match.

    The four fields are the whole matching rule (AAP 0.3.2, AAP 0.4.6): the method, the path, the
    decoded query pairs and the body hash. The path is
    :attr:`httpx.URL.path` alone, with no scheme, host or port, so a cassette recorded against one
    port replays on another (assumption A7), and the query is passed through
    :func:`~foundry.cassette.models.normalise_query` so parameter order and percent-encoding cannot
    separate two identical requests. Headers are absent by design: a header the client sets for
    itself would make a recording unplayable elsewhere.

    Args:
        request: The request about to be sent, as ``httpx`` built it.

    Returns:
        The fingerprint, normalised, frozen and hashable, so it can key a replay cursor.
    """
    return RequestFingerprint(
        method=request.method,
        path=request.url.path,
        query=normalise_query(request.url.query),
        body_sha256=body_sha256_of(_request_body(request)),
    )


def headers_subset(request: httpx.Request) -> Mapping[str, str]:
    """Return the request headers a cassette keeps, by lower-cased name.

    :data:`VOLATILE_HEADERS` are dropped; every other header is kept exactly as received, denied
    names included, because :class:`~foundry.cassette.store.CassetteStore` redacts their values
    before the document is written and records what it scrubbed (implicit requirement I4). Nothing
    is redacted here: a second redaction rule in this module would drift from the engine the
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


def _recorded_request(request: httpx.Request) -> RecordedRequest:
    """Return the capture of ``request``, built from the fingerprint a lookup will compare.

    Going through :func:`fingerprint_from_request` rather than recomputing the fields is what keeps
    capture and matching in step: a request that records differently from the way it is looked up
    would produce a cassette that cannot replay the run which wrote it.
    """
    fingerprint = fingerprint_from_request(request)
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

    The transport holds the document's interactions, its path for the miss diagnostic and a cursor
    per fingerprint, and nothing else: no client, no socket, no live transport unless the caller
    supplies one for ``passthrough``. That is what makes a replay run's offline behaviour a property
    of the design rather than of the request sequence (AAP 0.9.2, AAP 0.14.3).

    The cursor is what serves a repeated request in recording order. A cassette holding two
    interactions for one fingerprint hands them out on the first and second lookup, and a third
    lookup replays the last one, because that is what
    :func:`~foundry.cassette.matcher.match_interaction` answers for a count past the last match
    (AAP 0.2.1, AAP 0.4.6).
    """

    def __init__(
        self,
        cassette: CassetteDocument,
        cassette_path: Path,
        *,
        fallback: httpx.BaseTransport | None = None,
        on_served: Callable[[Interaction], None] | None = None,
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
        """
        self._interactions = cassette.interactions
        self._cassette_path = cassette_path
        self._fallback = fallback
        self._on_served = on_served
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
                structured attributes both name the method, the path and the cassette document, and
                no socket was touched on the way here.
        """
        fingerprint = fingerprint_from_request(request)
        found = match_interaction(
            fingerprint, self._interactions, consumed=self._consumed.get(fingerprint, 0)
        )
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
        """Return the fallback's reply for an unmatched request, or raise the typed miss."""
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
    normally a list the ``record`` path saves at teardown -- and a fault in that capture is reported
    as one ``WARNING`` and absorbed, because it is Foundry's plumbing rather than the test's
    (AAP 0.1.2, AAP 0.14.3).
    """

    def __init__(
        self, live: httpx.BaseTransport, on_exchange: Callable[[Interaction], None]
    ) -> None:
        """Bind the transport to the live transport it wraps and the sink that keeps captures.

        Args:
            live: The transport that actually sends the request. ``httpx`` raises its faults through
                this class unchanged, so a retry policy above it sees exactly what it would see
                without recording.
            on_exchange: Called once per successful exchange with the captured interaction, in the
                order the requests were issued.
        """
        self._live = live
        self._on_exchange = on_exchange

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
                    request=_recorded_request(request),
                    response=recorded_response(response),
                )
            )
        except _CAPTURE_ERRORS as exc:
            emit_plumbing_failure(
                _COMPONENT,
                f"could not record the exchange for {request.method} {request.url.path}",
                exc=exc,
            )
