"""Node-id set verification, worker-loss records, and the two xdist channel names.

Foundry verifies a distributed run; it never distributes one. pytest-xdist owns the scheduling, and
this module proves that the node ids the workers ran are exactly the set the controller collected --
the observation AAP 0.14.1 makes for criterion 7 -- and turns a worker that never reported into the
``run_error`` record AAP 0.4.6 carries. No distribution algorithm, worker count, load-balancing mode
or work queue lives here, because AAP 0.9.2 places reimplementing any of them out of scope.

A distributed run shares state only through what xdist provides, and each channel has one owner:
:data:`WORKER_INPUT_KEY` names the ``config.workerinput`` entry carrying the payload
:func:`foundry.execution.session.worker_payload` produces, :data:`WORKER_OUTPUT_KEY` names the
``config.workeroutput`` entry a worker fills with the node ids it ran, and per-test state rides
``TestReport.user_properties`` as :mod:`foundry.execution.state` defines. Everything crossing a
channel is serialisable (implicit requirement I3).

``foundry.execution`` may never import ``foundry.reporting`` (AAP 0.4.8), so the element this module
produces is :class:`foundry.execution.state.RunErrorPayload`, and ``plugin.py`` appends those
elements when it writes the result document. A lost worker leaves collected items with no result at
all (assumption A12), so :meth:`ShardingService.missing_after_run` names them for ``session.py`` to
emit as ``not_run`` records. Nothing here raises: a ``run_error`` is a record, not an exception, and
every collection the module returns is sorted, so two runs serialise one shape.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Final

from foundry.core.ids import to_iso8601, utc_now
from foundry.execution.state import RunErrorPayload
from foundry.obs.events import emit_sharding_decision
from foundry.obs.redaction import Redactor, default_redactor

__all__ = [
    "CONTROLLER_WORKER_ID",
    "NodeLedger",
    "RUN_ERROR_MESSAGE_MAX_BYTES",
    "RunErrorLedger",
    "ShardingReport",
    "ShardingService",
    "WORKER_INPUT_KEY",
    "WORKER_OUTPUT_KEY",
    "record_worker_output",
]

WORKER_OUTPUT_KEY: Final[str] = "foundry_nodeids"
"""The ``config.workeroutput`` entry a worker fills with the node ids it ran.

The value is a ``tuple[str, ...]``, so it crosses the worker boundary as plain serialisable data;
the controller reads it in ``pytest_testnodedown`` through :func:`record_worker_output`
(AAP 0.2.1).
"""

WORKER_INPUT_KEY: Final[str] = "foundry"
"""The ``config.workerinput`` entry carrying the payload ``session.worker_payload()`` produces.

It is the worker's copy of the run id, the artifacts root and the resolved configuration; the plugin
writes it on the controller and reads it on the worker, and nothing else writes ``workerinput``.
"""

CONTROLLER_WORKER_ID: Final[str] = "controller"
"""The id the controller's own collection is recorded under, so a lost worker's items stay known.

No xdist gateway uses this id, which is why it can name the serial collection inside a ledger whose
other keys are worker ids.
"""

RUN_ERROR_MESSAGE_MAX_BYTES: Final[int] = 2_000
"""Cap on a ``run_error`` message in UTF-8 bytes: enough for a traceback tail, bounded for a report.

Captured worker output can be arbitrarily large, and the message is persisted in ``results.json``,
``junit.xml`` and ``report.html``, so it is redacted first and cut second (AAP 0.4.6).
"""


def _text(value: object) -> str:
    """Coerce ``value`` to stripped text, and never raise while doing it."""
    if isinstance(value, str):
        return value.strip()
    if value is None:
        return ""
    try:
        return str(value).strip()
    except (TypeError, ValueError):
        # Failure-isolation rule: a value that cannot be rendered names nothing, never an exception.
        return ""


def _count(value: object) -> int:
    """Coerce ``value`` to a count of zero or more, and never raise while doing it."""
    try:
        return max(0, int(_text(value) or 0))
    except (TypeError, ValueError):
        # Failure-isolation rule: an unreadable count is reported as zero, never raised.
        return 0


def _clean(values: Iterable[object]) -> list[str]:
    """Return the non-blank node ids in ``values``, stripped, de-duplicated, in first-seen order.

    A lone ``str`` counts as one value rather than as its characters, and a value that is not text
    is ignored rather than rendered: a node id is text by contract (AAP 0.4.6), so inventing one
    from a number or a path would put an id into a report that no item answers to.
    """
    if isinstance(values, str):
        values = (values,)
    try:
        iterator = iter(values)
    except TypeError:
        # Failure-isolation rule: a value that is not iterable contributes nothing.
        return []
    seen: dict[str, None] = {}
    for item in iterator:
        name = item.strip() if isinstance(item, str) else ""
        if name and name not in seen:
            seen[name] = None
    return list(seen)


class NodeLedger:
    """The node ids each worker collected, and the controller's own serial collection.

    One instance per session, behind ``foundry.execution.state.SESSION_NODES``. Each worker's node
    ids keep first-seen order and are exposed sorted, so a repeated ``pytest_testnodedown`` for one
    gateway cannot double-count a node id and no set iteration reaches a report.
    """

    __slots__ = ("_by_worker", "_expected", "_expected_seen", "_seen")

    def __init__(self) -> None:
        """Start an empty ledger; nothing is recorded until a worker or the controller reports."""
        self._by_worker: dict[str, list[str]] = {}
        self._seen: dict[str, set[str]] = {}
        self._expected: list[str] = []
        self._expected_seen: set[str] = set()

    def record(self, worker_id: str, nodeids: Iterable[object]) -> None:
        """Add ``nodeids`` to ``worker_id``'s collection, de-duplicated and in first-seen order.

        A blank worker id names no worker, so nothing is recorded for it: the ``run_error`` record
        that reports a lost worker names that worker, and an entry no gateway answers to would be a
        second, empty worker in every count.
        """
        worker = _text(worker_id)
        if not worker:
            return
        recorded = self._by_worker.setdefault(worker, [])
        seen = self._seen.setdefault(worker, set())
        for nodeid in _clean(nodeids):
            if nodeid not in seen:
                seen.add(nodeid)
                recorded.append(nodeid)

    def record_expected(self, nodeids: Iterable[object]) -> None:
        """Record the controller's serial collection, de-duplicated and in first-seen order.

        Without it, :meth:`expected_nodes` falls back to what the workers reported, which is enough
        to compare sets but cannot name the items of a worker that never reported at all.
        """
        for nodeid in _clean(nodeids):
            if nodeid not in self._expected_seen:
                self._expected_seen.add(nodeid)
                self._expected.append(nodeid)

    def workers(self) -> tuple[str, ...]:
        """Return every worker id that reported a collection, sorted."""
        return tuple(sorted(self._by_worker))

    def nodes_for(self, worker_id: str) -> tuple[str, ...]:
        """Return one worker's node ids, sorted; an unknown worker id yields an empty tuple."""
        return tuple(sorted(self._by_worker.get(_text(worker_id), ())))

    def all_nodes(self) -> tuple[str, ...]:
        """Return every node id any worker reported, sorted and de-duplicated."""
        union: set[str] = set()
        for recorded in self._by_worker.values():
            union.update(recorded)
        return tuple(sorted(union))

    def expected_nodes(self) -> tuple[str, ...]:
        """Return the controller's serial collection, or the worker union when none was recorded."""
        if self._expected:
            return tuple(sorted(self._expected))
        return self.all_nodes()

    @property
    def worker_count(self) -> int:
        """Return how many workers reported a collection."""
        return len(self._by_worker)

    @property
    def total(self) -> int:
        """Return how many distinct node ids the workers reported between them."""
        return len(self.all_nodes())

    def duplicates(self) -> tuple[str, ...]:
        """Return the node ids more than one worker reported, sorted."""
        counts: dict[str, int] = {}
        for recorded in self._by_worker.values():
            for nodeid in recorded:
                counts[nodeid] = counts.get(nodeid, 0) + 1
        return tuple(sorted(nodeid for nodeid, count in counts.items() if count > 1))

    def as_dict(self) -> dict[str, list[str]]:
        """Return worker id to sorted node ids, with the worker keys sorted."""
        return {worker_id: list(self.nodes_for(worker_id)) for worker_id in self.workers()}


class RunErrorLedger:
    """The run's own failures, kept in insertion order and de-duplicated.

    One instance per session, behind ``foundry.execution.state.SESSION_RUN_ERRORS``. Two payloads
    are the same run error when the worker they name and the message they carry match, and only the
    first is kept: ``pytest_testnodedown`` can fire again for one crash, and a second entry would
    report one lost worker twice.
    """

    __slots__ = ("_entries", "_keys")

    def __init__(self) -> None:
        """Start an empty ledger; a run that loses no worker keeps it empty."""
        self._entries: list[RunErrorPayload] = []
        self._keys: set[tuple[str, str, str]] = set()

    def add(self, payload: RunErrorPayload) -> None:
        """Append ``payload`` unless the same worker and message are already recorded."""
        if not isinstance(payload, RunErrorPayload):
            return
        key = (payload.record_type, payload.worker_id, payload.message)
        if key not in self._keys:
            self._keys.add(key)
            self._entries.append(payload)

    def entries(self) -> tuple[RunErrorPayload, ...]:
        """Return every recorded run error, in insertion order."""
        return tuple(self._entries)

    @property
    def count(self) -> int:
        """Return how many distinct run errors are recorded."""
        return len(self._entries)

    def lost_workers(self) -> tuple[str, ...]:
        """Return the worker ids a recorded run error names, sorted and de-duplicated."""
        return tuple(sorted({_text(payload.worker_id) for payload in self._entries} - {""}))

    def as_dicts(self) -> tuple[dict[str, object], ...]:
        """Return every run error as the JSON-ready object ``results.json`` holds, in that order."""
        return tuple(payload.as_dict() for payload in self._entries)


def record_worker_output(
    ledger: NodeLedger, worker_id: str, worker_output: Mapping[str, object] | None
) -> tuple[str, ...]:
    """Record the node ids a finished worker published under :data:`WORKER_OUTPUT_KEY`.

    A worker that died never published ``config.workeroutput`` at all, so the mapping arrives here
    as ``None`` or without the key; both record nothing instead of raising, and the node ids now
    held for ``worker_id`` are returned so the caller can log their count.
    """
    if not isinstance(worker_output, Mapping):
        return ledger.nodes_for(worker_id)
    published = worker_output.get(WORKER_OUTPUT_KEY)
    if isinstance(published, str):
        ledger.record(worker_id, (published,))
    elif isinstance(published, Iterable):
        ledger.record(worker_id, tuple(published))
    return ledger.nodes_for(worker_id)


@dataclass(frozen=True, slots=True)
class ShardingReport:
    """What one comparison of the serial and the distributed node-id sets found.

    ``missing`` is in the controller's collection but in no worker's, ``duplicate`` was reported by
    more than one worker, ``extra`` was reported by a worker and never collected, and
    ``worker_losses`` names the workers the run recorded a ``run_error`` for. Every collection is
    sorted and de-duplicated on construction, so two runs produce one shape.
    """

    serial_count: int
    distributed_count: int
    worker_ids: tuple[str, ...] = ()
    per_worker: tuple[tuple[str, int], ...] = ()
    missing: tuple[str, ...] = ()
    duplicate: tuple[str, ...] = ()
    extra: tuple[str, ...] = ()
    worker_losses: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Canonicalise every collection: sorted, de-duplicated, and never a negative count."""
        object.__setattr__(self, "serial_count", _count(self.serial_count))
        object.__setattr__(self, "distributed_count", _count(self.distributed_count))
        object.__setattr__(self, "worker_ids", tuple(sorted(_clean(self.worker_ids))))
        object.__setattr__(
            self,
            "per_worker",
            tuple(
                sorted((_text(worker_id), _count(count)) for worker_id, count in self.per_worker)
            ),
        )
        object.__setattr__(self, "missing", tuple(sorted(_clean(self.missing))))
        object.__setattr__(self, "duplicate", tuple(sorted(_clean(self.duplicate))))
        object.__setattr__(self, "extra", tuple(sorted(_clean(self.extra))))
        object.__setattr__(self, "worker_losses", tuple(sorted(_clean(self.worker_losses))))

    @property
    def ok(self) -> bool:
        """Whether distribution covered the serial collection exactly, losing no worker."""
        return not (self.missing or self.duplicate or self.extra or self.worker_losses)

    @property
    def has_errors(self) -> bool:
        """Whether the run itself failed, which is what moves the exit code to ``1`` (AAP 0.4.7)."""
        return bool(self.worker_losses)

    def as_dict(self) -> dict[str, object]:
        """Return the report as a JSON-ready object with its keys sorted."""
        return {
            "distributed_count": self.distributed_count,
            "duplicate": list(self.duplicate),
            "extra": list(self.extra),
            "has_errors": self.has_errors,
            "missing": list(self.missing),
            "ok": self.ok,
            "per_worker": [
                {"count": count, "worker_id": worker_id} for worker_id, count in self.per_worker
            ],
            "serial_count": self.serial_count,
            "worker_ids": list(self.worker_ids),
            "worker_losses": list(self.worker_losses),
        }

    def summary(self) -> str:
        """Return the report as one line, for the caller that logs the decision."""
        return (
            f"sharding: workers={len(self.worker_ids)} serial={self.serial_count} "
            f"distributed={self.distributed_count} missing={len(self.missing)} "
            f"duplicate={len(self.duplicate)} extra={len(self.extra)} "
            f"losses={len(self.worker_losses)} ok={'true' if self.ok else 'false'}"
        )


@dataclass(frozen=True, slots=True)
class ShardingService:
    """The controller's view of a distributed run: compare the sets, record the lost workers.

    Frozen and read-only over two session-scoped ledgers, so the plugin holds it behind the session
    state and every call is a query. ``redactor`` defaults to the built-in deny-lists and the
    session widens it with the configured ``secrets.redact_keys``.
    """

    nodes: NodeLedger
    errors: RunErrorLedger
    run_id: str
    redactor: Redactor = field(default_factory=default_redactor)

    def verify(self, expected_nodeids: Iterable[object]) -> ShardingReport:
        """Compare ``expected_nodeids`` with what the workers reported, and log the decision.

        The controller's collection is recorded in the ledger as a side effect, so a worker lost
        after this point still leaves its items nameable by :meth:`missing_after_run`. A run with no
        recorded worker and no lost one is serial: the serial set is compared with itself.

        An empty ``expected_nodeids`` beside one or more reporting workers is an absent input rather
        than divergence, because xdist's distribution leaves the controller's ``session.items``
        empty; the workers' union then becomes the verified universe, and divergence is reported by
        ``duplicate`` -- the defect this check exists for. One ``DEBUG`` ``sharding_decision`` event
        per worker carries that worker's count and never a node id.
        """
        serial = tuple(sorted(_clean(expected_nodeids)))
        self.nodes.record_expected(serial)
        losses = self.errors.lost_workers()
        workers = self.nodes.workers()
        if not workers and not losses:
            serial_only = ShardingReport(
                serial_count=len(serial),
                distributed_count=len(serial),
            )
            emit_sharding_decision(CONTROLLER_WORKER_ID, serial_only.serial_count)
            return serial_only
        managed = self.nodes.all_nodes()
        serial_set = set(serial) or set(managed)
        managed_set = set(managed)
        report = ShardingReport(
            serial_count=len(serial_set),
            distributed_count=len(managed),
            worker_ids=workers,
            per_worker=tuple(
                (worker_id, len(self.nodes.nodes_for(worker_id))) for worker_id in workers
            ),
            missing=tuple(sorted(serial_set - managed_set)),
            duplicate=self.nodes.duplicates(),
            extra=tuple(sorted(managed_set - serial_set)),
            worker_losses=losses,
        )
        for worker_id, count in report.per_worker:
            emit_sharding_decision(worker_id, count)
        if not report.per_worker:
            # Every worker was lost before it reported: one event names the count that did reach a
            # record (none), so a sharded run never ends without a decision in the log.
            emit_sharding_decision(CONTROLLER_WORKER_ID, report.distributed_count)
        return report

    def worker_down(self, worker_id: str, message: str) -> RunErrorPayload:
        """Record a lost worker as the AAP 0.4.6 ``run_error`` element and return it.

        ``message`` is redacted before it is bounded, so a secret a worker echoed into its captured
        output can never reach ``results.json``, ``junit.xml`` or ``report.html``.
        """
        worker = _text(worker_id)
        payload = RunErrorPayload(
            run_id=self.run_id,
            recorded_at=to_iso8601(utc_now()),
            worker_id=worker,
            message=self._bounded(worker, message),
        )
        self.errors.add(payload)
        return payload

    def missing_after_run(
        self, reported_nodeids: Iterable[object], *, expected: Iterable[object] | None = None
    ) -> tuple[str, ...]:
        """Return the sorted node ids that produced no result, which become ``not_run`` records.

        The universe is ``expected`` when the caller passes the controller's collection, else the
        serial collection recorded by :meth:`verify`, else the union of what the workers reported.
        """
        universe = (
            tuple(sorted(_clean(expected))) if expected is not None else self.nodes.expected_nodes()
        )
        reported = set(_clean(reported_nodeids))
        return tuple(nodeid for nodeid in universe if nodeid not in reported)

    def errors_payloads(self) -> tuple[RunErrorPayload, ...]:
        """Return every recorded run error, in insertion order, for the result document."""
        return self.errors.entries()

    def exit_code_contribution(self) -> int:
        """Return ``1`` when the run lost a worker and ``0`` otherwise (AAP 0.4.7)."""
        return 1 if self.errors.lost_workers() else 0

    def _bounded(self, worker_id: str, message: str) -> str:
        """Return ``message`` redacted and cut to the run-error budget, never empty."""
        source = _text(message) or f"worker {worker_id or 'unknown'} was lost before it reported"
        try:
            return self.redactor.text(source, limit=RUN_ERROR_MESSAGE_MAX_BYTES)
        except (AttributeError, KeyError, TypeError, ValueError):
            # Failure-isolation rule: an unredactable message is replaced, never raised into a run.
            return f"worker {worker_id or 'unknown'} was lost before it reported"
