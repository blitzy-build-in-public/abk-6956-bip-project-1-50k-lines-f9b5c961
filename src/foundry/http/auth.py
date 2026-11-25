"""The four credential strategies of Foundry's HTTP layer and the rule that keeps them safe."""

from __future__ import annotations

import base64
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

from foundry.core.exceptions import ConfigError
from foundry.obs.redaction import REDACTED

__all__: Final[tuple[str, ...]] = (
    "AUTH_STRATEGIES",
    "AuthHeader",
    "AUTHORIZATION",
    "AuthSettings",
    "header_pairs",
    "normalise_strategy",
    "resolve_auth",
    "STRATEGY_BASIC",
    "STRATEGY_BEARER",
    "STRATEGY_HEADER",
    "STRATEGY_NONE",
)

#: Strategy that sends no credential. It is the default, and the one ``foundry_http`` implies.
STRATEGY_NONE: Final[str] = "none"

#: Strategy that sends ``Authorization: Bearer <token>`` from ``http.auth.token_env``.
STRATEGY_BEARER: Final[str] = "bearer"

#: Strategy that sends ``Authorization: Basic <base64(user:password)>`` from the two
#: environment variables ``http.auth.user_env`` and ``http.auth.password_env`` name.
STRATEGY_BASIC: Final[str] = "basic"

#: Strategy that sends the header named by ``http.auth.header_name`` with the value held by
#: the environment variable ``http.auth.header_value_env`` names.
STRATEGY_HEADER: Final[str] = "header"

#: Every accepted ``http.auth.strategy`` value, in the order the configuration documents them.
#: Immutable, and the single source of the text an unknown-strategy error renders.
AUTH_STRATEGIES: Final[tuple[str, ...]] = (
    STRATEGY_NONE,
    STRATEGY_BEARER,
    STRATEGY_BASIC,
    STRATEGY_HEADER,
)

#: The header both RFC 7617 strategies write. Casing is fixed here rather than taken from the
#: caller so two requests from two workers serialise the same header name.
AUTHORIZATION: Final[str] = "Authorization"

#: The dotted configuration keys this module reads. Spelling them once keeps an error message
#: and its ``key`` attribute from drifting apart.
_KEY_STRATEGY: Final[str] = "http.auth.strategy"
_KEY_TOKEN_ENV: Final[str] = "http.auth.token_env"
_KEY_USER_ENV: Final[str] = "http.auth.user_env"
_KEY_PASSWORD_ENV: Final[str] = "http.auth.password_env"
_KEY_HEADER_NAME: Final[str] = "http.auth.header_name"
_KEY_HEADER_VALUE_ENV: Final[str] = "http.auth.header_value_env"

#: The layer a credential fault is attributed to: the fault is that the environment does not
#: supply what configuration named, so ``config show --resolved`` reports the same spelling.
_ENV_LAYER: Final[str] = "env"

#: The two accepted values that resolve to no header at all. ``""`` is the documented
#: tolerance, so a strategy left blank behaves exactly like the default.
_NO_AUTH_STRATEGIES: Final[frozenset[str]] = frozenset({"", STRATEGY_NONE})

#: Authentication scheme prefixes. Written once so the header value is built from a fixed
#: template rather than by string concatenation at each call site.
_BEARER_SCHEME: Final[str] = "Bearer"
_BASIC_SCHEME: Final[str] = "Basic"

#: The accepted value list as it appears in an error message: ``none, bearer, basic, header``.
_ACCEPTED_TEXT: Final[str] = ", ".join(AUTH_STRATEGIES)


@dataclass(frozen=True, slots=True)
class AuthSettings:
    """The six ``http.auth.*`` keys, carrying variable *names* and never a secret.

    Each ``*_env`` field is required by the strategy that reads it, and a frozen, slotted dataclass
    keeps an instance cheap to build per test and impossible to change.
    """

    strategy: str = STRATEGY_NONE
    token_env: str | None = None
    user_env: str | None = None
    password_env: str | None = None
    header_name: str | None = None
    header_value_env: str | None = None


@dataclass(frozen=True, slots=True)
class AuthHeader:
    """One resolved credential header, holding its value out of every representation.

    ``value`` is declared with ``field(repr=False)`` and no value-bearing ``__str__`` or ``__eq__``
    is added, so the generated ``__repr__`` renders the name alone, which ``str()``, an f-string, a
    ``%r`` format and a logging call inherit. A caller logging a session or an object graph holding
    this header writes ``AuthHeader(name='...')`` and never the credential.
    The value lives for the lifetime of the session that resolved it. It is never written to an
    artifact, a cassette, a result record or a report -- the sinks persist values only after
    :mod:`foundry.obs.redaction` has masked the names in its deny-list, and ``Authorization`` is
    one of them -- and it never enters a configuration model.

    Attributes:
        name: The header name to send: :data:`AUTHORIZATION` for the RFC 7617 strategies,
            the configured name for ``header``.
        value: The header value, excluded from ``repr``.
    """

    name: str
    value: str = field(repr=False)

    def masked(self) -> str:
        """Return the mask a redacted rendering shows in place of the value."""
        return REDACTED


def normalise_strategy(value: str | object) -> str:
    """Return the comparable strategy name for ``value``.

    The result is stripped and case-folded, and an enum member is unwrapped through its ``value``,
    so a caller may pass :class:`foundry.config.enums.AuthStrategy` without this module importing
    the enum. ``None``, an empty string and a whitespace-only string all normalise to ``""``.
    """
    member: object = getattr(value, "value", value)
    if member is None:
        return ""
    text = member if isinstance(member, str) else str(member)
    return text.strip().casefold()


def _environ(environ: Mapping[str, str] | None) -> Mapping[str, str]:
    """Return the environment to read: the injected mapping, or the process environment.

    ``None`` means ``os.environ``, which is the production path and the only place this module
    touches the process environment. A supplied mapping is what lets a test inject credentials
    without mutating ``os.environ``, so no test depends on the ambient environment and two
    workers cannot observe each other's variables.

    Args:
        environ: The injected mapping, or ``None``.

    Returns:
        The mapping to look a credential variable up in.
    """
    return os.environ if environ is None else environ


def _require_setting(value: str | None, key: str, strategy: str, expectation: str) -> str:
    """Return a setting the strategy needs, or raise naming the key that must be set.

    This is the first of the two failure shapes this module reports: the strategy is selected, but
    the configuration telling it where its credential comes from is absent. The message names the
    dotted configuration key and the strategy, and the hint says what to put there; neither mentions
    a value, because none has been read at this point.

    Args:
        value: The configured value, or ``None``.
        key: The dotted configuration key it came from.
        strategy: The normalised strategy that requires it.
        expectation: What the key should hold, phrased to follow ``set <key> to``.

    Returns:
        The configured value with surrounding whitespace removed.

    Raises:
        ConfigError: When ``value`` is absent or blank.
    """
    if value is None or not value.strip():
        raise ConfigError(
            f"{key} is required when {_KEY_STRATEGY} is {strategy}",
            key=key,
            hint=f"set {key} to {expectation}",
        )
    return value.strip()


def _credential_error(message: str, key: str) -> ConfigError:
    """Build the error for a credential variable the environment does not supply.

    The second failure shape: configuration named the variable and the environment holds no usable
    value for it. The message names the variable alone -- never a value, not even a partial one --
    and the attribution is the ``http.auth.*`` key that named it with ``layer="env"``, the layer
    ``config show --resolved`` reports for a value read from the environment.

    Returns:
        The error to raise, carrying its key and layer.
    """
    return ConfigError(
        message,
        key=key,
        layer=_ENV_LAYER,
        hint="export the variable in the environment that runs the suite; a credential is "
        "never stored in configuration",
    )


def _read_secret(environ: Mapping[str, str], variable: str, key: str) -> str:
    """Return the value ``variable`` holds, or raise without revealing any value.

    The lookup is one mapping read -- cheap enough for ``foundry_api``'s per-test resolution -- and
    no value is cached anywhere: a test may change the environment between tests with
    ``monkeypatch``, so the next resolution must observe the change.
    Three states are reported separately and none quotes the variable's content: absent
    (``not set``), not text (defensive, because a test may inject a mapping of anything) and empty
    (``empty``), which no credential strategy can use.

    Returns:
        The variable's value, unchanged: a credential is never stripped or transformed.

    Raises:
        ConfigError: When the variable is absent, empty or not text.
    """
    raw = environ.get(variable)
    if raw is None:
        raise _credential_error(f"environment variable not set: {variable}", key)
    if not isinstance(raw, str):
        raise _credential_error(f"environment variable is not text: {variable}", key)
    if not raw:
        raise _credential_error(f"environment variable is empty: {variable}", key)
    return raw


def _basic_credential(user: str, password: str, variable: str) -> str:
    """Return the base64 ``user:password`` token RFC 7617 defines, from UTF-8 bytes.

    ``str.encode`` with no argument produces the UTF-8 bytes RFC 7617 expects, so the header is
    identical on every host. RFC 7617 section 2 forbids a colon inside the user id, because the
    credential is decoded at the first colon; such a user id is rejected here rather than producing
    a credential the server reads as a different user, and the message names the variable that held
    it, never the value.

    Returns:
        The base64 token as ASCII text, ready to follow ``Basic ``.

    Raises:
        ConfigError: When ``user`` contains a colon.
    """
    if ":" in user:
        raise ConfigError(
            f"basic-auth user id from {variable} contains ':', which RFC 7617 forbids",
            key=_KEY_USER_ENV,
            layer=_ENV_LAYER,
            hint="use a user id without a colon; the credential is encoded as 'user:password'",
        )
    credential = f"{user}:{password}".encode()
    return base64.b64encode(credential).decode("ascii")


def resolve_auth(
    settings: AuthSettings | None,
    *,
    environ: Mapping[str, str] | None = None,
) -> AuthHeader | None:
    """Resolve ``settings`` into the header ``foundry_api`` attaches to every request.

    The HTTP session calls this once, when it is constructed, so a credential is read once per
    session rather than once per request. ``foundry_http`` calls it with ``None``: it is
    deliberately unauthenticated, so a test can drive a login flow itself and assert on the
    token it obtains (AAP 0.3.3), and ``None`` therefore resolves to no header at all.

    Args:
        settings: The resolved ``http.auth.*`` configuration, or ``None`` for no credential.
        environ: The environment the named variables are read from. ``None`` means
            ``os.environ``; a mapping is what a test injects, so exercising a strategy never
            mutates the process environment.

    Returns:
        The header to send, or ``None`` when ``settings`` is ``None``, when the strategy is
        ``none`` (the default), or when the strategy is empty.

    Raises:
        ConfigError: When the selected strategy is not one of :data:`AUTH_STRATEGIES`; when a
            setting the strategy requires is absent, named by its dotted configuration key; or
            when the environment variable such a setting names is missing, empty or not text,
            named by the variable alone. No message this function raises contains a credential.

    Example:
        >>> header = resolve_auth(
        ...     AuthSettings(strategy="bearer", token_env="FOUNDRY_TOKEN"),
        ...     environ={"FOUNDRY_TOKEN": "abc"},
        ... )
        >>> header.name, header.value
        ('Authorization', 'Bearer abc')
    """
    if settings is None:
        return None
    strategy = normalise_strategy(settings.strategy)
    if strategy in _NO_AUTH_STRATEGIES:
        return None
    if strategy == STRATEGY_BEARER:
        variable = _require_setting(
            settings.token_env,
            _KEY_TOKEN_ENV,
            STRATEGY_BEARER,
            "the name of the environment variable that holds the token",
        )
        token = _read_secret(_environ(environ), variable, _KEY_TOKEN_ENV)
        return AuthHeader(name=AUTHORIZATION, value=f"{_BEARER_SCHEME} {token}")
    if strategy == STRATEGY_BASIC:
        user_variable = _require_setting(
            settings.user_env,
            _KEY_USER_ENV,
            STRATEGY_BASIC,
            "the name of the environment variable that holds the user id",
        )
        password_variable = _require_setting(
            settings.password_env,
            _KEY_PASSWORD_ENV,
            STRATEGY_BASIC,
            "the name of the environment variable that holds the password",
        )
        source = _environ(environ)
        user = _read_secret(source, user_variable, _KEY_USER_ENV)
        password = _read_secret(source, password_variable, _KEY_PASSWORD_ENV)
        credential = _basic_credential(user, password, user_variable)
        return AuthHeader(name=AUTHORIZATION, value=f"{_BASIC_SCHEME} {credential}")
    if strategy == STRATEGY_HEADER:
        name = _require_setting(
            settings.header_name, _KEY_HEADER_NAME, STRATEGY_HEADER, "the header name to send"
        )
        value_variable = _require_setting(
            settings.header_value_env,
            _KEY_HEADER_VALUE_ENV,
            STRATEGY_HEADER,
            "the name of the environment variable that holds the header value",
        )
        value = _read_secret(_environ(environ), value_variable, _KEY_HEADER_VALUE_ENV)
        return AuthHeader(name=name, value=value)
    raise ConfigError(
        f"unknown {_KEY_STRATEGY}: {settings.strategy!s}; accepted values are {_ACCEPTED_TEXT}",
        key=_KEY_STRATEGY,
        hint=f"set {_KEY_STRATEGY} to one of {_ACCEPTED_TEXT}",
    )


def header_pairs(header: AuthHeader | None) -> tuple[tuple[str, str], ...]:
    """Return ``header`` as the ordered pairs the session merges into a request.

    The session merges ``http.headers``, then this header, then the per-request headers with
    the later winning (AAP 0.7.3), so pairs are the form it consumes. Exposing the credential
    this way is what keeps every other module out of :attr:`AuthHeader.value`: the code that
    builds a request is the only reader, and it hands the value straight to ``httpx``.

    Args:
        header: The header :func:`resolve_auth` returned, or ``None``.

    Returns:
        A single ``(name, value)`` pair, or an empty tuple when no credential is configured.
    """
    if header is None:
        return ()
    return ((header.name, header.value),)
