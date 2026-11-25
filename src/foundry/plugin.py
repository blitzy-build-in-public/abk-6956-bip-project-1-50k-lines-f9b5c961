"""The plugin adapter: Foundry's only module that implements a pytest hookspec (AAP 0.4.8).

Installing the distribution is the whole activation: the ``pytest11`` entry point named ``foundry``
targets this module, so pytest imports it during ``_preparse``, and the option group, the owned
marker, the twelve fixtures, the run identity and the three report files are in place with no flag,
no ``conftest.py`` declaration and no configuration file (AAP 0.4.2).

**Import budget** (AAP 0.14.2). Module scope imports ``pytest``, ``typing``, ``foundry.core`` and
``foundry.execution.state`` only; everything heavier is imported inside the hook that needs it.

**State and isolation** (AAP 0.4.8, AAP 0.1.2). Run state lives in ``config.stash`` behind
``foundry.execution.state``'s keys, per-item state in ``item.stash``, and what crosses a worker
boundary rides ``TestPayload`` on ``TestReport.user_properties`` (I3); Foundry's own faults are one
``WARNING`` and change neither an outcome nor an exit code, the ``report.html`` fallback excepted.
"""

from __future__ import annotations

import typing
from typing import Final

import pytest

from foundry.core import exceptions, ids
from foundry.execution import state as exec_state

if typing.TYPE_CHECKING:
    from collections.abc import Generator, Sequence
    from pathlib import Path

    from foundry.core.types import AttemptRecord
    from foundry.execution.session import SessionState
    from foundry.execution.state import RunErrorPayload, TestPayload
    from foundry.reporting.record import ResultElement

_COMPONENT: Final[str] = "plugin"
"""The component name every plumbing warning from this module carries, matching its dotted path."""

_FIXTURES_PLUGIN_NAME: Final[str] = "foundry-fixtures"
"""The name the twelve-fixture module is registered under, and the guard against a second time."""

_RECORDS_PLUGIN_NAME: Final[str] = "foundry-records"
"""The name the controller's report collector is registered under (AAP 0.4.2)."""

_XDIST_PLUGIN_NAME: Final[str] = "foundry-xdist"
"""The name the deferred xdist bridge is registered under, only when xdist is loaded."""

_TIMEOUT_PLUGIN_NAME: Final[str] = "foundry-timeout"
"""The name the deferred pytest-timeout substitution is registered under (AAP 0.3.2)."""

_OPTION_GROUP: Final[str] = "foundry"
"""The ``--help`` group the whole ``--foundry-*`` surface is listed under (AAP 0.4.4)."""

_CONFIG_EXIT: Final[int] = 2
"""The exit code a configuration or usage fault ends the session with, on both surfaces (A4)."""

_USAGE_EXIT: Final[int] = 4
"""pytest's usage-error code, preserved unchanged: no run at this code writes a report.

The other codes skip the write only in the named empty states: exit 5 when a deselecting filter
emptied the run, exit 0 under ``--collect-only``, and any code whose configuration fault left no
session state.
"""

_JSON_REPORT_OPTION_DEST: Final[str] = "foundry_json_report"
"""The ``dest`` :func:`foundry.config.cli_layer.alias_dest` gives ``--foundry-json-report``."""

_FLAG_KINDS: Final[frozenset[str]] = frozenset({"record-flag", "replay-flag"})
"""The two named options that are switches rather than value options (AAP 0.4.4)."""

_START_PHASE: Final[str] = "setup"
"""The phase whose report opens one attempt, and so the point the in-flight report list resets."""

_DATA_MARKER: Final[str] = "foundry_data"
"""The opt-in marker binding a test to one data file's cases, delegated to ``foundry.data``."""


class _TerminalReporter(typing.Protocol):
    """The two terminal-reporter members the profile banner is written through (AAP 0.7.4).

    A protocol: only these two members are touched, so no ``rich`` import and no reporter state.
    """

    def write_sep(self, sep: str, title: str, **markup: bool) -> object:
        """Write a separator line with ``title``, as pytest's own sections are written."""
        ...

    def write_line(self, line: str, **markup: bool) -> object:
        """Write one line to the terminal."""
        ...


class _XdistNode(typing.Protocol):
    """The members an xdist gateway and a worker controller both provide, as read here (AAP 0.2.1).

    ``pytest-xdist`` ships no types, so importing it for annotations would need a fragile ignore.
    """

    workerinput: dict[str, object]
    """The mapping a worker receives at start-up, holding its ``workerid`` among other keys."""

    workeroutput: dict[str, object]
    """The mapping a worker fills at the end, carrying the node ids it ran."""


def pytest_addoption(parser: pytest.Parser, pluginmanager: pytest.PytestPluginManager) -> None:
    """Declare the whole ``--foundry-*`` option surface, with no configuration file read.

    Historic hook, so it runs before any argument is parsed for an entry-point plugin too (AAP
    0.4.2). Registered are the aliases AAP 0.4.4 publishes, a generic ``--foundry-<dotted.key>`` for
    every *scalar* key -- criterion 4's ``--foundry-http.timeout 1.0`` and ``--foundry-timeout``
    both exist -- and a placeholder for a misspelt spelling, which AAP 0.4.7 answers with exit 2
    where the parser answers 4. Nothing is read from the parsed namespace, so ``--help`` is safe.
    """
    from foundry.config import cli_layer, keys

    group = parser.getgroup(_OPTION_GROUP, "Foundry test framework")
    for option in cli_layer.SPECIAL_OPTIONS:
        _add_option(
            group,
            option.spelling,
            cli_layer.alias_dest(option.spelling),
            help_text="switch" if option.kind in _FLAG_KINDS else "path",
            is_flag=option.kind in _FLAG_KINDS,
        )
    for spec in keys.KEY_SPECS:
        spelling = keys.cli_option(spec.dotted)
        if keys.is_scalar(spec):
            _add_option(
                group,
                spelling,
                keys.option_dest(spec.dotted),
                help_text=f"{spec.dotted} ({spec.constraint})",
            )
        for alias in spec.aliases:
            if alias == spelling:
                continue
            _add_option(
                group,
                alias,
                cli_layer.alias_dest(alias),
                help_text=f"{spec.dotted} ({spec.constraint})",
            )
    cli_layer.register_unregistered_options(parser, pluginmanager)


def _add_option(
    group: pytest.OptionGroup,
    spelling: str,
    dest: str,
    *,
    help_text: str,
    is_flag: bool = False,
) -> None:
    """Register one option under ``dest``, the name ``foundry.config.cli_layer`` reads it back by.

    ``dest`` is never derived here: the registry's ``option_dest`` and ``cli_layer.alias_dest``
    state the convention, and the reader calls the same two functions. Every value option is
    read as text, so ``foundry.config.validation.coerce_cli_value`` judges it -- a numeric key's
    declared type included -- and a bad value ends the run at 2 rather than at pytest's 4 (A4).
    """
    if is_flag:
        group.addoption(spelling, dest=dest, action="store_true", default=None, help=help_text)
        return
    group.addoption(
        spelling,
        dest=dest,
        default=None,
        type=str,
        metavar="VALUE",
        help=help_text,
    )


@pytest.hookimpl(tryfirst=True)
def pytest_load_initial_conftests(early_config: pytest.Config, args: list[str]) -> None:
    """Repair what the pre-parse read before this module's options were registered (AAP 0.2.1)."""
    from foundry.config import cli_layer

    cli_layer.restore_preparse_state(early_config, args)


@pytest.hookimpl(trylast=True)
def pytest_configure(config: pytest.Config) -> None:
    """Resolve the run once and register before collection, exiting 2 on a configuration fault (A4).

    ``trylast`` so every peer plugin has configured: the deferred registrations see the final plugin
    set, and the ``pytest-timeout`` seed overwrites that peer's derived value, not the reverse.
    """
    from foundry.execution import quarantine, session

    config.addinivalue_line(
        "markers",
        f"{quarantine.QUARANTINE_MARKER}: quarantined test. A failure is recorded as "
        "quarantined_failed and, in the default 'report' mode, does not fail the run.",
    )
    config.addinivalue_line(
        "markers",
        f'{_DATA_MARKER}("<file>"[, argname="<name>"]): the cases of one data file, from data.dir.',
    )
    _register_fixtures(config)
    _register_peers(config)
    try:
        state = _boot(config)
    except exceptions.FoundryError as exc:
        pytest.exit(exc.format_message(), returncode=_CONFIG_EXIT)
    session.stash_session(config, state)
    _seed_timeout(config, state)


def _boot(config: pytest.Config) -> SessionState:
    """Return this process's run state, adopting the controller's on an xdist worker.

    Resolving on a worker would mint a second run id and artifact directory, so it adopts the
    controller's state (requirement I3); ``config.workerinput`` exists only there (AAP 0.4.8).
    """
    from foundry.config import cli_layer
    from foundry.execution import session
    from foundry.execution.sharding import WORKER_INPUT_KEY

    inbound = getattr(config, "workerinput", None)
    if isinstance(inbound, dict):
        shared = inbound.get(WORKER_INPUT_KEY)
        if isinstance(shared, dict):
            return session.state_from_worker_payload(shared)
    return session.bootstrap_session(
        cli_input=cli_layer.collect_from_pytest(config),
        invocation_dir=config.invocation_params.dir,
    )


def _register_fixtures(config: pytest.Config) -> None:
    """Register the twelve public fixtures before collection, once per process (AAP 0.4.2).

    One module object, not twelve registrations: fixture discovery scans whatever it is handed and
    ``foundry.fixtures.plugin`` re-exports each fixture itself, guarded against a second configure.
    """
    if config.pluginmanager.hasplugin(_FIXTURES_PLUGIN_NAME):
        return
    from foundry.fixtures import plugin as fixtures_plugin

    config.pluginmanager.register(fixtures_plugin, _FIXTURES_PLUGIN_NAME)


def _register_peers(config: pytest.Config) -> None:
    """Register the collector and the two peer hooks, each only where it can be registered.

    Declaring the xdist or pytest-timeout hookspecs without that peer fails pluggy's validation, so
    both live behind ``hasplugin`` (AAP 0.3.3), and neither peer is imported here.
    """
    manager = config.pluginmanager
    if manager.hasplugin("xdist") and not manager.hasplugin(_XDIST_PLUGIN_NAME):
        manager.register(_XdistBridge(config), _XDIST_PLUGIN_NAME)
    if manager.hasplugin("timeout") and not manager.hasplugin(_TIMEOUT_PLUGIN_NAME):
        manager.register(_TimeoutHook(config), _TIMEOUT_PLUGIN_NAME)
    if not manager.hasplugin(_RECORDS_PLUGIN_NAME):
        manager.register(_RecordCollector(config), _RECORDS_PLUGIN_NAME)


def _seed_timeout(config: pytest.Config, state: SessionState) -> None:
    """Make the resolved ``execution.timeout`` the limit ``pytest-timeout`` enforces.

    The peer derives its settings once and arms a timer only for a positive limit; this hook runs
    ``trylast`` and overwrites them from the run's policy, keeping one timer (AAP 0.4.7).
    """
    if not config.pluginmanager.hasplugin("timeout"):
        return
    policy = state.timeout
    option = getattr(config, "option", None)
    if option is not None and hasattr(option, "timeout"):
        option.timeout = policy.seconds
    for name, value in (
        ("_env_timeout", policy.seconds),
        ("_env_timeout_func_only", True),
        ("_env_timeout_method", policy.method),
    ):
        if hasattr(config, name):
            setattr(config, name, value)


def pytest_sessionstart(session: pytest.Session) -> None:
    """Ensure this run's ``artifacts/<run_id>/`` directory exists, and never fail the run for it.

    The bootstrap already created it, so this is idempotent for a worker; an unwritable directory is
    the writer's single ``WARNING`` and leaves every record carrying ``artifacts_disabled`` (0.4.7).
    The same hook puts the run's redactor in front of pytest's terminal (AAP 0.14.3).
    """
    state = session.config.stash.get(exec_state.SESSION_STATE, None)
    if state is None:
        return
    try:
        state.writer.ensure_run_dir()
    except Exception as exc:
        # Failure-isolation rule: an artifact directory may not change an outcome or an exit code.
        _warn("could not prepare this run's artifact directory", exc)
    try:
        _scrub_terminal(session.config, state)
    except Exception as exc:  # Failure-isolation rule: neither outcome nor exit code changes
        _warn("could not install the terminal redactor", exc)


def _scrub_terminal(config: pytest.Config, state: SessionState) -> None:
    """Make pytest's terminal a sink of the run's redactor, so no secret is printed (AAP 0.14.3)."""
    from foundry.obs import capture

    reporter = config.pluginmanager.get_plugin("terminalreporter")
    capture.scrub_terminal_writer(getattr(reporter, "_tw", None), state.redactor)


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    """Parametrise a marked test over one data file's cases, delegating to :mod:`foundry.data`.

    ``foundry_data("<file>"[, argname="<name>"])`` names a file under ``data.dir``; a loader fault
    propagates as pytest's collection error, and an unmarked test returns before ``foundry.data``
    is imported (AAP 0.3.3, 0.4.7).
    """
    definition = metafunc.definition
    marker = None if definition is None else definition.get_closest_marker(_DATA_MARKER)
    if marker is None:
        return
    from foundry.data.registry import generate_cases
    from foundry.execution import session

    state = session.session_state(metafunc.config)
    data = state.config.data
    data_dir = state.invocation_dir / data.dir
    generate_cases(metafunc, marker=marker, data_dir=data_dir, strict=data.schema_strict)


def pytest_runtest_protocol(item: pytest.Item, nextitem: pytest.Item | None) -> bool:
    """Run one item through Foundry's attempt loop, and stop the ``firstresult`` chain.

    A one-line delegation: :func:`foundry.execution.coordinator.run_attempts` invokes pytest's own
    ``runtestprotocol`` per attempt, so only the final attempt's reports reach the log. Retries are
    in-framework to interoperate with the quarantine verdict and artifact capture (AAP 0.5.1).
    """
    from foundry.execution.coordinator import run_attempts

    return run_attempts(item, nextitem)


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    """Stamp each phase's report with the payload the controller records (requirement I3).

    Fires for ``setup``, ``call`` and ``teardown``. ``tryfirst`` because this is a ``wrapper``: the
    stamp must land on the report every other implementation has finished with, and the payload is
    what an xdist worker serialises back. The outcome is never rewritten and no test exception is
    touched -- the exit code is composed on the controller from the verdicts -- and a stamping fault
    is one ``WARNING`` that leaves the report as pytest built it (AAP 0.1.2). ``KeyboardInterrupt``
    and pytest's own ``Exit`` propagate untouched, so an interrupted run stays interrupted.
    """
    report = yield
    try:
        _stamp(item, report)
    except Exception as exc:
        # Failure-isolation rule: Foundry's own capture may not change a test's outcome.
        _warn("could not stamp a report with Foundry's record data", exc)
    return report


def _stamp(item: pytest.Item, report: pytest.TestReport) -> None:
    """Assemble the item's payload from the data available and write it onto ``report``.

    The attempt's reports live in :data:`~foundry.execution.state.ATTEMPT_REPORTS`, cleared by that
    attempt's ``setup``, since a ``teardown`` report alone cannot tell a pass from a skip. The
    verdict is the registry's.
    """
    from foundry.execution import coordinator, quarantine, session

    state = session.session_state(item.config)
    reports = item.stash.setdefault(exec_state.ATTEMPT_REPORTS, [])
    if report.when == _START_PHASE:
        reports.clear()
    reports.append(report)
    markers = tuple(marker.name for marker in item.iter_markers())
    attempts: tuple[AttemptRecord, ...] = coordinator.ledger_of(item).records
    outcome = coordinator.final_outcome(tuple(reports))
    decision = item.stash.get(exec_state.ITEM_QUARANTINE, None) or state.quarantine.decide(
        quarantined=quarantine.is_quarantined(markers), outcome=outcome
    )
    artifacts, log_tail = coordinator.capture_artifacts(
        item, tuple(reports), final=report.when == "teardown"
    )
    payload = session.assemble_payload(
        state=state,
        nodeid=item.nodeid,
        markers=markers,
        reports=tuple(reports),
        attempts=(*attempts, _in_flight(item, outcome)),
        quarantine=decision,
        artifacts=artifacts,
        log_tail=log_tail,
        started_at=item.stash.get(exec_state.ITEM_STARTED_AT, state.started_at),
        finished_at=ids.utc_now(),
    )
    exec_state.write_properties(report, payload)


def _in_flight(item: pytest.Item, outcome: exec_state.OutcomeValue) -> AttemptRecord:
    """Return the attempt in flight as the record its ledger holds once it ends.

    The coordinator appends an attempt *after* its reports are made, so at the last phase the ledger
    still holds only the attempts already finished; this adds the current one, which is what makes
    ``attempts`` and ``duration_ms`` the counts the record publishes (AAP 0.4.6, assumption A2). The
    span comes from the per-attempt stamp the coordinator sets, so it covers this attempt alone:
    ``assemble_payload`` sums the ledger and ``duration_ms`` is the item's wall clock across all
    attempts, not a total that counts earlier ones twice. An empty ledger falls back to the item's
    own start, and a stamp a stash fault cost reports ``0.0`` rather than the whole item's span.
    """
    import time

    attempts = item.stash.get(exec_state.ITEM_ATTEMPTS, ())
    started = item.stash.get(exec_state.ITEM_ATTEMPT_STARTED_MONOTONIC, None)
    if started is None and not attempts:
        started = item.stash.get(exec_state.ITEM_STARTED_MONOTONIC, None)
    elapsed = 0.0 if started is None else max(0.0, (time.monotonic() - started) * 1000.0)
    count = len(attempts) + 1
    return exec_state.AttemptPayload(number=count, outcome=outcome, elapsed_ms=elapsed).to_record()


class _RecordCollector:
    """The controller's payload collector: one record per node id, the last phase winning.

    Its hookspec carries no ``config``, so the state it fills lives on a registered object.
    """

    def __init__(self, config: pytest.Config) -> None:
        """Store the configuration whose stash holds the payloads."""
        self._config = config

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        """Record the payload ``report`` carries, naming a lost worker's item ``not_run`` (A12)."""
        from foundry.execution.session import not_run_payload, session_state

        try:
            payload = exec_state.read_properties(report)
            if payload is None and report.when not in exec_state.PHASES:
                payload = not_run_payload(session_state(self._config), report.nodeid)
            if payload is not None:
                self._config.stash.setdefault(exec_state.RUN_RECORDS, {})[payload.nodeid] = payload
        except Exception as exc:
            # Failure-isolation rule: a bookkeeping fault may not change an outcome or exit code.
            _warn("could not record a test's result payload", exc)


class _XdistBridge:
    """The three xdist hookspecs Foundry implements, registered only when xdist is loaded.

    Declaring them unconditionally fails pluggy's validation when the peer is absent, so they live
    here rather than as module-level functions (AAP 0.2.1). Neither body imports ``xdist``.
    """

    def __init__(self, config: pytest.Config) -> None:
        """Store the controller's configuration, which owns the run state."""
        self._config = config

    def pytest_configure_node(self, node: _XdistNode) -> None:
        """Ferry the controller's run state to ``node`` before it starts (AAP 0.4.8).

        The payload is the only way a worker learns the run id, the start, the invocation directory
        and the resolved configuration, so both processes write one ``artifacts/<run_id>/`` tree.
        """
        from foundry.execution import session
        from foundry.execution.sharding import WORKER_INPUT_KEY

        state = self._config.stash.get(exec_state.SESSION_STATE, None)
        if state is None:
            return
        node.workerinput[WORKER_INPUT_KEY] = session.worker_payload(state)

    def pytest_xdist_node_collection_finished(self, node: _XdistNode, ids: Sequence[str]) -> None:
        """Record the ids ``node`` collected, so a lost worker's items stay nameable (A12)."""
        from foundry.execution.session import node_ledger

        try:
            node_ledger(self._config).record_expected(ids)
        except Exception as exc:
            # Failure-isolation rule: a bookkeeping fault changes no outcome or exit code.
            _warn("could not record a worker's collected node ids", exc)

    def pytest_testnodedown(self, node: _XdistNode, error: object) -> None:
        """Record what ``node`` ran, and turn a lost worker into a ``run_error`` (AAP 0.4.7).

        A worker that died published no ``config.workeroutput``, so the node-id record is attempted
        first and its absence records nothing; a non-``None`` ``error`` then becomes the
        ``run_error`` naming the worker id, so a hard death yields partial results and exit ``1``.
        """
        from foundry.execution import session
        from foundry.execution.sharding import record_worker_output

        worker_id = str(node.workerinput.get("workerid", ""))
        try:
            record_worker_output(
                session.node_ledger(self._config), worker_id, getattr(node, "workeroutput", None)
            )
        except Exception as exc:
            # Failure-isolation rule: a bookkeeping fault may not change an outcome or exit code.
            _warn("could not record a worker's collected node ids", exc)
        if error is None:
            return
        try:
            session.sharding_service(self._config).worker_down(worker_id, str(error))
        except Exception as exc:
            # Failure-isolation rule: a bookkeeping fault may not change an outcome or exit code.
            _warn("could not record a lost worker", exc)


class _TimeoutHook:
    """pytest-timeout's per-item timer, substituted by the run's own policy (AAP 0.3.2).

    Both hookspecs are ``firstresult`` and the peer's own implementations are ``trylast`` precisely
    so another plugin can substitute the strategy; ``None`` lets the chain fall through to the peer.
    """

    def __init__(self, config: pytest.Config) -> None:
        """Store the configuration whose run state holds the resolved policy."""
        self._config = config

    def pytest_timeout_set_timer(self, item: pytest.Item, settings: object) -> bool | None:
        """Arm the timer derived from ``execution.timeout``, or fall through to the peer."""
        from foundry.execution.timeout import apply_timer

        state = self._config.stash.get(exec_state.SESSION_STATE, None)
        if state is None:
            return None
        return True if apply_timer(self._config, item, state.timeout) else None

    def pytest_timeout_cancel_timer(self, item: pytest.Item) -> bool | None:
        """Cancel whatever timer was armed for ``item``, or fall through to the peer."""
        from foundry.execution.timeout import cancel_timer

        return True if cancel_timer(item) else None


def pytest_terminal_summary(
    terminalreporter: _TerminalReporter, exitstatus: int, config: pytest.Config
) -> None:
    """Print ``foundry: profile=<name>``, then the run id and artifacts root (AAP 0.4.2, 0.7.4).

    The first line is the activation proof a run with no configuration at all still prints.
    """
    try:
        state = config.stash.get(exec_state.SESSION_STATE, None)
        if state is None:
            return
        profile_line, run_line = state.banner_lines()
        terminalreporter.write_sep("-", profile_line)
        terminalreporter.write_line(run_line)
    except Exception as exc:
        # Failure-isolation rule: the banner may not change an outcome or an exit code.
        _warn("could not print the Foundry activation banner", exc)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Write this run's report files and compose the exit code, on the controller only.

    This hook runs in a ``finally`` block in every process, workers included, so only the controller
    writes: a second writer would corrupt ``results.json``. A deselected-only run writes no file, a
    zero-test run an empty document, an interrupted run flushes what it has, and a configuration
    fault that ended the session in ``pytest_configure`` writes nothing to report on.
    """
    from foundry.execution.session import compose_exit_status

    config = session.config
    if _is_worker(config):
        _publish_nodeids(config)
        return
    if exitstatus == _USAGE_EXIT:
        return
    # AAP 0.4.6 reserves the empty document for a run that collected nothing, so a run a deselecting
    # filter (-k, -m or --deselect) emptied writes no file, nor does a --collect-only run.
    filters = config.option.keyword or config.option.markexpr or config.option.deselect
    emptied = exitstatus == pytest.ExitCode.NO_TESTS_COLLECTED and bool(filters)
    if not config.stash.get(exec_state.RUN_RECORDS, None) and (
        config.option.collectonly or emptied
    ):
        return
    state = config.stash.get(exec_state.SESSION_STATE, None)
    if state is None:
        return
    try:
        collected = [item.nodeid for item in session.items]
        elements, payloads, errors = _assemble(config, state, collected)
    except Exception as exc:
        # Failure-isolation rule: assembling may not change the exit code the tests earned.
        _warn("could not assemble this run's result records", exc)
        return
    try:
        _write_reports(config, state, elements)
    except Exception as exc:
        # Failure-isolation rule: a report fault leaves the exit code to the tests.
        _warn("could not write this run's report files", exc)
    session.exitstatus = compose_exit_status(state, payloads, exitstatus, len(errors))


def _assemble(
    config: pytest.Config, state: SessionState, collected: Sequence[str]
) -> tuple[list[ResultElement], dict[str, TestPayload], tuple[RunErrorPayload, ...]]:
    """Return the run's elements, the payloads behind them and its run errors.

    Assumption A12: a lost worker leaves collected items with no report at all, and they are named
    ``not_run`` rather than silently omitted; ``verify`` records the universe that names them -- the
    controller's collection, or the ids xdist reports per worker -- and logs each decision.
    Result records are sorted by node id and the run errors follow them (AAP 0.4.6).
    """
    from foundry.execution.session import not_run_payload, sharding_service
    from foundry.reporting import parse_element

    payloads = config.stash.setdefault(exec_state.RUN_RECORDS, {})
    service = sharding_service(config)
    service.verify(collected or service.nodes.expected_nodes())
    for nodeid in service.missing_after_run(payloads):
        payloads[nodeid] = not_run_payload(state, nodeid)
    errors = service.errors_payloads()
    source = state.invocation_dir / "results.json"
    elements: list[ResultElement] = [
        parse_element(payloads[nodeid].as_result_fields(), source=source)
        for nodeid in sorted(payloads)
    ]
    elements.extend(parse_element(error.as_dict(), source=source) for error in errors)
    return elements, payloads, errors


def _write_reports(
    config: pytest.Config, state: SessionState, elements: Sequence[ResultElement]
) -> None:
    """Write ``results.json``, ``report.html`` and ``junit.xml`` where AAP 0.4.6 places them.

    The three fixed names go to the invocation directory, and ``--foundry-json-report PATH`` moves
    the result document to ``PATH`` with the other two beside it (assumption A5). Each file follows
    its own ``reporting.*`` switch, and a fallback HTML step suppresses ``junit.xml``.
    """
    from foundry.reporting import write_all, write_results

    named = _json_report_path(config)
    directory = state.invocation_dir if named is None else named.parent
    if named is not None:
        write_results(elements, named, redactor=state.redactor)
    write_all(
        elements,
        directory,
        redactor=state.redactor,
        artifacts_root=state.artifacts_root if state.artifacts_enabled else None,
        log_lines=state.capture_lines,
        run_id=state.run_id,
        profile=state.profile,
        finished_at=ids.to_iso8601(ids.utc_now()),
        artifacts_disabled=not state.artifacts_enabled or state.writer.disabled,
        write_json=named is None and bool(state.config.reporting.json),
        write_html=bool(state.config.reporting.html),
    )


def _json_report_path(config: pytest.Config) -> Path | None:
    """Return the ``results.json`` path ``--foundry-json-report`` named, or ``None`` when unset."""
    from pathlib import Path

    raw = config.getoption(_JSON_REPORT_OPTION_DEST, default=None)
    if isinstance(raw, str) and raw.strip():
        return Path(raw.strip())
    return None


def _is_worker(config: pytest.Config) -> bool:
    """Report whether this process is an xdist worker, which ``config.workerinput`` alone tells.

    ``worker_id`` is a fixture rather than a hook parameter, so this detection imports nothing.
    """
    return isinstance(getattr(config, "workerinput", None), dict)


def _publish_nodeids(config: pytest.Config) -> None:
    """Publish the node ids this worker ran under ``WORKER_OUTPUT_KEY`` (AAP 0.2.1).

    The controller reads it in ``pytest_testnodedown``; a process whose ``config.workeroutput`` has
    gone publishes nothing.
    """
    from foundry.execution.sharding import WORKER_OUTPUT_KEY

    output = getattr(config, "workeroutput", None)
    if not isinstance(output, dict):
        return
    try:
        records = config.stash.get(exec_state.RUN_RECORDS, None) or {}
        output[WORKER_OUTPUT_KEY] = tuple(sorted(records))
    except Exception as exc:
        # Failure-isolation rule: a bookkeeping fault may not change an outcome or exit code.
        _warn("could not publish this worker's node ids", exc)


def _warn(message: str, exc: BaseException | None = None) -> None:
    """Report a fault in Foundry's own plumbing at ``WARNING``, and never raise from the report."""
    from foundry.obs.events import emit_plumbing_failure

    emit_plumbing_failure(_COMPONENT, message, exc=exc)
