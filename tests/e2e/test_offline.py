"""Offline evidence for Foundry's own suite: the guard's counters plus a mechanical folder scan.
The guard sees this interpreter only, so the scan proves the offline discipline is written into this
folder's code — the installer allowlist and the loopback-only URLs (AAP 0.14.3). Every rule is a
pure function over parsed source, so a docstring naming an installer or a URL is never a finding."""

import ast
import pathlib
import re
from collections.abc import Sequence
from typing import Final, Protocol

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
INSTALLER_MODULE: Final[str] = "test_install.py"
INSTALLER_ALLOWLIST: Final[frozenset[str]] = frozenset({INSTALLER_MODULE})
SCANNER_EXCLUSIONS: Final[frozenset[str]] = frozenset({SELF_MODULE})
INSTALLER_TOKENS: Final[frozenset[str]] = frozenset({"pip", "uv"})
INSTALL_VERBS: Final[frozenset[str]] = frozenset({"install", "build", "venv"})
# Assembled from fragments: a literal spelling would make this module an offender in the tree-wide
# scan tests/unit/test_no_dash_p_flag.py runs over every file.
_PLUGIN_FLAG: Final[str] = "-p"
_PLUGIN_NAME: Final[str] = "foundry"
_JOINERS: Final[tuple[str, ...]] = (" ", "no:", " no:")
PLUGIN_FLAG_NEEDLES: Final[tuple[str, ...]] = tuple(
    f"{_PLUGIN_FLAG}{j}{_PLUGIN_NAME}" for j in _JOINERS
)
_PLUGIN_FLAG_PATTERN: Final[re.Pattern[str]] = re.compile(
    rf"{_PLUGIN_FLAG}\W*?(?:no:)?{_PLUGIN_NAME}"
)
_URL_STOP: Final[str] = r"""\s"'`\\/?#<>"""
_URL_PATTERN: Final[re.Pattern[str]] = re.compile(rf"https?://(?P<authority>[^{_URL_STOP}]*)")
_LOOPBACK_HOSTS: Final[frozenset[str]] = frozenset({"localhost", "::1"})
_LOOPBACK_IPV4: Final[re.Pattern[str]] = re.compile(r"127\.\d{1,3}\.\d{1,3}\.\d{1,3}")
GUARD_ATTRIBUTE: Final[str] = "FOUNDRY_SOCKET_GUARD"
ATTEMPTS_ATTRIBUTE: Final[str] = "FOUNDRY_NON_LOOPBACK_ATTEMPTS"


class _Guard(Protocol):
    installed: bool
    non_loopback_attempts: int
    violations: Sequence[str]


def _constants(source: str) -> set[str]:
    """Every string constant that is not a bare expression: prose is never a flag."""
    tree = ast.parse(source)
    prose = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
    }
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in prose
    }


def _installer_violations(source: str) -> list[str]:
    """The ways ``source`` starts a process of its own or reaches an installer (AAP 0.14.3)."""
    nodes = list(ast.walk(ast.parse(source)))
    names = {a.name.partition(".")[0] for n in nodes if isinstance(n, ast.Import) for a in n.names}
    names |= {(n.module or "").partition(".")[0] for n in nodes if isinstance(n, ast.ImportFrom)}
    constants = _constants(source)
    violations = []
    if "subprocess" in names:
        violations.append(f"imports subprocess; only {INSTALLER_MODULE} starts a child of its own")
    tokens, verbs = sorted(constants & INSTALLER_TOKENS), sorted(constants & INSTALL_VERBS)
    if tokens and verbs:
        violations.append(f"holds the installer tokens {tokens} beside the verbs {verbs}")
    return violations


def _dash_p_violations(source: str) -> list[str]:
    """The plugin-selection spellings ``source`` holds: activation is the entry point alone."""
    constants = _constants(source)
    violations = []
    if {_PLUGIN_FLAG, _PLUGIN_NAME} <= constants:
        violations.append(f"holds {_PLUGIN_FLAG!r} and {_PLUGIN_NAME!r} as an argument vector")
    violations.extend(
        f"holds {constant!r}, which spells the plugin-selection option"
        for constant in sorted(constants)
        if _PLUGIN_FLAG_PATTERN.search(constant)
    )
    return violations


def _non_loopback_urls(source: str) -> list[str]:
    """The URLs whose host is not loopback, sorted: no test may call home (AAP 0.9.2)."""
    findings: set[str] = set()
    for constant in _constants(source):
        for match in _URL_PATTERN.finditer(constant):
            authority = match.group("authority").rpartition("@")[2]
            if authority.startswith("["):
                host = authority.partition("]")[0].lstrip("[")
            else:
                host = authority.partition(":")[0]
            if not host or host in _LOOPBACK_HOSTS or _LOOPBACK_IPV4.fullmatch(host):
                continue
            findings.add(f"holds {match.group(0)!r}, whose host {host!r} is not loopback")
    return sorted(findings)


def test_socket_guard_observed_no_non_loopback_attempt(socket_guard: _Guard) -> None:
    assert socket_guard.installed is True, "the loopback-only socket guard is not installed"
    attempts = socket_guard.non_loopback_attempts
    assert attempts == 0, f"{attempts} non-loopback attempt(s): {list(socket_guard.violations)}"
    assert list(socket_guard.violations) == [], "a violation was recorded beside a zero counter"
    published = getattr(socket_guard, ATTEMPTS_ATTRIBUTE, None)
    announced = getattr(socket_guard, GUARD_ATTRIBUTE, None)
    assert (announced, published) == (True, attempts), "foundry doctor cannot read the guard"


def test_only_the_install_module_touches_an_installer() -> None:
    sources = {p.name: p.read_text(encoding="utf-8") for p in sorted(E2E_FOLDER.glob("test_*.py"))}
    missing = sorted(EXPECTED_MODULES - set(sources))
    assert missing == [], f"the folder holds no {missing}"
    for name in sorted(set(sources) - SCANNER_EXCLUSIONS - INSTALLER_ALLOWLIST):
        violations = _installer_violations(sources[name])
        assert violations == [], f"tests/e2e/{name}: {'; '.join(violations)}"
    assert frozenset({INSTALLER_MODULE}) == INSTALLER_ALLOWLIST, "one module may install"


def test_no_module_asks_pytest_to_load_the_plugin_explicitly() -> None:
    sources = {p.name: p.read_text(encoding="utf-8") for p in sorted(E2E_FOLDER.glob("test_*.py"))}
    for name in sorted(set(sources) - SCANNER_EXCLUSIONS):
        violations = _dash_p_violations(sources[name])
        assert violations == [], f"tests/e2e/{name}: {'; '.join(violations)}"
    own_text = (E2E_FOLDER / SELF_MODULE).read_text(encoding="utf-8")
    spelled = [needle for needle in PLUGIN_FLAG_NEEDLES if needle in own_text]
    assert spelled == [], f"this module spells the plugin-selection option {spelled}"


def test_every_url_in_this_folder_is_loopback() -> None:
    sources = {p.name: p.read_text(encoding="utf-8") for p in sorted(E2E_FOLDER.glob("test_*.py"))}
    for name in sorted(set(sources) - SCANNER_EXCLUSIONS):
        violations = _non_loopback_urls(sources[name])
        assert violations == [], f"tests/e2e/{name}: {'; '.join(violations)}"


def test_scanner_detects_each_violation_type() -> None:
    process_sample = "import subprocess\n\nRUN = subprocess.run(['pip', 'install', 'x'])\n"
    assert _installer_violations(process_sample) != [], "a subprocess import with an install"
    assert _installer_violations("from subprocess import run\n") != [], "a from-import of it"
    assert _installer_violations("clean = 'nothing to see'\n") == [], "an ordinary module"
    assert _dash_p_violations("ARGS = ['-p', 'foundry']\n") != [], "an explicit plugin selection"
    assert _dash_p_violations("ARGS = ['-k', 'smoke']\n") == [], "an ordinary option"
    assert _non_loopback_urls("URL = 'https://example.invalid/x'\n") != [], "a remote host"
    assert _non_loopback_urls("URL = 'http://127.0.0.1:9'\n") == [], "a dead loopback port"
    documented = '"""A documented command: pip install x against https://example.invalid/x."""\n'
    assert _installer_violations(documented) == [], "prose is not an install"
    assert _non_loopback_urls(documented) == [], "prose is not a call home"
