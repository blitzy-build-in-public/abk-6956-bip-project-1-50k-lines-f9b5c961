"""Redaction engine: the single place where Foundry scrubs secrets out of values.

Every sink passes its values through a :class:`Redactor` *before* serialisation (AAP 0.3.2), which
recognises a secret by name or shape. Standard library only, no I/O, every method total.
"""

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
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

#: Recursion guard, so a cyclic or hostile structure cannot exhaust the stack.
MAX_DEPTH: Final[int] = 32

#: What a scan leaves where it stopped: text dropped, not a value scrubbed, unlike a masked one.
NESTING_TRUNCATION_MARKER: Final[str] = "... [truncated: nesting depth]"

#: Header names that are always redacted; configuration can widen this set but never narrow it.
DEFAULT_HEADER_DENY_LIST: Final[frozenset[str]] = frozenset(
    {"authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key"}
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

#: Names secret by nature: the catch-all no deny-list enumerates -- ``X-Auth-Token``, a user name.
SECRET_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?:^|[^a-z0-9])"
    r"(?:access[-_]?token|refresh[-_]?token|authorization|credential[s]?"
    r"|password[s]?|passwd|pwd|pass|secret[s]?|signature|token[s]?|api[-_]?key"
    r"|user(?:name)?s?|login|auth|key[s]?)"
    r"(?:$|[^a-z0-9])",
    re.IGNORECASE,
)

#: A camelCase boundary, so a JSON key such as ``clientSecret`` matches the same words.
_CAMEL_BOUNDARY_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])"
)

#: ``Bearer``/``Basic`` credentials in free text; six is the floor, so prose like "basic" survives.
_CREDENTIAL_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b(bearer|basic)\s+([A-Za-z0-9\-._~+/]{6,}={0,2})", re.IGNORECASE
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

#: A cookie header loses its whole value, marked: its pairs hold no word that can be resolved.
_COOKIE_HEADER_LINE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b(?P<name>cookie|set-cookie)(?P<sep>\s*:\s*)(?P<value>[^\r\n]+)", re.IGNORECASE
)

#: A ``Name: value`` line, safe by checking its name with :meth:`Redactor._free_text_redacts`.
_HEADER_LINE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?P<name>[A-Za-z0-9][A-Za-z0-9\-_.]{0,63})(?P<sep>[ \t]*:[ \t]*)(?P<value>[^\r\n]+)"
)

#: A URL in free text: a quoted run joins it only as a ``=`` value, so a pair after it survives.
_URL_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"[A-Za-z][A-Za-z0-9+.\-]*://"
    r"(?:[^\s\"'<>`\\()=]+(?:=(?:\"[^\"]*\"|'[^']*'|[^\s\"'<>`\\()]*))?)+"
)

#: The credential a URL carries before its host, ``scheme://user:pass@``, stripped from free text.
_USERINFO_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*://)(?:[^\s/?#@\"'<>`\\()]+@)+"
)

#: A ``name=value`` or ``"name": "value"`` pair, its name checked by ``_free_text_redacts``.
_KEY_VALUE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?P<open>[\"']?)"
    r"(?P<name>\b[A-Za-z0-9][A-Za-z0-9_.\-]{0,63})"
    r"(?P<close>[\"']?)"
    r"(?P<sep>\s*[=:]\s*)"
    r"(?:"
    r"\"(?P<dquoted>[^\"\r\n]*)\"?"
    r"|'(?P<squoted>[^'\r\n]*)'?"
    r"|(?P<bracketed>\[(?:[^\[\]]|\[[^\[\]]*\])*\]|\{(?:[^{}]|\{[^{}]*\})*\})"
    r"|(?P<bare>[^\"'&\s,;}\]]+)"
    r")"
)

#: A quoted run wrapped around a concatenation or left open by one: a source line's literal.
_LITERAL_TAIL_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[\"'][ \t]*[+%][ \t]*(?:[^\"']*[+%][ \t]*[\"']|[^\"']*$)"
)

#: A quoted pair-list entry, ``["token", "value"]``: its value goes when its name is deny-listed.
_QUOTED_PAIR_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?P<name_quote>[\"'])(?P<name>[A-Za-z0-9][A-Za-z0-9_.\-]{0,63})(?P=name_quote)"
    r"(?P<sep>[ \t\r\n]{0,8},[ \t\r\n]{0,8})"
    r"(?P<value_quote>[\"'])(?P<value>(?:[^\"'\\\r\n]|\\.)*)(?P=value_quote)"
)

#: A pair inside a JSON document embedded in a JSON string: a plain scalar value, level by level.
_ESCAPED_PAIR_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?P<name_quote>\\+\")(?P<name>[A-Za-z0-9][A-Za-z0-9_.\-]{0,63})(?P=name_quote)"
    r"(?P<sep>\s*:\s*)(?P<value_quote>\\+\")(?P<value>[^\"\\]*)(?P=value_quote)"
)

#: A name, a space and a token carrying a digit and a letter; ``token bucket`` keeps its words.
_SPACED_SECRET_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?P<name>\b[A-Za-z][A-Za-z0-9_\-]{0,63})(?P<sep>[ \t]+)"
    r"(?P<value>(?=[\w+=/.\-]*[0-9])(?=[\w+=/.\-]*[A-Za-z])[\w+=/.\-]{12,})"
)

#: Header lines one multipart part may carry: RFC 7578 gives a part MIME headers; eight suffice.
_MAX_PART_HEADER_LINES: Final[int] = 8

#: Pre-check for the multipart scan: a part header names its field through a ``name`` parameter.
_MULTIPART_HINT_PATTERN: Final[re.Pattern[str]] = re.compile(r"name[ \t]*=", re.IGNORECASE)

#: The ``name`` parameter naming a part, matched inside one line so ``filename=`` is never a name.
_MULTIPART_NAME_PARAM_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?:^|;)[ \t]*(?i:name)[ \t]*=[ \t]*"
    r"(?P<quote>[\"']?)(?P<name>[A-Za-z0-9][A-Za-z0-9_.\-]{0,63})(?P=quote)"
)

#: The three line breaks the scan treats as one; ``str.splitlines`` breaks on more than these.
_LINE_BREAK_PATTERN: Final[re.Pattern[str]] = re.compile(r"\r\n|\n|\r")

#: Re-scan pre-check: without ``=``, ``:`` or ``Bearer``/``Basic`` a span cannot hold a pair.
_NESTED_SCAN_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"[=:]|\b(?:bearer|basic)\b", re.IGNORECASE
)

#: Extensions that make a name a file, not a key: masking one loses the location (AAP 0.4.6).
_FILE_EXTENSION_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?i:py|pyi|pyx|json|jsonl|toml|yaml|yml|xml|csv|txt|log|ini|cfg|md|sql|sh|db)\Z"
)

_QUOTED_SCALAR_PATTERN: Final[re.Pattern[str]] = re.compile(r"\"([^\"\r\n]*)\"|'([^'\r\n]*)'")


def _is_name_list(collection: str, denies: Callable[[str], bool]) -> bool:
    """Report whether a rendered collection's entries are deny-listed names (AAP 0.4.5)."""
    scalars = [dquote or squote for dquote, squote in _QUOTED_SCALAR_PATTERN.findall(collection)]
    return bool(scalars) and all(denies(scalar) for scalar in scalars)


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
    """Return where the line at ``start`` ends, and where the line below it begins."""
    match = _LINE_BREAK_PATTERN.search(text, start)
    if match is None:
        return len(text), len(text)
    return match.start(), match.end()


def _part_spans(text: str, line_end: int, next_start: int) -> _PartSpans | None:
    """Return the body spans of the part whose ``name`` parameter line ends at ``line_end``."""
    if next_start <= line_end:
        # The ``name`` parameter line is the last one and carries no break, so no part follows it.
        return None
    position = next_start
    consumed = 0
    while True:
        header_end, header_next = _next_line_end(text, position)
        content = text[position:header_end]
        if not content.strip(" \t"):
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

    def _is_deny_name(self, name: str) -> bool:
        """Report whether ``name`` is a deny-listed name, never a value merely shaped like one."""
        return _as_text(name).strip().casefold() in self.keys

    def _free_text_redacts(self, name: str, value: str = "") -> bool:
        """Report whether a name is secret-bearing in free text; a location names no secret.

        A line or node-id separator after the name keeps a location readable (AAP 0.4.6), so
        ``data/password.csv:12:3`` survives while ``{"db_password.json": "x"}`` masks.
        """
        stem, dot, extension = _as_text(name).rpartition(".")
        if dot and _FILE_EXTENSION_PATTERN.match(extension):
            return not (value[:1].isdigit() or value[:1] == ":") and self.redacts(stem)
        return self.redacts(name)

    def header(self, name: str, value: str) -> str:
        """Return a header value masked by name then scrubbed as free text (AAP 0.3.2, 0.14.3).

        A recognised name loses its value; any other goes through :meth:`text` (AAP 0.14.3).
        """
        try:
            if self.redacts(name):
                return REDACTED
            return self.text(_as_text(value))
        except (AttributeError, KeyError, TypeError, ValueError):
            return REDACTED

    def headers(self, headers: Mapping[str, str]) -> dict[str, str]:
        """Return a redacted copy of ``headers``, ordered by case-folded name (AAP 0.3.2)."""
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

        Only the name decides (AAP 0.3.2); unlike :meth:`header`, another name's value is kept.
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
            # urlsplit rejects e.g. an unterminated IPv6 literal; the textual scrub cannot recurse.
            return self._scrub(candidate, include_urls=False)
        if not parts.scheme and not parts.netloc:
            return self._scrub(candidate, include_urls=False)
        netloc = parts.netloc.rsplit("@", 1)[-1]
        # No port follows the colon, so the authority is userinfo whose ``@`` truncation removed.
        if ":" in netloc and "[" not in netloc and not netloc.rpartition(":")[2].isdigit():
            netloc = REDACTED
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
        """Scrub free text, clipping to ``limit``; redaction precedes truncation (AAP 0.14.3)."""
        return self._scrubbed(text, limit=limit, diagnostic=False)

    def diagnostic(self, text: str, *, limit: int | None = None) -> str:
        """Scrub the values a ``Foundry`` diagnostic carries, and read its own prose as prose.

        AAP 0.4.5 puts the key, its closest match and its range in the message, so only the
        free-text ``Name: value`` inference is skipped. ``limit`` as in :meth:`text`.
        """
        return self._scrubbed(text, limit=limit, diagnostic=True)

    def _scrubbed(self, text: str, *, limit: int | None, diagnostic: bool) -> str:
        """Return ``text`` scrubbed in the named mode, truncated to a UTF-8 byte budget."""
        try:
            source = text if isinstance(text, str) else _as_text(text)
            scrubbed = self._scrub(source, diagnostic=diagnostic)
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
                # A two-element ``[name, value]`` pair from a decoded query or header list: only the
                # value is secret, and any other sequence recurses element by element.
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

    def _scrub(
        self,
        value: str,
        *,
        include_urls: bool = True,
        nested: int = 0,
        diagnostic: bool = False,
    ) -> str:
        """Apply every free-text pattern to ``value``; ``include_urls`` is off on a failed parse."""
        if nested > MAX_DEPTH:
            return f"{REDACTED} {NESTING_TRUNCATION_MARKER}"
        # First, whatever ``include_urls`` says: a URL the splitter rejects loses its userinfo.
        value = _USERINFO_PATTERN.sub(r"\g<scheme>", value)
        if include_urls:
            value = _URL_PATTERN.sub(lambda match: self.url(match.group(0)), value)
        value = _CREDENTIAL_PATTERN.sub(lambda match: f"{match.group(1)} {REDACTED}", value)
        # Before the header-line passes, which own the line naming a multipart part.
        value = self._scrub_multipart_parts(value, nested=nested, diagnostic=diagnostic)
        value = _SECRET_HEADER_LINE_PATTERN.sub(self._redact_known_header_line, value)
        if not diagnostic:
            # A diagnostic's line is prose: ``unknown configuration key: <key>`` names a key.
            value = _HEADER_LINE_PATTERN.sub(
                lambda match: self._redact_header_line(match, nested=nested), value
            )
        value = _KEY_VALUE_PATTERN.sub(
            lambda match: self._redact_key_value(match, nested=nested, diagnostic=diagnostic), value
        )
        # A JSON document embedded in a JSON string is invisible to every pattern above.
        value = _ESCAPED_PAIR_PATTERN.sub(self._redact_escaped_pair, value)
        # Prose separates a name from its token with a space, which only this pass reads.
        value = _SPACED_SECRET_PATTERN.sub(self._redact_spaced_secret, value)
        # After the pair pass that resolved a ``name: value`` pair, and before the cookie pass.
        value = _QUOTED_PAIR_PATTERN.sub(self._redact_quoted_pair, value)
        return _COOKIE_HEADER_LINE_PATTERN.sub(self._redact_cookie_header_line, value)

    def _scrub_multipart_parts(
        self, value: str, *, nested: int = 0, diagnostic: bool = False
    ) -> str:
        """Replace the body of every multipart part named by a secret-bearing ``name`` parameter.

        One left-to-right walk; a part whose name is not secret keeps a scrubbed body in turn.
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
                # Sliced rather than searched in place so the pattern's ``^`` is the line's start.
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
                        pieces.append(
                            self._scrub(
                                body, include_urls=False, nested=nested + 1, diagnostic=diagnostic
                            )
                        )
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
        """Return the substitute for a secret-bearing header line's value (AAP 0.4.5).

        A resolved credential is replaced alone; the rest loses all of it, and a marked value stays.
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
        if self._free_text_redacts(name, match.group("value")):
            return f"{prefix}{self._redact_header_value(match.group('value'))}"
        return self._rescan_value(prefix, match.group("value"), nested=nested)

    def _redact_key_value(
        self, match: re.Match[str], *, nested: int = 0, diagnostic: bool = False
    ) -> str:
        """Replace the value of a ``name=value`` or ``"name": "value"`` pair, keeping shape."""
        name = match.group("name")
        prefix = f"{match.group('open')}{name}{match.group('close')}{match.group('sep')}"
        value = match.group(0)[len(prefix) :]
        if match.group("bare") is None and _LITERAL_TAIL_PATTERN.match(value) is not None:
            # A source line's literal: what follows the separator is an operator and the next
            # literal, not a value, so masking it would drop the code around it.
            return match.group(0)
        # Prose names no credential and an unknown name is no key, so both keep scrubbed text.
        prose = diagnostic and ":" in match.group("sep") and not match.group("open")
        if prose or not self._free_text_redacts(name, value):
            return self._rescan_value(prefix, value, nested=nested, diagnostic=diagnostic)
        if match.group("bracketed") is not None:
            # A collection is published only while every quoted entry is a deny-listed name.
            published = _is_name_list(value, self._is_deny_name)
            return prefix + value if published else f"{prefix}{REDACTED}"
        if match.group("dquoted") is not None:
            return f'{prefix}"{REDACTED}"'
        if match.group("squoted") is not None:
            return f"{prefix}'{REDACTED}'"
        return f"{prefix}{REDACTED}"

    def _redact_spaced_secret(self, match: re.Match[str]) -> str:
        """Replace the token a secret-bearing name introduces with a space, keeping the name."""
        if not self._free_text_redacts(match.group("name"), match.group("value")):
            return match.group(0)
        return f"{match.group('name')}{match.group('sep')}{REDACTED}"

    def _redact_quoted_pair(self, match: re.Match[str]) -> str:
        """Replace the value of a quoted pair-list entry, keeping its name (AAP 0.4.5).

        The text before the value stays as written, so a re-scan changes nothing.
        """
        if _is_name_list(match.group(0), self._is_deny_name) or not self._free_text_redacts(
            match.group("name"), match.group("value")
        ):
            return match.group(0)
        kept = match.group(0)[: match.start("value") - match.start(0)]
        return f"{kept}{REDACTED}{match.group('value_quote')}"

    def _redact_escaped_pair(self, match: re.Match[str]) -> str:
        """Mask the value of a pair inside a JSON document embedded in a JSON string."""
        if not self._free_text_redacts(match.group("name"), match.group("value")):
            return match.group(0)
        kept = match.group(0)[: match.start("value") - match.start(0)]
        return f"{kept}{REDACTED}{match.group('value_quote')}"

    def _rescan_value(
        self, prefix: str, value: str, *, nested: int, diagnostic: bool = False
    ) -> str:
        """Return ``prefix`` and ``value`` scrubbed again, reaching a pair nested behind it."""
        if _NESTED_SCAN_PATTERN.search(value) is None:
            return prefix + value
        return prefix + self._scrub(
            value, include_urls=False, nested=nested + 1, diagnostic=diagnostic
        )


def default_redactor() -> Redactor:
    """Return a redactor carrying only the built-in deny-lists; for sinks that cannot skip."""
    return Redactor()
