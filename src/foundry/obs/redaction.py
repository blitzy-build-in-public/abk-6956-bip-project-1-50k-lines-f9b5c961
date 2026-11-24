"""Redaction engine: the single place where Foundry scrubs secrets out of values.

Every sink that persists or displays data passes its values through a :class:`Redactor` *before*
serialisation (AAP 0.3.2). Standard library only, no state, no I/O, no ``foundry`` import; keys
arrive as names, and a secret is recognised by name or credential shape. Every method is total,
because a redaction fault must never change a test's outcome or an exit code.
"""

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, NamedTuple
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

__all__ = [
    "DEFAULT_HEADER_DENY_LIST",
    "DEFAULT_QUERY_DENY_LIST",
    "MAX_DEPTH",
    "MAX_EXCERPT_BYTES",
    "NESTING_TRUNCATION_MARKER",
    "REDACTED",
    "SECRET_NAME_PATTERN",
    "TRUNCATION_MARKER",
    "Redactor",
    "default_redactor",
]

#: Marker substituted for every redacted value; ``***`` is the form the acceptance criteria require.
REDACTED: Final[str] = "***"

#: Suffix appended to a truncated excerpt. ASCII only, so a character count and a byte budget agree.
TRUNCATION_MARKER: Final[str] = "... [truncated]"

#: Cap on a redacted body excerpt in UTF-8 bytes, the unit the contract fixes for a non-JSON body.
MAX_EXCERPT_BYTES: Final[int] = 200

#: Recursion guard for bodies and for the ``name: value`` pairs a line of text carries, so a cyclic
#: or hostile structure cannot exhaust the stack. A failure message holds many pairs, each one
#: costing a descent, so this is also the depth at which a scan stops and marks what it left.
MAX_DEPTH: Final[int] = 32

#: What a scan leaves where it stopped: text dropped, not a value scrubbed, so the two never read
#: alike. :data:`TRUNCATION_MARKER` is the excerpt form, which says only that the text was cut.
NESTING_TRUNCATION_MARKER: Final[str] = "... [truncated: nesting depth]"

#: Header names that are always redacted; configuration can widen this set but never narrow it.
DEFAULT_HEADER_DENY_LIST: Final[frozenset[str]] = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "x-api-key",
    }
)

#: Query parameter names that are always redacted, in addition to the header deny-list.
DEFAULT_QUERY_DENY_LIST: Final[frozenset[str]] = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "auth",
        "client_secret",
        "key",
        "password",
        "refresh_token",
        "secret",
        "sig",
        "signature",
        "token",
    }
)

#: Names secret by nature, the catch-all for names no deny-list enumerates (``X-Auth-Token``).
SECRET_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?:^|[^a-z0-9])"
    r"(?:access[-_]?token|refresh[-_]?token|authorization|credential[s]?"
    r"|password[s]?|passwd|pwd|secret[s]?|signature|token[s]?|api[-_]?key|auth|key[s]?)"
    r"(?:$|[^a-z0-9])",
    re.IGNORECASE,
)

#: A camelCase boundary, so a JSON key such as ``clientSecret`` matches the same words.
_CAMEL_BOUNDARY_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])"
)

#: ``Bearer``/``Basic`` credentials in free text; six is the floor, so prose like "basic" survives.
_CREDENTIAL_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b(bearer|basic)\s+([A-Za-z0-9\-._~+/]{6,}={0,2})",
    re.IGNORECASE,
)

#: A credential after a scheme the engine resolves: only it is replaced, so its tail stays readable.
_SCHEME_CREDENTIAL_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?i:bearer|basic)\s+(?:\"[^\"]*\"|'[^']*'|\S+)"
)

#: Schemes whose parameters are the credential itself (``Digest username="u", response="..."``).
_PARAMETERISED_SCHEMES: Final[frozenset[str]] = frozenset({"digest", "negotiate", "ntlm"})

#: Deny-listed credential headers, matched anywhere in a line; the value is resolved, not kept.
_SECRET_HEADER_LINE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b(?P<name>authorization|proxy-authorization|x-api-key)(?P<sep>\s*:\s*)(?P<value>[^\r\n]+)",
    re.IGNORECASE,
)

#: Cookie headers keep the whole value: a cookie's pairs need hold no secret word, so no part of it
#: can be resolved. :data:`TRUNCATION_MARKER` says text was dropped.
_COOKIE_HEADER_LINE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b(?P<name>cookie|set-cookie)(?P<sep>\s*:\s*)(?P<value>[^\r\n]+)",
    re.IGNORECASE,
)

#: A ``Name: value`` line, kept safe by checking its captured name with :meth:`Redactor.redacts`.
_HEADER_LINE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?P<name>[A-Za-z0-9][A-Za-z0-9\-_.]{0,63})(?P<sep>[ \t]*:[ \t]*)(?P<value>[^\r\n]+)"
)

#: A URL embedded in free text, so a logged request line is scrubbed like a recorded one; a quoted
#: run inside it is matched whole, and a lone quote after it ends the match rather than joining it.
_URL_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"[A-Za-z][A-Za-z0-9+.\-]*://(?:\"[^\"]*\"|'[^']*'|[^\s\"'<>`\\()])+"
)

#: A ``name=value`` or ``"name": "value"`` pair; the captured name goes to ``redacts``.
#: A quoted value keeps its spaces, and a pair inside ``[...]`` keeps its shape on a re-scan.
_KEY_VALUE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?P<open>[\"']?)"
    r"(?P<name>\b[A-Za-z0-9][A-Za-z0-9_\-]{0,63})"
    r"(?P<close>[\"']?)"
    r"(?P<sep>\s*[=:]\s*)"
    r"(?:"
    r"\"(?P<dquoted>[^\"\r\n]*)\"?"
    r"|'(?P<squoted>[^'\r\n]*)'?"
    r"|(?P<bracketed>\[(?:[^\[\]]|\[[^\[\]]*\])*\]|\{(?:[^{}]|\{[^{}]*\})*\})"
    r"|(?P<bare>[^\"'&\s,;}\]]+)"
    r")"
)

#: A quoted pair-list entry, ``["token", "value"]`` or ``['token', 'value']``: the shape a decoded
#: query or header pair list takes once it is printed or recorded rather than walked structurally.
#: The ``name=value`` pass cannot reach it, a pair carrying no separator, so the value of a pair
#: whose name :meth:`Redactor.redacts` is replaced here while every other pair is left whole. Both
#: quotes are backreferences and the value admits a raw quote only when it is escaped, so the scan
#: stays linear and cannot run across a line break inside the value.
_QUOTED_PAIR_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?P<name_quote>[\"'])(?P<name>[A-Za-z0-9][A-Za-z0-9_.\-]{0,63})(?P=name_quote)"
    r"(?P<sep>[ \t\r\n]{0,8},[ \t\r\n]{0,8})"
    r"(?P<value_quote>[\"'])(?P<value>(?:[^\"'\\\r\n]|\\.)*)(?P=value_quote)"
)

#: Further header lines one multipart part may carry below the line whose ``name`` parameter the
#: scan resolves. RFC 7578 gives a part only MIME headers, so a part a real encoder writes holds
#: ``Content-Disposition`` and at most ``Content-Type``, ``Content-Transfer-Encoding`` and
#: ``Content-ID``; eight covers those with room to spare. The cap is one of the two reasons the scan
#: is linear in the length of the text: a candidate reads at most this many lines below its own and
#: the walk then moves on, so a line is read by at most this many candidates and the whole scan
#: stays a constant factor of the text's length.
_MAX_PART_HEADER_LINES: Final[int] = 8

#: Pre-check for the multipart scan, run twice: once over the whole value, so text that names no
#: part skips the walk before it starts, and once per line, so a line that names none is never
#: sliced. A part header always names its field through a ``name`` parameter, which no other shape
#: holds, so a value this pattern does not find cannot carry a part. The blanks it admits are read
#: once each, at the one ``name`` occurrence that owns them, which keeps the search linear.
_MULTIPART_HINT_PATTERN: Final[re.Pattern[str]] = re.compile(r"name[ \t]*=", re.IGNORECASE)

#: The ``name`` parameter that names a multipart part, matched inside one line: at the line's start
#: or after the ``;`` that separates parameters, so ``filename=`` is never read as a name; the quote
#: is optional and must close the way it opened. A multipart body defeats the ``name=value`` pass
#: twice: the name is a ``Content-Disposition`` parameter, so the value is not on that line, and the
#: value is the part body, which no line-oriented pass joins back to its name. Recognising that this
#: line names a part is all this pattern decides; whether the line is a part header at all -- a
#: blank line below it is the terminator that says so -- is :func:`_part_spans`'s question.
_MULTIPART_NAME_PARAM_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?:^|;)[ \t]*(?i:name)[ \t]*=[ \t]*"
    r"(?P<quote>[\"']?)(?P<name>[A-Za-z0-9][A-Za-z0-9_.\-]{0,63})(?P=quote)"
)

#: The three line breaks the scan treats as one. A multipart body is framed by exactly these, and
#: ``str.splitlines`` would additionally break on ``\v``, ``\f`` and the Unicode separators, which
#: would read a part header terminator into text that holds none.
_LINE_BREAK_PATTERN: Final[re.Pattern[str]] = re.compile(r"\r\n|\n|\r")

#: Re-scan pre-check: without ``=``, ``:`` or ``Bearer``/``Basic`` a span cannot hold a pair.
_NESTED_SCAN_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"[=:]|\b(?:bearer|basic)\b", re.IGNORECASE
)


def _as_text(value: object) -> str:
    """Coerce any value to ``str`` without raising."""
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    try:
        return str(value)
    except (TypeError, ValueError):
        return ""


def _clip_utf8(text: str, budget: int) -> str:
    """Return the longest prefix of ``text`` whose UTF-8 bytes fit ``budget``."""
    encoded = text.encode("utf-8", errors="surrogatepass")
    if len(encoded) <= budget:
        return text
    return encoded[:budget].decode("utf-8", errors="ignore")


def _casefold_names(values: Iterable[str]) -> frozenset[str]:
    """Return the case-folded, non-empty names in ``values``; a lone ``str`` is one name."""
    if isinstance(values, str):
        values = (values,)
    found: set[str] = set()
    try:
        iterator = iter(values)
    except (TypeError, ValueError):
        return frozenset()
    for value in iterator:
        name = _as_text(value).strip().casefold()
        if name:
            found.add(name)
    return frozenset(found)


def _name_value_pairs(source: object) -> list[tuple[str, str]]:
    """Normalise a mapping, a sequence of pairs or a query string to ordered ``str`` pairs."""
    if source is None:
        return []
    if isinstance(source, str):
        return list(parse_qsl(source, keep_blank_values=True))
    if isinstance(source, Mapping):
        return [(_as_text(key), _as_text(value)) for key, value in source.items()]
    if isinstance(source, Iterable):
        pairs: list[tuple[str, str]] = []
        for item in source:
            if isinstance(item, (tuple, list)) and len(item) == 2:
                pairs.append((_as_text(item[0]), _as_text(item[1])))
        return pairs
    return []


def _pair_sort_key(pair: tuple[str, str]) -> tuple[str, str]:
    """Sort key that makes header and query output order stable and case-insensitive."""
    return (pair[0].casefold(), pair[0])


class _PartSpans(NamedTuple):
    """Where the body of a recognised multipart part starts and ends, and where the walk resumes."""

    body_start: int
    body_end: int
    resume: int


def _next_line_end(text: str, start: int) -> tuple[int, int]:
    """Return where the line beginning at ``start`` ends, and where the line below it begins.

    The two are equal for the last line of a text that carries no break after it, which is how a
    caller tells "another line follows" from "the text ends here".
    """
    match = _LINE_BREAK_PATTERN.search(text, start)
    if match is None:
        return len(text), len(text)
    return match.start(), match.end()


def _is_blank_line(content: str) -> bool:
    """Report whether a line holds only spaces and tabs, the form a header terminator takes."""
    return not content.strip(" \t")


def _part_spans(text: str, line_end: int, next_start: int) -> _PartSpans | None:
    """Return the body spans of the part whose ``name`` parameter line ends at ``line_end``.

    ``next_start`` is where the line below that one begins. The part header ends at a blank line --
    two line breaks with only blanks between them, so a blank line carrying trailing spaces still
    ends the headers while a single break does not -- and at most :data:`_MAX_PART_HEADER_LINES`
    further header lines are read looking for that terminator, which is what bounds the work one
    candidate costs. The body then runs from below the blank line to the line opening with ``--``,
    the boundary of the next part, so the break before that boundary stays in place.

    Args:
        text: The whole value being scrubbed; the spans are offsets into it.
        line_end: Where the content of the ``name`` parameter line stops, before its break.
        next_start: Where the first line below it starts.

    Returns:
        The spans of the body and of the position the walk resumes at, or ``None`` when no header
        terminator follows within the cap: the line is then an ordinary ``name=value`` line, which
        the passes after this one own.
    """
    if next_start <= line_end:
        # The ``name`` parameter line is the last one and carries no break, so no part follows it.
        return None
    position = next_start
    consumed = 0
    while True:
        header_end, header_next = _next_line_end(text, position)
        content = text[position:header_end]
        if _is_blank_line(content):
            if header_next <= header_end:
                # A blank line the text ends on closes nothing: a terminator needs two breaks.
                return None
            terminator_end = header_next
            break
        if content.startswith("--") or consumed >= _MAX_PART_HEADER_LINES:
            # A boundary line ends the part's headers, and a part carries a bounded number of them.
            return None
        consumed += 1
        position = header_next
    body_start = terminator_end
    body_end = body_start
    position = body_start
    first_line = True
    while True:
        body_line_end, body_next = _next_line_end(text, position)
        if not first_line and text.startswith("--", position):
            # The break that opens the boundary line is not part of the body it closes.
            return _PartSpans(body_start=body_start, body_end=body_end, resume=position)
        body_end = body_line_end
        first_line = False
        if body_next <= body_line_end:
            return _PartSpans(body_start=body_start, body_end=body_end, resume=body_next)
        position = body_next


@dataclass(frozen=True, slots=True, init=False)
class Redactor:
    """Redact secret values out of headers, query parameters, URLs, text and JSON bodies; pure."""

    _keys: frozenset[str]
    _query_deny: frozenset[str]

    def __init__(
        self,
        redact_keys: Iterable[str] = (),
        *,
        header_deny_list: Iterable[str] = DEFAULT_HEADER_DENY_LIST,
        query_deny_list: Iterable[str] = DEFAULT_QUERY_DENY_LIST,
    ) -> None:
        """Build a redactor from configured keys and the built-in deny-lists, which always apply."""
        keys = _casefold_names(redact_keys) | _casefold_names(header_deny_list)
        object.__setattr__(self, "_keys", keys | DEFAULT_HEADER_DENY_LIST)
        object.__setattr__(
            self, "_query_deny", _casefold_names(query_deny_list) | DEFAULT_QUERY_DENY_LIST
        )

    @property
    def keys(self) -> frozenset[str]:
        """Return every name that is unconditionally redacted, case-folded, deny-lists included."""
        return self._keys | self._query_deny

    def redacts(self, name: str) -> bool:
        """Report whether ``name`` is secret-bearing: key, deny-list entry, pattern or camelCase."""
        raw = _as_text(name).strip()
        folded = raw.casefold()
        if not folded:
            return False
        if folded in self._keys or folded in self._query_deny:
            return True
        if SECRET_NAME_PATTERN.search(folded) is not None:
            return True
        split = _CAMEL_BOUNDARY_PATTERN.sub(" ", raw).casefold()
        return split != folded and SECRET_NAME_PATTERN.search(split) is not None

    def header(self, name: str, value: str) -> str:
        """Return a header value masked by name and then scrubbed as free text.

        A name :meth:`redacts` recognises loses its whole value to :data:`REDACTED`, whatever the
        value is. A name it does not recognise keeps a value that still passes through
        :meth:`text`, because the deny-lists cannot enumerate every header a peer invents: a
        credential echoed back in an unlisted header such as ``x-echo: token=...`` loses its value
        before the header reaches a dump, a cassette or a terminal, which is the AAP 0.3.2
        "Security architecture" rule that one engine stands in front of every sink, and the
        AAP 0.14.3 rule that no secret reaches a file. An ordinary value -- a media type, a date, an
        identifier -- comes back unchanged, and a value already scrubbed comes back as it stands, so
        a sink that re-scans its own output changes nothing. Total, like every method here: a value
        the scrub cannot read becomes the marker rather than an exception (AAP 0.14.3).

        Args:
            name: The header name, in any case.
            value: The header value.

        Returns:
            :data:`REDACTED` for a secret-bearing name, otherwise ``value`` with every free-text
            pattern applied -- which is ``value`` itself when it holds no credential.
        """
        try:
            if self.redacts(name):
                return REDACTED
            return self.text(_as_text(value))
        except (AttributeError, KeyError, TypeError, ValueError):
            return REDACTED

    def headers(self, headers: Mapping[str, str]) -> dict[str, str]:
        """Return a redacted copy of ``headers``, ordered by case-folded name.

        Every value goes through :meth:`header`, so a secret-bearing name renders as the marker and
        a value under any other name is free-text scrubbed rather than trusted: the header a peer
        echoes a credential in is usually one no deny-list names (AAP 0.3.2, AAP 0.14.3). A header
        whose value holds no credential is kept exactly as it was supplied.
        """
        try:
            pairs = _name_value_pairs(headers)
        except (AttributeError, KeyError, TypeError, ValueError):
            return {}
        result: dict[str, str] = {}
        for name, value in sorted(pairs, key=_pair_sort_key):
            result[name] = self.header(name, value)
        return result

    def query(self, params: Mapping[str, str] | Sequence[tuple[str, str]]) -> dict[str, str]:
        """Return a redacted copy of query parameters, ordered by case-folded name.

        Only the name decides here, as the URL-parameter deny-list of AAP 0.3.2 states it: a
        parameter whose name :meth:`redacts` recognises renders as the marker and every other value
        is kept as it was supplied. :meth:`header` is the method that goes further and scrubs a
        value it keeps, and :meth:`text` the one that scrubs a whole line or body.
        """
        result: dict[str, str] = {}
        for name, value in sorted(_name_value_pairs(params), key=_pair_sort_key):
            result[name] = REDACTED if self.redacts(name) else value
        return result

    def url(self, url: str) -> str:
        """Return ``url`` with userinfo dropped and secret query parameters redacted."""
        try:
            candidate = url if isinstance(url, str) else _as_text(url)
        except (AttributeError, KeyError, TypeError, ValueError):
            return REDACTED
        try:
            parts = urlsplit(candidate)
        except ValueError:
            # urlsplit rejects e.g. an unterminated IPv6 literal. Fall back to the textual
            # scrub, which cannot re-enter url() and therefore cannot recurse.
            return self._scrub(candidate, include_urls=False)
        if not parts.scheme and not parts.netloc:
            return self._scrub(candidate, include_urls=False)
        netloc = parts.netloc.rsplit("@", 1)[-1]
        redacted: list[tuple[str, str]] = [
            (name, REDACTED if self.redacts(name) else value)
            for name, value in parse_qsl(parts.query, keep_blank_values=True)
        ]
        ordered: list[tuple[str, str]] = sorted(redacted, key=_pair_sort_key)
        query = urlencode(ordered, doseq=True, safe="*", quote_via=quote)
        fragment = parts.fragment
        if fragment and (
            self.redacts(fragment) or self._scrub(fragment, include_urls=False) != fragment
        ):
            fragment = REDACTED
        return urlunsplit((parts.scheme, netloc, parts.path, query, fragment))

    def text(self, text: str, *, limit: int | None = None) -> str:
        """Scrub secrets out of free text such as a message, a URL or a traceback.

        ``Bearer``/``Basic`` credentials, URLs, secret pairs and header lines lose their values;
        redaction precedes truncation, and ``limit`` is a UTF-8 byte budget splitting no character.
        """
        try:
            source = text if isinstance(text, str) else _as_text(text)
            scrubbed = self._scrub(source)
        except (AttributeError, KeyError, TypeError, ValueError):
            return REDACTED
        if limit is not None:
            budget = max(0, limit)
            clipped = _clip_utf8(scrubbed, budget)
            if clipped != scrubbed or _clip_utf8(source, budget) is not source:
                scrubbed = clipped + TRUNCATION_MARKER
        return scrubbed

    def body_excerpt(self, body: str | bytes, *, limit: int = MAX_EXCERPT_BYTES) -> str:
        """Return a redacted, bounded excerpt of a body; ``limit`` is a UTF-8 byte budget."""
        try:
            if isinstance(body, bytes | bytearray | memoryview):
                source = bytes(body).decode("utf-8", errors="replace")
            else:
                source = body if isinstance(body, str) else _as_text(body)
        except (AttributeError, KeyError, TypeError, ValueError):
            return REDACTED
        return self.text(source, limit=limit)

    def json_value(self, value: object, *, depth: int = 0) -> object:
        """Return a JSON-shaped value with every secret-bearing part redacted."""
        try:
            return self._redact_value(value, depth)
        except (AttributeError, KeyError, TypeError, ValueError):
            return REDACTED

    def mapping(self, data: Mapping[str, object]) -> dict[str, object]:
        """Return a redacted ``dict`` for a JSON mapping, a convenience over ``json_value``."""
        if not isinstance(data, Mapping):
            return {}
        try:
            return self._redact_mapping(data, 0)
        except (AttributeError, KeyError, TypeError, ValueError):
            return self._redact_keys_only(data)

    def _redact_value(self, value: object, depth: int) -> object:
        """Redact one JSON-shaped value, recursing no deeper than :data:`MAX_DEPTH`."""
        if depth >= MAX_DEPTH:
            return REDACTED
        if isinstance(value, Mapping):
            return self._redact_mapping(value, depth)
        if isinstance(value, list | tuple):
            if len(value) == 2 and isinstance(value[0], str) and self.redacts(value[0]):
                # A two-element ``[name, value]`` pair is how a decoded query or header list
                # arrives: the name is the key, so only the value is secret and the name stays
                # readable, which is the form the free-text pair pass produces as well. Any other
                # sequence recurses element by element, so a plain array of two values is untouched.
                return [value[0], REDACTED]
            return [self._redact_value(item, depth + 1) for item in value]
        if isinstance(value, set | frozenset):
            # Sorted so a set cannot make the output order depend on hashing.
            return [self._redact_value(item, depth + 1) for item in sorted(value, key=repr)]
        if isinstance(value, str):
            return self.text(value)
        if value is None or isinstance(value, bool | int | float):
            return value
        return self.text(_as_text(value))

    def _redact_mapping(self, data: Mapping[str, object], depth: int) -> dict[str, object]:
        """Redact a mapping: a secret-bearing key loses its value, every other key recurses."""
        result: dict[str, object] = {}
        for key, item in data.items():
            name = _as_text(key)
            if self.redacts(name):
                result[name] = REDACTED
            else:
                result[name] = self._redact_value(item, depth + 1)
        return result

    def _redact_keys_only(self, data: Mapping[str, object]) -> dict[str, object]:
        """Return every key of ``data`` mapped to the marker, for :meth:`mapping`'s fallback."""
        try:
            return {_as_text(key): REDACTED for key in data}
        except (AttributeError, KeyError, TypeError, ValueError):
            return {}

    def _scrub(self, value: str, *, include_urls: bool = True, nested: int = 0) -> str:
        """Apply every free-text pattern to ``value``; ``include_urls`` is off on a failed parse."""
        if nested > MAX_DEPTH:
            return f"{REDACTED} {NESTING_TRUNCATION_MARKER}"
        if include_urls:
            value = _URL_PATTERN.sub(self._redact_embedded_url, value)
        value = _CREDENTIAL_PATTERN.sub(self._redact_credential_value, value)
        # Before the header-line passes: they own the line that names a multipart part, and a
        # secret-bearing header name there would take the part's body down with it as a value.
        value = self._scrub_multipart_parts(value, nested=nested)
        value = _SECRET_HEADER_LINE_PATTERN.sub(self._redact_known_header_line, value)
        value = _HEADER_LINE_PATTERN.sub(
            lambda match: self._redact_header_line(match, nested=nested), value
        )
        value = _KEY_VALUE_PATTERN.sub(
            lambda match: self._redact_key_value(match, nested=nested), value
        )
        # After the ``name=value`` pass, so a ``"name": "value"`` pair is already resolved and this
        # pass sees only the comma-separated form nothing else owns; before the cookie pass, which
        # stays last.
        value = _QUOTED_PAIR_PATTERN.sub(self._redact_quoted_pair, value)
        # Last, so nothing reduces the value it produced - the truncation marker included - to
        # the single token the pair and header-line passes stop at.
        return _COOKIE_HEADER_LINE_PATTERN.sub(self._redact_cookie_header_line, value)

    def _redact_embedded_url(self, match: re.Match[str]) -> str:
        """Return the redacted form of a URL found in free text."""
        return self.url(match.group(0))

    def _redact_credential_value(self, match: re.Match[str]) -> str:
        """Replace the credential in a ``Bearer``/``Basic`` pair, keeping the scheme."""
        return f"{match.group(1)} {REDACTED}"

    def _scrub_multipart_parts(self, value: str, *, nested: int = 0) -> str:
        """Replace the body of every multipart part named by a secret-bearing ``name`` parameter.

        One left-to-right walk, linear in the length of ``value``: the text is read line by line,
        each line is searched once for the ``; name=`` parameter that names a part, and the lines
        below a candidate are bounded by :data:`_MAX_PART_HEADER_LINES`, so a line is read by at
        most that many candidates and the walk stays a constant factor of the text's length. The
        single regular expression this replaces could not promise that -- it gave back the rest of
        a line at every candidate on it before its attempt failed, which cost time quadratic in the
        length of a line holding many ``name`` parameters.

        The part header is kept as it was written, because it is what tells a reader which field
        lost its body, and so are the further header lines a part may carry. A part whose name is
        not secret keeps its body, but that body is scrubbed in turn, because a multipart body
        nested inside it is a shape no other pass reads; ``nested`` carries the depth of that
        descent, which :data:`MAX_DEPTH` bounds, and the scan then resumes below the body it passed.
        """
        if _MULTIPART_HINT_PATTERN.search(value) is None:
            return value
        pieces: list[str] = []
        copied = 0
        length = len(value)
        position = 0
        while position < length:
            line_end, line_next = _next_line_end(value, position)
            if _MULTIPART_HINT_PATTERN.search(value, position, line_end) is not None:
                # The line is sliced rather than searched in place so that the pattern's ``^`` is
                # the line's own start, which is one of the two positions a part name may take.
                parameter = _MULTIPART_NAME_PARAM_PATTERN.search(value[position:line_end])
                part = None if parameter is None else _part_spans(value, line_end, line_next)
                if parameter is not None and part is not None:
                    body = value[part.body_start : part.body_end]
                    pieces.append(value[copied : part.body_start])
                    if self.redacts(parameter.group("name")):
                        pieces.append(REDACTED)
                    elif _MULTIPART_HINT_PATTERN.search(body) is None:
                        # Kept whole: only the walk's position moves past it.
                        pieces.append(body)
                    else:
                        # Scrubbed in turn, so a part nested inside this one loses its value too.
                        pieces.append(self._scrub(body, include_urls=False, nested=nested + 1))
                    copied = part.body_end
                    position = part.resume
                    continue
            position = line_next
        if not pieces:
            return value
        pieces.append(value[copied:])
        return "".join(pieces)

    def _redact_known_header_line(self, match: re.Match[str]) -> str:
        """Replace the credential of a deny-listed header line, keeping a resolved tail."""
        name = match.group("name")
        return f"{name}{match.group('sep')}{self._redact_header_value(match.group('value'))}"

    def _redact_cookie_header_line(self, match: re.Match[str]) -> str:
        """Replace a cookie header's whole value and mark that text was dropped."""
        return f"{match.group('name')}: {REDACTED} {TRUNCATION_MARKER}"

    def _redact_header_value(self, value: str) -> str:
        """Return the substitute for the value of a secret-bearing header line.

        Only a credential the engine resolves is replaced alone, keeping its diagnostics; the rest
        loses all of it, with :data:`TRUNCATION_MARKER` saying so, and a value that already begins
        with the marker is left as it is, so re-scanning redacted text changes nothing.
        """
        tokens = value.split()
        if tokens and tokens[0] == REDACTED:
            return value
        if tokens and tokens[0].casefold() in _PARAMETERISED_SCHEMES:
            return f"{REDACTED} {TRUNCATION_MARKER}"
        credential = _SCHEME_CREDENTIAL_PATTERN.match(value)
        if credential is not None:
            return f"{REDACTED}{value[credential.end() :]}"
        if len(tokens) <= 1:
            return REDACTED
        return f"{REDACTED} {TRUNCATION_MARKER}"

    def _redact_header_line(self, match: re.Match[str], *, nested: int = 0) -> str:
        """Redact a header line: its value, or the pairs nested inside a value it keeps."""
        name = match.group("name")
        if name.casefold() in DEFAULT_HEADER_DENY_LIST:
            return match.group(0)
        prefix = f"{name}{match.group('sep')}"
        if self.redacts(name):
            return f"{prefix}{self._redact_header_value(match.group('value'))}"
        return self._rescan_value(prefix, match.group("value"), nested=nested)

    def _redact_key_value(self, match: re.Match[str], *, nested: int = 0) -> str:
        """Replace the value of a ``name=value`` or ``"name": "value"`` pair, keeping shape."""
        name = match.group("name")
        prefix = f"{match.group('open')}{name}{match.group('close')}{match.group('sep')}"
        if not self.redacts(name):
            # A value under a key that is not secret keeps its own text, scrubbed in turn: a quoted
            # value is how a credential-bearing node id travels (``"nodeid": "...[token=...]"``).
            return self._rescan_value(prefix, match.group(0)[len(prefix) :], nested=nested)
        if match.group("dquoted") is not None:
            return f'{prefix}"{REDACTED}"'
        if match.group("squoted") is not None:
            return f"{prefix}'{REDACTED}'"
        return f"{prefix}{REDACTED}"

    def _redact_quoted_pair(self, match: re.Match[str]) -> str:
        """Replace the value of a quoted ``["name", "value"]`` pair, keeping the name readable.

        Everything before the value -- the name's quotes, the name itself and the comma that
        separates the two -- is returned as it was written, so a second pass over masked text
        produces it again unchanged.
        """
        if not self.redacts(match.group("name")):
            return match.group(0)
        kept = match.group(0)[: match.start("value") - match.start(0)]
        return f"{kept}{REDACTED}{match.group('value_quote')}"

    def _rescan_value(self, prefix: str, value: str, *, nested: int) -> str:
        """Return ``prefix`` and ``value`` scrubbed again, reaching a pair nested behind it."""
        if _NESTED_SCAN_PATTERN.search(value) is None:
            return prefix + value
        return prefix + self._scrub(value, include_urls=False, nested=nested + 1)


def default_redactor() -> Redactor:
    """Return a redactor carrying only the built-in deny-lists; for sinks that cannot skip."""
    return Redactor()
