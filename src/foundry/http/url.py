"""The single place a request target is resolved and a URL is made safe to dump."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
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
    "require_same_origin",
    "split_url",
    "strip_userinfo",
)

#: The dotted key every :class:`ConfigError` this module raises names (AAP 0.4.5).
_BASE_URL_KEY: Final[str] = "http.base_url"

#: Characters that end a URL's authority, as RFC 3986 orders them.
_AUTHORITY_DELIMITERS: Final[tuple[str, ...]] = ("/", "?", "#")

#: The scheme separator, spelled once so :func:`strip_userinfo` and
#: :data:`ABSOLUTE_URL_PATTERN` cannot disagree about it.
_SCHEME_SEPARATOR: Final[str] = "://"

#: Ports a URL may carry, and the digits one can be written in: five, since 65535 has five. A value
#: outside the range is not a port, so the caller keeps the authority text instead of inventing one.
_MIN_PORT: Final[int] = 0
_MAX_PORT: Final[int] = 65535
_PORT_DIGITS: Final[int] = 5

#: The port each supported scheme implies when a URL leaves it out (RFC 3986 6.2.3). RFC 6454
#: makes an explicitly written default port the same origin as an absent one, so :func:`origin`
#: folds it away and ``http://h:80/x`` compares equal to ``http://h/x``.
_DEFAULT_PORTS: Final[Mapping[str, int]] = MappingProxyType({"http": 80, "https": 443})

#: Hint for a relative request with no base URL: it names the three layers a value arrives through.
_HINT_SET_BASE_URL: Final[str] = (
    "set http.base_url in foundry.toml, in FOUNDRY_HTTP__BASE_URL, or with --foundry-http.base_url"
)

#: Hint for a base URL carrying userinfo: the value would reach ``config show``, every result
#: record and ``report.html``, so the credential moves to the ``http.auth.*_env`` indirection (I9).
_HINT_MOVE_CREDENTIALS: Final[str] = (
    "remove the credentials from http.base_url and name the environment variable that holds them "
    "in http.auth.token_env, http.auth.user_env or http.auth.password_env"
)

#: Message for an absolute target off ``http.base_url``'s origin: it names the key that fixes the
#: request and never quotes the target, which may embed a credential (AAP 0.3.2, requirement I9).
_MESSAGE_OFF_ORIGIN: Final[str] = "request URL must share http.base_url's origin"

#: Hint for that target: both corrections -- issue a path, or move the base URL to the host the
#: target names -- and, like the message, no part of the target itself.
_HINT_OFF_ORIGIN: Final[str] = (
    "issue a relative path such as /health against http.base_url, or change http.base_url to the "
    "host you mean to reach"
)

#: The prefix marking a string absolute: a scheme per RFC 3986 followed by ``://``. ``http`` and
#: ``https`` are validated against :data:`SUPPORTED_SCHEMES`, so a target carrying another scheme is
#: recognised as absolute and rejected rather than appended to a base URL as if it were a path.
ABSOLUTE_URL_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*)" + re.escape(_SCHEME_SEPARATOR)
)

#: The schemes Foundry issues requests over; any other is rejected by the two requirements below.
SUPPORTED_SCHEMES: Final[frozenset[str]] = frozenset({"http", "https"})


@dataclass(frozen=True, slots=True)
class UrlParts:
    """The pieces of a URL that Foundry acts on, with any fragment already dropped.

    Splitting is total: a string with no scheme, an empty authority or a non-numeric port still
    yields parts rather than raising, so a caller can describe whatever it was handed.
    """

    scheme: str
    host: str
    port: int | None
    path: str
    query: str
    userinfo: str | None


def _as_port(text: str) -> int | None:
    """Return ``text`` as a port number, or ``None`` when it is not one.

    Only a run of ASCII digits denoting ``0..65535`` is a port; an empty string, a service name such
    as ``:http``, a digit string long enough to overflow and a non-ASCII digit all yield ``None``.
    """
    if not text or len(text) > _PORT_DIGITS or not text.isascii() or not text.isdigit():
        return None
    port = int(text)
    if _MIN_PORT <= port <= _MAX_PORT:
        return port
    return None


def _split_authority(authority: str) -> tuple[str | None, str, int | None]:
    """Split an authority into its userinfo, host and port.

    An IPv6 literal keeps its brackets so the host goes back into a URL unchanged, and an authority
    whose port is not a number keeps the ``host:port`` text with no port, losing nothing.
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

    Leaving userinfo out here is what makes :func:`origin` and the result of :func:`join_url`
    credential-free by construction rather than by a later scrub.
    """
    if parts.port is None:
        return parts.host
    return f"{parts.host}:{parts.port}"


def _base_url_not_set() -> ConfigError:
    """Build the error for a relative request issued with no base URL configured."""
    return ConfigError(
        "http.base_url is not set",
        key=_BASE_URL_KEY,
        layer=None,
        path=None,
        hint=_HINT_SET_BASE_URL,
    )


def _base_url_invalid() -> ConfigError:
    """Build the error for a base URL that is not an absolute ``http``/``https`` URL.

    One message covers every shape of the fault -- a relative value, an unsupported scheme and a
    missing host -- because the correction is the same and a test can assert on one text.
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

    The message never quotes the value, which is why the check exists: ``foundry config show`` would
    echo it and every result record of the run would store it.
    """
    return ConfigError(
        "http.base_url must not contain userinfo; credentials belong in http.auth.*_env",
        key=_BASE_URL_KEY,
        layer=None,
        path=None,
        hint=_HINT_MOVE_CREDENTIALS,
    )


def _request_url_off_origin() -> ConfigError:
    """Build the error for an absolute target that is not on ``http.base_url``'s origin.

    Neither the message nor the hint quotes the target, which a data row can fill with a credential
    or a query value; the fault is described by the key that resolves it.
    """
    return ConfigError(
        _MESSAGE_OFF_ORIGIN,
        key=_BASE_URL_KEY,
        layer=None,
        path=None,
        hint=_HINT_OFF_ORIGIN,
    )


def has_scheme(path: str) -> bool:
    """Report whether ``path`` begins with a ``scheme://`` prefix.

    True for ``https://x``, ``ftp://x`` and ``HTTP://x`` alike, a scheme being case-insensitive, and
    False for every relative form -- ``//host/path`` and a bare ``mailto:`` prefix included, both of
    which are treated as paths: Foundry's request targets are absolute URLs or paths, nothing else.
    """
    return ABSOLUTE_URL_PATTERN.match(path) is not None


def split_url(url: str) -> UrlParts:
    """Split ``url`` into its scheme, authority, path and query, dropping any fragment.

    Args:
        url: An absolute URL or a relative reference.

    Returns:
        The :class:`UrlParts` for ``url``, and nothing raises: a relative reference gives an empty
        scheme and host with the whole string as its path, which callers can put into error text.
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

    The origin is stable across requests and safe to log, because the session reports it instead of
    the full URL. A port the scheme implies is left out, so ``http://h:80/x`` and ``http://h/x`` --
    one origin under RFC 6454 -- yield the same text, which is what :func:`require_same_origin`
    compares; a relative reference has no origin and yields ``""``.
    """
    parts = split_url(url)
    if not parts.scheme or not parts.host:
        return ""
    if parts.port is None or parts.port == _DEFAULT_PORTS.get(parts.scheme):
        return f"{parts.scheme}{_SCHEME_SEPARATOR}{parts.host}"
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
    it. Credentials belong in the environment variables ``http.auth.*_env`` names rather than in
    ``http.base_url``, which ``config show`` echoes, every result record stores and ``report.html``
    shows (AAP 0.4.8; implicit requirement I9).

    Returns:
        ``base_url`` unchanged for an absolute ``http``/``https`` URL with a host and no userinfo.

    Raises:
        ConfigError: With ``key="http.base_url"`` when the value is absent or blank, is not an
            absolute ``http``/``https`` URL, has no host, or carries userinfo. No credential is
            quoted.
    """
    if base_url is None or not base_url.strip():
        raise _base_url_not_set()
    parts = split_url(base_url)
    if parts.scheme not in SUPPORTED_SCHEMES or not parts.host:
        raise _base_url_invalid()
    if parts.userinfo is not None:
        raise _base_url_has_userinfo()
    return base_url


def require_same_origin(base_url: str | None, target: str) -> None:
    """Refuse an absolute ``target`` that is not on ``base_url``'s own origin.

    A configured base URL contains every request to its own origin: an absolute target naming
    another scheme, host or port would carry the session's credential to a host the configuration
    never named, and the target can arrive from a data row or any helper forwarding user input
    (AAP 0.3.2; implicit requirement I9). The rule lives here once so :func:`join_url` and the
    session's own guard cannot disagree, and its error quotes neither the target nor any credential.

    Returns:
        ``None``, and the caller sends ``target`` as given, when no base URL is configured or the
        target already shares its origin.

    Raises:
        ConfigError: With ``key="http.base_url"`` when a base URL is set but is not an absolute
            ``http``/``https`` URL without userinfo, and when the target's scheme, host or port
            differs from the base URL's.
    """
    if base_url is None or not base_url.strip():
        return
    base = require_base_url(base_url)
    if origin(target) != origin(base):
        raise _request_url_off_origin()


def join_url(base_url: str | None, path: str) -> str:
    """Resolve a request target against ``http.base_url``.

    An absolute ``http``/``https`` target on the base URL's own origin is returned unchanged, an
    absolute path replaces the base URL's path, and a relative path is appended with exactly one
    ``/`` between the two, its query travelling with it. A fragment is dropped from either side, the
    base URL's own query is never inherited, and ``"//host/path"`` is treated as a path.
    A configured base URL contains every request to its own origin, so an absolute target naming
    another scheme, host or port is refused rather than sent with the session's credential: a path
    forwarded from a data row cannot move the request, or the secret, to a host the configuration
    never named (AAP 0.3.2; implicit requirement I9). A session with no base URL, which is what
    ``foundry_http`` carries, keeps issuing absolute targets unchanged.

    Returns:
        The URL to send: ``path`` unchanged when it is an absolute target on the base URL's origin,
        otherwise the target resolved against ``base_url``.

    Raises:
        ConfigError: For an absolute target that uses another scheme or carries no host, and with
            ``key="http.base_url"`` for a relative target with no usable base URL, a base URL that
            is not an absolute ``http``/``https`` URL without userinfo, or an absolute target
            outside the base URL's origin. No message quotes a target or a credential.
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
        require_same_origin(base_url, target)
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
