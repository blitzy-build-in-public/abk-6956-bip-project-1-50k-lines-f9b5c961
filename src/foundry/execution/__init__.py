"""The ``foundry.execution`` package: retries, quarantine, timeouts, sharding and the run's state.

Per-test state is held only in ``pytest.Stash`` entries keyed by the :class:`pytest.StashKey` values
:mod:`foundry.execution.state` declares -- never in a module-level global, and never in an attribute
attached to a ``pytest.Item`` -- and everything that must reach the controller from an
``pytest-xdist`` worker is JSON-serialisable, because a worker's reports are the only channel back
(AAP 0.4.8). Imports run one way: this package may import ``core``, ``obs``, ``config``, ``http``,
``cassette`` and ``data``, and must never import ``reporting``, ``cli`` or ``plugin``. The names
below are re-exported lazily through the PEP 562 :func:`__getattr__` rather than imported eagerly,
because ``plugin.py`` imports :mod:`foundry.execution.state` at module scope and importing a
submodule executes this file first: one eager sibling import here would land on the 150 ms
plugin-startup budget (AAP 0.14.2). :data:`__all__` and :data:`_LAZY_IMPORTS` are kept in sync and
in the same order, and a name outside the table raises ``AttributeError``, which is what lets ``from
foundry.execution import quarantine`` fall through to importing that submodule.
"""

from __future__ import annotations

import importlib
from collections.abc import Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

__all__: list[str] = [
    "ArtifactPayload",
    "AttemptLedger",
    "AttemptPayload",
    "EMPTY_LEDGER",
    "ExecutionRetries",
    "FailurePayload",
    "ITEM_ARTIFACTS",
    "ITEM_ATTEMPTS",
    "ITEM_LOG_RING",
    "ITEM_QUARANTINE",
    "NodeLedger",
    "OUTCOME_VALUES",
    "QUARANTINED_SKIP_MESSAGE",
    "QUARANTINE_MARKER",
    "QUARANTINE_PASSED_NOTE",
    "QuarantineDecision",
    "QuarantinePayload",
    "QuarantineRegistry",
    "RETRYABLE_PHASES",
    "RunErrorLedger",
    "RunErrorPayload",
    "SESSION_CONFIG",
    "SESSION_NODES",
    "SESSION_RUN_ERRORS",
    "SESSION_STATE",
    "SessionState",
    "ShardingReport",
    "ShardingService",
    "TIMEOUT_MARKER",
    "TestPayload",
    "TimeoutPolicy",
    "WORKER_INPUT_KEY",
    "WORKER_OUTPUT_KEY",
    "apply_timer",
    "assemble_payload",
    "bootstrap_session",
    "cancel_timer",
    "create_state",
    "is_quarantined",
    "is_timeout_failure",
    "not_run_payload",
    "quarantined_outcomes",
    "read_properties",
    "resolve_exit_status",
    "retry_label",
    "run_attempts",
    "session_state",
    "should_retry",
    "stash_session",
    "state_from_worker_payload",
    "worker_payload",
    "write_properties",
]

_LAZY_IMPORTS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "ArtifactPayload": "foundry.execution.state",
        "AttemptLedger": "foundry.execution.coordinator",
        "AttemptPayload": "foundry.execution.state",
        "EMPTY_LEDGER": "foundry.execution.coordinator",
        "ExecutionRetries": "foundry.execution.retry",
        "FailurePayload": "foundry.execution.state",
        "ITEM_ARTIFACTS": "foundry.execution.state",
        "ITEM_ATTEMPTS": "foundry.execution.state",
        "ITEM_LOG_RING": "foundry.execution.state",
        "ITEM_QUARANTINE": "foundry.execution.state",
        "NodeLedger": "foundry.execution.sharding",
        "OUTCOME_VALUES": "foundry.execution.state",
        "QUARANTINED_SKIP_MESSAGE": "foundry.execution.quarantine",
        "QUARANTINE_MARKER": "foundry.execution.quarantine",
        "QUARANTINE_PASSED_NOTE": "foundry.execution.quarantine",
        "QuarantineDecision": "foundry.execution.quarantine",
        "QuarantinePayload": "foundry.execution.state",
        "QuarantineRegistry": "foundry.execution.quarantine",
        "RETRYABLE_PHASES": "foundry.execution.retry",
        "RunErrorLedger": "foundry.execution.sharding",
        "RunErrorPayload": "foundry.execution.state",
        "SESSION_CONFIG": "foundry.execution.state",
        "SESSION_NODES": "foundry.execution.state",
        "SESSION_RUN_ERRORS": "foundry.execution.state",
        "SESSION_STATE": "foundry.execution.state",
        "SessionState": "foundry.execution.session",
        "ShardingReport": "foundry.execution.sharding",
        "ShardingService": "foundry.execution.sharding",
        "TIMEOUT_MARKER": "foundry.execution.timeout",
        "TestPayload": "foundry.execution.state",
        "TimeoutPolicy": "foundry.execution.timeout",
        "WORKER_INPUT_KEY": "foundry.execution.sharding",
        "WORKER_OUTPUT_KEY": "foundry.execution.sharding",
        "apply_timer": "foundry.execution.timeout",
        "assemble_payload": "foundry.execution.session",
        "bootstrap_session": "foundry.execution.session",
        "cancel_timer": "foundry.execution.timeout",
        "create_state": "foundry.execution.session",
        "is_quarantined": "foundry.execution.quarantine",
        "is_timeout_failure": "foundry.execution.timeout",
        "not_run_payload": "foundry.execution.session",
        "quarantined_outcomes": "foundry.execution.quarantine",
        "read_properties": "foundry.execution.state",
        "resolve_exit_status": "foundry.execution.session",
        "retry_label": "foundry.execution.retry",
        "run_attempts": "foundry.execution.coordinator",
        "session_state": "foundry.execution.session",
        "should_retry": "foundry.execution.retry",
        "stash_session": "foundry.execution.session",
        "state_from_worker_payload": "foundry.execution.session",
        "worker_payload": "foundry.execution.session",
        "write_properties": "foundry.execution.state",
    }
)
"""Which module defines each name of :data:`__all__`, for :func:`__getattr__` to import.

Read-only through :class:`~types.MappingProxyType` and never written after import, so this module
keeps no mutable module-level state (AAP 0.4.8).
"""


def __getattr__(name: str) -> object:
    """Resolve one published name on first access, importing its defining module at that point.

    A name outside :data:`_LAZY_IMPORTS` raises the ``AttributeError`` PEP 562 expects. Nothing is
    written back into the module globals, so a second access costs one dictionary lookup and returns
    the very same object.
    """
    module_name = _LAZY_IMPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module(module_name), name)


def __dir__() -> list[str]:
    """List the published surface, sorted, without importing the modules behind it."""
    return sorted(__all__)


if TYPE_CHECKING:
    # The runtime resolves these names through __getattr__, which a static checker cannot follow,
    # so the same published names are bound here under their real types. The ``as``-form is the
    # explicit re-export spelling, which keeps them from reading as unused imports.
    from foundry.execution.coordinator import EMPTY_LEDGER as EMPTY_LEDGER
    from foundry.execution.coordinator import AttemptLedger as AttemptLedger
    from foundry.execution.coordinator import run_attempts as run_attempts
    from foundry.execution.quarantine import QUARANTINE_MARKER as QUARANTINE_MARKER
    from foundry.execution.quarantine import QUARANTINE_PASSED_NOTE as QUARANTINE_PASSED_NOTE
    from foundry.execution.quarantine import QUARANTINED_SKIP_MESSAGE as QUARANTINED_SKIP_MESSAGE
    from foundry.execution.quarantine import QuarantineDecision as QuarantineDecision
    from foundry.execution.quarantine import QuarantineRegistry as QuarantineRegistry
    from foundry.execution.quarantine import is_quarantined as is_quarantined
    from foundry.execution.quarantine import quarantined_outcomes as quarantined_outcomes
    from foundry.execution.retry import RETRYABLE_PHASES as RETRYABLE_PHASES
    from foundry.execution.retry import ExecutionRetries as ExecutionRetries
    from foundry.execution.retry import retry_label as retry_label
    from foundry.execution.retry import should_retry as should_retry
    from foundry.execution.session import SessionState as SessionState
    from foundry.execution.session import assemble_payload as assemble_payload
    from foundry.execution.session import bootstrap_session as bootstrap_session
    from foundry.execution.session import create_state as create_state
    from foundry.execution.session import not_run_payload as not_run_payload
    from foundry.execution.session import resolve_exit_status as resolve_exit_status
    from foundry.execution.session import session_state as session_state
    from foundry.execution.session import stash_session as stash_session
    from foundry.execution.session import state_from_worker_payload as state_from_worker_payload
    from foundry.execution.session import worker_payload as worker_payload
    from foundry.execution.sharding import WORKER_INPUT_KEY as WORKER_INPUT_KEY
    from foundry.execution.sharding import WORKER_OUTPUT_KEY as WORKER_OUTPUT_KEY
    from foundry.execution.sharding import NodeLedger as NodeLedger
    from foundry.execution.sharding import RunErrorLedger as RunErrorLedger
    from foundry.execution.sharding import ShardingReport as ShardingReport
    from foundry.execution.sharding import ShardingService as ShardingService
    from foundry.execution.state import ITEM_ARTIFACTS as ITEM_ARTIFACTS
    from foundry.execution.state import ITEM_ATTEMPTS as ITEM_ATTEMPTS
    from foundry.execution.state import ITEM_LOG_RING as ITEM_LOG_RING
    from foundry.execution.state import ITEM_QUARANTINE as ITEM_QUARANTINE
    from foundry.execution.state import OUTCOME_VALUES as OUTCOME_VALUES
    from foundry.execution.state import SESSION_CONFIG as SESSION_CONFIG
    from foundry.execution.state import SESSION_NODES as SESSION_NODES
    from foundry.execution.state import SESSION_RUN_ERRORS as SESSION_RUN_ERRORS
    from foundry.execution.state import SESSION_STATE as SESSION_STATE
    from foundry.execution.state import ArtifactPayload as ArtifactPayload
    from foundry.execution.state import AttemptPayload as AttemptPayload
    from foundry.execution.state import FailurePayload as FailurePayload
    from foundry.execution.state import QuarantinePayload as QuarantinePayload
    from foundry.execution.state import RunErrorPayload as RunErrorPayload
    from foundry.execution.state import TestPayload as TestPayload
    from foundry.execution.state import read_properties as read_properties
    from foundry.execution.state import write_properties as write_properties
    from foundry.execution.timeout import TIMEOUT_MARKER as TIMEOUT_MARKER
    from foundry.execution.timeout import TimeoutPolicy as TimeoutPolicy
    from foundry.execution.timeout import apply_timer as apply_timer
    from foundry.execution.timeout import cancel_timer as cancel_timer
    from foundry.execution.timeout import is_timeout_failure as is_timeout_failure
