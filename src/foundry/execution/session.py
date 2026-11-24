"""Session bootstrap and per-run record assembly: the state one pytest run shares.

``plugin.py`` holds the pytest hooks; this is the plain Python it calls. :func:`bootstrap_session`
resolves the configuration once (AAP 0.14.2) and builds the :class:`SessionState`, which carries the
artifact writer, redaction engine, retry budget, quarantine registry and timeout policy, so no
consumer rebuilds one from configuration (AAP 0.4.8). :func:`assemble_payload` turns a final attempt
into the ``TestPayload`` the controller records, a boundary no reporting type crosses, and
:func:`resolve_exit_status` composes the exit code of AAP 0.4.4 and AAP 0.4.7. Importing defines
names only (AAP 0.4.8).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol

from foundry.config.cli_layer import CliInput
from foundry.config.merge import resolve
from foundry.config.models import FoundryConfig
from foundry.config.sources import ConfigSource
from foundry.core.exceptions import FoundryError
from foundry.core.ids import is_valid_run_id, parse_iso8601, to_iso8601, utc_now
from foundry.core.ids import run_id as new_run_id
from foundry.core.types import PHASES, AttemptRecord, Phase
from foundry.execution.quarantine import QuarantineDecision, QuarantineRegistry
from foundry.execution.retry import ExecutionRetries
from foundry.execution.sharding import NodeLedger, RunErrorLedger, ShardingService
from foundry.execution.state import (
    SESSION_CONFIG,
    SESSION_NODES,
    SESSION_RUN_ERRORS,
    SESSION_STATE,
    ArtifactPayload,
    FailurePayload,
    OutcomeValue,
    TestPayload,
    bounded_tail,
)
from foundry.execution.timeout import TimeoutPolicy
from foundry.obs.artifacts import ArtifactRef, ArtifactWriter
from foundry.obs.events import emit_run_start
from foundry.obs.redaction import Redactor

if TYPE_CHECKING:
    import pytest

__all__: Final[tuple[str, ...]] = (
    "FAILURE_EXIT_CODE",
    "PYTEST_PASSTHROUGH_EXIT_CODES",
    "PhaseReport",
    "SUCCESS_EXIT_CODE",
    "SessionState",
    "assemble_payload",
    "bootstrap_session",
    "create_state",
    "node_ledger",
    "not_run_payload",
    "resolve_exit_status",
    "run_error_ledger",
    "session_state",
    "sharding_service",
    "stash_node_ledger",
    "stash_run_errors",
    "stash_session",
    "state_from_worker_payload",
    "worker_payload",
)

SUCCESS_EXIT_CODE: Final[int] = 0
"""The exit code of a run no failure counts against (AAP 0.4.4)."""

FAILURE_EXIT_CODE: Final[int] = 1
"""The exit code of a failing test, a lost worker or a strictly quarantined failure."""

PYTEST_PASSTHROUGH_EXIT_CODES: Final[tuple[int, ...]] = (2, 3, 4, 5)
"""pytest's interrupt, internal-error, usage-error and nothing-collected codes, left unchanged.

Foundry never rewrites them (AAP 0.4.4): a configuration fault already ends the session with
``pytest.exit(returncode=2)``, so code 2 means the same thing on both surfaces.
"""

_FAILED: Final[str] = "failed"
_PASSED: Final[str] = "passed"

_PHASE_RANK: Final[Mapping[str, int]] = {phase: rank for rank, phase in enumerate(PHASES)}
"""Execution order of the phases, so failure entries come out setup, call, teardown."""

_REQUIRED_CONFIG_FIELDS: Final[tuple[str, ...]] = tuple(
    name for name in FoundryConfig.model_fields if name != "sources"
)
"""The model fields a payload's configuration dump must carry: ``worker_payload`` dumps them all.

A dump missing a section would otherwise validate against the field defaults and hand the worker
settings the controller never had.
"""

_UNRANKED: Final[int] = len(PHASES)
"""Rank for a report whose ``when`` names no phase: it sorts last and is skipped."""


class PhaseReport(Protocol):
    """The read-only part of a ``pytest.TestReport`` an outcome is derived from.

    ``pytest`` is imported for annotations only, so the report is described structurally: an outcome
    is a function of the phases a report set carries, and nothing else is read. The members are
    properties, so ``TestReport``'s attributes satisfy them and a hand-built stub does too.
    """

    @property
    def when(self) -> str:
        """Return the phase the report describes: ``setup``, ``call`` or ``teardown``."""
        ...

    @property
    def outcome(self) -> str:
        """Return pytest's outcome for that phase: ``passed``, ``failed`` or ``skipped``."""
        ...

    @property
    def longrepr(self) -> object:
        """Return the failure representation, or ``None`` when the phase did not fail."""
        ...


@dataclass(frozen=True, slots=True)
class SessionState:
    """Everything one run resolved once, shared by every hook, fixture and renderer.

    Frozen because the plugin holds it in stash and hands it to collaborators that must not alter
    what another one sees. It carries the objects the run's behaviour is decided by -- artifact
    writer, redaction engine, retry budget, quarantine registry, timeout policy -- so no consumer
    rebuilds one from configuration and no two consumers can disagree (AAP 0.4.8).
    ``run_id`` is also the artifact directory's name and ``artifacts_root`` is
    ``<artifacts.dir>/<run_id>``; ``artifacts_enabled`` is false when that directory could not be
    created; ``capture_lines`` and ``retries`` are the resolved ``logging.capture_lines`` and
    ``execution.retries`` bounds.
    """

    run_id: str
    profile: str
    config: FoundryConfig
    invocation_dir: Path
    artifacts_root: Path
    artifacts_enabled: bool
    writer: ArtifactWriter
    redactor: Redactor
    capture_lines: int
    retries: ExecutionRetries
    quarantine: QuarantineRegistry
    timeout: TimeoutPolicy
    started_at: datetime

    def banner_lines(self) -> tuple[str, str]:
        """Return the two lines the terminal summary prints to prove Foundry is active.

        The first is the activation proof AAP 0.4.2 publishes: a run with no configuration at all
        prints ``foundry: profile=default``. The second names the run id and the artifact directory,
        so an operator finds the run without opening a JSON document.

        Returns:
            Exactly two single-line strings holding no random value other than the run id.
        """
        return (
            f"foundry: profile={self.profile}",
            f"foundry: run {self.run_id} artifacts at {self.artifacts_root.as_posix()}",
        )

    def source_of(self, dotted_key: str) -> ConfigSource:
        """Return which configuration layer supplied ``dotted_key``, for diagnostics.

        A delegate to ``foundry.config.models.FoundryConfig.source_of``, so ``foundry doctor`` and
        the debug log read provenance from the run's own state instead of re-resolving the
        configuration they describe (AAP 0.4.5). An unregistered key raises ``ConfigError``.
        """
        return self.config.source_of(dotted_key)


def create_state(
    config: FoundryConfig,
    *,
    run_id: str | None = None,
    invocation_dir: Path | None = None,
    started_at: datetime | None = None,
) -> SessionState:
    """Build one run's state from a resolved configuration, resolving nothing further.

    The single constructor of :class:`SessionState`: the controller's bootstrap calls it, and so
    does every worker, with the controller's run id, start and invocation directory. Taking the
    configuration as an argument is what keeps resolution single. The artifact directory is created
    by :meth:`foundry.obs.artifacts.ArtifactWriter.ensure_run_dir`, whose one-warning failure
    isolation is AAP 0.4.7's.

    ``run_id`` defaults to one minted from ``started_at``; a worker passes the controller's, which
    is what makes one run share one artifact directory. ``invocation_dir`` defaults to the working
    directory, and a relative ``artifacts.dir`` resolves against it rather than against a worker's
    own directory. ``started_at`` is injected rather than read, so two runs differ only in the
    values the determinism criterion normalises. A run id of the wrong shape raises
    ``FoundryError``, and a budget outside its accepted range raises ``ConfigError``.
    """
    moment = utc_now() if started_at is None else started_at
    identifier = new_run_id(now=moment) if run_id is None else _validated_run_id(run_id)
    directory = Path.cwd() if invocation_dir is None else Path(invocation_dir).resolve()
    profile = str(config.profile)
    redactor = Redactor(config.secrets.redact_keys)
    parent = _artifacts_parent(config.artifacts.dir, directory)
    writer = ArtifactWriter(
        parent,
        run_id=identifier,
        profile=profile,
        redactor=redactor,
        keep_passed=config.artifacts.keep_passed,
        created_at=to_iso8601(moment),
    )
    return SessionState(
        run_id=identifier,
        profile=profile,
        config=config,
        invocation_dir=directory,
        artifacts_root=writer.run_dir,
        artifacts_enabled=writer.ensure_run_dir(),
        writer=writer,
        redactor=redactor,
        capture_lines=int(config.logging.capture_lines),
        retries=ExecutionRetries.from_config(config.execution.retries),
        quarantine=QuarantineRegistry(mode=config.execution.quarantine_mode),
        timeout=TimeoutPolicy.from_config(config.execution.timeout),
        started_at=moment,
    )


def bootstrap_session(
    *,
    cli_input: CliInput | None = None,
    invocation_dir: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> SessionState:
    """Resolve the configuration once, build the run's state and log the run's one ``INFO`` line.

    The controller's bootstrap, called from ``pytest_configure`` before collection, so the run id,
    the artifact directory and the profile are fixed before the first item is collected (AAP 0.4.2).
    Everything it needs is an argument: nothing reads the process environment unless ``environ`` is
    ``None``, and nothing reads the working directory unless ``invocation_dir`` is ``None``.

    ``cli_input`` is what the command line supplied, or ``None`` for an invocation that typed no
    ``--foundry-*`` option (AAP 0.4.7). ``invocation_dir`` is where pytest was invoked, or
    ``None`` for the working directory. ``environ`` is the environment to read ``FOUNDRY_*`` from,
    or ``None`` for the process environment. The first fatal configuration fault raises
    ``ConfigError`` from ``foundry.config.merge.resolve``, and no state is built when it does.
    """
    directory = None if invocation_dir is None else Path(invocation_dir)
    config = resolve(cli_overrides=cli_input, environ=environ, cwd=directory)
    state = create_state(config, invocation_dir=directory)
    emit_run_start(run_id=state.run_id, profile=state.profile, artifacts_root=state.artifacts_root)
    return state


# --- Stash plumbing: one accessor pair per typed key, so no module reaches into Stash.


def stash_session(config: pytest.Config, state: SessionState) -> None:
    """Store the run's state and its configuration, which are one resolution (AAP 0.14.2)."""
    config.stash[SESSION_STATE] = state
    config.stash[SESSION_CONFIG] = state.config


def session_state(config: pytest.Config) -> SessionState:
    """Return the run's state, which the bootstrap stored before collection began.

    Raises:
        foundry.core.exceptions.FoundryError: The state was read before the bootstrap ran; the hint
            names where the bootstrap belongs.
    """
    state = config.stash.get(SESSION_STATE, None)
    if state is None:
        raise FoundryError(
            "the Foundry session state was read before it was bootstrapped",
            hint="call bootstrap_session() from pytest_configure, before collection starts",
        )
    return state


def stash_node_ledger(config: pytest.Config, ledger: NodeLedger) -> None:
    """Store the node ledger the sharding verification reads."""
    config.stash[SESSION_NODES] = ledger


def node_ledger(config: pytest.Config) -> NodeLedger:
    """Return the session's node ledger, creating and storing it on first use."""
    return config.stash.setdefault(SESSION_NODES, NodeLedger())


def stash_run_errors(config: pytest.Config, ledger: RunErrorLedger) -> None:
    """Store the ledger of failures of the run itself."""
    config.stash[SESSION_RUN_ERRORS] = ledger


def run_error_ledger(config: pytest.Config) -> RunErrorLedger:
    """Return the session's run-error ledger, creating and storing it on first use."""
    return config.stash.setdefault(SESSION_RUN_ERRORS, RunErrorLedger())


def sharding_service(config: pytest.Config) -> ShardingService:
    """Return the controller's view of a distributed run, over the session's own ledgers.

    Raises:
        foundry.core.exceptions.FoundryError: The session was never bootstrapped, so no run id
            exists to attribute a lost worker to.
    """
    state = session_state(config)
    return ShardingService(
        nodes=node_ledger(config),
        errors=run_error_ledger(config),
        run_id=state.run_id,
        redactor=state.redactor,
    )


# --- The xdist handshake: the controller writes it, every worker rebuilds its state from it.


def worker_payload(state: SessionState) -> dict[str, object]:
    """Return the payload a worker needs to reconstruct the controller's state.

    Every value is JSON-serialisable, because xdist ferries the payload between processes: the
    scalars are text and numbers, and the configuration travels as the model's own JSON dump, which
    :func:`state_from_worker_payload` validates back into a configuration.

    The keys are the run id, profile, start, invocation directory, capture flag, capture bound and
    the resolved configuration: exactly those :func:`state_from_worker_payload` reads.
    """
    return {
        "run_id": state.run_id,
        "profile": state.profile,
        "started_at": to_iso8601(state.started_at),
        "invocation_dir": state.invocation_dir.as_posix(),
        "artifacts_enabled": state.artifacts_enabled,
        "capture_lines": state.capture_lines,
        "config": state.config.model_dump(mode="json"),
    }


def state_from_worker_payload(payload: Mapping[str, object]) -> SessionState:
    """Rebuild a worker's state from the controller's payload, re-resolving nothing.

    The worker half of the handshake: it shares the controller's run id, start, artifact directory
    and settings, so both sides write into one ``artifacts/<run_id>/`` tree and a record stamped
    on a worker carries the timestamps the controller's own records carry.

    A malformed payload raises rather than falling back to defaults: a worker that invented its own
    run id would write its results into a second, unreachable run.

    ``payload`` is the mapping stored in ``config.workerinput[WORKER_INPUT_KEY]``: every value is
    JSON-serialisable, so a payload that crossed a process boundary decodes as it was sent. A
    missing or mistyped field, a configuration dump that is not a resolved configuration, a scalar
    mirror that disagrees with it, or a run id or timestamp of the wrong shape raises
    ``FoundryError``.
    """
    if not isinstance(payload, Mapping):
        raise _payload_fault("payload", "a mapping of the controller's run state")
    run = _payload_text(payload, "run_id")
    profile = _payload_text(payload, "profile")
    started = parse_iso8601(_payload_text(payload, "started_at"))
    directory = Path(_payload_text(payload, "invocation_dir"))
    enabled = _payload_boolean(payload, "artifacts_enabled")
    capture_lines = _payload_integer(payload, "capture_lines")
    state = create_state(
        _payload_config(payload), run_id=run, invocation_dir=directory, started_at=started
    )
    if state.profile != profile or state.capture_lines != capture_lines:
        raise FoundryError(
            "the xdist worker payload's scalar fields contradict its config field",
            hint=f"expected profile={state.profile!r} capture_lines={state.capture_lines!r}",
        )
    return state if enabled else replace(state, artifacts_enabled=False)


# --- Per-record assembly --------------------------------------------------


def assemble_payload(
    *,
    state: SessionState,
    nodeid: str,
    markers: Iterable[str],
    reports: Sequence[PhaseReport],
    attempts: Sequence[AttemptRecord],
    quarantine: QuarantineDecision,
    artifacts: Sequence[ArtifactRef],
    log_tail: Iterable[str],
    started_at: datetime,
    finished_at: datetime,
) -> TestPayload:
    """Assemble one collected item's record from the data the capture phase already redacted.

    A pure function of its arguments -- no clock, no filesystem, no pytest -- so the same inputs
    yield an equal payload (AAP 0.13.1). The outcome comes from the final attempt's reports:
    ``error`` when setup or teardown failed, ``failed`` for a failing call, else ``passed`` or
    ``skipped``, with ``xfailed``/``xpassed`` read from the call report's ``wasxfail`` marker; a
    quarantine verdict then replaces it, so the record, the report, the JUnit element and the exit
    code agree (AAP 0.4.7). ``duration_ms`` sums the attempts' own elapsed times, artifacts carry
    run-relative paths, and ``log_tail`` is cut to the newest ``logging.capture_lines`` lines here,
    so a record crossing the worker boundary stays bounded (requirement I7).

    Returns:
        The record, with its markers, failures and artifacts in canonical order.
    """
    outcome = _outcome_from_reports(reports) if quarantine.state == "none" else quarantine.outcome
    traceback_path = _traceback_path(artifacts)
    return TestPayload(
        run_id=state.run_id,
        started_at=to_iso8601(started_at),
        finished_at=to_iso8601(finished_at),
        profile=state.profile,
        nodeid=nodeid,
        outcome=outcome,
        attempts=max(1, len(attempts)),
        duration_ms=max(0.0, sum(record.elapsed_ms for record in attempts)),
        markers=tuple(markers),
        failures=_failure_entries(reports, state.redactor, traceback_path),
        artifacts=tuple(
            ArtifactPayload(kind=ref.kind, path=ref.path, bytes=ref.bytes) for ref in artifacts
        ),
        quarantine=quarantine.as_payload(),
        log_tail=bounded_tail(log_tail, state.capture_lines),
        artifacts_disabled=not state.artifacts_enabled or state.writer.disabled,
    )


def not_run_payload(state: SessionState, nodeid: str) -> TestPayload:
    """Return the record of an item that never ran, carrying the partial data available.

    Assumption A12: a worker lost mid-run leaves collected items with no report, and the document
    names them ``not_run`` instead of silently omitting them. Nothing was observed, so there are no
    failures, artifacts, markers or quarantine verdict. ``attempts`` is the record schema's floor of
    one rather than the zero that happened: every element of the document carries at least one
    attempt, and ``not_run`` is the field that tells a reader nothing executed. Timestamps run from
    the run's start to now.
    """
    return TestPayload(
        run_id=state.run_id,
        started_at=to_iso8601(state.started_at),
        finished_at=to_iso8601(utc_now()),
        profile=state.profile,
        nodeid=nodeid,
        outcome="not_run",
        attempts=1,
        duration_ms=0.0,
        artifacts_disabled=not state.artifacts_enabled or state.writer.disabled,
    )


# --- The exit code --------------------------------------------------------


def resolve_exit_status(
    pytest_exitstatus: int,
    *,
    quarantine: QuarantineRegistry,
    decisions: Iterable[QuarantineDecision],
    run_error_count: int,
) -> int:
    """Compose the run's exit code from pytest's status, the verdicts and the run errors.

    The one composition point for AAP 0.4.4's exit codes and every AAP 0.4.7 row that changes one:

    1. pytest's ``2``, ``3``, ``4`` and ``5`` are returned unchanged, because Foundry never rewrites
       them (AAP 0.4.4); a configuration fault already exits 2 through ``pytest.exit``.
    2. Any ``run_error`` -- a lost xdist worker -- fails the run with ``1`` (AAP 0.4.7).
    3. Any decision carrying ``fails_run`` fails it with ``1``: ``QuarantineRegistry.run_failed`` is
       the single quarantine rule, so ``strict`` counts a marked failure and ``report`` does not.
    4. A pytest status of ``1`` whose every failure is a ``quarantined_failed`` in ``report`` mode
       becomes ``0``, the documented downgrade that keeps a flaky test visible.
    5. Otherwise pytest's status stands, so a failure no verdict explains is never downgraded.

    Returns:
        The exit code the process ends with, identically for ``pytest`` and for ``foundry run``.
    """
    recorded = tuple(decisions)
    if pytest_exitstatus in PYTEST_PASSTHROUGH_EXIT_CODES:
        return pytest_exitstatus
    if run_error_count > 0 or quarantine.run_failed(recorded):
        return FAILURE_EXIT_CODE
    if pytest_exitstatus == FAILURE_EXIT_CODE and _only_quarantined_failures(recorded):
        return SUCCESS_EXIT_CODE
    return pytest_exitstatus


# --- Private helpers ------------------------------------------------------


def _validated_run_id(value: str) -> str:
    """Return ``value`` as a run id.

    Raises:
        foundry.core.exceptions.FoundryError: The value does not match ``RUN_ID_PATTERN``.
    """
    candidate = value.strip()
    if not is_valid_run_id(candidate):
        raise FoundryError(
            f"malformed run id: {value!r}",
            hint="a run id is a UTC timestamp, a hyphen and six lowercase hex characters",
        )
    return candidate


def _artifacts_parent(configured: Path, invocation_dir: Path) -> Path:
    """Return the directory the run's artifact directory is created inside.

    ``artifacts.dir`` is relative to the invocation directory by default and an absolute value is
    used as it stands, so ``artifacts/<run_id>/`` appears where the documented command was run
    (AAP 0.4.6) instead of where a worker's process happens to sit.
    """
    return configured if configured.is_absolute() else invocation_dir / configured


def _payload_text(payload: Mapping[str, object], key: str) -> str:
    """Return a required text field of a worker payload, or raise naming the field."""
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise _payload_fault(key, "non-empty text")
    return value


def _payload_boolean(payload: Mapping[str, object], key: str) -> bool:
    """Return a required boolean field of a worker payload, or raise naming the field."""
    value = payload.get(key)
    if not isinstance(value, bool):
        raise _payload_fault(key, "a boolean")
    return value


def _payload_integer(payload: Mapping[str, object], key: str) -> int:
    """Return a required integer field of a worker payload, or raise naming the field."""
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise _payload_fault(key, "an integer")
    return value


def _payload_config(payload: Mapping[str, object]) -> FoundryConfig:
    """Return the resolved configuration a worker payload carries, or raise naming the field."""
    value = payload.get("config")
    if not isinstance(value, Mapping):
        raise _payload_fault("config", "a mapping of resolved configuration values")
    missing = sorted(set(_REQUIRED_CONFIG_FIELDS).difference(value))
    if missing:
        raise _payload_fault("config", f"complete, but {', '.join(missing)} is missing")
    try:
        return FoundryConfig.model_validate(dict(value))
    except ValueError as exc:
        raise _payload_fault("config", "a mapping of resolved configuration values") from exc


def _payload_fault(key: str, expectation: str) -> FoundryError:
    """Return the fault a malformed worker payload raises, naming the field and its shape."""
    return FoundryError(
        f"the xdist worker payload's {key} field must be {expectation}",
        hint="write the payload with session.worker_payload(), never by hand",
    )


def _phase_of(when: str) -> Phase | None:
    """Return ``when`` as a phase, or ``None`` for a report that names no phase."""
    for phase in PHASES:
        if phase == when:
            return phase
    return None


def _outcome_from_reports(reports: Sequence[PhaseReport]) -> OutcomeValue:
    """Return the outcome one attempt's reports describe, before any quarantine verdict.

    A setup failure is an ``error``, and so is a teardown failure after a passing call, because
    both are failures outside the test body (AAP 0.4.6); a failing call is ``failed``.
    ``wasxfail`` on the call report is what separates an xfail or xpass from an ordinary skip or
    pass.
    """
    by_phase = {report.when: report for report in reports}
    setup = by_phase.get("setup")
    if setup is not None and setup.outcome == _FAILED:
        return "error"
    call = by_phase.get("call")
    if call is None:
        teardown = by_phase.get("teardown")
        return "error" if teardown is not None and teardown.outcome == _FAILED else "skipped"
    if getattr(call, "wasxfail", None) is not None:
        return "xpassed" if call.outcome == _PASSED else "xfailed"
    if call.outcome == _FAILED:
        return "failed"
    if call.outcome != _PASSED:
        return "skipped"
    teardown = by_phase.get("teardown")
    if teardown is not None and teardown.outcome == _FAILED:
        return "error"
    return "passed"


def _failure_entries(
    reports: Sequence[PhaseReport], redactor: Redactor, traceback_path: str | None
) -> tuple[FailurePayload, ...]:
    """Return one redacted failure entry per failing phase, in setup-call-teardown order."""
    entries: list[FailurePayload] = []
    for report in sorted(reports, key=lambda item: _PHASE_RANK.get(item.when, _UNRANKED)):
        phase = _phase_of(report.when)
        if phase is None or report.outcome != _FAILED:
            continue
        longrepr = report.longrepr
        message = redactor.text("" if longrepr is None else str(longrepr))
        entries.append(FailurePayload(phase=phase, message=message, traceback_path=traceback_path))
    return tuple(entries)


def _traceback_path(artifacts: Sequence[ArtifactRef]) -> str | None:
    """Return the run-relative path of the traceback artifact, or ``None`` when none was written."""
    for ref in sorted(artifacts, key=lambda item: (item.kind, item.path)):
        if ref.kind == "traceback":
            return ref.path
    return None


def _only_quarantined_failures(decisions: Sequence[QuarantineDecision]) -> bool:
    """Return whether every failure decision is a quarantined failure no mode fails the run for.

    The downgrade of AAP 0.4.7 needs a failure to downgrade: a run pytest failed with no decision to
    explain it keeps pytest's status.
    """
    failing = tuple(
        decision
        for decision in decisions
        if decision.outcome in ("failed", "error", "quarantined_failed")
    )
    if not failing:
        return False
    return all(decision.state == "failed" and not decision.fails_run for decision in failing)
