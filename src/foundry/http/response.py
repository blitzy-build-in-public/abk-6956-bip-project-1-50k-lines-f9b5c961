"""The response object of Foundry's HTTP layer, and the record of the exchange it closes.

Every status, 4xx and 5xx included, is returned rather than raised, and both dumps are redacted
before their mapping is built.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Literal, TypeAlias, cast

import httpx

from foundry.cassette.models import body_sha256_of
from foundry.cassette.transport import SOURCE_CASSETTE, SOURCE_EXTENSION_KEY, SOURCE_LIVE
from foundry.contract import excerpt
from foundry.core.exceptions import ContractError
from foundry.http.url import path_and_query
from foundry.obs.redaction import MAX_EXCERPT_BYTES, Redactor

__all__: Final[tuple[str, ...]] = (
    "FoundryResponse",
    "SOURCE_CASSETTE",
    "SOURCE_EXTENSION_KEY",
    "SOURCE_LIVE",
    "from_httpx",
)

_Source: TypeAlias = Literal["live", "cassette"]
"""Where a response came from, the annotation AAP 0.4.3 publishes for ``source``.

Private because the two values are published as :data:`~foundry.cassette.transport.SOURCE_LIVE` and
:data:`~foundry.cassette.transport.SOURCE_CASSETTE`; the alias only gives the annotation a readable
name.
"""


def _folded(headers: Mapping[str, str]) -> dict[str, str]:
    """Return ``headers`` keyed by case-folded name, so equality ignores the names' case.

    ``headers`` is any header mapping, ``httpx``'s own included.

    Returns:
        A plain ``dict`` with case-folded names. A name repeated in the input keeps its last value,
        which is the rule a mapping already has.
    """
    return {name.casefold(): value for name, value in headers.items()}


class _FrozenHeaders(Mapping[str, str]):
    """An immutable, case-insensitive view of the headers a peer returned.

    ``FoundryResponse.headers`` is published as ``Mapping[str, str]`` (AAP 0.4.3), but a header
    lookup has to ignore case, and ``httpx``'s own ``Headers`` already implements that over its
    normalized name list. This is the read-only view that exposes it as a ``Mapping`` -- no method
    that adds, replaces or removes a header -- so the published annotation and the behaviour a test
    relies on both hold; ``get``, ``__contains__`` and ``items`` come from
    :class:`collections.abc.Mapping`. Every value rendered here passes through the injected
    :class:`~foundry.obs.redaction.Redactor` (AAP 0.3.2, AAP 0.14.3).
    """

    __slots__ = ("_headers", "_redactor")

    def __init__(self, headers: httpx.Headers, redactor: Redactor) -> None:
        """Store the headers and the engine :meth:`__repr__` masks values with.

        ``headers`` arrives as ``httpx`` holds them and ``redactor`` is the run's redaction engine.
        """
        self._headers = headers
        self._redactor = redactor

    def __getitem__(self, name: str) -> str:
        """Return the value of the header called ``name``, matching its case.

        Returns:
            The value, joined by ``httpx`` when the peer sent the name more than once.

        Raises:
            KeyError: The response carries no header of that name. The message quotes the name alone
                and never another header's value.
        """
        return self._headers[name]

    def __iter__(self) -> Iterator[str]:
        """Iterate the header names, lower-cased, in the order the peer sent them.

        Returns:
            An iterator over the names as ``httpx`` normalised them.
        """
        return iter(self._headers)

    def __len__(self) -> int:
        """Return the number of headers, a name the peer repeated counting once.

        Returns:
            The length of ``httpx``'s normalised name list, which joins repeated names.
        """
        return len(self._headers)

    def __eq__(self, other: object) -> bool:
        """Report whether ``other`` holds the same headers, ignoring the case of their names.

        Returns:
            ``True`` when both sides carry the same case-folded name-to-value pairs, and
            ``NotImplemented`` for a value that is not a mapping, so Python falls back to the other
            operand's comparison.
        """
        if not isinstance(other, Mapping):
            return NotImplemented
        return _folded(self) == _folded(other)

    def __repr__(self) -> str:
        """Render the headers with every value in the form the redaction engine gives it.

        A value under a name the engine masks is shown as its marker, and a value under any other
        name is shown free-text scrubbed, so the echo a peer answers with cannot reach a log line or
        a terminal through a header's representation (AAP 0.3.2, AAP 0.14.3).

        Returns: ``_FrozenHeaders({'<name>': '<value>', ...})``, sorted by name so two runs render
            the same.
        """
        shown = ", ".join(
            f"{name!r}: {self._redactor.header(name, value)!r}"
            for name, value in sorted(self.items())
        )
        return f"{type(self).__name__}({{{shown}}})"


@dataclass(frozen=True, slots=True)
class _RequestSnapshot:
    """The parts of the outgoing request a dump reports, read once when the response is built.

    The body is kept as raw bytes, because the hash a dump reports is computed over exactly those
    bytes, and the query pairs stay in the order the request carried them for
    :meth:`FoundryResponse.request_dump` to sort.
    """

    method: str
    path: str
    query: tuple[tuple[str, str], ...]
    headers: Mapping[str, str]
    body: bytes


@dataclass(frozen=True, slots=True, repr=False, kw_only=True)
class FoundryResponse:
    """One completed HTTP exchange: the answer to a request, whatever its status.

    A test receives this object for a ``200``, a ``404``, a ``503`` or a replay served from a
    cassette, and decides what to assert about it (AAP 0.4.3). Nothing here raises a status:
    :func:`assert_status`, :func:`assert_json_subset` and :func:`assert_json_schema` are the helpers
    that turn a status or a body into an assertion failure, and a caller that wants a fault below
    the protocol gets it from :mod:`foundry.http.retries` rather than from this class. Construction
    goes through :func:`from_httpx`, the seam :mod:`foundry.http.session` uses, so the private
    members that carry the request and the redaction engine are filled in one place and cannot be
    forgotten.

    Attributes: status_code: The numeric status, ``4xx`` and ``5xx`` included (AAP 0.4.3). headers:
        The response headers, case-insensitive and read-only. A name the configuration lists in
        ``secrets.redact_keys`` is still readable here; every route that persists or displays a
        header value passes it through the redaction engine first (AAP 0.3.2). text: The body
        decoded as text, using the charset the response declared. elapsed_ms: Wall-clock
        milliseconds for the whole logical request, every attempt and its backoff included. The
        session measures it with :func:`time.monotonic` around the retry loop and passes it in, so
        it is no per-attempt timer and no clock is read here. source: ``"live"`` or ``"cassette"``,
        taken from the tag :mod:`foundry.cassette.transport` puts on every reply it returns.
    """

    status_code: int
    headers: Mapping[str, str]
    text: str
    elapsed_ms: float
    source: _Source
    _request: _RequestSnapshot = field(repr=False, compare=False)
    _redactor: Redactor = field(repr=False, compare=False)

    def json(self) -> Any:
        """Decode the body as JSON.

        The one place in this module where ``Any`` is the honest annotation: the body is an
        arbitrary JSON tree, and :func:`foundry.assertions.assert_json_subset` consumes it as one
        (AAP 0.4.3).

        Returns:
            The decoded value: a ``dict``, a ``list``, a ``str``, a number, a ``bool`` or ``None``.

        Raises: ContractError: The body is not JSON. ``json_path`` is ``$``, the whole document
            being the violation, and ``detail`` is a single-line redacted excerpt of at most
            :data:`~foundry.obs.redaction.MAX_EXCERPT_BYTES` bytes, the bound AAP 0.4.7 fixes for
            it; the message quotes no part of the body.
        """
        try:
            return json.loads(self.text)
        except json.JSONDecodeError as exc:
            raise ContractError(
                f"response body is not JSON: {exc.msg} at line {exc.lineno} column {exc.colno}",
                json_path="$",
                detail=excerpt(self.text, limit=MAX_EXCERPT_BYTES, redact=self._redactor.text),
            ) from exc

    def request_dump(self) -> dict[str, object]:
        """Return the redacted ``request.json`` payload of this exchange.

        Every value passes through the redaction engine, so no header, query or body value the
        configuration masks survives, and the ``body_sha256`` is the digest of the raw bytes the
        cassette matcher compares. The mapping holds exactly ``method``, ``path``, ``query``,
        ``headers``, ``body_sha256`` and ``body``.
        """
        redactor = self._redactor
        snapshot = self._request
        return {
            "method": snapshot.method.upper(),
            "path": redactor.url(snapshot.path),
            # ``header`` is the engine's one method for a value that travels under a name: the name
            # decides whether the whole value is masked, and a value the name does not mask is
            # scrubbed as free text. This module therefore holds no redaction rule of its own that
            # could drift from the engine.
            "query": [
                [name, redactor.header(name, value)] for name, value in sorted(snapshot.query)
            ],
            "headers": redactor.headers(snapshot.headers),
            "body_sha256": body_sha256_of(snapshot.body),
            "body": _dumped_body(snapshot.body, redactor),
        }

    def response_dump(self) -> dict[str, object]:
        """Return the redacted ``response.json`` payload of this exchange.

        The mapping holds exactly ``status``, ``headers``, ``body`` and ``source``, each header
        value masked by name and scrubbed as free text.
        """
        return {
            "status": self.status_code,
            "headers": self._redactor.headers(self.headers),
            "body": _dumped_body(self.text, self._redactor),
            "source": self.source,
        }

    def __repr__(self) -> str:
        """Render the exchange without its body and without any header value.

        A response carries a credential whenever the peer echoes one, so the representation names
        the status, the source, the elapsed time, the number of headers and the length of the body
        and nothing more -- the same rule :mod:`foundry.core.exceptions` follows for free text
        (AAP 0.3.2, AAP 0.14.3).

        Returns:
            ``FoundryResponse(status_code=500, source=live, elapsed_ms=12.3, headers=4,
            text=<137 characters>)``.
        """
        return (
            f"{type(self).__name__}(status_code={self.status_code}, source={self.source}, "
            f"elapsed_ms={self.elapsed_ms:.1f}, headers={len(self.headers)}, "
            f"text=<{len(self.text)} characters>)"
        )


def _dumped_body(body: str | bytes, redactor: Redactor) -> str | None:
    """Return the whole redacted body a dump reports for ``body``, or ``None`` when there is none.

    A dump exists so a reader can see what was sent and what came back, so this is the complete body
    with every value passed through the redaction engine, never an excerpt: nothing is cut and no
    truncation marker is appended (AAP 0.4.6, AAP 0.3.2). Bytes are decoded as UTF-8 with
    ``errors="replace"`` -- the decoding :meth:`~foundry.obs.redaction.Redactor.body_excerpt`
    performs -- and text is kept as it stands. An empty body yields ``None``, which is the rule
    :func:`foundry.cassette.models.body_sha256_of` applies to the hash beside it.
    """
    if not body:
        return None
    source = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else body
    return redactor.text(source)


def _request_body(request: httpx.Request) -> bytes:
    """Return the body of ``request``, or ``b""`` when it was never read into memory.

    ``httpx`` raises :class:`httpx.RequestNotRead` for a request built around a caller's own stream
    until it is sent. Foundry does not stream a request body (AAP 0.9.2), and
    :func:`foundry.cassette.transport.fingerprint_from_request` resolves the same case the same way,
    so a request that carries one dumps the empty body it fingerprints rather than failing the
    exchange.

    Returns:
        The raw body bytes, or ``b""`` when the body has not been read.
    """
    try:
        return request.content
    except httpx.RequestNotRead:
        return b""


def _request_path(url: str) -> str:
    """Return the path of ``url`` alone, with no query string, as a request dump reports it.

    A request dump reports the query in its own field, so the path it reports has to be the request
    target without one, which is the meaning the cassette document gives the same field name. The
    empty-path and leading-slash rules stay in :func:`foundry.http.url.path_and_query`.
    """
    path, _, _ = path_and_query(url).partition("?")
    return path


def _request_snapshot(request: httpx.Request) -> _RequestSnapshot:
    """Return the parts of ``request`` that :meth:`FoundryResponse.request_dump` reports.

    Args:
        request: The request the response answers, as ``httpx`` built it.

    Returns:
        The snapshot, with the path rendered by :func:`_request_path` and the query decoded into
        pairs, so the dump carries no scheme, host or port -- a recording made on one port
        therefore dumps the same as a replay on another (assumption A7) -- and no query in its
        ``path``, which the dump reports in the member of its own (AAP 0.4.6).
    """
    return _RequestSnapshot(
        method=request.method,
        path=_request_path(str(request.url)),
        query=tuple(request.url.params.multi_items()),
        headers=request.headers,
        body=_request_body(request),
    )


def _response_text(response: httpx.Response) -> str:
    """Return the body of ``response`` decoded as text, reading it first when it was streamed.

    A response built with ``content=`` is already read; a live transport's is not, and reading
    :attr:`httpx.Response.text` before the body is loaded raises :class:`httpx.ResponseNotRead`.
    Foundry never streams a response (AAP 0.9.2), so reading it here consumes the body and the
    client's own read afterwards is handed the buffered bytes.

    Args:
        response: The response whose body is wanted.

    Returns:
        The body as text, decoded with the charset the response declared.
    """
    try:
        return response.text
    except httpx.ResponseNotRead:
        response.read()
        return response.text


def _source_of(response: httpx.Response) -> _Source:
    """Return which side answered ``response``, defaulting to live when no tag is present.

    :mod:`foundry.cassette.transport` tags every reply it returns under
    :data:`~foundry.cassette.transport.SOURCE_EXTENSION_KEY`, with ``SOURCE_CASSETTE`` for a
    recorded interaction and ``SOURCE_LIVE`` for a reply the live transport produced, the one a
    ``passthrough`` replay delegates included. A reply carrying no tag, or a value no Foundry
    transport writes, is reported as live rather than raised: where a response came from is a
    diagnostic detail, and a missing extension must never turn an exchange into a failure (AAP
    0.1.2).

    Returns:
        ``"cassette"`` when the tag says a cassette answered, and ``"live"`` otherwise.
    """
    tag: object = response.extensions.get(SOURCE_EXTENSION_KEY, SOURCE_LIVE)
    # ``cast`` states exactly what the comparison establishes: the only two values a Foundry
    # transport writes are the two constants, so the result is one of the literals the annotation
    # names -- which mypy cannot see through their ``Final[str]`` declaration.
    return cast(_Source, SOURCE_CASSETTE if tag == SOURCE_CASSETTE else SOURCE_LIVE)


def from_httpx(
    response: httpx.Response,
    *,
    elapsed_ms: float,
    redactor: Redactor,
    request: httpx.Request,
) -> FoundryResponse:
    """Build the Foundry response for one completed ``httpx`` exchange.

    This is the construction seam :mod:`foundry.http.session` uses: the retry loop hands back an
    :class:`httpx.Response` for every status, and this function turns it into the object a test
    asserts against, capturing the request it answered so the two dumps can be produced later (AAP
    0.4.6). Nothing here reads a clock, so the elapsed measurement is the caller's.

    Args: response: The reply ``httpx`` produced, or the one a cassette transport served.
        elapsed_ms: Wall-clock milliseconds the whole logical request took, retries and their
        backoff included, as measured by the session with :func:`time.monotonic`. A negative
        measurement cannot arise from a monotonic clock; it is clamped to zero rather than raised,
        because only :class:`~foundry.core.exceptions.ContractError` may cross this module's
        boundary (AAP 0.14.3) and an artefact of the caller's timing must never fail a test (AAP
        0.1.2). redactor: The run's redaction engine, built from ``secrets.redact_keys``. Both dumps
        and every representation of this response take their redaction from it. request: The request
        ``response`` answers, whose method, target, query, headers and body
        :meth:`FoundryResponse.request_dump` reports.

    Returns: The response, with case-insensitive headers, the body decoded once, the status
        untouched and the source taken from the transport's tag.
    """
    return FoundryResponse(
        status_code=response.status_code,
        headers=_FrozenHeaders(response.headers, redactor),
        text=_response_text(response),
        elapsed_ms=max(0.0, elapsed_ms),
        source=_source_of(response),
        _request=_request_snapshot(request),
        _redactor=redactor,
    )
