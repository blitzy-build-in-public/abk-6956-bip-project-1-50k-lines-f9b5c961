"""The sync-only HTTP/API layer: the session, its responses, its retry policy and its auth.

``foundry.http`` is the package a suite reaches the systems under test through, and this module is
its import surface: ``from foundry.http import FoundrySession`` is the AAP 0.4.3 form, and the five
names in :data:`__all__` are the whole of it. The layer is synchronous by design -- one request at a
time, no event loop, no background thread -- and it is built on ``httpx.BaseTransport`` rather than
on ``httpx.Client``, because four of the client's behaviours are ones Foundry must not have: a
cookie jar, redirect following, proxy discovery, and a deadline that would override ``http.timeout``
(AAP 0.3.1).

Every status is a value, never an exception
    :meth:`~foundry.http.session.FoundrySession.request` returns a
    :class:`~foundry.http.response.FoundryResponse` for a ``3xx``, ``4xx`` or ``5xx`` exactly as it
    does for a ``2xx``. Only a transport fault raises, as
    :class:`~foundry.core.exceptions.TransportError` or
    :class:`~foundry.core.exceptions.RetryExhaustedError` once ``http.retries.max_attempts`` is
    spent; a fault in the settings raises :class:`~foundry.core.exceptions.ConfigError` (AAP 0.7.3).

The transport seam is what makes replay socket-free
    Replay is a transport -- ``foundry.cassette.transport.ReplayTransport`` -- handed a recorded
    document and no second transport, so ``httpcore`` is never entered and no replay path reaches
    ``socket.connect``. That makes "replay opens no socket" a property of the object graph rather
    than a promise about behaviour (AAP 0.2.1, AAP 0.3.3, acceptance criterion 8).

Why the three settings types are exported
    :class:`~foundry.http.session.SessionSettings`,
    :class:`~foundry.http.retries.RetryPolicy` and :class:`~foundry.http.auth.AuthSettings` are the
    plain primitives the ``foundry_http`` and ``foundry_api`` fixtures build from resolved
    configuration when they construct a session -- ``foundry_http`` with no base URL and no auth,
    ``foundry_api`` with ``http.base_url``, the auth strategy, the static headers and the cassette
    mode. Publishing them keeps those fixtures on this one surface instead of reaching into three
    private modules. ``from_httpx`` is deliberately absent: it is the session's own construction
    seam and stays importable from :mod:`foundry.http.response`.

Layering and import-time discipline (AAP 0.4.8)
    This package imports :mod:`foundry.core`, :mod:`foundry.obs`, :mod:`foundry.contract` and
    :mod:`foundry.cassette`, and never :mod:`foundry.config` (settings arrive here as primitives),
    :mod:`foundry.reporting`, :mod:`foundry.cli`, :mod:`foundry.plugin` or
    :mod:`foundry.execution`. Nothing on ``foundry.plugin``'s import path reaches this package, so
    the ``httpx`` import inherited from :mod:`foundry.http.session` stays clear of the 150 ms
    startup budget. Importing this module defines names only: it opens no socket, reads no
    environment variable, configures no logger and builds no module-level state. Names may be added
    to :data:`__all__`; renaming or removing one is not permitted (AAP 0.4.9). Every re-export is
    written ``X as X``, which is what makes it a re-export under ``mypy --strict``'s
    no-implicit-reexport and a used import under ruff's F401.
"""

from __future__ import annotations

# The six ``http.auth.*`` keys as names of environment variables, holding no secret of their own.
from foundry.http.auth import AuthSettings as AuthSettings

# A reply as a value: status, headers, text, elapsed time, source, and the two redacted dumps.
from foundry.http.response import FoundryResponse as FoundryResponse

# The ``http.retries.*`` policy: how many attempts, the backoff, and the predicates that retry.
from foundry.http.retries import RetryPolicy as RetryPolicy

# The configured request pipeline, and the primitives a fixture builds one from.
from foundry.http.session import FoundrySession as FoundrySession
from foundry.http.session import SessionSettings as SessionSettings

__all__ = [
    "AuthSettings",
    "FoundryResponse",
    "FoundrySession",
    "RetryPolicy",
    "SessionSettings",
]
