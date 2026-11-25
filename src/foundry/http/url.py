"""The single place a request target is resolved and a URL is made safe to dump."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from foundry.core.exceptions import ConfigError
from foundry.obs.redaction import Redactor, default_redactor

__all__: Final[tuple[str, ...]] = (
    "ABSOLUTE_URL_PATTERN",
    "SUPPORTED_SCHEMES",
    "UrlParts",
    "has_scheme",
    "join_url",
    "origin",
    "path_and_query",
    "redact_url",
    "require_base_url",
    "split_url",
    "strip_userinfo",
)

#: The dotted configuration key every :class:`ConfigError` this module raises names
#: (AAP 0.4.5).
_BASE_URL_KEY: Final[str] = "http.base_url"

#: Characters that end a URL's authority, as RFC 3986 orders them.
_AUTHORITY_DELIMITERS: Final[tuple[str, ...]] = ("/", "?", "#")

#: The scheme separator, spelled once so :func:`strip_userinfo` and
#: :data:`ABSOLUTE_URL_PATTERN` cannot disagree about it.
_SCHEME_SEPARATOR: Final[str] = "://"

#: Longest port a URL can carry, and the shortest. A value outside the range is not a port,
#: so the caller keeps the authority text instead of inventing a number.
_MIN_PORT: Final[int] = 0
_MAX_PORT: Final[int] = 65535

#: Digits a port can be written in: five, since 65535 has five. The bound keeps
#: :func:`int` off a pathological digit string before the range check runs.
_PORT_DIGITS: Final[int] = 5

#: Hint attached when a relative request is issued with no base URL configured. It names the
#: three layers a value can arrive through rather than guessing which one the operator uses.
_HINT_SET_BASE_URL: Final[str] = (
    "set http.base_url in foundry.toml, in FOUNDRY_HTTP__BASE_URL, or with --foundry-http.base_url"
)

#: Hint attached when ``http.base_url`` carries userinfo: the value would reach
#: ``foundry config show``, a result record and ``report.html``, so the credential is moved
#: to the environment variable indirection the auth strategies read (implicit requirement I9).
_HINT_MOVE_CREDENTIALS: Final[str] = (
    "remove the credentials from http.base_url and name the environment variable that holds them "
    "in http.auth.token_env, http.auth.user_env or http.auth.password_env"
)

#: The prefix that marks a string as an absolute URL: a scheme per RFC 3986 followed by
#: ``://``. ``http`` and ``https`` are validated separately against :data:`SUPPORTED_SCHEMES`,
#: so a target carrying another scheme is recognised as absolute and then rejected, rather
#: than being silently appended to a base URL as if it were a path.
ABSOLUTE_URL_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*)" + re.escape(_SCHEME_SEPARATOR)
)

#: The schemes Foundry issues requests over. A base URL or an absolute target naming anything
#: else is rejected by :func:`require_base_url` and :func:`join_url`.
SUPPORTED_SCHEMES: Final[frozenset[str]] = frozenset({"http", "https"})


@dataclass(frozen=True, slots=True)
class UrlParts:
    """The pieces of a URL that Foundry acts on, with any fragment already dropped.

    Splitting is total: a string that carries no scheme, an empty authority or a non-numeric port
    still yields parts instead of raising, because a caller composing a ``TransportError`` or a log
    line must be able to describe whatever it was handed.
    """

    scheme: str
    host: str
    port: int | None
    path: str
    query: str
    userinfo: str | None


def _as_port(text: str) -> int | None:
    """Return ``text`` as a port number, or ``None`` when it is not one.

    Args:
        text: The characters following the ``:`` in an authority.

    Returns:
        The integer port for a run of ASCII digits that denotes a value in
        ``0..65535`` inclusive, and ``None`` for anything else -- an empty string, a service
        name such as ``:http``, a digit string long enough to overflow, or a non-ASCII digit
        that :func:`int` would reject.
    """
    if not text or len(text) > _PORT_DIGITS or not text.isascii() or not text.isdigit():
        return None
    port = int(text)
    if _MIN_PORT <= port <= _MAX_PORT:
        return port
    return None


def _split_authority(authority: str) -> tuple[str | None, str, int | None]:
    """Split an authority into its userinfo, host and port.

    Args:
        authority: The text between ``://`` and the first ``/``, ``?`` or ``#``.

    Returns:
        The userinfo (or ``None``), the host and the port (or ``None``). An IPv6 literal keeps
        its brackets, so the host can be placed back into a URL unchanged. An authority whose
        port is not a number keeps the whole ``host:port`` text as the host and reports no
        port, so no part of the input is lost.
    """
    userinfo: str | None = None
    hostport = authority
    if "@" in hostport:
        userinfo, _, hostport = hostport.rpartition("@")
    if hostport.startswith("["):
        closing = hostport.find("]")
        if closing == -1:
            return userinfo, hostport, None
        host = hostport[: closing + 1]
        tail = hostport[closing + 1 :]
        if not tail:
            return userinfo, host, None
        port = _as_port(tail[1:]) if tail.startswith(":") else None
        if port is None:
            return userinfo, hostport, None
        return userinfo, host, port
    if ":" in hostport:
        host, _, port_text = hostport.rpartition(":")
        port = _as_port(port_text)
        if port is None:
            return userinfo, hostport, None
        return userinfo, host, port
    return userinfo, hostport, None


def _authority(parts: UrlParts) -> str:
    """Return the ``host[:port]`` authority of ``parts``, never including userinfo.

    Args:
        parts: The parsed URL.

    Returns:
        The host alone when the URL carries no port, otherwise ``host:port``. Leaving
        userinfo out here is what makes :func:`origin` and the result of :func:`join_url`
        credential-free by construction rather than by a later scrub.
    """
    if parts.port is None:
        return parts.host
    return f"{parts.host}:{parts.port}"


def _base_url_not_set() -> ConfigError:
    """Build the error for a relative request issued with no base URL configured.

    Returns:
        The :class:`ConfigError` naming ``http.base_url`` and the layers that can set it.
    """
    return ConfigError(
        "http.base_url is not set",
        key=_BASE_URL_KEY,
        layer=None,
        path=None,
        hint=_HINT_SET_BASE_URL,
    )


def _base_url_invalid() -> ConfigError:
    """Build the error for a base URL that is not an absolute ``http``/``https`` URL.

    One message covers every shape of the fault -- a relative value, an unsupported scheme and
    a missing host -- because the correction is the same in each case and a test can assert on
    one text.

    Returns:
        The :class:`ConfigError` naming ``http.base_url`` and the accepted form.
    """
    return ConfigError(
        "http.base_url must be an absolute http or https URL",
        key=_BASE_URL_KEY,
        layer=None,
        path=None,
        hint=_HINT_SET_BASE_URL,
    )


def _base_url_has_userinfo() -> ConfigError:
    """Build the error for a base URL that carries userinfo.

    Returns:
        The :class:`ConfigError` naming ``http.base_url`` and stating that credentials belong
        in the ``http.auth.*_env`` variables. The message never quotes the value, which is the
        reason the check exists: the value would be echoed by ``foundry config show`` and
        stored in every result record of the run.
    """
    return ConfigError(
        "http.base_url must not contain userinfo; credentials belong in http.auth.*_env",
        key=_BASE_URL_KEY,
        layer=None,
        path=None,
        hint=_HINT_MOVE_CREDENTIALS,
    )


def has_scheme(path: str) -> bool:
    """Report whether ``path`` begins with a ``scheme://`` prefix.

    Args:
        path: A request target: an absolute URL or a path relative to ``http.base_url``.

    Returns:
        True when the string starts with a scheme followed by ``://`` -- ``https://x``,
        ``ftp://x`` and ``HTTP://x`` alike, the last because a scheme is case-insensitive.
        False for every relative form, including ``//host/path`` and a bare ``mailto:`` style
        prefix, both of which are treated as paths: Foundry's request targets are either
        absolute URLs or paths, and nothing else.
    """
    return ABSOLUTE_URL_PATTERN.match(path) is not None


def split_url(url: str) -> UrlParts:
    """Split ``url`` into its scheme, authority, path and query, dropping any fragment.

    Args:
        url: An absolute URL or a relative reference.

    Returns:
        The :class:`UrlParts` for ``url``. A relative reference yields an empty scheme and
        host with the whole string as its path. A string that cannot be read as a URL still
        yields parts rather than raising, because callers build error messages from the
        result: an empty authority gives an empty host, and a non-numeric port gives the
        ``host:port`` text as the host with no port.
    """
    without_fragment = url.partition("#")[0]
    head, _, query = without_fragment.partition("?")
    match = ABSOLUTE_URL_PATTERN.match(head)
    if match is None:
        return UrlParts(scheme="", host="", port=None, path=head, query=query, userinfo=None)
    scheme = match.group("scheme").casefold()
    authority_start = match.end()
    separator = head.find("/", authority_start)
    if separator == -1:
        authority = head[authority_start:]
        path = ""
    else:
        authority = head[authority_start:separator]
        path = head[separator:]
    userinfo, host, port = _split_authority(authority)
    return UrlParts(
        scheme=scheme,
        host=host.casefold(),
        port=port,
        path=path,
        query=query,
        userinfo=userinfo,
    )


def origin(url: str) -> str:
    """Return ``scheme://host[:port]`` for ``url``, with no userinfo, path or query.

    Args:
        url: An absolute URL or a relative reference.

    Returns:
        The origin, which is stable across requests and safe to log: the session reports it
        instead of the full URL, so no query value reaches a log line. ``""`` is returned
        for a relative reference, which has no origin at all.
    """
    parts = split_url(url)
    if not parts.scheme or not parts.host:
        return ""
    return f"{parts.scheme}{_SCHEME_SEPARATOR}{_authority(parts)}"


def path_and_query(url: str) -> str:
    """Return the request target of ``url``: its path and query, and no fragment.

    The query is kept byte for byte and in the order it was given -- order-insensitive comparison
    belongs to the cassette matcher, not here -- and an empty path renders as ``"/"``.
    """
    parts = split_url(url)
    path = parts.path or "/"
    if not path.startswith("/"):
        path = f"/{path}"
    if parts.query:
        return f"{path}?{parts.query}"
    return path


def require_base_url(base_url: str | None) -> str:
    """Return ``base_url`` when a session can resolve relative paths against it, raising otherwise.

    This is the check ``foundry_api`` performs; ``foundry_http`` carries no base URL and never calls
    it. A base URL may not carry userinfo: credentials belong in the environment variables that
    ``http.auth.*_env`` names, because ``http.base_url`` is echoed by ``foundry config show``,
    stored in every result record and shown in ``report.html`` (AAP 0.4.8; implicit requirement I9).

    Returns:
        ``base_url`` unchanged when it is an absolute ``http`` or ``https`` URL with a host and no
        userinfo.

    Raises:
        ConfigError: With ``key="http.base_url"`` when the value is absent or blank, is not an
        absolute
            ``http``/``https`` URL, has no host, or carries userinfo. No message quotes a
            credential.
    """
    if base_url is None or not base_url.strip():
        raise _base_url_not_set()
    parts = split_url(base_url)
    if parts.scheme not in SUPPORTED_SCHEMES or not parts.host:
        raise _base_url_invalid()
    if parts.userinfo is not None:
        raise _base_url_has_userinfo()
    return base_url


def join_url(base_url: str | None, path: str) -> str:
    """Resolve a request target against ``http.base_url``.

    An absolute ``http``/``https`` target is returned unchanged, an absolute path replaces the base
    URL's path, and a relative path is appended with exactly one ``/`` between the two, its query
    travelling with it. A fragment is dropped from either side, the base URL's own query is never
    inherited, and ``"//host/path"`` is treated as a path. A relative target with no usable base URL
    raises :class:`ConfigError`.
    """
    target = path.partition("#")[0]
    if has_scheme(target):
        parts = split_url(target)
        if parts.scheme not in SUPPORTED_SCHEMES:
            raise ConfigError(
                f"request URL must use http or https, not {parts.scheme!r}",
                key=None,
                layer=None,
                path=None,
                hint=f"issue the request against {_BASE_URL_KEY} with a relative path instead",
            )
        if not parts.host:
            raise ConfigError(
                "request URL must be an absolute http or https URL with a host",
                key=None,
                layer=None,
                path=None,
                hint=f"issue the request against {_BASE_URL_KEY} with a relative path instead",
            )
        return target
    base = require_base_url(base_url)
    if not target:
        return base
    requested = split_url(target)
    base_parts = split_url(base)
    if requested.path.startswith("/"):
        resolved_path = requested.path
    else:
        base_path = base_parts.path.rstrip("/")
        resolved_path = f"{base_path}/{requested.path}" if base_path else f"/{requested.path}"
    url = f"{base_parts.scheme}{_SCHEME_SEPARATOR}{_authority(base_parts)}{resolved_path}"
    if requested.query:
        return f"{url}?{requested.query}"
    return url


def strip_userinfo(url: str) -> str:
    """Remove any ``user:password@`` from the authority of ``url``.

    Args:
        url: The URL to clean.

    Returns:
        ``url`` with the userinfo removed and every other character left byte-identical:
        scheme, host, port, path, query and fragment are not re-serialised, because a round
        trip through a parser would re-encode them. A string with no ``://`` is returned
        unchanged, and so is one whose authority carries no ``@``.
    """
    scheme_end = url.find(_SCHEME_SEPARATOR)
    if scheme_end == -1:
        return url
    authority_start = scheme_end + len(_SCHEME_SEPARATOR)
    ends = [url.find(delimiter, authority_start) for delimiter in _AUTHORITY_DELIMITERS]
    authority_end = min((index for index in ends if index != -1), default=len(url))
    authority = url[authority_start:authority_end]
    marker = authority.rfind("@")
    if marker == -1:
        return url
    return f"{url[:authority_start]}{authority[marker + 1 :]}{url[authority_end:]}"


def redact_url(url: str, redactor: Redactor | None = None) -> str:
    """Return ``url`` safe to write to a log, an artifact, a cassette, a result or a report.

    Userinfo is stripped structurally, so it leaves the string whether or not anything is
    configured, and the result is then handed to :meth:`foundry.obs.redaction.Redactor.url`, which
    masks every deny-listed query parameter and emits the query in a stable order. No deny-list is
    duplicated here, so a key configured once applies at every sink that dumps a URL (AAP 0.3.2).

    Returns:
        The redacted URL.
    """
    engine = default_redactor() if redactor is None else redactor
    return engine.url(strip_userinfo(url))
