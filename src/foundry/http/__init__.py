"""The sync-only HTTP/API layer: the session, its responses, its retry policy and its auth.

AAP 0.4.3 and AAP 0.7.3: every status is returned as a ``FoundryResponse``, never raised, and replay
is a transport, so a replay never reaches ``httpcore`` (AAP 0.4.8).
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
