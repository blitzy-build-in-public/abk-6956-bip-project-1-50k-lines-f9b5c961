"""The probes behind ``foundry doctor``: what each one checks, and the report they build.

AAP 0.3.2 and AAP 0.7.1 place the environment probes in this package, and this module is their
public surface: :func:`~foundry.diagnostics.checks.run_checks` runs every probe once,
:class:`~foundry.diagnostics.report.DoctorReport` carries the rows that AAP 0.7.4 prints as a table
and AAP 0.7.3 serialises as a document, and ``foundry.cli`` renders either form. Nothing here
decides a finding of its own; the probes read configuration and observe the environment, importing
only ``core``, ``obs`` and ``config`` and never ``plugin``, ``reporting`` or ``cli`` (AAP 0.4.8).
Two contracts hold for every probe, and they are stated here because they outlive any single one of
them. The exit code belongs to :data:`~foundry.diagnostics.checks.REQUIRED_PROBES` alone: a ``fail``
on one of those five makes ``foundry doctor`` exit ``2``, while a ``warn`` never changes the code
and the optional socket-guard row never can (AAP 0.4.4). And no probe opens a socket, not even a
loopback one, because the guard row reads state a host project published rather than connecting to
anything (AAP 0.9.2). A probe defined outside this package would escape both contracts, so none
exists.
"""

from __future__ import annotations

# Every import below is written as ``X as X``: under ``mypy --strict``'s no-implicit-reexport
# that spelling is an explicit re-export rather than a private import, which is what makes this
# module the package's published surface and nothing more.
#
# The five probe identifiers of AAP 0.7.1 and the tuple that collects them come first, so a caller
# imports a name instead of repeating its string; the probe functions that report each row follow.
from foundry.diagnostics.checks import PROBE_ARTIFACTS as PROBE_ARTIFACTS
from foundry.diagnostics.checks import PROBE_CASSETTES as PROBE_CASSETTES
from foundry.diagnostics.checks import PROBE_CONFIGURATION as PROBE_CONFIGURATION
from foundry.diagnostics.checks import PROBE_INTERPRETER as PROBE_INTERPRETER
from foundry.diagnostics.checks import PROBE_PLUGIN as PROBE_PLUGIN
from foundry.diagnostics.checks import REQUIRED_PROBES as REQUIRED_PROBES
from foundry.diagnostics.checks import check_artifacts_dir as check_artifacts_dir
from foundry.diagnostics.checks import check_cassette_dir as check_cassette_dir
from foundry.diagnostics.checks import check_configuration as check_configuration
from foundry.diagnostics.checks import check_interpreter as check_interpreter
from foundry.diagnostics.checks import check_plugin_registered as check_plugin_registered
from foundry.diagnostics.checks import run_checks as run_checks

# The interpreter policy AAP 0.4.1 states, and the report model that carries every row's verdict.
from foundry.diagnostics.report import MAX_SUPPORTED_VERSION as MAX_SUPPORTED_VERSION
from foundry.diagnostics.report import MIN_SUPPORTED_VERSION as MIN_SUPPORTED_VERSION
from foundry.diagnostics.report import SUPPORTED_IMPLEMENTATION as SUPPORTED_IMPLEMENTATION
from foundry.diagnostics.report import DoctorReport as DoctorReport
from foundry.diagnostics.report import InterpreterInfo as InterpreterInfo
from foundry.diagnostics.report import ProbeResult as ProbeResult
from foundry.diagnostics.report import ProbeStatus as ProbeStatus

# The socket-guard row: the two attribute names a host project's guard publishes, the probe that
# reads them, and the row name it reports under.
from foundry.diagnostics.socket_guard import ATTEMPTS_ATTRIBUTE as ATTEMPTS_ATTRIBUTE
from foundry.diagnostics.socket_guard import GUARD_ATTRIBUTE as GUARD_ATTRIBUTE
from foundry.diagnostics.socket_guard import PROBE_NAME as PROBE_NAME
from foundry.diagnostics.socket_guard import SocketGuardState as SocketGuardState
from foundry.diagnostics.socket_guard import check_socket_guard as check_socket_guard
from foundry.diagnostics.socket_guard import detect_socket_guard as detect_socket_guard

__all__ = [
    "ATTEMPTS_ATTRIBUTE",
    "DoctorReport",
    "GUARD_ATTRIBUTE",
    "InterpreterInfo",
    "MAX_SUPPORTED_VERSION",
    "MIN_SUPPORTED_VERSION",
    "PROBE_ARTIFACTS",
    "PROBE_CASSETTES",
    "PROBE_CONFIGURATION",
    "PROBE_INTERPRETER",
    "PROBE_NAME",
    "PROBE_PLUGIN",
    "ProbeResult",
    "ProbeStatus",
    "REQUIRED_PROBES",
    "SUPPORTED_IMPLEMENTATION",
    "SocketGuardState",
    "check_artifacts_dir",
    "check_cassette_dir",
    "check_configuration",
    "check_interpreter",
    "check_plugin_registered",
    "check_socket_guard",
    "detect_socket_guard",
    "run_checks",
]
