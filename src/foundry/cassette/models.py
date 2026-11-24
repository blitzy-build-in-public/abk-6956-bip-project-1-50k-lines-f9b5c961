"""The persisted shapes of a record/replay cassette.

One JSON document per test: ``recorded_at``, ``redacted_keys`` and ordered ``{request, response}``
entries, with the field names AAP 0.4.6 and AAP 0.7.2 publish; none may be renamed (AAP 0.4.9).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Final
from urllib.parse import parse_qsl

from foundry.core.exceptions import FoundryError
from foundry.core.ids import parse_iso8601, to_iso8601

__all__: Final[tuple[str, ...]] = (
    "CASSETTE_ENCODING",
    "CassetteDocument",
    "Interaction",
    "RecordedRequest",
    "RecordedResponse",
    "body_sha256_of",
    "empty_document",
    "from_json_text",
    "normalise_query",
    "to_json_text",
)

CASSETTE_ENCODING: Final[str] = "utf-8"
"""The encoding a cassette file is written and read with."""

_JSON_INDENT: Final[int] = 2
"""The indentation :func:`to_json_text` writes, so a recorded cassette diffs line by line."""

# The locations an error message names: the document itself, one interaction (with its 1-based
# position appended), or a request or response read outside an interaction.
_DOCUMENT: Final[str] = "cassette document"
_INTERACTION: Final[str] = "cassette interaction"
_REQUEST: Final[str] = "cassette request"
_RESPONSE: Final[str] = "cassette response"

# The next step for a document whose shape is wrong. It never quotes a stored value, which could
# be a secret.
_RECORD_HINT: Final[str] = "re-record the cassette with --foundry-record"


# --- Query and body normalisation ------------------------------------------


def normalise_query(
    value: str | bytes | Mapping[str, str] | Iterable[tuple[str, str]],
) -> tuple[tuple[str, str], ...]:
    """Return a query in any stored form as decoded pairs sorted by name and then value.

    One normaliser serves capture and replay, which is what makes parameter order and
    percent-encoding irrelevant to matching (AAP 0.2.1, AAP 0.4.6).

    Args:
        value: The query as ``httpx.URL.query`` bytes, a query string with or without its leading
            ``?``, a mapping of parameter names to values, or decoded ``(name, value)`` pairs.

    Returns:
        The pairs sorted by name and then value, with blank values kept because ``?flag=`` is a
        present parameter.

    Raises:
        FoundryError: The text cannot be parsed as a query string, or an entry is not a two-element
            sequence of strings. No message quotes the query, which may carry a protected value.
    """
    if isinstance(value, bytes):
        # A query is percent-encoded ASCII in practice; a stray byte becomes U+FFFD rather than
        # raising, so both sides of a comparison normalise it alike and the value stays
        # serialisable for the store.
        return _sorted_pairs(_parsed(value.decode("utf-8", errors="replace")))
    if isinstance(value, str):
        # A caller may hand over ``url.query`` or the part of the URL after the ``?``; without
        # this, ``parse_qsl("?a=1")`` would read the parameter's name as ``"?a"``.
        return _sorted_pairs(_parsed(value[1:] if value.startswith("?") else value))
    if isinstance(value, Mapping):
        return _sorted_pairs(value.items())
    return _sorted_pairs(_entries(value, where="query"))


def body_sha256_of(content: bytes) -> str | None:
    """Return the hexadecimal SHA-256 of a body, or ``None`` when there is no body.

    An empty body is ``None`` rather than the hash of the empty string, so a request with no body
    and one with a zero-length body are the same request.

    Returns:
        The 64-character lowercase hexadecimal digest, or ``None`` when ``content`` is empty.
    """
    if len(content) == 0:
        return None
    return hashlib.sha256(content).hexdigest()


def _parsed(text: str) -> list[tuple[str, str]]:
    """Return the decoded pairs of a query string, keeping blank values.

    Raises:
        FoundryError: The parser rejects the text, which happens only past its field limit. The
            message never quotes the query.
    """
    try:
        return parse_qsl(text, keep_blank_values=True)
    except ValueError as exc:
        raise FoundryError(
            "query string could not be parsed as name/value pairs",
            hint="record the request again, or check the URL the client built",
        ) from exc


def _sorted_pairs(value: Iterable[object]) -> tuple[tuple[str, str], ...]:
    """Return validated query pairs sorted by name and then value.

    Raises:
        FoundryError: An entry is not a two-element sequence of strings.
    """
    return tuple(sorted(_pair(entry, index=index) for index, entry in enumerate(value, start=1)))


def _pair(entry: object, *, index: int) -> tuple[str, str]:
    """Return one query entry as a pair of strings.

    A pair must be a sequence rather than any iterable, because a set of two would not say which
    element is the name and its order varies between runs.

    Raises:
        FoundryError: The entry is not a two-element sequence of strings.
    """
    hint = "pass the decoded parameters, for example [('page', '2')]"
    if isinstance(entry, str | bytes) or not isinstance(entry, Sequence) or len(entry) != 2:
        raise FoundryError(
            f"query entry {index} must be a [name, value] pair, got {_type_name(entry)}", hint=hint
        )
    name, value = entry
    if not isinstance(name, str) or not isinstance(value, str):
        raise FoundryError(
            f"query entry {index} must hold two strings, got {_type_name(name)} and "
            f"{_type_name(value)}",
            hint=hint,
        )
    return (name, value)


# --- Shape guards ----------------------------------------------------------
#
# Run twice: in ``__post_init__``, so a directly built document is normalised like a stored one,
# and in ``from_dict``, so a hand-edited cassette is rejected with a message naming the key rather
# than with a builtin exception (AAP 0.14.3). No message quotes a value.


def _type_name(value: object) -> str:
    """Return the name of a value's type, for use in an error message."""
    return type(value).__name__


def _require_mapping(value: object, *, where: str) -> Mapping[str, object]:
    """Return a value that must be a mapping of names to values."""
    if not isinstance(value, Mapping):
        raise FoundryError(f"{where} must be a mapping, got {_type_name(value)}", hint=_RECORD_HINT)
    return value


def _entries(value: object, *, where: str) -> Iterable[object]:
    """Return a value that must be readable as a list of entries.

    Text and mappings are refused explicitly: ``str`` is iterable, so a bare string would be read
    one entry per character, and a mapping as its keys.
    """
    if isinstance(value, str | bytes | Mapping) or not isinstance(value, Iterable):
        raise FoundryError(f"{where} must be a list, got {_type_name(value)}", hint=_RECORD_HINT)
    return value


def _require_value(
    data: Mapping[str, object], key: str, *, where: str, verb: str = "is missing"
) -> object:
    """Return the value stored under ``key``, naming the key when it is absent.

    ``verb`` is how the message reads: a nested object passes ``has no``.
    """
    if key not in data:
        raise FoundryError(f"{where} {verb} {key!r}", hint=_RECORD_HINT)
    return data[key]


def _require_object(
    data: Mapping[str, object], key: str, *, where: str, verb: str = "has no"
) -> Mapping[str, object]:
    """Return the nested object stored under ``key``."""
    return _require_mapping(
        _require_value(data, key, where=where, verb=verb), where=f"{where} {key!r}"
    )


def _require_str(data: Mapping[str, object], key: str, *, where: str) -> str:
    """Return the string stored under ``key``."""
    return _require_text(_require_value(data, key, where=where), where=f"{where} {key!r}")


def _optional_str(data: Mapping[str, object], key: str, *, where: str) -> str | None:
    """Return the string stored under ``key``, or ``None`` when it is absent or null.

    An absent key and an explicit ``null`` mean the same thing for an optional field.
    """
    value = data.get(key)
    if value is None:
        return None
    return _require_text(value, where=f"{where} {key!r}")


def _require_text(value: object, *, where: str) -> str:
    """Return a value that must be plain text."""
    if not isinstance(value, str):
        raise FoundryError(f"{where} must be a string, got {_type_name(value)}", hint=_RECORD_HINT)
    return value


def _frozen_method(value: object) -> str:
    """Return a request method upper-cased and stripped of surrounding whitespace.

    Upper-casing at construction makes ``get`` and ``GET`` record the same way, so a replay matches
    a recording made by a differently spelled client.
    """
    if not isinstance(value, str) or not value.strip():
        raise FoundryError(
            f"{_REQUEST} 'method' must be a non-empty string, got {_type_name(value)}",
            hint="record the method the client sent, for example 'GET'",
        )
    return value.strip().upper()


def _frozen_status(value: object) -> int:
    """Return a recorded HTTP status, refusing a boolean.

    The status is whatever the peer sent, so it is not range-checked; ``bool`` is refused because
    ``True`` is an ``int`` in Python.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise FoundryError(
            f"{_RESPONSE} 'status' must be an integer, got {_type_name(value)}", hint=_RECORD_HINT
        )
    return value


def _frozen_moment(value: object, *, where: str) -> datetime:
    """Return a datetime at the precision a cassette stores it, timezone-aware and in UTC.

    A cassette stores milliseconds (:func:`foundry.core.ids.to_iso8601`), so the value is truncated
    through the same writer and reader a stored document uses and a round trip is lossless.
    """
    if not isinstance(value, datetime):
        raise FoundryError(
            f"{where} must be a datetime, got {_type_name(value)}",
            hint="pass foundry.core.ids.utc_now() or another timezone-aware datetime",
        )
    return parse_iso8601(to_iso8601(value))


def _frozen_headers(value: object, *, where: str) -> Mapping[str, str]:
    """Return a header mapping as a read-only copy ordered by name.

    The copy keeps a captured request unalterable, and ordering the keys here keeps direct iteration
    deterministic too (AAP 0.13.1).
    """
    frozen: dict[str, str] = {}
    for name, header in _require_mapping(value, where=where).items():
        if not isinstance(name, str) or not isinstance(header, str):
            raise FoundryError(
                f"{where} must map header names to string values, got {_type_name(header)}",
                hint=_RECORD_HINT,
            )
        frozen[name] = header
    return MappingProxyType({name: frozen[name] for name in sorted(frozen)})


def _frozen_names(value: object, *, where: str) -> tuple[str, ...]:
    """Return the redacted key names, sorted and de-duplicated.

    A redactor reports the names it scrubbed as an unordered set, so the document sorts them at
    construction (AAP 0.13.1).
    """
    names: set[str] = set()
    for entry in _entries(value, where=where):
        if not isinstance(entry, str) or not entry.strip():
            raise FoundryError(
                f"{where} must hold non-blank strings, got {_type_name(entry)}", hint=_RECORD_HINT
            )
        names.add(entry.strip())
    return tuple(sorted(names))


def _frozen_interactions(value: object, *, where: str) -> tuple[Interaction, ...]:
    """Return the interactions as a tuple, in the order they were recorded.

    Replay serves them in the order the test issued them: a test that creates an entity and then
    reads it back needs both, in that sequence (AAP 0.4.6).
    """
    interactions: list[Interaction] = []
    for index, entry in enumerate(_entries(value, where=where), start=1):
        if not isinstance(entry, Interaction):
            raise FoundryError(
                f"{where} entry {index} must be an Interaction, got {_type_name(entry)}",
                hint=_RECORD_HINT,
            )
        interactions.append(entry)
    return tuple(interactions)


def _stored_interaction(entry: object, *, index: int) -> Interaction:
    """Return one stored interaction, naming its 1-based position in an error message."""
    where = f"{_INTERACTION} {index}"
    return Interaction.from_dict(_require_mapping(entry, where=where), where=where)


# --- RecordedRequest -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RecordedRequest:
    """The request half of one recorded interaction (AAP 0.4.6, AAP 0.7.2).

    Only what matching needs is stored; ``headers_subset`` is kept for a reader and never matched,
    because a header the client adds for itself would make a recording unplayable elsewhere.

    Attributes:
        method: The request method, upper-cased at construction so ``get`` and ``GET`` record alike.
        path: The request path without its query string, as the recorded URL carried it.
        query: The decoded pairs sorted by name and then value, with blank values kept.
        body_sha256: The lowercase hexadecimal SHA-256 of the body, or ``None`` when it was empty.
        headers_subset: The kept request headers, every secret-bearing value already redacted.
    """

    method: str
    path: str
    query: tuple[tuple[str, str], ...] = ()
    body_sha256: str | None = None
    headers_subset: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Normalise the method and freeze the query and the headers.

        A directly built request is normalised exactly as a stored one is, so ``as_dict`` ->
        ``from_dict`` round-trips.
        """
        object.__setattr__(self, "method", _frozen_method(self.method))
        object.__setattr__(self, "path", _require_text(self.path, where=f"{_REQUEST} 'path'"))
        object.__setattr__(self, "query", normalise_query(self.query))
        object.__setattr__(
            self,
            "headers_subset",
            _frozen_headers(self.headers_subset, where=f"{_REQUEST} 'headers_subset'"),
        )
        if self.body_sha256 is not None:
            object.__setattr__(
                self,
                "body_sha256",
                _require_text(self.body_sha256, where=f"{_REQUEST} 'body_sha256'"),
            )

    def as_dict(self) -> dict[str, object]:
        """Return this request as the ``request`` object of a cassette document (AAP 0.4.6).

        ``query`` becomes a list of two-element lists and ``headers_subset`` an object of sorted
        keys.
        """
        return {
            "method": self.method,
            "path": self.path,
            "query": [[name, value] for name, value in self.query],
            "body_sha256": self.body_sha256,
            "headers_subset": dict(self.headers_subset),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object], *, where: str = _REQUEST) -> RecordedRequest:
        """Build a request from the ``request`` object of a cassette document.

        Keys beyond the documented five are ignored, so a cassette written by a later release still
        loads (AAP 0.4.9). ``where`` names the location in an error message.

        Raises:
            FoundryError: A documented key is missing, the object is not a mapping, the query is not
            a list
                of pairs or a header is not a string.
        """
        fields = _require_mapping(data, where=where)
        method = _require_str(fields, "method", where=where)
        path = _require_str(fields, "path", where=where)
        query = _entries(_require_value(fields, "query", where=where), where=f"{where} 'query'")
        body_sha256 = _optional_str(fields, "body_sha256", where=where)
        headers = _frozen_headers(
            _require_value(fields, "headers_subset", where=where),
            where=f"{where} 'headers_subset'",
        )
        return cls(
            method=method,
            path=path,
            query=_sorted_pairs(query),
            body_sha256=body_sha256,
            headers_subset=headers,
        )


# --- RecordedResponse ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RecordedResponse:
    """The response half of one recorded interaction (AAP 0.4.6, AAP 0.7.2).

    The body is stored as text, because every assertion a cassette serves reads text and it is the
    body as the peer sent it: a recorded 500 replays as a 500.

    Attributes:
        status: The status the peer sent.
        headers: The response headers, sorted by name and read-only.
        body: The response body as text, empty when the peer sent none.
    """

    status: int
    headers: Mapping[str, str] = field(default_factory=dict)
    body: str = ""

    def __post_init__(self) -> None:
        """Check the status and the body and freeze the headers."""
        object.__setattr__(self, "status", _frozen_status(self.status))
        object.__setattr__(
            self, "headers", _frozen_headers(self.headers, where=f"{_RESPONSE} 'headers'")
        )
        object.__setattr__(self, "body", _require_text(self.body, where=f"{_RESPONSE} 'body'"))

    def as_dict(self) -> dict[str, object]:
        """Return this response as the ``response`` object of a cassette document (AAP 0.4.6)."""
        return {"status": self.status, "headers": dict(self.headers), "body": self.body}

    @classmethod
    def from_dict(cls, data: Mapping[str, object], *, where: str = _RESPONSE) -> RecordedResponse:
        """Build a response from the ``response`` object of a cassette document.

        Raises:
            FoundryError: ``status``, ``headers`` or ``body`` is missing or of the wrong type.
        """
        fields = _require_mapping(data, where=where)
        return cls(
            status=_frozen_status(_require_value(fields, "status", where=where)),
            headers=_frozen_headers(
                _require_value(fields, "headers", where=where), where=f"{where} 'headers'"
            ),
            body=_require_str(fields, "body", where=where),
        )


# --- Interaction -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Interaction:
    """One request and the response it received, as one entry of a cassette (AAP 0.4.6).

    Attributes:
        request: The request as it was issued.
        response: The response the peer returned for it.
    """

    request: RecordedRequest
    response: RecordedResponse

    def __post_init__(self) -> None:
        """Reject a half-built interaction.

        Passing the raw stored mappings instead of the two models is the mistake this catches.
        """
        if not isinstance(self.request, RecordedRequest):
            raise FoundryError(
                f"{_INTERACTION} 'request' must be a RecordedRequest, got"
                f" {_type_name(self.request)}",
                hint="build it with RecordedRequest.from_dict(...)",
            )
        if not isinstance(self.response, RecordedResponse):
            raise FoundryError(
                f"{_INTERACTION} 'response' must be a RecordedResponse, got"
                f" {_type_name(self.response)}",
                hint="build it with RecordedResponse.from_dict(...)",
            )

    def as_dict(self) -> dict[str, object]:
        """Return the interaction as the ``{request, response}`` object AAP 0.4.6 fixes."""
        return {"request": self.request.as_dict(), "response": self.response.as_dict()}

    @classmethod
    def from_dict(cls, data: Mapping[str, object], *, where: str = _INTERACTION) -> Interaction:
        """Build an interaction from one entry of the document's ``interactions`` list.

        Raises:
            FoundryError: ``request`` or ``response`` is missing or is not an object, or a half
            holds a key
                of the wrong type.
        """
        fields = _require_mapping(data, where=where)
        return cls(
            request=RecordedRequest.from_dict(
                _require_object(fields, "request", where=where), where=f"{where} 'request'"
            ),
            response=RecordedResponse.from_dict(
                _require_object(fields, "response", where=where), where=f"{where} 'response'"
            ),
        )


# --- CassetteDocument ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CassetteDocument:
    """One test's recorded interactions, and what was redacted from them (AAP 0.4.6, AAP 0.7.2).

    Attributes:
        recorded_at: When the cassette was written, timezone-aware UTC at millisecond precision.
        redacted_keys: The names whose values were scrubbed at capture, sorted and de-duplicated.
        interactions: One :class:`Interaction` per recorded request, in recording order.
    """

    recorded_at: datetime
    redacted_keys: tuple[str, ...] = ()
    interactions: tuple[Interaction, ...] = ()

    def __post_init__(self) -> None:
        """Truncate the timestamp to the stored precision and freeze the two collections."""
        object.__setattr__(
            self,
            "recorded_at",
            _frozen_moment(self.recorded_at, where=f"{_DOCUMENT} 'recorded_at'"),
        )
        object.__setattr__(
            self,
            "redacted_keys",
            _frozen_names(self.redacted_keys, where=f"{_DOCUMENT} 'redacted_keys'"),
        )
        object.__setattr__(
            self,
            "interactions",
            _frozen_interactions(self.interactions, where=f"{_DOCUMENT} 'interactions'"),
        )

    def as_dict(self) -> dict[str, object]:
        """Return the document as the JSON object AAP 0.4.6 fixes.

        The keys are ``recorded_at``, ``redacted_keys`` and ``interactions``, and there are no
        others.
        """
        return {
            "recorded_at": to_iso8601(self.recorded_at),
            "redacted_keys": list(self.redacted_keys),
            "interactions": [interaction.as_dict() for interaction in self.interactions],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> CassetteDocument:
        """Build a document from a stored cassette object.

        Keys beyond the documented three are ignored, so a cassette written by a later release still
        loads (AAP 0.4.9).

        Raises:
            FoundryError: A documented key is missing, ``recorded_at`` is not an ISO 8601 timestamp,
                ``redacted_keys`` is not a list of names, or an interaction is malformed. That error
                names the interaction's 1-based position.
        """
        fields = _require_mapping(data, where=_DOCUMENT)
        recorded_at = parse_iso8601(_require_str(fields, "recorded_at", where=_DOCUMENT))
        names = _entries(
            _require_value(fields, "redacted_keys", where=_DOCUMENT),
            where=f"{_DOCUMENT} 'redacted_keys'",
        )
        entries = _entries(
            _require_value(fields, "interactions", where=_DOCUMENT),
            where=f"{_DOCUMENT} 'interactions'",
        )
        return cls(
            recorded_at=recorded_at,
            redacted_keys=_frozen_names(names, where=f"{_DOCUMENT} 'redacted_keys'"),
            interactions=tuple(
                _stored_interaction(entry, index=index)
                for index, entry in enumerate(entries, start=1)
            ),
        )


# --- Document helpers ------------------------------------------------------


def empty_document(recorded_at: datetime) -> CassetteDocument:
    """Return a document with nothing recorded yet.

    What a replay reads when the cassette holds none: a present but empty cassette is the recording
    of a test that made no request, which is not the same as a missing file.
    """
    return CassetteDocument(recorded_at=recorded_at)


def to_json_text(document: CassetteDocument) -> str:
    """Return the JSON text a cassette file holds, without touching the filesystem.

    Keys are sorted and non-ASCII text is left readable, so two runs of one recording produce
    identical bytes; the text is indented by :data:`_JSON_INDENT` and ends with one newline.
    """
    rendered = json.dumps(
        document.as_dict(), indent=_JSON_INDENT, sort_keys=True, ensure_ascii=False
    )
    return f"{rendered}\n"


def from_json_text(text: str) -> CassetteDocument:
    """Read the JSON text of a cassette file into a document, without touching the filesystem.

    Raises:
        FoundryError: The text is not valid JSON, or the object it holds is not a cassette. A syntax
            error is reported with its line and column, and no message quotes the content.
    """
    try:
        loaded: object = json.loads(text)
    except json.JSONDecodeError as exc:
        raise FoundryError(
            f"cassette document is not valid JSON: {exc.msg} at line {exc.lineno}"
            f" column {exc.colno}",
            hint=_RECORD_HINT,
        ) from exc
    return CassetteDocument.from_dict(_require_mapping(loaded, where=_DOCUMENT))
