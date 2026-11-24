"""Deciding which recorded interaction, if any, is the request about to be sent.

Method, path, sorted query and body hash decide a match, in recording order (AAP 0.2.1, AAP 0.4.6);
the entry point is ``match_interaction`` because AAP 0.7.1's ``match`` is a soft keyword.
:func:`mask_query` is the one query-value rule, applied at capture and at lookup alike, and a
marker on either side matches any value, so masking a parameter costs no recording its replay
(AAP 0.3.2, AAP 0.4.9).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlencode

from foundry.cassette.models import Interaction, RecordedRequest, normalise_query
from foundry.obs.redaction import REDACTED, Redactor

__all__: Final[tuple[str, ...]] = (
    "RequestFingerprint",
    "describe",
    "mask_query",
    "match_interaction",
    "matches_recorded",
    "matching_indices",
)

_ORIGIN_PATH: Final[str] = "/"
"""What :func:`describe` shows when the request carried no path: the origin itself.

Matching is unaffected: :func:`matches_recorded` compares the stored path exactly as it is.
"""


@dataclass(frozen=True, slots=True)
class RequestFingerprint:
    """The part of a request a match is decided by, normalised so that equal requests are equal.

    No host, no port and no headers: that is what lets a recording replay on any machine (assumption
    A7).

    Attributes:
        method: The request method, stripped and upper-cased at construction.
        path: The request path without its query string.
        query: ``(name, value)`` pairs sorted by name and then value; blank values kept.
        body_sha256: Lowercase hexadecimal SHA-256 of the body, or ``None`` when it was empty.
    """

    method: str
    path: str
    query: tuple[tuple[str, str], ...] = ()
    body_sha256: str | None = None

    def __post_init__(self) -> None:
        """Normalise the method and the query, so two spellings of one request compare equal.

        :class:`~foundry.cassette.models.RecordedRequest` normalises itself the same way.
        """
        object.__setattr__(self, "method", self.method.strip().upper())
        object.__setattr__(self, "query", normalise_query(self.query))


def mask_query(
    query: Iterable[tuple[str, str]],
    redactor: Redactor,
    *,
    found: set[str] | None = None,
) -> tuple[tuple[str, str], ...]:
    """Return ``query`` with every secret-bearing value replaced by the redaction marker.

    The one implementation of the query-value rule (AAP 0.2.1, AAP 0.3.2 "Security architecture"),
    called before a cassette is written and before a request is looked up. Because both sides of a
    comparison are masked the same way, a recording of one request still replays it, and the
    document never holds a value whose name the engine redacts.

    Order, duplicates and names are kept exactly as given, so the pairs a normalised query produced
    keep the one order capture and matching compare in, and ``?a=1&a=2`` stays two parameters.

    Applying this twice changes nothing: a masked pair already holds the marker and the engine
    still redacts its name, so re-redacting an interaction yields an equal one.

    Args:
        query: The decoded pairs, normally :func:`~foundry.cassette.models.normalise_query` output.
        redactor: The run's redaction engine. What counts as secret is asked of it and never
            restated here.
        found: The accumulator the scrubbed names are added to, case-folded as
            :func:`foundry.cassette.store.redact_interaction` records header names, so
            ``redacted_keys`` can name a query parameter. ``None`` when only the pairs are wanted.

    Returns:
        The pairs in the order given, each value replaced by
        :data:`~foundry.obs.redaction.REDACTED` when the engine redacts its name.
    """
    masked: list[tuple[str, str]] = []
    for name, value in query:
        if redactor.redacts(name):
            if found is not None:
                found.add(name.strip().casefold())
            masked.append((name, REDACTED))
        else:
            masked.append((name, value))
    return tuple(masked)


def _values_match(left: str, right: str) -> bool:
    """Return whether two query values match: equal, or either of them the redaction marker.

    A masked pair holds the marker instead of the value the client sent, and the two sides of a
    comparison are masked with the engine of their own run, which need not be the same one: a
    cassette recorded before a name became secret-bearing, or under a different
    ``secrets.redact_keys`` set, holds a plaintext value where the lookup holds the marker. Reading
    the marker as a value that matches any value widens matching only -- no request that matched
    before stops matching -- and a narrowing rule would invalidate every cassette already recorded,
    which AAP 0.4.9 forbids without a new major version.
    """
    return left == right or REDACTED in (left, right)


def _queries_match(left: tuple[tuple[str, str], ...], right: tuple[tuple[str, str], ...]) -> bool:
    """Return whether two sorted query lists describe the same parameters, marker-tolerantly.

    A parameter is the same parameter only when its name is the same and it sits at the same
    position: both lists come from
    :func:`~foundry.cassette.models.normalise_query`, so equal parameters are in equal order. A
    length difference is a parameter the recording does not hold, which is another request.
    """
    if len(left) != len(right):
        return False
    return all(
        left_name == right_name and _values_match(left_value, right_value)
        for (left_name, left_value), (right_name, right_value) in zip(left, right, strict=True)
    )


def matches_recorded(fp: RequestFingerprint, recorded: RecordedRequest) -> bool:
    """Return whether ``fp`` is the request that ``recorded`` describes.

    Four fields decide it and no others (AAP 0.2.1, AAP 0.3.2): method, path, sorted query pairs and
    body hash. ``recorded.headers_subset`` is there for a person reading the cassette, and no host
    or port is compared, because the fingerprint carries none (assumption A7). A query value the
    engine masked matches any value (:func:`mask_query`), so a recording made before a
    parameter was masked still replays.

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
    if not _queries_match(fp.query, recorded.query):
        return False
    return fp.body_sha256 == recorded.body_sha256


def matching_indices(
    fp: RequestFingerprint, interactions: Sequence[Interaction]
) -> tuple[int, ...]:
    """Return the positions in ``interactions`` whose request ``fp`` describes.

    The positions are 0-based indexes in document order, one per matching interaction, so a cassette
    holding the same request twice yields two and a caller can tell which recording it is on.

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

    ``consumed`` counts the matches already replayed, so a request issued twice replays in recording
    order. A count at or past the last match answers with the last match, so a re-read of one
    resource replays instead of reporting a miss; a negative count is read as ``0``. The position is
    returned beside the interaction so the transport can advance its cursor without a second scan.

    Returns:
        ``(index, interaction)`` at the requested offset, or ``None`` when nothing matches.
    """
    indices = matching_indices(fp, interactions)
    if not indices:
        return None
    position = len(indices) - 1 if consumed >= len(indices) else max(consumed, 0)
    index = indices[position]
    return (index, interactions[index])


def describe(fp: RequestFingerprint) -> str:
    """Return the request as ``"GET /checkout?page=2"``, for a miss diagnostic.

    The query is re-encoded with :func:`urllib.parse.urlencode` from the already sorted pairs, so
    one fingerprint always renders the same text, and a redaction marker stays readable as ``***``
    because ``urlencode`` is called with ``safe="*"``, the way
    :meth:`foundry.obs.redaction.Redactor.url` renders the same target. ``transport.py`` embeds
    the result in
    :class:`foundry.core.exceptions.CassetteMissError`.

    Returns:
        The one-line description, with the query omitted when the request carried none.
    """
    target = fp.path or _ORIGIN_PATH
    if not fp.query:
        return f"{fp.method} {target}"
    return f"{fp.method} {target}?{urlencode(fp.query, safe='*')}"
