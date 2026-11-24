"""The ``foundry_http`` and ``foundry_api`` fixtures: the two HTTP sessions a test can hold.

Two of Foundry's twelve public fixtures (AAP 0.4.3), both function scoped, because a session is one
test's object: it is built when that test asks for it, it carries that test's cassette when the run
records, and it is closed in teardown, so nothing outlives the test that opened it.

``foundry_http`` -- unauthenticated, and carrying no base URL
    Returns a :class:`foundry.http.FoundrySession` built from the run's ``http.retries.*`` policy
    and ``http.timeout``, with ``http.headers`` applied and no credential attached. It holds no
    base URL, so a caller passes absolute URLs; that is what makes it the login-flow fixture of
    AAP 0.3.3, where a test drives the sign-in request itself and asserts on the token it obtains.

``foundry_api`` -- the configured client
    Returns a :class:`foundry.http.FoundrySession` with ``http.base_url`` joined against every
    relative path, the configured ``http.auth.strategy`` attached, the static ``http.headers``
    applied and ``cassette.mode`` honoured. A run whose ``http.base_url`` is unset errors on the one
    test that requested this fixture, naming ``http.base_url``, while the rest of the session still
    runs (AAP 0.4.7).

The pipeline belongs to :mod:`foundry.http.session`, not here
    AAP 0.7.3's order -- join ``http.base_url``, merge ``http.headers`` then the auth pair then the
    per-request headers, resolve the timeout, select the transport, run the retry loop, wrap the
    response -- is :class:`foundry.http.FoundrySession`'s, and this module builds the plain settings
    that session is constructed from. It resolves no URL, merges no header, computes no backoff,
    retries nothing and constructs no ``httpx`` object of its own. Every status is therefore
    *returned* as a :class:`foundry.http.FoundryResponse`, 4xx and 5xx included, and only a
    transport fault raises: :class:`foundry.core.exceptions.TransportError` for a fault the policy
    does not retry, and :class:`foundry.core.exceptions.RetryExhaustedError` once the attempts are
    spent.

Authentication is a name, never a value (AAP 0.3.3, implicit requirement I9)
    ``foundry_config`` carries the *names* of the environment variables holding a credential --
    ``http.auth.token_env`` and its siblings -- and this module copies those names into an
    :class:`foundry.http.auth.AuthSettings`. The session resolves them once, when it is constructed,
    and no value reaches this file, a docstring, a message, a log line or a repr: a variable the
    configuration named but the environment does not set raises
    :class:`foundry.core.exceptions.ConfigError` naming that variable alone. This module never reads
    the process environment; :mod:`foundry.config.env` and :mod:`foundry.http.auth` are the only two
    readers AAP 0.4.8 allows.

Only ``foundry_api`` honours ``cassette.mode``
    AAP 0.4.3 gives ``foundry_http`` "configured retry policy and timeouts" and no cassette
    behaviour, and gives ``foundry_api`` "cassette mode applied". A ``replay`` run therefore drives
    the recorded surface through ``foundry_api``, which sends on a transport built from the document
    and never reaches ``httpcore``, so the "zero connections with the server stopped" observation of
    acceptance criterion 8 is made through that fixture. ``foundry_http`` always sends on a live
    transport.

Teardown changes no outcome (AAP 0.1.2, AAP 0.14.3)
    Both fixtures close their session in a ``finally``, which is the only teardown step: the
    session's :meth:`~foundry.http.FoundrySession.close` is idempotent, closes the transport it sent
    on and saves the cassette, and reports either fault as one ``WARNING`` on the ``foundry`` logger
    rather than raising. A recorded cassette is therefore flushed even when the test failed.

Imported lazily and layered (AAP 0.4.8, AAP 0.14.2)
    ``foundry.plugin`` imports this module during ``pytest_configure``, at every pytest start,
    inside the 150 ms plugin-startup budget, so module scope imports ``pytest``,
    :mod:`collections.abc`, :mod:`pathlib` and :mod:`typing` only: the :mod:`foundry.http` imports
    that pull in ``httpx``, and the :mod:`foundry.cassette` and :mod:`foundry.obs` imports behind
    them, sit inside the helper that first needs them, and every annotation name is imported under
    ``TYPE_CHECKING``. Importing this module defines names only and holds no module-level state. On
    the capability rung this module may import ``core``, ``obs``, ``contract``, ``cassette`` and
    ``http``, and reaches ``config`` only through the ``foundry_config`` fixture argument, which is
    the single upward edge AAP 0.4.8 grants ``foundry.fixtures``.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
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
    order decides which value wins a duplicate and a mapping's order is its insertion order. The
    configuration model holds a ``dict``, so it is copied here rather than handed on: the resolved
    configuration is immutable and shared by the whole session, and no session may see it mutated.

    Args:
        http_config: The resolved ``http.*`` model.

    Returns:
        The configured header names and values, in the order the model holds them.
    """
    return tuple((name, value) for name, value in http_config.headers.items())


def _retry_policy(http_config: HttpConfig) -> RetryPolicy:
    """Return the ``http.retries.*`` policy every request of one session runs under.

    The four values are copied from the resolved configuration into the policy that owns their
    ranges and their backoff formula (:mod:`foundry.http.retries`), so this module restates neither.
    The predicates are rendered as their plain names because ``retry_on`` is published as a set of
    strings; the policy normalises them again and reports an unknown name as a
    :class:`foundry.core.exceptions.ConfigError` naming ``http.retries.retry_on``.

    Args:
        http_config: The resolved ``http.*`` model.

    Returns:
        The policy AAP 0.4.5's ``http.retries.*`` keys describe.

    Raises:
        foundry.core.exceptions.ConfigError: A value is outside the range AAP 0.4.5 publishes, or
            names a predicate outside :data:`foundry.http.retries.RETRY_PREDICATES`. The message
            names the dotted key. A configuration model already refuses such a value, so reaching
            this means the settings were overridden, and the fault still names what to fix.
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
    credential: the resolved configuration holds no secret (AAP 0.3.3, implicit requirement I9), and
    neither does the :class:`foundry.http.auth.AuthSettings` this returns. The session resolves the
    names once, when it is constructed, so a strategy the run selected as ``none`` resolves to no
    header at all.

    Args:
        auth_config: The resolved ``http.auth.*`` model.

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

    ``cassette.dir`` is written by an author as the directory they keep their recordings in, so a
    relative value means "relative to where I ran pytest": it is resolved once per request against
    :attr:`pytest.Config.invocation_params`, exactly as ``foundry_data`` resolves ``data.dir``
    (AAP 0.4.5 anchors both to the invocation directory). Resolving it here rather than leaving it
    to the store keeps a recording and its replay in the same place when the process working
    directory is not the invocation directory.

    Args:
        configured: The resolved ``cassette.dir``, absolute or relative.
        request: The fixture request, read for the invocation directory.

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
) -> CassetteSession | None:
    """Return this test's cassette session, or ``None`` when the run keeps no cassette.

    One document per test (AAP 0.4.6): the session binds the test's node id, the resolved
    ``cassette.mode`` and a store over ``cassette.dir`` whose redaction engine is built from
    ``secrets.redact_keys``, so a capture is masked by the same keys every other sink uses (implicit
    requirement I4). ``cassette.mode = off`` is the one row of the mode table that neither reads nor
    writes a document, and it returns ``None`` so the session builds and closes its own live
    transport; every other mode gets the live transport it needs, and ``replay`` is handed none
    because its row has no network at all -- which is what makes offline replay structural rather
    than a promise about which transport gets used.

    The mode table decides, and it is read through :func:`foundry.cassette.modes.behaviour_for`, so
    this module keeps no second copy of the four mode names.

    Args:
        request: The fixture request, read for the test's node id and the invocation directory.
        foundry_config: The resolved configuration, read for ``cassette.dir``, ``cassette.mode`` and
            ``secrets.redact_keys``.

    Returns:
        The cassette session the API fixture hands to its HTTP session, or ``None`` in ``off`` mode.

    Raises:
        foundry.core.exceptions.FoundryError: ``cassette.mode`` names none of the four modes, or the
            test carries no node id to record under. Both are reported before the first request.
    """
    # Imported inside the helper: `foundry.cassette` pulls in the replay transport and, behind it,
    # httpx, which the plugin-startup budget cannot pay for at import time (I5, AAP 0.14.2).
    from foundry.cassette.modes import CassetteSession, behaviour_for
    from foundry.cassette.store import CassetteStore
    from foundry.http.transport import build_live_transport
    from foundry.obs.redaction import Redactor

    mode = str(foundry_config.cassette.mode)
    behaviour = behaviour_for(mode)
    if behaviour.name == _OFF_MODE:
        return None
    store = CassetteStore(
        _cassette_directory(foundry_config.cassette.dir, request),
        Redactor(tuple(foundry_config.secrets.redact_keys)),
    )
    return CassetteSession(
        request.node.nodeid,
        mode=mode,
        store=store,
        # A transport that can reach a peer is built only for a mode whose row has a network, so a
        # replay holds no object that could open a socket (AAP 0.2.1, acceptance criterion 8).
        live_transport=build_live_transport() if behaviour.network else None,
    )


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
    :class:`foundry.http.session.SessionSettings`, the seam AAP 0.4.8 needs: ``foundry.http`` may
    not import ``foundry.config``, so the values travel as primitives and the session never sees a
    pydantic model. The two fixtures differ only in the arguments below, which is why the shared
    body is here rather than twice in the fixture functions.

    Args:
        foundry_config: The resolved configuration.
        base_url: The ``http.base_url`` relative paths resolve against, or ``None``.
        auth: The auth settings whose variable names the session resolves, or ``None`` for no
            credential.
        require_base_url: Whether the session must refuse a missing base URL at construction time,
            naming ``http.base_url``; ``foundry_api`` sets it and ``foundry_http`` does not.
        cassette_mode: The mode name the session reports; the cassette session governs behaviour.

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

    Function scoped (AAP 0.4.3): one session per test, closed in teardown, so two tests cannot share
    a connection or a cookie and an xdist worker runs exactly what a serial run runs.

    The session is built from the run's resolved settings and nothing else: the ``http.retries.*``
    policy decides how often a retryable fault is retried, ``http.timeout`` is the deadline of a
    request that names none, and ``http.headers`` are sent on every request. No credential is
    attached -- :mod:`foundry.http.auth` is handed ``None`` -- which makes this the fixture a test
    uses to drive a login flow itself and assert on the token it obtains (AAP 0.3.3).

    It carries **no base URL**, so ``path`` must be an absolute ``http``/``https`` URL; a relative
    one raises :class:`foundry.core.exceptions.ConfigError` from the session rather than resolving
    against something this fixture never had. It also takes **no cassette**: AAP 0.4.3 gives the
    cassette mode to ``foundry_api``, so a request made here reaches the peer in every run.

    Every status is returned as a :class:`foundry.http.FoundryResponse`, 4xx and 5xx included. Only
    a transport fault raises, as :class:`foundry.core.exceptions.TransportError` or, once the
    attempts are spent, :class:`foundry.core.exceptions.RetryExhaustedError`.

    Args:
        foundry_config: The configuration this session resolved, requested as a fixture argument so
            that a project which overrides the public fixture changes the settings this one is
            built from.

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

    Function scoped (AAP 0.4.3), because the cassette it may record belongs to this test alone and
    the credential is resolved for it: a test that changes an environment variable sees its own
    change and no other test does.

    Four settings are applied. ``http.base_url`` is joined against every relative path. The strategy
    ``http.auth.strategy`` names is resolved once, here, from the environment variables the
    ``http.auth.*_env`` keys name, and its header is sent after the static ``http.headers`` so a
    per-request header still wins. ``http.headers`` is sent on every request. And ``cassette.mode``
    is honoured: ``record`` captures this test's exchanges into
    ``cassettes/<sanitized-nodeid>.json``, redacted before the write; ``replay`` answers from that
    document on a transport that never reaches ``httpcore``, so no socket is opened; ``passthrough``
    replays a match and otherwise uses the live transport; ``off`` keeps no cassette at all.

    Args:
        request: The fixture request, read for the test's node id and the invocation directory.
        foundry_config: The resolved configuration, requested as a fixture argument so that a
            project which overrides the public fixture changes the client this one builds.

    Yields:
        The configured :class:`foundry.http.FoundrySession`, closed when the test ends.

    Raises:
        foundry.core.exceptions.ConfigError: ``http.base_url`` is unset, blank, not an absolute
            ``http``/``https`` URL or carries userinfo -- the message names ``http.base_url`` and
            carries no credential -- or a variable the configured auth strategy names is absent
            from the environment, in which case the message names the variable alone. Either fault
            errors the one test that requested this fixture while the rest of the session runs
            (AAP 0.4.7).
        foundry.core.exceptions.CassetteMissError: Raised by a request in ``replay`` mode whose
            method, query and body hash match no recorded interaction, naming the method, the path
            and the cassette path, with no socket opened (AAP 0.4.7).
    """
    from foundry.http import FoundrySession

    settings = _session_settings(
        foundry_config,
        base_url=foundry_config.http.base_url,
        auth=_auth_settings(foundry_config.http.auth),
        require_base_url=True,
        cassette_mode=str(foundry_config.cassette.mode),
    )
    cassette = _cassette_session(request, foundry_config)
    # The base URL and the credential are resolved by the constructor, before any transport exists,
    # so an unusable configuration raises here -- as the fixture is requested, naming the key or the
    # variable -- and nothing has been opened (AAP 0.4.7).
    session = FoundrySession(settings, cassette=cassette)
    try:
        yield session
    finally:
        # Close, and only close: the session saves the cassette this test recorded and releases the
        # transport it sent on, exactly once, reporting either fault as one WARNING. Calling the
        # cassette session's own finish() here as well would be a second capture of the same test.
        session.close()
