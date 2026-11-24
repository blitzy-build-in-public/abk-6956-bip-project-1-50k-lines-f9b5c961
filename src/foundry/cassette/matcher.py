"""Deciding which recorded interaction, if any, is the request about to be sent.

``foundry.cassette`` owns record/replay (AAP 0.3.2), and this module answers the one question the
replay transport asks: of the interactions a cassette recorded, which one is this request?
:class:`RequestFingerprint` is the comparable half of a request, holding only the fields a match is
decided by, and :func:`match_interaction` is the single lookup the transport performs.

Four fields decide a match, and nothing else does (AAP 0.2.1, AAP 0.3.2, AAP 0.4.6)
    The method, the path, the decoded query pairs and the body hash are compared; a header never is.
    A client sets ``host``, ``content-length`` and its own request id for itself, so a recording
    that matched on them would not replay on another machine -- and they are kept in the document
    as ``headers_subset`` for the person reading it, not for this module. The query is compared as
    :func:`foundry.cassette.models.normalise_query` returns it, sorted by name and then value, so
    ``?b=2&a=1`` and ``?a=1&b=2`` are one request, and the encoding a client chose cannot separate
    ``?q=a+b`` from ``?q=a%20b``. The body is compared by its SHA-256 rather than by its bytes, so a
    serialiser's whitespace inside one equivalent JSON document cannot split a request in two.
    Neither host nor port takes part, because the fingerprint carries the path alone (assumption
    A7): a cassette recorded against ``127.0.0.1:54321`` replays wherever the suite runs.

One recorded interaction per request, served in recording order
    A test that creates an entity and then reads it back issues two requests, and a poll issues one
    request several times. :func:`match_interaction` therefore takes a ``consumed`` count and
    answers with the ``consumed``-th match in document order, so the second lookup replays the
    second recording rather than the first. A count past the last match answers with the last
    match, so a re-read of one resource replays instead of failing the run.

The seam is named ``match_interaction``, not ``match`` (AAP 0.7.1)
    AAP 0.7.1 describes the transport's collaborator as ``match(request, interactions)``. ``match``
    has been a soft keyword since Python 3.10 and reads as a statement at the start of a ``case``
    block, so the function carries the explicit name ``match_interaction``. Its behaviour is exactly
    the plan's.

No exceptions, no I/O, no state
    The import list is the standard library plus :mod:`foundry.cassette.models` and nothing else --
    no ``httpx``, because the replay transport builds the fingerprint with
    ``transport.fingerprint_from_request`` and this module never holds a client (AAP 0.4.8).
    Importing it reads no environment variable, opens no file, configures no logger and creates no
    mutable module-level container: constants and definitions only. Nothing here reports a failure
    of its own -- a request with no counterpart is answered with ``None``, and ``transport.py``,
    which knows the cassette's path, reports that miss as
    :class:`foundry.core.exceptions.CassetteMissError`. Matching is pure and total, so one
    fingerprint and one document always give one answer, and a match is reported with its index in
    the document so the transport can advance a cursor without a second scan (AAP 0.13.1).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlencode

from foundry.cassette.models import Interaction, RecordedRequest, normalise_query

__all__: Final[tuple[str, ...]] = (
    "RequestFingerprint",
    "describe",
    "match_interaction",
    "matches_recorded",
    "matching_indices",
)

_ORIGIN_PATH: Final[str] = "/"
"""What :func:`describe` shows when the request carried no path.

An empty path means the origin itself, and a diagnostic reading ``GET`` with nothing after it would
leave a person wondering whether the path or the whole message were lost. Matching is unaffected:
:func:`matches_recorded` compares the stored path exactly as it is.
"""


@dataclass(frozen=True, slots=True)
class RequestFingerprint:
    """The part of a request a match is decided by, normalised so that equal requests are equal.

    The four fields are exactly AAP 0.3.2's matching rule and nothing more: no host, no port, no
    headers (assumption A7, AAP 0.4.6). Building one from an outgoing request is the transport's job
    (``transport.fingerprint_from_request``), which is what keeps this module free of ``httpx`` and
    testable without a client.

    Attributes:
        method: The request method, stripped and upper-cased at construction, so ``get`` and ``GET``
            fingerprint alike.
        path: The request path without its query string, as the client's URL carried it.
        query: The decoded query parameters as ``(name, value)`` pairs sorted by name and then value
            by :func:`~foundry.cassette.models.normalise_query`, with blank values kept because
            ``?flag=`` is a present parameter.
        body_sha256: The lowercase hexadecimal SHA-256 of the request body, or ``None`` when the
            body was empty, which is the one spelling of "no body"
            (:func:`~foundry.cassette.models.body_sha256_of`).
    """

    method: str
    path: str
    query: tuple[tuple[str, str], ...] = ()
    body_sha256: str | None = None

    def __post_init__(self) -> None:
        """Normalise the method and the query, so two spellings of one request compare equal.

        Normalising here rather than at comparison time is what makes :func:`matches_recorded` a
        plain field comparison, and a :class:`~foundry.cassette.models.RecordedRequest` normalises
        itself the same way in its own ``__post_init__``, so a fingerprint and the recording of one
        request hold equal fields.
        """
        object.__setattr__(self, "method", self.method.strip().upper())
        object.__setattr__(self, "query", normalise_query(self.query))


def matches_recorded(fp: RequestFingerprint, recorded: RecordedRequest) -> bool:
    """Return whether ``fp`` is the request that ``recorded`` describes.

    Four fields decide it and no others (AAP 0.2.1, AAP 0.3.2): the method, compared
    case-insensitively although both sides are upper-cased at construction; the path, compared
    exactly; the decoded query pairs, both sides sorted by name and then value, which is what makes
    parameter order and percent-encoding irrelevant; and the body hash, with ``None`` equal to
    ``None`` because a request with no body and one with a zero-length body are one request.

    Headers are deliberately absent from the comparison. ``recorded.headers_subset`` exists for a
    person reading the cassette, and matching on it would make a recording refuse to replay wherever
    a client sets ``host`` or ``content-length`` for itself. No host or port is compared either,
    because the fingerprint does not carry one (assumption A7).

    Args:
        fp: The request about to be sent, as the transport fingerprinted it.
        recorded: One interaction's recorded request.

    Returns:
        ``True`` when the two describe the same request, ``False`` otherwise.
    """
    if fp.method.upper() != recorded.method.upper():
        return False
    if fp.path != recorded.path:
        return False
    if fp.query != recorded.query:
        return False
    return fp.body_sha256 == recorded.body_sha256


def matching_indices(
    fp: RequestFingerprint, interactions: Sequence[Interaction]
) -> tuple[int, ...]:
    """Return the positions in ``interactions`` whose request ``fp`` describes.

    The positions are 0-based indexes into ``interactions`` in document order, one per matching
    interaction, so a cassette holding the same request twice yields two positions and a caller can
    tell which recording it is on. An empty tuple means the cassette holds no counterpart, which the
    transport reports as a miss.

    Args:
        fp: The request about to be sent.
        interactions: The cassette's interactions, in the order they were recorded.

    Returns:
        The matching indexes, ascending, as a tuple; empty when none of them match.
    """
    return tuple(
        index
        for index, interaction in enumerate(interactions)
        if matches_recorded(fp, interaction.request)
    )


def match_interaction(
    fp: RequestFingerprint,
    interactions: Sequence[Interaction],
    *,
    consumed: int = 0,
) -> tuple[int, Interaction] | None:
    """Return the ``consumed``-th interaction whose request ``fp`` describes, with its position.

    ``consumed`` is how many matches the caller has already replayed, counted from zero: a test that
    issues one request twice passes ``0`` for the first lookup and ``1`` for the second, so the
    recorded interactions are served in the order they were recorded. A count at or past the last
    match answers with the last match, so a re-read of one resource replays rather than reporting a
    miss; a negative count is read as ``0``.

    The position is returned beside the interaction, so the transport can advance its cursor
    without scanning the document a second time.

    Args:
        fp: The request about to be sent.
        interactions: The cassette's interactions, in the order they were recorded.
        consumed: How many matching interactions the caller has already served.

    Returns:
        ``(index, interaction)`` for the match at the requested offset, or ``None`` when no
        interaction matches at all.
    """
    indices = matching_indices(fp, interactions)
    if not indices:
        return None
    position = len(indices) - 1 if consumed >= len(indices) else max(consumed, 0)
    index = indices[position]
    return (index, interactions[index])


def describe(fp: RequestFingerprint) -> str:
    """Return the request as ``"GET /checkout?page=2"``, for a miss diagnostic.

    The rendering names the method, then the path, then the query -- re-encoded with
    :func:`urllib.parse.urlencode` from the already sorted pairs, so one fingerprint always renders
    the same text and the string stays on one line however a stored value was spelled. The wording
    is kept stable because ``transport.py`` embeds it in the message of
    :class:`foundry.core.exceptions.CassetteMissError`, where the method and the path are what a
    person needs in order to find the request in the cassette.

    Args:
        fp: The request about to be sent.

    Returns:
        The one-line description, with the query omitted when the request carried none.
    """
    target = fp.path or _ORIGIN_PATH
    if not fp.query:
        return f"{fp.method} {target}"
    return f"{fp.method} {target}?{urlencode(fp.query)}"
