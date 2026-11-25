"""The ``foundry_http`` and ``foundry_api`` fixtures: the two HTTP sessions a test can hold.

The ``foundry_http`` and ``foundry_api`` fixtures: the two HTTP sessions a test can hold. Both are
function scoped, because a session is one test's object: built when that test asks for it, carrying
that test's cassette when the run records, closed in teardown, so nothing outlives the test that
opened it. ``foundry_http`` returns a :class:`foundry.http.FoundrySession` built from the run's
``http.retries.*`` policy and ``http.timeout``, with ``http.headers`` applied and no credential
attached. It holds no base URL, so a caller passes absolute URLs, which is what makes it the
login-flow fixture where a test drives the sign-in request itself. ``foundry_api`` returns a session
with ``http.base_url`` joined against every relative path, the configured ``http.auth.strategy``
attached, ``http.headers`` applied and ``cassette.mode`` honoured; a run whose ``http.base_url`` is
unset errors on the one test that requested it, naming ``http.base_url``, while the rest of the
session still runs.

**The pipeline belongs to :mod:`foundry.http.session`.** Joining the base URL, merging headers,
resolving the timeout, selecting the transport and running the retry loop are
:class:`foundry.http.FoundrySession`'s; this module builds the plain settings that session is
constructed from. Every status is therefore *returned* as a :class:`foundry.http.FoundryResponse`,
4xx and 5xx included, and only a transport fault raises:
:class:`foundry.core.exceptions.TransportError` for a fault the policy does not retry, and
:class:`foundry.core.exceptions.RetryExhaustedError` once the attempts are spent.

**Authentication is a name, never a value.** ``foundry_config`` carries the *names* of the
environment variables holding a credential and this module copies those names into an
:class:`foundry.http.auth.AuthSettings`; the session resolves them once at construction, and no
value reaches this file, a message, a log line or a repr. A variable the configuration named but the
environment does not set raises :class:`foundry.core.exceptions.ConfigError` naming that variable
alone. Only ``foundry_api`` honours ``cassette.mode``: a ``replay`` run drives the recorded surface
through it, on a transport built from the document that never reaches ``httpcore``, while
``foundry_http`` always sends live.

**Teardown changes no outcome.** Both fixtures close their session in a ``finally`` -- idempotent,
releasing the transport and saving the cassette -- and report either fault as one ``WARNING`` on the
``foundry`` logger, so a recorded cassette is flushed even when a test failed.

**Imported lazily.** ``foundry.plugin`` imports this module during ``pytest_configure`` inside the
150 ms budget, so module scope imports ``pytest``, :mod:`collections.abc`, :mod:`pathlib` and
:mod:`typing` only, and the imports pulling in ``httpx``, :mod:`foundry.cassette` and
:mod:`foundry.obs` sit inside the helper that first needs them.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    import httpx

    from foundry.cassette.modes import CassetteSession
    from foundry.config.models import FoundryConfig, HttpAuthConfig, HttpConfig
    from foundry.http import AuthSettings, FoundrySession, RetryPolicy, SessionSettings

__all__ = ["foundry_api", "foundry_http"]
"""The two public names this module contributes to the twelve-fixture set (AAP 0.4.3)."""

#: The cassette mode ``foundry_http`` reports: it sends on a live transport in every run, so it
#: honours no document and its settings say so rather than echoing a mode it ignores.
_NO_CASSETTE_MODE = "off"

#: The row of the cassette mode table that keeps no document, which is the one this package skips.
_OFF_MODE = "off"


def _static_headers(http_config: HttpConfig) -> tuple[tuple[str, str], ...]:
    """Return ``http.headers`` as an ordered tuple of pairs, copied out of the frozen model.

    :attr:`foundry.http.session.SessionSettings.headers` publishes pairs, because a header table's
    order decides which value wins a duplicate. The configuration model holds a ``dict``, copied
    here rather than handed on: the resolved configuration is immutable and shared by the session.

    Returns:
        The configured header names and values, in the order the model holds them.
    """
    return tuple((name, value) for name, value in http_config.headers.items())


def _retry_policy(http_config: HttpConfig) -> RetryPolicy:
    """Return the ``http.retries.*`` policy every request of one session runs under.

    The four values are copied into the policy that owns their ranges and their backoff formula, so
    this module restates neither. The predicates are rendered as their plain names.

    Returns:
        The policy the ``http.retries.*`` keys describe.

    Raises:
        foundry.core.exceptions.ConfigError: A value is outside its published range, or names a
            predicate outside :data:`foundry.http.retries.RETRY_PREDICATES`; the message names the
            dotted key. A configuration model already refuses such a value, so reaching this means
            the settings were overridden.
    """
    # Imported inside the helper rather than at module scope: `foundry.http` imports httpx, which
    # the 150 ms plugin-startup budget of AAP 0.14.2 cannot pay for at `pytest_configure` (I5).
    from foundry.http import RetryPolicy

    retries = http_config.retries
    return RetryPolicy(
        max_attempts=retries.max_attempts,
        backoff_base=retries.backoff_base,
        backoff_max=retries.backoff_max,
        retry_on=frozenset(str(predicate) for predicate in retries.retry_on),
    )


def _auth_settings(auth_config: HttpAuthConfig) -> AuthSettings:
    """Return ``http.auth.*`` as the settings the session resolves a credential from.

    The six values are environment-variable *names* and the strategy that reads them, never a
    credential. The session resolves the names once, at construction, so a strategy the run selected
    as ``none`` resolves to no header at all.

    Returns:
        The strategy and the five variable names, copied into a frozen settings object.
    """
    from foundry.http import AuthSettings

    return AuthSettings(
        strategy=str(auth_config.strategy),
        token_env=auth_config.token_env,
        user_env=auth_config.user_env,
        password_env=auth_config.password_env,
        header_name=auth_config.header_name,
        header_value_env=auth_config.header_value_env,
    )


def _cassette_directory(configured: Path, request: pytest.FixtureRequest) -> Path:
    """Return the absolute directory one test's cassette document is read from and written to.

    A relative ``cassette.dir`` means "relative to where I ran pytest", so it is resolved once per
    request against :attr:`pytest.Config.invocation_params`, exactly as ``foundry_data`` resolves
    ``data.dir``: a recording and its replay then share a place whatever the working directory is.

    Returns:
        The normalised absolute directory, which is not required to exist: a ``record`` run creates
        it when it writes, and a ``replay`` run names a missing document through the typed miss.
    """
    directory = Path(configured)
    if not directory.is_absolute():
        directory = Path(request.config.invocation_params.dir) / directory
    return directory.resolve()


def _cassette_session(
    request: pytest.FixtureRequest,
    foundry_config: FoundryConfig,
) -> tuple[CassetteSession | None, httpx.BaseTransport | None]:
    """Return this test's cassette session and the live transport it was handed.

    One document per test: the session binds the test's node id, the resolved ``cassette.mode`` and
    a store over ``cassette.dir`` whose redaction engine is built from ``secrets.redact_keys``, so a
    capture is masked by the same keys every other sink uses. ``cassette.mode = off`` is the one row
    of the mode table that neither reads nor writes a document, and it answers ``(None, None)`` so
    the session builds and closes its own live transport; every other mode gets the live transport
    it needs, and ``replay`` is handed none because its row has no network, which is what makes
    offline replay structural. The transport comes back beside the session because nothing owns it
    until the HTTP session wraps it.

    Returns:
        The cassette session the API fixture hands to its HTTP session, with the transport built
        for it; ``(None, None)`` in ``off`` mode.

    Raises:
        foundry.core.exceptions.FoundryError: ``cassette.mode`` names none of the four modes, or the
            test carries no node id to record under; both are reported before the first request.
    """
    # Imported inside the helper: `foundry.cassette` pulls in the replay transport and, behind it,
    # httpx, which the plugin-startup budget cannot pay for at import time (I5, AAP 0.14.2).
    from foundry.cassette.modes import CassetteSession, behaviour_for
    from foundry.cassette.store import CassetteStore
    from foundry.http.transport import build_live_transport, close_quietly
    from foundry.obs.redaction import Redactor

    mode = str(foundry_config.cassette.mode)
    behaviour = behaviour_for(mode)
    if behaviour.name == _OFF_MODE:
        return None, None
    store = CassetteStore(
        _cassette_directory(foundry_config.cassette.dir, request),
        Redactor(tuple(foundry_config.secrets.redact_keys)),
    )
    # A transport that can reach a peer is built only for a mode whose row has a network, so a
    # replay holds no object that could open a socket (AAP 0.2.1, acceptance criterion 8).
    transport = build_live_transport() if behaviour.network else None
    try:
        cassette = CassetteSession(
            request.node.nodeid, mode=mode, store=store, live_transport=transport
        )
    except BaseException:
        close_quietly(transport)  # nothing else would release what was built above
        raise
    return cassette, transport


def _session_settings(
    foundry_config: FoundryConfig,
    *,
    base_url: str | None,
    auth: AuthSettings | None,
    require_base_url: bool,
    cassette_mode: str,
) -> SessionSettings:
    """Return the plain settings both fixtures construct their session from.

    One place copies the resolved configuration into
    :class:`foundry.http.session.SessionSettings`: ``foundry.http`` may not import
    ``foundry.config``, so the values travel as primitives and the session never sees a pydantic
    model. The two fixtures differ only in the arguments below, which is why the shared body is here
    rather than twice in the fixture functions.

    Returns:
        The settings the session resolves its transport, its retry policy and its redactor from.
    """
    from foundry.http import SessionSettings

    return SessionSettings(
        base_url=base_url,
        timeout=foundry_config.http.timeout,
        headers=_static_headers(foundry_config.http),
        retry=_retry_policy(foundry_config.http),
        auth=auth,
        redact_keys=tuple(foundry_config.secrets.redact_keys),
        cassette_mode=cassette_mode,
        require_base_url=require_base_url,
    )


@pytest.fixture
def foundry_http(foundry_config: FoundryConfig) -> Iterator[FoundrySession]:
    """Return this test's unauthenticated session, holding no base URL.

    Function scoped, so two tests cannot share a connection or a cookie. The session is built from
    the run's resolved settings and nothing else: ``http.retries.*`` decides how often a retryable
    fault is retried, ``http.timeout`` is the default deadline and ``http.headers`` are sent on
    every request. It carries **no base URL**, so ``path`` must be an absolute ``http``/``https``
    URL; a relative one raises :class:`foundry.core.exceptions.ConfigError` from the session rather
    than resolving against something this fixture never had. It also takes **no cassette**, so a
    request made here reaches the peer in every run, and every status is returned as a
    :class:`foundry.http.FoundryResponse`, 4xx and 5xx included.

    Yields:
        The live, unauthenticated :class:`foundry.http.FoundrySession`, closed when the test ends.
    """
    # Imported inside the fixture: `foundry.http` imports httpx, which the 150 ms plugin-startup
    # budget cannot pay for at import time (I5, AAP 0.14.2).
    from foundry.http import FoundrySession

    session = FoundrySession(
        _session_settings(
            foundry_config,
            base_url=None,
            auth=None,
            require_base_url=False,
            cassette_mode=_NO_CASSETTE_MODE,
        )
    )
    try:
        yield session
    finally:
        # Closing is the whole teardown and is idempotent: it releases the transport through
        # close_quietly, and a fault while closing is one WARNING rather than a result (AAP 0.14.3).
        session.close()


@pytest.fixture
def foundry_api(
    request: pytest.FixtureRequest,
    foundry_config: FoundryConfig,
) -> Iterator[FoundrySession]:
    """Return this test's configured API client: base URL, auth, static headers and cassette mode.

    Function scoped, because the cassette it may record belongs to this test alone and the
    credential is resolved for it: a test that changes an environment variable sees its own change
    and no other test does. Four settings are applied. ``http.base_url`` is joined against every
    relative path. The strategy ``http.auth.strategy`` names is resolved once, here, from the
    environment variables the ``http.auth.*_env`` keys name, and its header is sent after the static
    ``http.headers`` so a per-request header still wins. ``http.headers`` is sent on every request.
    And ``cassette.mode`` is honoured: ``record`` captures this test's exchanges into
    ``cassettes/<sanitized-nodeid>.json``, redacted before the write; ``replay`` answers from that
    document without reaching ``httpcore``; ``passthrough`` replays a match and otherwise goes live;
    ``off`` keeps no cassette.

    Yields:
        The configured :class:`foundry.http.FoundrySession`, closed when the test ends.

    Raises:
        foundry.core.exceptions.ConfigError: ``http.base_url`` is unset, blank, not an absolute
            ``http``/``https`` URL or carries userinfo -- the message names ``http.base_url`` and
            carries no credential -- or a variable the configured auth strategy names is absent from
            the environment, in which case the message names the variable alone. Either fault errors
            the one test that requested this fixture, releasing the transport the cassette mode
            built, while the rest of the session runs.
        foundry.core.exceptions.CassetteMissError: Raised by a request in ``replay`` mode whose
            method, query and body hash match no recorded interaction, naming the method, the path
            and the cassette path, with no socket opened.
    """
    from foundry.http import FoundrySession
    from foundry.http.transport import close_quietly

    settings = _session_settings(
        foundry_config,
        base_url=foundry_config.http.base_url,
        auth=_auth_settings(foundry_config.http.auth),
        require_base_url=True,
        cassette_mode=str(foundry_config.cassette.mode),
    )
    cassette, transport = _cassette_session(request, foundry_config)
    # The base URL and the credential are resolved by the constructor before it chooses a transport,
    # so an unusable configuration raises here, naming the key or the variable, and nothing has been
    # sent (AAP 0.4.7).
    try:
        session = FoundrySession(settings, cassette=cassette)
    except BaseException:
        close_quietly(transport)  # the session that would have owned it was never built
        raise
    try:
        yield session
    finally:
        # Close, and only close: it saves the cassette this test recorded and releases the transport
        # it sent on, exactly once; a second finish() here would capture the same test twice.
        session.close()
