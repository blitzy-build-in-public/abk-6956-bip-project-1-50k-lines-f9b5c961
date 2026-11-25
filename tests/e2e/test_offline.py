"""Offline evidence for Foundry's own suite: the guard's counters plus a mechanical folder scan.

AAP 0.14.1 criterion 3 asks that the suite, the examples and the runtime open no non-loopback
socket, and AAP 0.14.3 makes that a mechanism rather than a promise. The two halves of this module
are that mechanism.

**What the guard sees, and what it cannot.** ``tests/conftest.py`` patches ``connect``,
``connect_ex``, ``getaddrinfo``, ``sendto`` and ``sendmsg`` in the interpreter that imports it, so
the counters read here cover every socket this process opens, in-process ``pytester`` runs
included. A child process is a separate interpreter that nothing here can observe: children are
kept offline by what their own tests hand them, ``--offline`` and ``--no-index`` for the installer
in ``tests/e2e/test_install.py`` and loopback-only addresses everywhere else. So this module
asserts the guard's counters *and* proves mechanically that the offline discipline is written into
this folder's code, where an edit could otherwise undo it.

**Why the scan parses instead of grepping.** Every rule is a pure function over source text
resolved through :mod:`ast`, so a docstring naming an installer or a URL is never a finding while
the code that runs one is, and each rule is tested against a synthetic violating snippet in
:func:`test_scanner_detects_each_violation_type`. Nothing here imports ``subprocess``, starts a
server, reads a clock or waits (AAP 0.14.3).
"""

from __future__ import annotations

import ast
import pathlib
import re
from collections.abc import Sequence
from typing import Final, Protocol

# The folder scanned, and this module's own name: the scan excludes the second, and the
# plugin-spelling check reads its raw text.
E2E_FOLDER: Final[pathlib.Path] = pathlib.Path(__file__).resolve().parent
SELF_MODULE: Final[str] = pathlib.Path(__file__).name

EXPECTED_MODULES: Final[frozenset[str]] = frozenset(
    {
        SELF_MODULE,
        "test_determinism.py",
        "test_example_project.py",
        "test_init_project.py",
        "test_install.py",
        "test_not_run.py",
        "test_sharding.py",
        "test_zero_config_subprocess.py",
    }
)
"""Every module the folder holds, so a renamed or deleted sibling cannot escape the scan."""

# AAP 0.14.3's single exception, pinned by a test: widening it is a visible diff in this constant.
INSTALLER_MODULE: Final[str] = "test_install.py"
INSTALLER_ALLOWLIST: Final[frozenset[str]] = frozenset({INSTALLER_MODULE})

# This module out of its own scan: its probe tokens and positive samples would read as real.
SCANNER_EXCLUSIONS: Final[frozenset[str]] = frozenset({SELF_MODULE})

INSTALLER_TOKENS: Final[frozenset[str]] = frozenset({"pip", "uv"})
"""The two resolvers a test must not run: the build's own tool and the one a venv ships."""

INSTALL_VERBS: Final[frozenset[str]] = frozenset({"install", "build", "venv"})
"""The argv tokens an install invocation is made of, beside the resolver's own name."""

# The offline flags: --offline forbids the socket a resolver would open, --no-index refuses an
# index so an install cannot resolve through it, and --no-isolation stops `python -m build` from
# creating a venv and installing its backend from an index (AAP 0.14.3).
_NO_NETWORK_FLAG: Final[str] = "--offline"
_NO_INDEX_FLAG: Final[str] = "--no-index"
_NO_ISOLATION_FLAG: Final[str] = "--no-isolation"
_INSTALL_VERB: Final[str] = "install"
_MODULE_BUILD_TOKENS: Final[frozenset[str]] = frozenset({"-m", "build"})

# The three spellings of an explicit plugin selection are assembled from these fragments, so this
# file never holds a complete one: tests/unit/test_no_dash_p_flag.py walks the whole tree for them
# and allowlists a single path outside this folder, so a literal here would make this module an
# offender in that scan's own report (AAP 0.4.2).
_PLUGIN_FLAG: Final[str] = "-p"
_PLUGIN_SEPARATOR: Final[str] = " "
_PLUGIN_NEGATION: Final[str] = "no:"
_PLUGIN_NAME: Final[str] = "foundry"

PLUGIN_FLAG_NEEDLES: Final[tuple[str, ...]] = (
    f"{_PLUGIN_FLAG}{_PLUGIN_SEPARATOR}{_PLUGIN_NAME}",
    f"{_PLUGIN_FLAG}{_PLUGIN_NEGATION}{_PLUGIN_NAME}",
    f"{_PLUGIN_FLAG}{_PLUGIN_SEPARATOR}{_PLUGIN_NEGATION}{_PLUGIN_NAME}",
)
"""The enable form, the joined negation and the spaced negation pytest also accepts."""

_PLUGIN_FLAG_PATTERN: Final[re.Pattern[str]] = re.compile(
    rf"{_PLUGIN_FLAG}\W*?(?:{_PLUGIN_NEGATION})?{_PLUGIN_NAME}"
)
"""The option with its argument, however quoting and separators are written between the two."""

# A URL's authority stops at whitespace, a quote or a delimiter; 127.0.0.0/8 is loopback.
_URL_STOP_CHARACTERS: Final[str] = r"""\s"'`\\/?#<>"""
_URL_PATTERN: Final[re.Pattern[str]] = re.compile(
    rf"(?P<scheme>https?)://(?P<authority>[^{_URL_STOP_CHARACTERS}]*)"
)
_LOOPBACK_HOSTNAMES: Final[frozenset[str]] = frozenset({"localhost", "[::1]", "::1"})
_LOOPBACK_IPV4: Final[re.Pattern[str]] = re.compile(r"127\.\d{1,3}\.\d{1,3}\.\d{1,3}")

# What a guard publishes to `foundry doctor`, mirroring foundry.diagnostics.socket_guard.
GUARD_ATTRIBUTE: Final[str] = "FOUNDRY_SOCKET_GUARD"
ATTEMPTS_ATTRIBUTE: Final[str] = "FOUNDRY_NON_LOOPBACK_ATTEMPTS"


class _Guard(Protocol):
    """The part of ``tests/conftest.py``'s ``SocketGuardState`` read here, declared not imported."""

    installed: bool
    loopback_attempts: int
    non_loopback_attempts: int
    violations: Sequence[str]


def _parse(source: str) -> ast.Module:
    """Return ``source`` parsed as a module, letting :exc:`SyntaxError` propagate."""
    return ast.parse(source)


def _docstring_node_ids(tree: ast.Module) -> set[int]:
    """Return the ``id`` of every docstring: prose may name installers and URLs inside them."""
    owners = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        first = node.body[0] if isinstance(node, owners) and node.body else None
        if not isinstance(first, ast.Expr) or not isinstance(first.value, ast.Constant):
            continue
        if isinstance(first.value.value, str):
            docstrings.add(id(first.value))
    return docstrings


def _string_constants(tree: ast.Module) -> set[str]:
    """Return every string constant in ``tree`` that is not a docstring: where flags are written."""
    docstrings = _docstring_node_ids(tree)
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    }


def _imports_subprocess(tree: ast.Module) -> bool:
    """Return True for ``import subprocess`` and for ``from subprocess import ...``."""
    names = [
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    ]
    modules = [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
    return any(name.partition(".")[0] == "subprocess" for name in [*names, *modules])


def _installer_violations(source: str) -> list[str]:
    """Return the ways ``source`` reaches an installer or starts a process of its own.

    Each finding names its tokens: an import of :mod:`subprocess` instead of the shared fixtures,
    and a resolver's name beside an install verb.
    """
    tree = _parse(source)
    constants = _string_constants(tree)
    violations: list[str] = []
    if _imports_subprocess(tree):
        violations.append(
            "imports subprocess; subprocesses go through run_pytest and run_foundry from "
            f"tests/conftest.py, and installing is reserved for {INSTALLER_MODULE} (AAP 0.14.3)"
        )
    installers = sorted(constants & INSTALLER_TOKENS)
    verbs = sorted(constants & INSTALL_VERBS)
    if installers and verbs:
        violations.append(
            f"holds the installer tokens {installers} beside the install verbs {verbs}, which is "
            f"an install invocation outside {INSTALLER_MODULE} (AAP 0.14.3)"
        )
    return violations


def _offline_flag_violations(source: str) -> list[str]:
    """Return the offline flags a module's own command vectors omit: ``--offline`` always,
    ``--no-index`` whenever the module would install, ``--no-isolation`` for ``python -m build``.
    """
    constants = _string_constants(_parse(source))
    required: list[str] = [_NO_NETWORK_FLAG]
    if _INSTALL_VERB in constants:
        required.append(_NO_INDEX_FLAG)
    if constants >= _MODULE_BUILD_TOKENS:
        required.append(_NO_ISOLATION_FLAG)
    return [
        f"omits {flag}, so a command it declares can reach an index: {_NO_NETWORK_FLAG} forbids "
        f"the socket, {_NO_INDEX_FLAG} refuses the index and {_NO_ISOLATION_FLAG} keeps an "
        "isolated build from installing its backend (AAP 0.14.3)"
        for flag in required
        if flag not in constants
    ]


def _dash_p_violations(source: str) -> list[str]:
    """Return the plugin-selection spellings ``source`` holds in its string constants.

    Activation is the installed ``pytest11`` entry point alone, so naming the plugin is redundant
    at best and, negated, a run that proves nothing about the plugin (AAP 0.4.2). An argument
    vector writes the option and its argument as two constants, which is the paired finding below.
    """
    constants = _string_constants(_parse(source))
    violations: list[str] = []
    if _PLUGIN_FLAG in constants and _PLUGIN_NAME in constants:
        violations.append(
            f"holds {_PLUGIN_FLAG!r} and {_PLUGIN_NAME!r} as an argument vector, which asks pytest "
            "to load the plugin explicitly; activation comes from the pytest11 entry point alone "
            "(AAP 0.4.2)"
        )
    violations.extend(
        f"holds {constant!r}, which spells the plugin-selection option; activation comes from the "
        "pytest11 entry point alone (AAP 0.4.2)"
        for constant in sorted(constants)
        if _PLUGIN_FLAG_PATTERN.search(constant)
    )
    return violations


def _host_of(authority: str) -> str | None:
    """Return the host of a URL authority, or ``None`` for a bare scheme that carries none."""
    host_port = authority.rpartition("@")[2]
    if not host_port:
        return None
    if host_port.startswith("["):
        return f"{host_port.partition(']')[0]}]"
    return host_port.partition(":")[0] or None


def _is_loopback_host(host: str) -> bool:
    """Return True when ``host`` is loopback, the only peer a test in this folder may reach: a
    ``localhost`` or ``::1`` name, every address in ``127.0.0.0/8`` by number.
    """
    return host.lower() in _LOOPBACK_HOSTNAMES or bool(_LOOPBACK_IPV4.fullmatch(host))


def _non_loopback_urls(source: str) -> list[str]:
    """Return the URLs in ``source``'s string constants whose host is not loopback.

    No test here may address another machine (AAP 0.9.2, AAP 0.14.3); findings are sorted.
    """
    findings: set[str] = set()
    for constant in _string_constants(_parse(source)):
        for match in _URL_PATTERN.finditer(constant):
            host = _host_of(match.group("authority"))
            if host is None or _is_loopback_host(host):
                continue
            findings.add(
                f"holds {match.group(0)!r}, whose host {host!r} is not loopback; serve HTTP from "
                "an in-process http.server on 127.0.0.1, or replay a cassette (AAP 0.9.2)"
            )
    return sorted(findings)


def _module_sources(folder: pathlib.Path) -> dict[str, str]:
    """Return ``{module name: source text}`` for every ``test_*.py`` in ``folder``, sorted."""
    return {
        path.name: path.read_text(encoding="utf-8") for path in sorted(folder.glob("test_*.py"))
    }


def _scanned_sources() -> dict[str, str]:
    """Return this folder's module sources with :data:`SCANNER_EXCLUSIONS` removed."""
    sources = _module_sources(E2E_FOLDER)
    return {name: source for name, source in sources.items() if name not in SCANNER_EXCLUSIONS}


def test_socket_guard_observed_no_non_loopback_attempt(socket_guard: _Guard) -> None:
    """The run attempted no non-loopback connection, and the guard refused none.

    The guard refuses such a target before the syscall and counts it, so a zero count is itself
    the observation that no attempt was made.
    """
    assert socket_guard.installed is True, (
        "the loopback-only socket guard is not installed, so every offline claim this suite makes "
        "is unverifiable: tests/conftest.py installs it at import (AAP 0.14.3)"
    )
    attempts = socket_guard.non_loopback_attempts
    violations = list(socket_guard.violations)
    assert attempts == 0, (
        f"{attempts} non-loopback connection attempt(s) were refused by the guard: {violations}. "
        "The suite, the examples and the runtime open no non-loopback socket (AAP 0.14.1, "
        "criterion 3): serve HTTP from an in-process http.server on 127.0.0.1, or replay a "
        "cassette (AAP 0.14.3)"
    )
    assert violations == [], (
        f"the guard recorded the refused target(s) {violations} beside a zero counter, so it "
        "counted and recorded inconsistently (AAP 0.14.3)"
    )
    # Deliberately not asserted: that loopback_attempts is above zero, which depends on which tests
    # this -n auto worker received and in what order, so asserting it would be order-dependent
    # flakiness. The assertions above are order-independent: nothing in the suite gets refused.
    published = getattr(socket_guard, ATTEMPTS_ATTRIBUTE, None)
    announced = getattr(socket_guard, GUARD_ATTRIBUTE, None)
    assert (announced, published) == (True, attempts), (
        f"the guard publishes {(announced, published)!r} under {GUARD_ATTRIBUTE} and "
        f"{ATTEMPTS_ATTRIBUTE}, so `foundry doctor` cannot detect it or reports a count other "
        f"than {attempts} (AAP 0.7.1, AAP 0.14.3)"
    )


def test_only_the_install_module_touches_an_installer() -> None:
    """No module here but the allowlisted installer names an installer or starts a process.

    AAP 0.14.3 reserves installing for ``tests/e2e/test_install.py``; that module's offline flags
    are checked here too, because an installer missing them reaches an index.
    """
    sources = _module_sources(E2E_FOLDER)
    missing = sorted(EXPECTED_MODULES - set(sources))
    assert missing == [], (
        f"scanning {E2E_FOLDER} found no {missing}, so a renamed or deleted module would escape "
        "this check: update EXPECTED_MODULES with the folder (AAP 0.14.3)"
    )
    checked = sorted(set(sources) - SCANNER_EXCLUSIONS - INSTALLER_ALLOWLIST)
    assert checked, "the scan skipped every module, so it proves nothing (AAP 0.14.3)"
    for name in checked:
        violations = _installer_violations(sources[name])
        assert violations == [], f"tests/e2e/{name}: {'; '.join(violations)}"
    flag_violations = _offline_flag_violations(sources[INSTALLER_MODULE])
    assert flag_violations == [], f"tests/e2e/{INSTALLER_MODULE}: {'; '.join(flag_violations)}"
    assert frozenset({"test_install.py"}) == INSTALLER_ALLOWLIST, (
        "AAP 0.14.3 allows exactly one module to install a dependency; a wider allowlist would "
        "let a second installer in without review"
    )


def test_no_module_asks_pytest_to_load_the_plugin_explicitly() -> None:
    """No module in this folder passes pytest the plugin-selection option (AAP 0.4.2).

    The folder is checked beside the tree-wide scan in ``tests/unit/test_no_dash_p_flag.py``, and
    this module's raw text with it, because its needles are fragments rather than whole spellings.
    """
    for name, source in sorted(_scanned_sources().items()):
        violations = _dash_p_violations(source)
        assert violations == [], f"tests/e2e/{name}: {'; '.join(violations)}"
    own_text = (E2E_FOLDER / SELF_MODULE).read_text(encoding="utf-8")
    spelled = [needle for needle in PLUGIN_FLAG_NEEDLES if needle in own_text]
    assert spelled == [], (
        f"this module's text spells the plugin-selection option {spelled}, which the fragments in "
        "PLUGIN_FLAG_NEEDLES exist to prevent: tests/unit/test_no_dash_p_flag.py would report "
        "this file (AAP 0.4.2)"
    )


def test_every_url_in_this_folder_is_loopback() -> None:
    """Every URL a module here holds addresses loopback, so no test can call home (AAP 0.9.2)."""
    for name, source in sorted(_scanned_sources().items()):
        violations = _non_loopback_urls(source)
        assert violations == [], f"tests/e2e/{name}: {'; '.join(violations)}"


def test_scanner_detects_each_violation_type() -> None:
    """Every rule fires on the snippet it exists for and stays quiet on the near misses.

    Prose legitimately names an installer and a remote URL, and no rule may read it as code.
    """
    process_sample = "import subprocess\n\nRUN = subprocess.run(['pip', 'install', 'x'])\n"
    assert _installer_violations(process_sample) != [], "a subprocess import with an install"
    assert _installer_violations("from subprocess import run\n") != [], "a from-import of it"
    assert _installer_violations("clean = 'nothing to see'\n") == [], "an ordinary module"

    assert _offline_flag_violations("CMD = ['python', '-m', 'build']\n") != [], "a bare build"
    assert _offline_flag_violations("CMD = ['uv', 'build', '--offline']\n") == [], "an offline one"

    assert _dash_p_violations("ARGS = ['-p', 'foundry']\n") != [], "an explicit plugin selection"
    assert _dash_p_violations("ARGS = ['-k', 'smoke']\n") == [], "an ordinary option"

    assert _non_loopback_urls("URL = 'https://example.invalid/x'\n") != [], "a remote host"
    assert _non_loopback_urls("URL = 'http://127.0.0.1:9'\n") == [], "a dead loopback port"

    documented = (
        '"""A documented command: pip install x against https://example.invalid/x."""\n'
        "\n"
        "CLEAN: int = 1\n"
    )
    assert _installer_violations(documented) == [], "prose is not an install"
    assert _non_loopback_urls(documented) == [], "prose is not a call home"
