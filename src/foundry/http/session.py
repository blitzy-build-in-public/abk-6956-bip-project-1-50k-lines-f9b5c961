"""The HTTP session: one logical request, composed from the package's collaborators (AAP 0.4.3).

The pipeline is AAP 0.7.3's: closed-session guard, ``join_url`` against ``http.base_url``, the
header merge, the timeout as a request extension, the transport, the retry loop and
:func:`foundry.http.response.from_httpx`. Headers merge as ``http.headers``, then the auth
strategy's pair, then the per-request headers, a later value replacing an earlier one
case-insensitively. Every status, 3xx and 5xx included, is *returned* as a
:class:`~foundry.http.response.FoundryResponse`; only transport faults raise. The transport is
chosen once by :func:`foundry.http.transport.select_transport`, which builds the live one only
for a session with no cassette to send on, so a replay holds nothing that can reach a peer.
"""

from __future__ import annotations

import math
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from types import TracebackType
from typing import TYPE_CHECKING, Final, TypeAlias, cast

import httpx

from foundry.core.exceptions import ConfigError, FoundryError
from foundry.core.limits import (
    HTTP_TIMEOUT_DEFAULT,
    HTTP_TIMEOUT_MAX,
    HTTP_TIMEOUT_RANGE,
    range_fault_message,
)
from foundry.core.types import Sleep
from foundry.http.auth import AuthHeader, AuthSettings, header_pairs, resolve_auth
from foundry.http.response import FoundryResponse, from_httpx
from foundry.http.retries import RetryPolicy, RetryRunner
from foundry.http.transport import (
    build_live_transport,
    close_quietly,
    select_transport,
    timeout_extension,
)
from foundry.http.url import join_url, require_base_url
from foundry.obs.events import emit_cassette_mode, emit_plumbing_failure
from foundry.obs.redaction import Redactor, default_redactor

if TYPE_CHECKING:
    from httpx._types import QueryParamTypes

    from foundry.cassette.modes import CassetteSession

__all__: Final[tuple[str, ...]] = (
    "DEFAULT_RETRY_POLICY",
    "DEFAULT_TIMEOUT",
    "FoundrySession",
    "MAX_TIMEOUT",
    "SessionSettings",
)

RequestParams: TypeAlias = Mapping[str, object] | Sequence[tuple[str, object]]
"""The shapes the ``params`` argument accepts: the ones :class:`httpx.Request` accepts.

Declared once so :meth:`FoundrySession.request` and the five verbs publish the same annotation. The
values reach ``httpx`` unchanged -- its own :class:`httpx.QueryParams` is what encodes them -- and
this module neither sorts nor re-encodes a parameter, so a caller's order reaches the peer and the
cassette matcher, which is where order-insensitive comparison belongs.
"""

DEFAULT_TIMEOUT: Final[float] = HTTP_TIMEOUT_DEFAULT
"""Default per-request timeout in seconds: the default of ``http.timeout`` (AAP 0.4.5).

Defined once in :mod:`foundry.core.limits` -- this package may not read the configuration model
(AAP 0.4.8) -- and re-published as the value a hand-built :class:`SessionSettings` carries.
"""

MAX_TIMEOUT: Final[float] = HTTP_TIMEOUT_MAX
"""Largest accepted per-request timeout in seconds: ``http.timeout``'s upper bound, enforced by
:func:`_validated_timeout` and defined once in :mod:`foundry.core.limits` beside its default.
"""

DEFAULT_RETRY_POLICY: Final[RetryPolicy] = RetryPolicy()
"""The default ``http.retries.*`` policy: three attempts, ``0.2`` s growing to ``2.0`` s.

Those are AAP 0.4.5's values. Frozen, so one instance can serve as a field default: no session can
change the policy another session runs under.
"""

_TIMEOUT_KEY: Final[str] = "http.timeout"
"""The dotted configuration key every timeout fault this module raises names."""

_TIMEOUT_CONSTRAINT: Final[str] = HTTP_TIMEOUT_RANGE
"""The accepted range of ``http.timeout``, the constraint text the configuration registry states."""

_HINT_TIMEOUT: Final[str] = (
    "set http.timeout in foundry.toml, in FOUNDRY_HTTP__TIMEOUT, or with --foundry-http.timeout"
)
"""The next step a timeout fault offers: the three surfaces a value can arrive through."""

_HINT_CLOSED: Final[str] = (
    "build a new session, or let the fixture that owns this one rebuild it; a closed session has "
    "no transport left to send on"
)
"""The next step a request on a closed session offers."""

_ARGUMENT_FAULTS: Final[tuple[type[BaseException], ...]] = (
    AttributeError,
    TypeError,
    ValueError,
    httpx.InvalidURL,
)
"""The faults a request argument raises inside ``httpx``, refused here as a ``ConfigError``.

``TypeError`` for a method or path that is not a string, refused before ``.upper()`` or ``join_url``
can coerce it, and for a body the encoder cannot serialise; ``AttributeError`` for a headers or
params shape it cannot read; ``ValueError`` for a body that refers to itself; ``httpx.InvalidURL``
for an unparseable target. Nothing else is caught, so a transport fault reaches the retry loop.
"""

_COMPONENT: Final[str] = "http.session"
"""The component name a plumbing-failure event from this module reports itself under."""

_CLOSE_ERRORS: Final[tuple[type[BaseException], ...]] = (FoundryError, OSError)
"""The faults a cassette save at teardown may raise: a Foundry error or a filesystem fault.

Nothing else is caught: a save that raised anything outside this set is a defect rather than a
plumbing fault, and it must stay visible (AAP 0.14.3).
"""


def _header_pairs(
    headers: Mapping[str, str] | Sequence[tuple[str, str]],
) -> tuple[tuple[str, str], ...]:
    """Return ``headers`` as a tuple of pairs, in the order they were given.

    :attr:`SessionSettings.headers` is published as pairs because a header table's order decides
    which value wins, and a mapping's order is its insertion order. The configuration model holds a
    ``dict``, so a mapping is accepted here as well and converted with its own order preserved: an
    attribute that can only be built one way is a trap for the fixture that builds it.

    Args:
        headers: A mapping of names to values, or a sequence of pairs.

    Returns:
        The pairs as a tuple, with every name and value coerced to ``str``.
    """
    items: Iterable[tuple[str, str]] = headers.items() if isinstance(headers, Mapping) else headers
    return tuple((str(name), str(value)) for name, value in items)


def _timeout_fault(value: object) -> ConfigError:
    """Return the ``http.timeout`` fault for a value a session cannot use as a deadline.

    One wording serves both places a timeout is refused -- a per-request argument and a configured
    default -- and it is the sentence every Foundry range fault uses, so
    ``http.timeout = -1`` reads the same whichever surface reported it (AAP 0.4.5).

    Args:
        value: The offending value.

    Returns:
        The :class:`ConfigError` naming ``http.timeout``, the value and the accepted range.
    """
    return ConfigError(
        range_fault_message(_TIMEOUT_KEY, value, _TIMEOUT_CONSTRAINT),
        key=_TIMEOUT_KEY,
        hint=_HINT_TIMEOUT,
    )


def _validated_timeout(value: object) -> float:
    """Return ``value`` as a usable per-request deadline, or raise naming ``http.timeout``.

    A ``bool`` is refused even though it is an ``int``, ``NaN`` and the infinities are refused, and
    a non-numeric value is refused rather than left to raise ``TypeError`` from a comparison: a
    caller's mistake becomes the typed configuration fault the command line maps to exit ``2``
    instead of an uncaught builtin.

    Args:
        value: The per-request timeout, or ``http.timeout`` when the request named none.

    Returns:
        The value as a finite ``float`` inside ``0 < t <= 300``.

    Raises:
        ConfigError: When the value is not a number inside the accepted range. The message names
            ``http.timeout``, the value and the range.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise _timeout_fault(value)
    number = float(value)
    if not math.isfinite(number) or not 0.0 < number <= MAX_TIMEOUT:
        raise _timeout_fault(value)
    return number


def _argument_fault(name: str, phrase: str, cause: BaseException) -> ConfigError:
    """Return the fault naming one request argument a caller supplied unusably.

    The value the caller gave never reaches that message (AAP 0.3.2).
    """
    return ConfigError(f"request argument {name!r} {phrase}: {type(cause).__name__}")


def _cassette_mode_name(cassette: CassetteSession, fallback: str) -> str:
    """Return the mode the cassette session actually runs under.

    The session's own behaviour is authoritative: a caller that built a ``replay`` session while its
    configuration said ``off`` is replaying, and the debug event should say so. The configured name
    is the fallback for a session that does not report one, and the function is total so a
    diagnostic can never fail a session's construction.

    Args:
        cassette: The session whose mode is being reported.
        fallback: The mode name to report when ``cassette`` does not carry one.

    Returns:
        The mode name, or ``fallback``.
    """
    behaviour: object = getattr(cassette, "behaviour", None)
    name: object = getattr(behaviour, "name", None)
    return name if isinstance(name, str) else fallback


def _cassette_nodeid(cassette: CassetteSession) -> str:
    """Return the node id the cassette session records under, for the debug event.

    A cassette belongs to one test, so the session binds that test's node id and no accessor
    publishes it; the spelling is read here rather than passed in because the session is the only
    object that holds it. A session that carries none yields the empty string: the event is a
    diagnostic, and an empty field keeps the line well formed instead of raising.

    Args:
        cassette: The session whose test is being reported.

    Returns:
        The node id, or ``""`` when the session does not expose one.
    """
    for attribute in ("nodeid", "_nodeid"):
        value: object = getattr(cassette, attribute, None)
        if isinstance(value, str):
            return value
    return ""


@dataclass(frozen=True, slots=True)
class SessionSettings:
    """The resolved ``http.*`` settings one session is built from, as plain primitives.

    This is the seam AAP 0.4.8 needs: ``foundry.http`` may not import ``foundry.config``, so the
    configuration model's values are copied here by the fixture that builds the session. Frozen and
    slotted, so a session cannot have its settings changed under it.

    Attributes:
        base_url: The ``http.base_url`` a relative path is resolved against, or ``None``.
        timeout: The default deadline in seconds for a request that names none.
        headers: The static ``http.headers`` pairs, where a later pair of the same name wins.
        retry: The ``http.retries.*`` policy every logical request runs under.
        auth: The ``http.auth.*`` settings, whose fields name credential variables, or ``None``.
        redact_keys: ``secrets.redact_keys``, the names the run masks at every sink.
        cassette_mode: The resolved ``cassette.mode``; the cassette session governs behaviour.
        require_base_url: Whether a base URL is mandatory, which ``foundry_api`` sets so the
            constructor raises naming ``http.base_url`` before the first request (AAP 0.4.7).
    """

    base_url: str | None = None
    timeout: float = DEFAULT_TIMEOUT
    headers: tuple[tuple[str, str], ...] = ()
    retry: RetryPolicy = DEFAULT_RETRY_POLICY
    auth: AuthSettings | None = None
    redact_keys: tuple[str, ...] = ()
    cassette_mode: str = "off"
    require_base_url: bool = False

    def __post_init__(self) -> None:
        """Normalise ``headers`` to pairs, accepting the configuration model's mapping."""
        object.__setattr__(self, "headers", _header_pairs(self.headers))


class FoundrySession:
    """One HTTP session: a configured request pipeline, and the responses it returns.

    Built by the ``foundry_http`` and ``foundry_api`` fixtures (AAP 0.4.3), and usable directly by a
    caller without pytest. A status is always a value and a caller's unusable argument is a
    :class:`~foundry.core.exceptions.ConfigError` naming it; a cassette miss is a ``FoundryError``
    the retry loop deliberately does not catch (AAP 0.4.7).

    No client, and nothing a client would add
        There is no cookie jar, no redirect following and no proxy discovery, because no
        ``httpx.Client`` is built: ``handle_request`` is called directly (AAP 0.3.1). The session
        holds no state a request can change, so two identical requests send identical bytes.

    ``close()`` is the whole teardown
        The fixture that owns a session calls :meth:`close` and must not also call the cassette
        session's ``finish()``; :meth:`close` is idempotent and never raises (AAP 0.14.3).
    """

    _settings: SessionSettings
    _base_url: str | None
    _redactor: Redactor
    _auth: AuthHeader | None
    _cassette: CassetteSession | None
    _transport: httpx.BaseTransport
    _runner: RetryRunner
    _closed: bool

    __slots__ = (
        "_auth",
        "_base_url",
        "_cassette",
        "_closed",
        "_redactor",
        "_runner",
        "_settings",
        "_transport",
    )

    def __init__(
        self,
        settings: SessionSettings = SessionSettings(),
        *,
        cassette: CassetteSession | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Sleep = time.sleep,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        """Resolve ``settings`` into the collaborators one request runs through.

        Nothing is sent and no socket is opened here: a cassette session builds the transport its
        mode calls for, and a live transport is built only when it is the object this session sends
        on.

        Args:
            settings: The resolved ``http.*`` values, as plain primitives.
            cassette: The test's cassette session, or ``None`` when the run keeps no cassette.
            transport: A transport to send on instead of the one this session would build; when
                given, this session builds none.
            sleep: The callable that performs a backoff wait, injected by a test that asserts it.
            environ: The environment a credential is read from; ``None`` is the process environment.

        Raises:
            ConfigError: ``http.base_url`` is absent or unusable where it is required, or a
                variable an auth strategy names is missing. Each message names the key or variable.
            FoundryError: The cassette session cannot produce the transport its mode calls for.
        """
        self._settings = settings
        # A base URL the caller declares mandatory is resolved here, not at the first request:
        # foundry_api has to fail as the fixture is requested, naming http.base_url, so the one test
        # that needed it errors and the rest of the session still runs (AAP 0.4.7).
        self._base_url = (
            require_base_url(settings.base_url) if settings.require_base_url else settings.base_url
        )
        # One engine for every response this session returns, and the only route a header, a query
        # value or a body takes into a dump. The default engine's own deny-lists apply even when
        # nothing is configured, so a session with no secrets configured still masks Authorization.
        self._redactor = (
            Redactor(settings.redact_keys) if settings.redact_keys else default_redactor()
        )
        # The credential is read once, at session start, from the variable *names* the settings
        # carry, so no value ever enters the settings, a dump, a cassette or a log line (I9).
        self._auth = resolve_auth(settings.auth, environ=environ)
        self._cassette = cassette
        # The transport is chosen once, here, and never per request. A live transport is built only
        # when it is the object this session sends on: a cassette session answers with its own, and
        # a replay's comes from its document, which is what makes offline replay structural.
        self._transport = (
            transport if transport is not None else select_transport(build_live_transport, cassette)
        )
        self._runner = RetryRunner(settings.retry, sleep=sleep, redactor=self._redactor)
        self._closed = False
        if cassette is not None:
            # A DEBUG decision line, naming the mode and the test: it carries no header, no
            # credential and no URL, and it is the only record of which transport a test ran on.
            emit_cassette_mode(
                _cassette_mode_name(cassette, settings.cassette_mode), _cassette_nodeid(cassette)
            )

    @property
    def settings(self) -> SessionSettings:
        """Return the settings this session was built from, unchanged and immutable."""
        return self._settings

    @property
    def base_url(self) -> str | None:
        """Return the base URL relative paths resolve against, or ``None`` when there is none."""
        return self._base_url

    @property
    def redactor(self) -> Redactor:
        """Return the redaction engine every dump and cassette of this session passes through."""
        return self._redactor

    @property
    def closed(self) -> bool:
        """Return whether :meth:`close` has run, which is what makes closing idempotent."""
        return self._closed

    def request(
        self,
        method: str,
        path: str,
        *,
        params: RequestParams | None = None,
        json: object | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> FoundryResponse:
        """Issue one logical request and return the response, whatever its status.

        The steps are AAP 0.7.3's, in the order the numbered comments in the body follow; ``path``
        is resolved against ``http.base_url``, which lets ``foundry_http`` take absolute URLs while
        ``foundry_api`` takes relative paths. ``params`` goes to :class:`httpx.Request`, merging
        it with any query already in ``path`` and lets the parameters win; this module does not sort
        them, because order-insensitive comparison belongs to the cassette matcher.

        Args:
            method: The HTTP method. Any verb is accepted and upper-cased.
            path: An absolute ``http``/``https`` URL, or a path relative to ``http.base_url``.
            params: Query parameters, as a mapping or a sequence of pairs.
            json: A JSON-serialisable body, or ``None`` for a request without one.
            headers: Per-request headers, which win over ``http.headers`` and the auth header.
            timeout: A deadline for this request, replacing ``http.timeout``, still in its range.

        Returns:
            The response for the status the peer sent, ``elapsed_ms`` measured across every attempt.

        Raises:
            FoundryError: The session is closed.
            ConfigError: An argument this session cannot use, or a timeout outside its range.
            TransportError: A transport fault the retry policy does not retry.
            RetryExhaustedError: Every attempt failed with a fault the policy does retry.
        """
        # 1. A closed session has no transport left and no cassette left to save.
        if self._closed:
            raise FoundryError("FoundrySession is closed", hint=_HINT_CLOSED)
        # 2. through 4. Each caller-supplied value is checked and coerced under its own guard, so a
        #    fault it raises leaves as the ConfigError naming that argument (AAP 0.14.3).
        if not isinstance(method, str):
            raise _argument_fault("method", "cannot be used as an HTTP method", TypeError())
        if not isinstance(path, str):
            raise _argument_fault("path", "cannot be used as a request target", TypeError())
        verb = method.upper()
        try:
            url = httpx.URL(join_url(self._base_url, path))
        except _ARGUMENT_FAULTS as exc:
            raise _argument_fault("path", "cannot be used as a request target", exc) from None
        try:
            merged_headers = self._merged_headers(headers)
        except _ARGUMENT_FAULTS as exc:
            raise _argument_fault("headers", "cannot be used as request headers", exc) from None
        # ``params`` is encoded here, so a shape httpx cannot read is named as ``params``.
        try:
            query_params = (
                None if params is None else httpx.QueryParams(cast("QueryParamTypes", params))
            )
        except _ARGUMENT_FAULTS as exc:
            raise _argument_fault("params", "cannot be encoded as query parameters", exc) from None
        # The deadline is mandatory -- a bare httpx.Request carries no deadline at all.
        effective_timeout = _validated_timeout(
            self._settings.timeout if timeout is None else timeout
        )
        sent: list[httpx.Request] = []

        def send() -> httpx.Response:
            """Build one attempt's request and send it on the session's transport."""
            try:
                attempt = httpx.Request(
                    method=verb,
                    url=url,
                    params=query_params,
                    headers=merged_headers,
                    json=json,
                    extensions={"timeout": timeout_extension(effective_timeout)},
                )
            except _ARGUMENT_FAULTS as exc:
                raise _argument_fault("json", "cannot be serialised", exc) from None
            sent.append(attempt)
            return self._transport.handle_request(attempt)

        # 5. and 6. The transport was selected in ``__init__``; the retry loop sends on it once per
        #    attempt, returns the last response whatever its status, or raises the typed error.
        outcome = self._runner.execute(send, method=verb, path=path)
        # 7. Wrap the reply together with the request that produced it, so both dumps describe the
        #    attempt whose response is being returned. ``execute`` always sends at least once.
        return from_httpx(
            outcome.response,
            elapsed_ms=outcome.elapsed_ms,
            redactor=self._redactor,
            request=sent[-1],
        )

    def get(
        self,
        path: str,
        *,
        params: RequestParams | None = None,
        json: object | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> FoundryResponse:
        """Issue a ``GET``; the pipeline, the result and the exceptions are :meth:`request`'s."""
        return self.request("GET", path, params=params, json=json, headers=headers, timeout=timeout)

    def post(
        self,
        path: str,
        *,
        params: RequestParams | None = None,
        json: object | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> FoundryResponse:
        """Issue a ``POST``; the pipeline, the result and the exceptions are :meth:`request`'s."""
        return self.request(
            "POST", path, params=params, json=json, headers=headers, timeout=timeout
        )

    def put(
        self,
        path: str,
        *,
        params: RequestParams | None = None,
        json: object | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> FoundryResponse:
        """Issue a ``PUT``; the pipeline, the result and the exceptions are :meth:`request`'s."""
        return self.request("PUT", path, params=params, json=json, headers=headers, timeout=timeout)

    def patch(
        self,
        path: str,
        *,
        params: RequestParams | None = None,
        json: object | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> FoundryResponse:
        """Issue a ``PATCH``; the pipeline, the result and the exceptions are :meth:`request`'s."""
        return self.request(
            "PATCH", path, params=params, json=json, headers=headers, timeout=timeout
        )

    def delete(
        self,
        path: str,
        *,
        params: RequestParams | None = None,
        json: object | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> FoundryResponse:
        """Issue a ``DELETE``; the pipeline, the result and the exceptions are :meth:`request`'s."""
        return self.request(
            "DELETE", path, params=params, json=json, headers=headers, timeout=timeout
        )

    def close(self) -> None:
        """Release the transport and save the cassette, at most once, and never raise.

        The fixture that owns a session calls this in teardown and must not also call the cassette
        session's ``finish()``: the save happens here, exactly once, because a second writer would
        be a second capture of the same test.

        A transport that fails to close and a cassette that fails to save are both reported as one
        ``WARNING`` on the ``foundry`` logger, and neither changes a test's outcome or the run's
        exit code (AAP 0.1.2, AAP 0.14.3). What it closes is the transport this session sent on --
        a cassette's own in every cassette mode, and the live transport this session built only
        when it had no cassette to send on -- so none is left behind for a caller to close.

        Calling this a second time does nothing.
        """
        if self._closed:
            return
        self._closed = True
        close_quietly(self._transport)
        if self._cassette is None:
            return
        try:
            self._cassette.finish()
        except _CLOSE_ERRORS as exc:
            # Failure isolation: a save that could not be written is a diagnostic, never a result.
            emit_plumbing_failure(
                _COMPONENT,
                "the cassette for this test could not be saved at teardown",
                exc=exc,
                redactor=self._redactor,
            )

    def __enter__(self) -> FoundrySession:
        """Return this session, so it can be used as a context manager."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Close the session on the way out, and never swallow the exception that caused it.

        Args:
            exc_type: The exception type a ``with`` body raised, or ``None``.
            exc: The exception instance, or ``None``.
            tb: The traceback, or ``None``.
        """
        self.close()

    def _merged_headers(self, headers: Mapping[str, str] | None) -> httpx.Headers:
        """Return the headers one request carries, the later source replacing an earlier one.

        Args:
            headers: The per-request headers, or ``None``.

        Returns:
            The merged headers, in the order ``http.headers`` supplied them, then the auth
            strategy's pair, then the caller's. A name that appears twice keeps its last value at
            the position it was first set in, because ``httpx.Headers`` removes the duplicate and
            overwrites in place, so two requests with the same inputs serialise identically.
        """
        merged = httpx.Headers()
        for name, value in self._settings.headers:
            merged[name] = value
        for name, value in header_pairs(self._auth):
            merged[name] = value
        if headers is not None:
            for name, value in headers.items():
                merged[name] = value
        return merged
