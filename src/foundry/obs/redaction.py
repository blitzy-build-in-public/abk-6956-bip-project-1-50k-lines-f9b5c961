"""Redaction engine: the single place where Foundry scrubs secrets out of values.

Every sink that persists or displays data - the artifact writer, the cassette store,
``results.json``, ``junit.xml``, ``report.html`` and the CLI renderer - passes its values
through a :class:`Redactor` *before* serialisation, because the tooling that produces them
would otherwise write headers, URLs and bodies verbatim.

The module is deliberately plain: standard library only, no state, no I/O, no logging and no
``foundry`` import. Configuration reaches it as a plain iterable of key names
(``secrets.redact_keys``), which is what keeps the configuration layer out of this package.
Every method is total: it tolerates ``None``, a non-``str``, a sequence of pairs where a
mapping was expected, and a self-referential structure, returning a redacted value instead
of raising, because a redaction fault must never change a test outcome or an exit code.

Redaction is name- and pattern-driven: a secret is recognised by the name that carries it (a
header name, a JSON key, a query parameter) or by the shape of a credential in text
(``Bearer ...``, ``token=...``). A bare value that arrives with neither cannot be identified
as one, which is why credentials travel in headers rather than in a payload.

Typical use::

    redactor = Redactor(["x-internal-key"])
    payload = redactor.mapping({"token": "abc", "items": [{"password": "p"}]})
    excerpt = redactor.body_excerpt(response_bytes, limit=200)
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

#: Marker substituted for every redacted value. ``***`` is the exact form the acceptance
#: criteria require in ``report.html``, ``results.json``, ``junit.xml`` and console output.
REDACTED: Final[str] = "***"

#: Suffix appended to a truncated excerpt. ASCII only, so a caller comparing a character
#: count against a byte budget gets the same answer either way.
TRUNCATION_MARKER: Final[str] = "... [truncated]"

#: Cap on a redacted body excerpt, in UTF-8 bytes. The ``assert_status`` message and the
#: ``ContractError`` raised by ``assert_json_schema`` both quote at most this many bytes, which is
#: the unit the contract fixes for the body of a non-JSON response.
MAX_EXCERPT_BYTES: Final[int] = 200

#: Recursion guard for request and response bodies, so a cyclic or hostile structure cannot
#: exhaust the stack.
MAX_DEPTH: Final[int] = 8

#: Header names that are always redacted. Configuration can widen this set but never narrow
#: it: ``Redactor([])`` still redacts ``Authorization``.
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

#: Names that are secret by nature, used as the catch-all for names the fixed deny-lists do
#: not enumerate (``X-Auth-Token``, ``client_secret``, ``X-Amz-Signature``). The surrounding
#: character class keeps a name component from matching inside a longer word, so ``monkey``
#: is not treated as a key and ``author`` is not treated as auth.
SECRET_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?:^|[^a-z0-9])"
    r"(?:access[-_]?token|refresh[-_]?token|authorization|credential[s]?"
    r"|password[s]?|passwd|pwd|secret[s]?|signature|token[s]?|api[-_]?key|auth|key[s]?)"
    r"(?:$|[^a-z0-9])",
    re.IGNORECASE,
)

#: A camelCase or PascalCase name boundary, so a JSON key such as ``clientSecret`` or
#: ``APIKey`` is matched against the same words :data:`SECRET_NAME_PATTERN` knows.
_CAMEL_BOUNDARY_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])"
)

#: ``Bearer``/``Basic`` credentials in free text. Six characters is the floor so that prose
#: such as "basic usage" survives; real credentials are far longer, and the header-line
#: patterns below cover the credential in its usual ``Authorization: ...`` form.
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

#: The credential headers of the fixed deny-list, matched anywhere in a line so that
#: ``sent Authorization: ...`` is caught too. The value is captured whole and
#: :meth:`Redactor._redact_header_value` resolves how much of it is the credential.
_SECRET_HEADER_LINE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b(?P<name>authorization|proxy-authorization|x-api-key)(?P<sep>\s*:\s*)(?P<value>[^\r\n]+)",
    re.IGNORECASE,
)

#: Cookie headers keep the whole value: a cookie carries several pairs whose names need hold no
#: secret word, so no part of it can be resolved. :data:`TRUNCATION_MARKER` says text was dropped.
_COOKIE_HEADER_LINE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b(?P<name>cookie|set-cookie)(?P<sep>\s*:\s*)(?P<value>[^\r\n]+)",
    re.IGNORECASE,
)

#: A generic ``Name: value`` line, kept safe by checking the captured name with
#: :meth:`Redactor.redacts` before replacing anything; it catches names the fixed list does not
#: enumerate (``X-Auth-Token: ...``) and keeps theirs, which is then re-scanned in turn.
_HEADER_LINE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?P<name>[A-Za-z0-9][A-Za-z0-9\-_.]{0,63})(?P<sep>[ \t]*:[ \t]*)(?P<value>[^\r\n]+)"
)

#: A URL embedded in free text, so a logged request line is scrubbed the same way a recorded
#: request is. A run of characters is matched, or a quoted run inside the URL, so a query value
#: written as ``token="..."`` is parsed and redacted with the rest of the query; a lone quote
#: after the URL - prose punctuation, or the closing quote of a string literal - ends the match
#: instead of being absorbed into the path.
_URL_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"[A-Za-z][A-Za-z0-9+.\-]*://(?:\"[^\"]*\"|'[^']*'|[^\s\"'<>`\\()])+"
)

#: A ``name=value`` or ``"name": "value"`` pair. The name is captured rather than enumerated,
#: so :meth:`Redactor.redacts` decides. A quoted value is matched whole - including any spaces
#: inside it and in either quote style, which is what a Python ``repr`` of a dict uses - because
#: a value cut at the first space would leave its tail in the output. A bracketed value is
#: matched whole too. An unquoted one ends at the first delimiter, the closing bracket included,
#: so a pair written inside ``[...]`` keeps the structure around it when it is re-scanned.
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
    """Return the longest prefix of ``text`` whose UTF-8 encoding fits ``budget`` bytes.

    A character slice is not a byte bound: one CJK character costs three bytes, so an excerpt
    counted in characters can exceed a byte cap threefold. The cut is made on the encoded bytes and
    decoded back with the trailing partial character discarded, which keeps the result valid text.

    Args:
        text: The text to cut.
        budget: The byte budget, already clamped to zero or more.

    Returns:
        ``text`` when it fits, else its longest byte-bounded prefix, cut with ``surrogatepass``.
    """
    encoded = text.encode("utf-8", errors="surrogatepass")
    if len(encoded) <= budget:
        return text
    return encoded[:budget].decode("utf-8", errors="ignore")


def _casefold_names(values: Iterable[str]) -> frozenset[str]:
    """Return the case-folded, stripped, non-empty names in ``values``.

    A lone ``str`` is treated as one name rather than as a sequence of characters, so a
    caller that passes a single configured key keeps it.
    """
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
    """Redact secret values out of headers, query parameters, URLs, text and JSON bodies.

    A redactor is immutable once constructed and every method is pure, so one instance serves
    a whole run: ``foundry.execution.session`` builds it once from ``secrets.redact_keys`` and
    hands it to the sinks instead of building one per record.

    The built-in header and query deny-lists are always applied. Configuration can only widen
    the set, never narrow it, so ``Redactor([])`` still redacts ``Authorization``.

    Attributes:
        _keys: Case-folded header and JSON key names that are always redacted.
        _query_deny: Case-folded query parameter names that are always redacted.
    """

    _keys: frozenset[str]
    _query_deny: frozenset[str]

    def __init__(
        self,
        redact_keys: Iterable[str] = (),
        *,
        header_deny_list: Iterable[str] = DEFAULT_HEADER_DENY_LIST,
        query_deny_list: Iterable[str] = DEFAULT_QUERY_DENY_LIST,
    ) -> None:
        """Build a redactor from configured keys and the built-in deny-lists.

        Args:
            redact_keys: Names to redact, from ``secrets.redact_keys``. It holds both header
                names and JSON keys and is matched case-insensitively.
            header_deny_list: Extra header names to redact. The built-in header deny-list is
                unioned in, so this argument can only widen the set.
            query_deny_list: Extra query parameter names to redact. The built-in query
                deny-list is unioned in as well.
        """
        keys = _casefold_names(redact_keys) | _casefold_names(header_deny_list)
        object.__setattr__(self, "_keys", keys | DEFAULT_HEADER_DENY_LIST)
        object.__setattr__(
            self, "_query_deny", _casefold_names(query_deny_list) | DEFAULT_QUERY_DENY_LIST
        )

    @property
    def keys(self) -> frozenset[str]:
        """Every name that is unconditionally redacted, case-folded.

        The set covers the configured keys, the built-in header deny-list and the built-in
        query deny-list. A name outside it can still be redacted by
        :data:`SECRET_NAME_PATTERN`, which is why :meth:`redacts` is the question a caller
        should ask.
        """
        return self._keys | self._query_deny

    def redacts(self, name: str) -> bool:
        """Report whether ``name`` is a secret-bearing name.

        Args:
            name: A header name, JSON key, query parameter name or URL fragment.

        Returns:
            True when the case-folded name is a configured key, a built-in header or query
            deny-list entry, or matches :data:`SECRET_NAME_PATTERN` as a word
            (``token``, ``client_secret``, ``X-Amz-Signature``). A camelCase key is matched
            with its boundaries split as well, so ``clientSecret`` counts as ``client_secret``.
        """
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
        """Return the value of a single header, redacted when its name is secret-bearing.

        Args:
            name: The header name.
            value: The header value.

        Returns:
            :data:`REDACTED` when ``name`` is secret-bearing, otherwise ``value`` coerced to
            ``str`` and returned unchanged.
        """
        try:
            if self.redacts(name):
                return REDACTED
            return _as_text(value)
        except (AttributeError, KeyError, TypeError, ValueError):
            return REDACTED

    def headers(self, headers: Mapping[str, str]) -> dict[str, str]:
        """Return a redacted copy of ``headers``, ordered by case-folded name.

        Args:
            headers: A mapping of header names to values, or a sequence of ``(name, value)``
                pairs. The argument itself is never mutated.

        Returns:
            A new ``dict`` whose keys are ordered by case-folded name, so two identical calls
            serialise identically.
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

        Args:
            params: A mapping, a sequence of ``(name, value)`` pairs, or a raw query string.

        Returns:
            A new ``dict`` with secret-bearing values replaced by :data:`REDACTED` and keys
            ordered by case-folded name.
        """
        result: dict[str, str] = {}
        for name, value in sorted(_name_value_pairs(params), key=_pair_sort_key):
            result[name] = REDACTED if self.redacts(name) else value
        return result

    def url(self, url: str) -> str:
        """Return ``url`` with userinfo dropped and secret query parameters redacted.

        User information is removed rather than replaced with the marker: ``https://host/x``
        is a valid URL and unmistakably credential-free, while ``https://***@host/x`` is not.
        Denied query values become :data:`REDACTED`, the query is re-emitted in a stable order
        with :func:`urllib.parse.urlencode`, and the fragment is replaced wholesale when it
        carries a secret. A relative or unparseable string is only scrubbed as text.

        Args:
            url: The URL to redact. Anything that is not a ``str`` is coerced with ``str``.

        Returns:
            The redacted URL, or the text-scrubbed input when it cannot be parsed as one.
        """
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

        ``Bearer``/``Basic`` credentials, URLs embedded in the text, and ``name=value`` or
        ``"name": "value"`` pairs whose name is secret-bearing all have their values replaced
        with :data:`REDACTED`.

        A header line is treated according to what its value is: a credential header keeps the
        diagnostics behind a credential the engine can resolve and otherwise loses its whole value,
        ``Cookie``/``Set-Cookie`` and a parameterised scheme lose theirs, and a name that is not
        secret-bearing keeps its text, which is scrubbed in turn.

        Redaction always runs before truncation: cutting first would leave the prefix of a
        secret that straddles the cut in the excerpt. The :data:`TRUNCATION_MARKER` is
        appended whenever the *source* text was longer than ``limit``, even if redaction
        collapsed it, so a caller can still tell that the excerpt is partial.

        Args:
            text: The text to scrub. Anything that is not a ``str`` is coerced with ``str``.
            limit: Optional budget, in UTF-8 bytes, for the returned excerpt.

        Returns:
            The scrubbed text, cut to ``limit`` bytes without splitting a character and with the
            marker appended when the source was longer.
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
        """Return a redacted, bounded excerpt of a request or response body.

        Every message that quotes a body calls this instead of slicing the raw body, so the
        excerpt cap of the ``assert_status`` message and of ``ContractError`` (raised by
        ``assert_json_schema``) is applied by one implementation and never leaks a secret.
        ``bytes`` are decoded as UTF-8 with ``errors="replace"`` so a binary body yields text
        rather than an exception.

        Args:
            body: The body as text or bytes. Any other value is coerced with ``str``.
            limit: Byte budget for the excerpt, :data:`MAX_EXCERPT_BYTES` by default.

        Returns:
            The redacted excerpt, at most ``limit`` UTF-8 bytes plus :data:`TRUNCATION_MARKER`.
        """
        try:
            if isinstance(body, bytes | bytearray | memoryview):
                source = bytes(body).decode("utf-8", errors="replace")
            else:
                source = body if isinstance(body, str) else _as_text(body)
        except (AttributeError, KeyError, TypeError, ValueError):
            return REDACTED
        return self.text(source, limit=limit)

    def json_value(self, value: object, *, depth: int = 0) -> object:
        """Return a JSON-shaped value with every secret-bearing part redacted.

        Mappings lose the values of secret-bearing keys, sequences are redacted element by
        element, strings pass through :meth:`text`, and scalars are returned unchanged.
        Beyond :data:`MAX_DEPTH` the value becomes :data:`REDACTED`, so a cyclic or hostile
        structure cannot exhaust the stack.

        Args:
            value: Any JSON-shaped value: a mapping, a sequence, a string or a scalar.
            depth: Current recursion depth; a caller leaves it at ``0``.

        Returns:
            A new value of the same shape, safe to serialise.
        """
        try:
            return self._redact_value(value, depth)
        except (AttributeError, KeyError, TypeError, ValueError):
            return REDACTED

    def mapping(self, data: Mapping[str, object]) -> dict[str, object]:
        """Return a redacted ``dict`` for a JSON-shaped mapping.

        A typed convenience over :meth:`json_value` for the call sites that already know they
        hold a mapping, kept so those call sites do not need a cast. Anything that is not a
        mapping yields an empty ``dict``; a mapping that cannot be walked yields its keys with
        every value replaced by :data:`REDACTED`.

        Args:
            data: The mapping to redact.

        Returns:
            A new ``dict`` of redacted values, in the input's key order.
        """
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
        """Apply every free-text pattern to ``value``.

        ``include_urls`` is cleared by :meth:`url` on the branch where a string cannot be
        parsed as a URL; without it, ``url()`` would call ``text()``, which finds the same
        URL-shaped substring and calls ``url()`` again. ``nested`` counts how many value spans
        deep this call is - a bracketed value or a value kept under a name that is not
        secret-bearing, re-scanned in turn - and stops at :data:`MAX_DEPTH`.
        """
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

        Only a credential the engine can resolve is replaced on its own: a value written after a
        ``Bearer``/``Basic`` scheme ends where that scheme's value ends, so the diagnostics behind
        it stay readable. A parameterised scheme carries its whole credential as parameters, and a
        value that is one token with nothing behind it is the credential alone. Anything else goes
        whole, because a token kept there could be the credential; :data:`TRUNCATION_MARKER` then
        says text was dropped, and the space before it keeps the marker out of the value the pair
        patterns stop at. A value that already begins with the marker is left as it is, so
        re-scanning redacted text changes nothing.

        Args:
            value: The header line's value, exactly as the line carried it.

        Returns:
            The marker, the marker and a resolved tail, or the marker and the truncation marker.
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
        """Redact a header line: its value, or the pairs nested inside a value it keeps.

        A secret-bearing name loses its value through :meth:`_redact_header_value`; any other name
        keeps the line and has its value scrubbed in turn, because the substitution has already
        scanned past it and a pair behind the colon (``error: token=...``) would otherwise never be
        visited. The fixed deny-list names are left to the passes that own them.
        """
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
            # A value under a key that is not secret keeps its own text, so every value shape is
            # scrubbed in turn: the substitution has already scanned past the span, and a quoted
            # value is how a credential-bearing node id travels (``"nodeid": "...[token=...]"``).
            return self._rescan_value(prefix, match.group(0)[len(prefix) :], nested=nested)
        if match.group("dquoted") is not None:
            return f'{prefix}"{REDACTED}"'
        if match.group("squoted") is not None:
            return f"{prefix}'{REDACTED}'"
        return f"{prefix}{REDACTED}"

    def _rescan_value(self, prefix: str, value: str, *, nested: int) -> str:
        """Return ``prefix`` and ``value`` scrubbed again as free text.

        Every pattern here consumes the value it matches without re-reading what it replaced, so a
        pair behind a name that is not secret-bearing is only reached by running the engine over
        that span. Its delimiters are part of what is scrubbed, so an unchanged result reproduces
        the original text verbatim.

        Args:
            prefix: The name and separator the caller's pattern matched, reproduced as matched.
            value: The value span, as the caller's pattern captured it.
            nested: How many value spans deep this is; the engine stops at :data:`MAX_DEPTH`.

        Returns:
            ``prefix`` and the re-scrubbed value, or the original text when nothing changed.
        """
        if _NESTED_SCAN_PATTERN.search(value) is None:
            return prefix + value
        return prefix + self._scrub(value, include_urls=False, nested=nested + 1)


def default_redactor() -> Redactor:
    """Return a redactor carrying only the built-in deny-lists.

    Used by the call sites that must not be able to skip redaction, such as a warning logged
    before configuration has resolved. Each call returns a fresh instance rather than a
    module-level cache, so nothing outlives a caller's ability to replace it.

    Returns:
        A :class:`Redactor` built with no configured keys.
    """
    return Redactor()
