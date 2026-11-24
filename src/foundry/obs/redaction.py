"""Redaction engine: the single place where Foundry scrubs secrets out of values.

Every sink that persists or displays data passes its values through a :class:`Redactor` *before*
serialisation. Standard library only, no state, no I/O, no ``foundry`` import; keys arrive as names.
A secret is recognised by name or credential shape, and every method is total, because a redaction
fault must never change a test's outcome or an exit code.
"""

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

__all__ = [
    "DEFAULT_HEADER_DENY_LIST",
    "DEFAULT_QUERY_DENY_LIST",
    "MAX_DEPTH",
    "MAX_EXCERPT_BYTES",
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

#: Recursion guard for bodies, so a cyclic or hostile structure cannot exhaust the stack.
MAX_DEPTH: Final[int] = 8

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
        """Return a header value, redacted when its name is secret-bearing."""
        try:
            if self.redacts(name):
                return REDACTED
            return _as_text(value)
        except (AttributeError, KeyError, TypeError, ValueError):
            return REDACTED

    def headers(self, headers: Mapping[str, str]) -> dict[str, str]:
        """Return a redacted copy of ``headers``, ordered by case-folded name."""
        try:
            pairs = _name_value_pairs(headers)
        except (AttributeError, KeyError, TypeError, ValueError):
            return {}
        result: dict[str, str] = {}
        for name, value in sorted(pairs, key=_pair_sort_key):
            result[name] = self.header(name, value)
        return result

    def query(self, params: Mapping[str, str] | Sequence[tuple[str, str]]) -> dict[str, str]:
        """Return a redacted copy of query parameters, ordered by case-folded name."""
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
            return REDACTED
        if include_urls:
            value = _URL_PATTERN.sub(self._redact_embedded_url, value)
        value = _CREDENTIAL_PATTERN.sub(self._redact_credential_value, value)
        value = _SECRET_HEADER_LINE_PATTERN.sub(self._redact_known_header_line, value)
        value = _HEADER_LINE_PATTERN.sub(
            lambda match: self._redact_header_line(match, nested=nested), value
        )
        value = _KEY_VALUE_PATTERN.sub(
            lambda match: self._redact_key_value(match, nested=nested), value
        )
        # Last, so nothing reduces the value it produced - the truncation marker included - to
        # the single token the pair and header-line passes stop at.
        return _COOKIE_HEADER_LINE_PATTERN.sub(self._redact_cookie_header_line, value)

    def _redact_embedded_url(self, match: re.Match[str]) -> str:
        """Return the redacted form of a URL found in free text."""
        return self.url(match.group(0))

    def _redact_credential_value(self, match: re.Match[str]) -> str:
        """Replace the credential in a ``Bearer``/``Basic`` pair, keeping the scheme."""
        return f"{match.group(1)} {REDACTED}"

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

    def _rescan_value(self, prefix: str, value: str, *, nested: int) -> str:
        """Return ``prefix`` and ``value`` scrubbed again, reaching a pair nested behind it."""
        if _NESTED_SCAN_PATTERN.search(value) is None:
            return prefix + value
        return prefix + self._scrub(value, include_urls=False, nested=nested + 1)


def default_redactor() -> Redactor:
    """Return a redactor carrying only the built-in deny-lists; for sinks that cannot skip."""
    return Redactor()
