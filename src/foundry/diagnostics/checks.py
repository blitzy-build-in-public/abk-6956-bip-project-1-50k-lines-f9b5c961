"""The five required ``foundry doctor`` probes and the runner that reports every row.

AAP 0.7.1 places the environment probes of ``foundry doctor`` here -- the interpreter, the
``pytest11`` entry point, configuration validity, artifact-directory writability and
cassette-directory state -- and each of them answers with a
:class:`~foundry.diagnostics.report.ProbeResult`, so the table of AAP 0.7.4 and the ``checks``
array of AAP 0.7.3 cannot disagree about what a probe found.

**Five required probes, one optional row.** AAP 0.4.4 lists six probes for ``foundry doctor``:
the five above plus socket-guard state. AAP 0.7.1 gives the guard its own module. Both hold as
follows: :data:`REQUIRED_PROBES` are the five defined here, each reported with ``required=True``
so a ``FAIL`` makes ``foundry doctor`` exit ``2``; the sixth row comes from
:func:`foundry.diagnostics.socket_guard.check_socket_guard` with ``required=False`` and therefore
cannot change that code. :func:`run_checks` reports all six in one fixed order -- interpreter,
plugin, configuration, artifacts, cassettes, socket_guard -- which is both the order the table
prints and the order the JSON document serialises.

**Exit codes** (AAP 0.4.4). Only a ``FAIL`` on a required probe fails the command;
:attr:`~foundry.diagnostics.report.DoctorReport.ok` is the value the CLI maps to ``0`` or ``2``,
so a ``WARN`` -- an unsupported platform, a cassette directory not created yet -- never moves it.

**Failure isolation** (AAP 0.1.2). A probe that cannot run returns a ``FAIL`` row carrying the
exception's class name alone. :func:`run_checks` calls every probe through :func:`_guarded`, which
logs one ``WARNING`` on the ``foundry`` logger through
:func:`foundry.obs.events.emit_plumbing_failure` and never lets a traceback reach the command. The
exception's message stays out of the detail: it can embed a path or a credential, and the detail
is serialised into the published document.

**No network** (AAP 0.9.2). Nothing here opens a socket. The plugin probe reads installed
distribution metadata, the configuration probe resolves through :mod:`foundry.config`, and the
directory probes touch the filesystem only.

**Determinism** (AAP 0.13.1). Details carry absolute resolved paths, sorted names and integer
counts, never a timestamp, a duration or an object ``repr``, so two runs on an unchanged machine
report identical text.

**Import-time discipline** (AAP 0.4.8). Importing this module runs no probe, reads no environment
variable, configures no logger and imports nothing heavy. :mod:`foundry.config.errors` is the one
exception: the stdlib-only module holding the pinned message templates, imported at module scope so
the configuration row and ``foundry config validate`` share one definition site rather than two
copies of the text. :class:`~foundry.config.models.FoundryConfig` enters only under
:data:`typing.TYPE_CHECKING`, and :func:`foundry.config.resolve` only inside
:func:`_resolve_configuration`, because the configuration package's models pull pydantic onto the
``foundry doctor`` path.

Every probe accepts its observation through a keyword, so a test drives it without touching the
real environment: an :class:`~foundry.diagnostics.report.InterpreterInfo`, the discovered
entry-point pairs, a resolved configuration, a directory and a working directory.
"""

from __future__ import annotations

import contextlib
import importlib.util
import tempfile
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Final

from foundry.config.errors import success_message
from foundry.core.exceptions import ConfigError
from foundry.diagnostics.report import DoctorReport, InterpreterInfo, ProbeResult, ProbeStatus
from foundry.diagnostics.socket_guard import PROBE_NAME, SocketGuardState, check_socket_guard
from foundry.obs.events import emit_plumbing_failure

if TYPE_CHECKING:
    from foundry.config import FoundryConfig

__all__ = [
    "PROBE_ARTIFACTS",
    "PROBE_CASSETTES",
    "PROBE_CONFIGURATION",
    "PROBE_INTERPRETER",
    "PROBE_PLUGIN",
    "REQUIRED_PROBES",
    "check_artifacts_dir",
    "check_cassette_dir",
    "check_configuration",
    "check_interpreter",
    "check_plugin_registered",
    "run_checks",
]

# The five stable probe identifiers of AAP 0.7.1. Each doubles as the ``Probe`` cell of the table
# (AAP 0.7.4) and as the ``name`` of the JSON ``checks`` element (AAP 0.7.3), and both belong to the
# additive-only public surface of AAP 0.4.9: they may be added to, never renamed. The sixth row's
# identifier is :data:`foundry.diagnostics.socket_guard.PROBE_NAME`, defined once in that module.
PROBE_INTERPRETER: Final[str] = "interpreter"
PROBE_PLUGIN: Final[str] = "plugin"
PROBE_CONFIGURATION: Final[str] = "configuration"
PROBE_ARTIFACTS: Final[str] = "artifacts"
PROBE_CASSETTES: Final[str] = "cassettes"

REQUIRED_PROBES: Final[tuple[str, ...]] = (
    PROBE_INTERPRETER,
    PROBE_PLUGIN,
    PROBE_CONFIGURATION,
    PROBE_ARTIFACTS,
    PROBE_CASSETTES,
)
"""The probes whose ``FAIL`` sets ``foundry doctor``'s exit code to ``2`` (AAP 0.4.4).

The socket-guard row is absent on purpose: a project that installs no guard is an ordinary
project, so that row is optional and cannot fail the command.
"""

_SUPPORTED_RANGE: Final[str] = "CPython 3.11-3.13"
"""The interpreter range AAP 0.4.1 supports, spelled as the probe's detail strings spell it."""

_SUPPORTED_PLATFORMS: Final[tuple[str, ...]] = ("linux", "darwin")
"""The :data:`sys.platform` prefixes AAP 0.4.1 supports; Windows is declared unsupported."""

_ENTRY_POINT_GROUP: Final[str] = "pytest11"
_ENTRY_POINT_NAME: Final[str] = "foundry"
_ENTRY_POINT_TARGET: Final[str] = "foundry.plugin"
"""The registration of AAP 0.4.2 that the plugin probe proves is installed."""

_PROBE_FILE_PREFIX: Final[str] = ".foundry-doctor-"
_PROBE_FILE_SUFFIX: Final[str] = ".tmp"
_PROBE_FILE_CONTENT: Final[bytes] = b"foundry\n"
"""The artifact probe's scratch file: hidden, temporary, and unlinked again in a ``finally``."""

_PROBE_FAILURE_DETAIL: Final[str] = "probe failed"
_CONFIGURATION_UNAVAILABLE: Final[str] = "not checked: configuration unavailable"
_CASSETTE_PATTERN: Final[str] = "*.json"


def _describe_oserror(error: OSError) -> str:
    """Return the operating-system text of ``error``, or its message when it carries none."""
    return error.strerror or str(error)


def _absolute(path: Path, cwd: Path | None) -> Path:
    """Return ``path`` as an absolute normalised path, resolving a relative one against ``cwd``.

    ``cwd`` defaults to the process working directory, and every detail string of the directory
    probes reports this resolved path, so two runs on one machine print identical text
    (AAP 0.13.1).
    """
    base = Path.cwd() if cwd is None else cwd
    if path.is_absolute():
        return path.resolve()
    return (base / path).resolve()


def _directory_for(
    directory: Path | None, configured: Path | None, cwd: Path | None
) -> Path | None:
    """Return the directory a filesystem probe inspects, or ``None`` when none can be determined.

    An explicit ``directory`` wins over the configured path, which is how a test probes a
    ``tmp_path`` without building a configuration model. ``None`` for both means configuration
    resolution already failed, and the probe then reports that it could not run. The chosen value
    is coerced through :class:`~pathlib.Path`, so a configuration that carries its directory as
    text is probed rather than faulting on a missing ``is_absolute``.
    """
    chosen = directory if directory is not None else configured
    if chosen is None:
        return None
    return _absolute(Path(chosen), cwd)


def _supported_platform(identifier: str) -> bool:
    """Return whether ``identifier`` -- a :data:`sys.platform` value -- is a supported platform."""
    return identifier.startswith(_SUPPORTED_PLATFORMS)


def check_interpreter(info: InterpreterInfo | None = None) -> ProbeResult:
    """Report whether the interpreter the command runs on is one Foundry supports (AAP 0.4.1).

    An interpreter outside the ``CPython 3.11-3.13`` range of AAP 0.3.1's ``requires-python`` is a
    ``FAIL``, because no other part of the product has been tested there. A supported interpreter
    on a platform other than Linux or macOS is a ``WARN``: AAP 0.4.1 declares Windows unsupported
    and asks ``foundry doctor`` to report a warning rather than a pass, and the same reasoning
    covers any platform ``sys.platform`` names outside the two that are tested.

    Args:
        info: the interpreter to judge. It defaults to ``None``, meaning
            :meth:`~foundry.diagnostics.report.InterpreterInfo.current` computed inside this call,
            so importing the module judges nothing (AAP 0.4.8).

    Returns:
        The row named :data:`PROBE_INTERPRETER`, with ``required=True``.
    """
    resolved = InterpreterInfo.current() if info is None else info
    identity = f"{resolved.implementation} {resolved.version} on {resolved.platform}"
    if not resolved.supported:
        return ProbeResult(
            name=PROBE_INTERPRETER,
            status=ProbeStatus.FAIL,
            detail=f"{identity} requires {_SUPPORTED_RANGE} (AAP 0.4.1)",
            required=True,
        )
    if not _supported_platform(resolved.platform):
        return ProbeResult(
            name=PROBE_INTERPRETER,
            status=ProbeStatus.WARN,
            detail=f"{identity} is unsupported: {_SUPPORTED_RANGE} on Linux or macOS only",
            required=True,
        )
    return ProbeResult(
        name=PROBE_INTERPRETER, status=ProbeStatus.PASS, detail=identity, required=True
    )


def _installed_entry_points() -> tuple[tuple[str, str], ...]:
    """Return the installed ``pytest11`` entry points as sorted ``(name, value)`` pairs.

    :mod:`importlib.metadata` reads the metadata of the installed distributions, so the answer
    comes from the environment rather than from a network index (AAP 0.9.2). It is imported here
    rather than at module scope to keep importing this module cheap, and the pairs are sorted so a
    failing probe's detail does not depend on the order the metadata lists them in.
    """
    from importlib.metadata import entry_points

    discovered = entry_points(group=_ENTRY_POINT_GROUP)
    return tuple(sorted((str(point.name), str(point.value)) for point in discovered))


def _normalise_pairs(pairs: Iterable[tuple[str, str]]) -> tuple[tuple[str, str], ...]:
    """Return ``pairs`` as a sorted tuple of ``str`` pairs, consuming a supplied iterable once."""
    return tuple(sorted((str(name), str(value)) for name, value in pairs))


def _module_importable(name: str) -> bool:
    """Return whether ``name`` resolves to an importable module, without importing it.

    :func:`importlib.util.find_spec` locates the module rather than executing it, so the plugin
    probe cannot trigger the hooks and registration work that importing :mod:`foundry.plugin`
    performs. A parent package that is missing raises instead of returning ``None``, which is
    reported as "not importable" rather than reaching the caller (AAP 0.1.2).
    """
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _target_module(value: str) -> str:
    """Return the module an entry-point ``value`` names, dropping any ``:attribute`` part."""
    return value.split(":", 1)[0].strip()


def check_plugin_registered(
    *,
    discovered: Iterable[tuple[str, str]] | None = None,
    module_available: bool | None = None,
) -> ProbeResult:
    """Report whether the ``pytest11`` entry point of AAP 0.4.2 is installed and resolvable.

    That entry point -- group ``pytest11``, name ``foundry``, target ``foundry.plugin`` -- is what
    makes zero-configuration activation work: pytest imports the module during ``_preparse``
    without a ``-p`` flag and without a configuration file. A distribution that declares it but
    ships no importable module would activate nothing, so both halves are checked and either
    shortfall is a ``FAIL`` that states which one it is.

    Args:
        discovered: the ``(name, value)`` pairs read from the ``pytest11`` group. It defaults to
            ``None``, meaning the installed metadata is read inside this call.
        module_available: whether ``foundry.plugin`` resolves. It defaults to ``None``, meaning
            :func:`importlib.util.find_spec` is consulted inside this call.

    Returns:
        The row named :data:`PROBE_PLUGIN`, with ``required=True``.
    """
    pairs = _installed_entry_points() if discovered is None else _normalise_pairs(discovered)
    importable = (
        _module_importable(_ENTRY_POINT_TARGET) if module_available is None else module_available
    )
    registered = any(
        name == _ENTRY_POINT_NAME and _target_module(value) == _ENTRY_POINT_TARGET
        for name, value in pairs
    )
    expected = f'{_ENTRY_POINT_GROUP} entry point {_ENTRY_POINT_NAME} = "{_ENTRY_POINT_TARGET}"'
    if not registered:
        found = ", ".join(sorted({name for name, _value in pairs})) or "none"
        return ProbeResult(
            name=PROBE_PLUGIN,
            status=ProbeStatus.FAIL,
            detail=f"{expected} is not registered; names found: {found}",
            required=True,
        )
    if not importable:
        return ProbeResult(
            name=PROBE_PLUGIN,
            status=ProbeStatus.FAIL,
            detail=f"{expected} is registered but {_ENTRY_POINT_TARGET} is not importable",
            required=True,
        )
    return ProbeResult(
        name=PROBE_PLUGIN,
        status=ProbeStatus.PASS,
        detail=f"{expected} registered",
        required=True,
    )


def _configuration_row(profile: str) -> ProbeResult:
    """Return the ``PASS`` row a resolved configuration earns.

    The detail is :func:`foundry.config.errors.success_message`'s output: the
    ``configuration valid (profile=<name>)`` message of AAP 0.4.4 and AAP 0.4.5, rendered from its
    one definition site rather than re-stated here, so ``foundry doctor`` and
    ``foundry config validate`` agree word for word and change together.

    Args:
        profile: the active profile name as text, which :func:`success_message` interpolates.

    Returns:
        The row named :data:`PROBE_CONFIGURATION`, with ``required=True``.
    """
    return ProbeResult(
        name=PROBE_CONFIGURATION,
        status=ProbeStatus.PASS,
        detail=success_message(profile),
        required=True,
    )


def _resolve_configuration(
    config: FoundryConfig | None,
) -> tuple[FoundryConfig | None, ProbeResult]:
    """Return the configuration a doctor run reports on, together with its probe row.

    The pair form exists because :func:`run_checks` needs the resolved model itself, to hand the
    artifact and cassette probes their configured directories, and resolving the four layers of
    AAP 0.4.5 exactly once per invocation is what keeps the report internally consistent.

    A supplied ``config`` is taken as given and nothing is verified: the caller already holds a
    validated model. Its profile is rendered with ``str`` rather than read as an enum member, so a
    configuration whose ``model_copy`` installs the name as a plain string -- the override
    :func:`foundry.fixtures.config.foundry_config` documents -- is reported rather than faulting.
    An absent one is resolved through :func:`foundry.config.resolve`, whose
    :class:`~foundry.core.exceptions.ConfigError` becomes the row's detail -- its message names
    the key, the variable or the file and never a secret value (implicit requirement I9). Any
    other exception propagates to the caller's isolation boundary.

    Args:
        config: the configuration to report on, or ``None`` to resolve one now.

    Returns:
        The resolved configuration -- ``None`` when resolution failed -- and the row named
        :data:`PROBE_CONFIGURATION`.
    """
    if config is not None:
        return config, _configuration_row(str(config.profile))
    # Imported here rather than at module scope: ``foundry.config.resolve`` pulls pydantic and
    # pydantic-settings, which must not load on every ``foundry.diagnostics`` import (AAP 0.4.8).
    from foundry.config import resolve

    try:
        resolved = resolve()
    except ConfigError as error:
        return None, ProbeResult(
            name=PROBE_CONFIGURATION,
            status=ProbeStatus.FAIL,
            detail=str(error),
            required=True,
        )
    return resolved, _configuration_row(str(resolved.profile))


def check_configuration(config: FoundryConfig | None = None) -> ProbeResult:
    """Report whether the configuration resolves and validates (AAP 0.7.1).

    The detail is the published message ``configuration valid (profile=<name>)`` on success, and
    the :class:`~foundry.core.exceptions.ConfigError` message on failure, which names the
    offending key, environment variable or file and never a credential.

    Args:
        config: a configuration to report on, or ``None`` to resolve one now.

    Returns:
        The row named :data:`PROBE_CONFIGURATION`, with ``required=True``.
    """
    _resolved, row = _resolve_configuration(config)
    return row


def _prove_writable(path: Path) -> None:
    """Write one hidden file into ``path`` and remove it again, proving the directory is writable.

    The only honest proof of writability is a write that really happens, so the probe creates a
    file with the unmistakable :data:`_PROBE_FILE_PREFIX` name and unlinks it in a ``finally``. It
    never creates ``<dir>/<run_id>/``: the run layout belongs to a run, and ``foundry doctor``
    leaves the artifact tree as it found it. A failed cleanup is suppressed, because a directory
    that accepted the write has already answered the question.
    """
    created: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=_PROBE_FILE_PREFIX,
            suffix=_PROBE_FILE_SUFFIX,
            dir=path,
            delete=False,
        ) as handle:
            created = Path(handle.name)
            handle.write(_PROBE_FILE_CONTENT)
    finally:
        if created is not None:
            with contextlib.suppress(OSError):
                created.unlink(missing_ok=True)


def check_artifacts_dir(
    config: FoundryConfig | None = None, *, directory: Path | None = None, cwd: Path | None = None
) -> ProbeResult:
    """Report whether the artifact directory can be created and written (AAP 0.7.1).

    The directory is a run's output root -- ``artifacts`` by default, with one ``<run_id>/`` below
    it per run -- so a run that cannot write there captures nothing. The probe therefore creates
    it with ``parents=True`` and proves the write, reporting the absolute resolved path either
    way.

    Args:
        config: the resolved configuration whose ``artifacts.dir`` is inspected. ``None`` reports
            that the probe could not run, which is a ``WARN`` because a configuration fault has
            already failed its own required row.
        directory: an explicit directory that wins over ``config.artifacts.dir``, which is how a
            test probes a ``tmp_path``.
        cwd: the directory a relative path resolves against; it defaults to the process working
            directory.

    Returns:
        The row named :data:`PROBE_ARTIFACTS`, with ``required=True``.
    """
    target = _directory_for(directory, None if config is None else config.artifacts.dir, cwd)
    if target is None:
        return ProbeResult(
            name=PROBE_ARTIFACTS,
            status=ProbeStatus.WARN,
            detail=_CONFIGURATION_UNAVAILABLE,
            required=True,
        )
    try:
        target.mkdir(parents=True, exist_ok=True)
        _prove_writable(target)
    except OSError as error:
        return ProbeResult(
            name=PROBE_ARTIFACTS,
            status=ProbeStatus.FAIL,
            detail=f"{target}: {_describe_oserror(error)}",
            required=True,
        )
    return ProbeResult(
        name=PROBE_ARTIFACTS,
        status=ProbeStatus.PASS,
        detail=f"{target} writable",
        required=True,
    )


def check_cassette_dir(
    config: FoundryConfig | None = None, *, directory: Path | None = None, cwd: Path | None = None
) -> ProbeResult:
    """Report the state of the cassette directory (AAP 0.7.1).

    ``cassette.mode`` defaults to ``off``, so a project that never records owns no cassette
    directory and that absence is a ``WARN`` rather than a fault: ``foundry doctor`` has to pass
    on a project that does not use the feature. An existing directory reports how many cassette
    documents it holds, counted over a sorted glob so the number and the text are stable. A path
    that exists but is not a directory is a ``FAIL``, because recording there could not work.

    Args:
        config: the resolved configuration whose ``cassette.dir`` is inspected; ``None`` reports
            that the probe could not run.
        directory: an explicit directory that wins over ``config.cassette.dir``.
        cwd: the directory a relative path resolves against; it defaults to the process working
            directory.

    Returns:
        The row named :data:`PROBE_CASSETTES`, with ``required=True``.
    """
    target = _directory_for(directory, None if config is None else config.cassette.dir, cwd)
    if target is None:
        return ProbeResult(
            name=PROBE_CASSETTES,
            status=ProbeStatus.WARN,
            detail=_CONFIGURATION_UNAVAILABLE,
            required=True,
        )
    try:
        if not target.exists():
            return ProbeResult(
                name=PROBE_CASSETTES,
                status=ProbeStatus.WARN,
                detail=f"{target} not created yet",
                required=True,
            )
        if not target.is_dir():
            return ProbeResult(
                name=PROBE_CASSETTES,
                status=ProbeStatus.FAIL,
                detail=f"{target} is not a directory",
                required=True,
            )
        count = len(sorted(target.glob(_CASSETTE_PATTERN)))
    except OSError as error:
        return ProbeResult(
            name=PROBE_CASSETTES,
            status=ProbeStatus.FAIL,
            detail=f"{target}: {_describe_oserror(error)}",
            required=True,
        )
    return ProbeResult(
        name=PROBE_CASSETTES,
        status=ProbeStatus.PASS,
        detail=f"{target} ({count} cassette file(s))",
        required=True,
    )


def _isolation_row(name: str, error: BaseException, *, required: bool) -> ProbeResult:
    """Return the ``FAIL`` row a probe that raised leaves behind, after one ``WARNING``.

    The detail carries the exception's class name alone. Its message can hold a path, a header
    value or another credential, and the detail is serialised into the published document, so it
    stays out of it; the probes already return rich details for the faults they expect. The record
    itself goes through :func:`foundry.obs.events.emit_plumbing_failure`, the package's one
    ``WARNING`` emitter, which takes scalars only and adds the class name of ``error``.
    """
    emit_plumbing_failure(name, _PROBE_FAILURE_DETAIL, exc=error)
    return ProbeResult(
        name=name,
        status=ProbeStatus.FAIL,
        detail=f"{_PROBE_FAILURE_DETAIL}: {type(error).__name__}",
        required=required,
    )


def _guarded(name: str, probe: Callable[[], ProbeResult], *, required: bool) -> ProbeResult:
    """Return ``probe()``'s row, or an isolated ``FAIL`` row when the probe raises (AAP 0.1.2).

    Foundry's own diagnostics never fail a run by crashing: a probe that cannot do its work
    reports ``FAIL`` with its row, logs at ``WARNING`` and lets ``foundry doctor`` print a table
    instead of a traceback.
    """
    try:
        return probe()
    except Exception as error:
        return _isolation_row(name, error, required=required)


def _resolve_configuration_guarded(
    config: FoundryConfig | None,
) -> tuple[FoundryConfig | None, ProbeResult]:
    """Resolve the configuration, reporting an unexpected fault as the configuration row.

    A :class:`~foundry.core.exceptions.ConfigError` is the expected fault and becomes the row's
    detail inside :func:`_resolve_configuration`; anything else is isolated here so the report
    survives it and the artifact and cassette probes receive no configuration to inspect.
    """
    try:
        return _resolve_configuration(config)
    except Exception as error:
        return None, _isolation_row(PROBE_CONFIGURATION, error, required=True)


def run_checks(
    *,
    config: FoundryConfig | None = None,
    cwd: Path | None = None,
    discovered: Iterable[tuple[str, str]] | None = None,
    interpreter: InterpreterInfo | None = None,
    guard_state: SocketGuardState | None = None,
) -> DoctorReport:
    """Run every probe once and return the report ``foundry doctor`` renders (AAP 0.7.3/0.7.4).

    The six rows appear in the fixed order interpreter, plugin, configuration, artifacts,
    cassettes, socket_guard, which is the order the table prints and the JSON array serialises.
    Configuration is resolved once and the resolved model is what the artifact and cassette probes
    inspect, so those three rows cannot disagree about which directories the command checked. Each
    probe runs inside :func:`_guarded`, so one probe's fault is a ``FAIL`` row, not a traceback.

    Args:
        config: a configuration to report on, or ``None`` to resolve one now.
        cwd: the directory relative configured paths resolve against; ``None`` means the process
            working directory.
        discovered: the ``pytest11`` entry-point pairs to judge, or ``None`` to read the installed
            metadata.
        interpreter: the interpreter to judge, or ``None`` to describe the running one.
        guard_state: the socket-guard state to report, or ``None`` to detect it now.

    Returns:
        The :class:`~foundry.diagnostics.report.DoctorReport` of this invocation, whose ``ok`` is
        ``True`` unless a required probe failed. Nothing is cached between calls and no module
        state is touched.
    """
    info = InterpreterInfo.current() if interpreter is None else interpreter
    resolved, configuration_row = _resolve_configuration_guarded(config)
    if resolved is not None:
        # Re-derive the row from the resolved model, so the configuration row is always the output
        # of the probe that names it and a replaced or instrumented probe shows up in the report.
        # Handing the model over means the four layers are still resolved exactly once.
        configuration_row = _guarded(
            PROBE_CONFIGURATION, lambda: check_configuration(resolved), required=True
        )
    rows: tuple[ProbeResult, ...] = (
        _guarded(PROBE_INTERPRETER, lambda: check_interpreter(info), required=True),
        _guarded(
            PROBE_PLUGIN, lambda: check_plugin_registered(discovered=discovered), required=True
        ),
        configuration_row,
        _guarded(PROBE_ARTIFACTS, lambda: check_artifacts_dir(resolved, cwd=cwd), required=True),
        _guarded(PROBE_CASSETTES, lambda: check_cassette_dir(resolved, cwd=cwd), required=True),
        _guarded(PROBE_NAME, lambda: check_socket_guard(guard_state), required=False),
    )
    return DoctorReport(interpreter=info, checks=rows)
