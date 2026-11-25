"""The enforceable import-direction matrix of AAP 0.4.8, checked over the package's own source.

AAP 0.4.8 fixes which Foundry units may import which, and AAP 0.10.1 makes this module the place
that holds the source to it. The plan's two columns are transcribed verbatim -- :data:`MAY_IMPORT`
is "May import (Foundry)" and :data:`NEVER` is "Must never import" -- while :data:`ALLOWED` is the
rung the scan enforces: the "May import" column plus :data:`DOCUMENTED_DEPARTURES`, whose single
entry AAP 0.3.2 requires and whose reason is stated at the constant.

Detection is structural. Every module is parsed with :mod:`ast` and each ``import`` statement is
resolved to the absolute module paths it names, so prose that mentions a package is not a finding
while a real import is; relative imports are resolved against the importing module's package rather
than ignored, and a path inside the importing unit is no edge between units. Imports inside ``if
TYPE_CHECKING:`` blocks never run and are dropped, and one inside a function body is marked
deferred, which is the distinction the plugin startup rule needs.

Four more rules ride the same walk: ``foundry.fixtures`` raises the one upward edge to
``foundry.config`` (AAP 0.4.8, "The one upward edge"); ``foundry.plugin`` imports nothing heavy at
module scope, the structural half of the 150 ms startup budget (AAP 0.14.2); no module imports the
plugin, which activates through the ``pytest11`` entry point alone (AAP 0.4.2); and ``foundry.http``
never reaches ``foundry.reporting`` (AAP 0.4.8, "The reporting boundary"). ``tests/conftest.py`` is
checked too: a plugin import there is the same activation mistake by another route.

Every violation carries its path, line and offending ``importer -> imported`` edge, all at once and
sorted, because a scanner that stops at the first offender costs one run per offender, and one that
silently passes is worse than none: anti-vacuity floors refuse an empty walk, and three synthetic
trees in ``tmp_path`` prove the scanner still reports what it patches.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Final

import pytest

# --- The matrix, transcribed from AAP 0.4.8 ---------------------------------

ROOT_PACKAGE: Final[str] = "foundry"
"""The import package's own name: every unit in the matrix is a subpackage or module of it."""

PACKAGE_NAMES: Final[tuple[str, ...]] = (
    "core",
    "obs",
    "config",
    "contract",
    "http",
    "cassette",
    "data",
    "db",
    "fixtures",
    "assertions",
    "execution",
    "reporting",
    "diagnostics",
    "cli",
    "plugin",
)
"""The fifteen units the matrix covers, in the order the plan lists them."""

MAY_IMPORT: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        "core": frozenset(),
        "obs": frozenset({"core"}),
        "config": frozenset({"core", "obs"}),
        "contract": frozenset({"core"}),
        "http": frozenset({"core", "obs", "contract", "cassette"}),
        "cassette": frozenset({"core", "obs", "contract"}),
        "data": frozenset({"core", "obs", "contract"}),
        "db": frozenset({"core", "obs"}),
        # The single upward edge of AAP 0.4.8: the fixture set builds the resolved configuration.
        "fixtures": frozenset(
            {"core", "obs", "contract", "cassette", "http", "data", "db", "assertions", "config"}
        ),
        "assertions": frozenset({"core", "obs", "contract"}),
        "execution": frozenset({"core", "obs", "config", "http", "cassette", "data"}),
        "reporting": frozenset({"core", "obs"}),
        "diagnostics": frozenset({"core", "obs", "config"}),
        "cli": frozenset({"core", "obs", "config", "execution", "diagnostics", "reporting"}),
        # Above every other unit, so each edge from here points down, and the only rule that
        # constrains it is the startup budget over its module scope.
        "plugin": frozenset(PACKAGE_NAMES) - {"plugin"},
    }
)
"""AAP 0.4.8's "May import (Foundry)" column, entry for entry."""

NEVER: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        "core": frozenset(PACKAGE_NAMES) - {"core"},
        "obs": frozenset({"http", "data", "db", "reporting", "cli", "plugin"}),
        "config": frozenset({"http", "data", "db", "reporting", "execution", "cli", "plugin"}),
        "contract": frozenset({"http", "reporting", "cli"}),
        "http": frozenset({"reporting", "cli", "plugin", "execution"}),
        "cassette": frozenset({"reporting", "cli", "plugin", "http"}),
        "data": frozenset({"reporting", "cli", "plugin", "http"}),
        "db": frozenset({"reporting", "cli", "plugin"}),
        "fixtures": frozenset({"execution", "reporting", "cli", "plugin"}),
        "assertions": frozenset({"reporting", "cli", "plugin", "http"}),
        "execution": frozenset({"reporting", "cli", "plugin"}),
        "reporting": frozenset({"http", "data", "db", "execution", "cli", "plugin"}),
        "diagnostics": frozenset({"plugin", "reporting"}),
        "cli": frozenset({"plugin"}),
        "plugin": frozenset(),
    }
)
"""AAP 0.4.8's "Must never import" column, entry for entry; the full perimeter is MAY_IMPORT."""

CAPABILITY_RUNG: Final[frozenset[str]] = frozenset(
    {"fixtures", "http", "data", "db", "contract", "cassette", "assertions"}
)
"""The capability layer of AAP 0.3.2: it may import ``core``, ``obs`` and its own siblings."""

FOUNDATION: Final[frozenset[str]] = frozenset({"core", "obs"})
"""The foundation layer of AAP 0.3.2, which every other unit may import."""

DOCUMENTED_DEPARTURES: Final[frozenset[tuple[str, str]]] = frozenset({("contract", "obs")})
"""Edges beyond the "May import" column that AAP 0.3.2 requires, and nothing else may join them.

AAP 0.3.2 puts ``contract`` on the capability rung, whose members may import ``core``, ``obs`` and
their siblings, and every other capability package's 0.4.8 row does list ``obs``: the contract cell
is the one omission, not a prohibition, which the 0.4.8 "Must never import" column confirms by not
listing ``obs`` at all. The edge is load-bearing too -- ``foundry.contract.violations`` scrubs text
through the single redaction engine of AAP 0.14.3, and may not import ``foundry.config`` to build
that engine's key set -- so it stands, and this set is the only way to add another.
"""

ALLOWED: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        unit: frozenset(
            MAY_IMPORT[unit]
            | {imported for source, imported in DOCUMENTED_DEPARTURES if source == unit}
        )
        for unit in PACKAGE_NAMES
    }
)
"""The rung the scan enforces: the plan's "May import" column plus its documented departures."""

PLUGIN_MODULE_SCOPE_ROOTS: Final[tuple[str, ...]] = (
    "__future__",
    "typing",
    "pytest",
    "foundry.core",
    "foundry.execution.state",
)
"""Every import root ``src/foundry/plugin.py`` may name at module scope (AAP 0.4.8, AAP 0.14.2).

The plugin is imported at every pytest start, so a heavy module at module scope spends the 150 ms
budget before a test is collected. ``foundry.execution.state`` is permitted because it holds the
typed stash keys and the ``user_properties`` wire format and imports only ``core`` and ``obs``.
"""

HEAVY_MODULE_SCOPE_NEIGHBOURS: Final[tuple[str, ...]] = (
    "foundry.config",
    "foundry.execution.coordinator",
    "foundry.fixtures",
    "foundry.reporting",
)
"""The collaborators AAP 0.4.8's import-time discipline defers, checked one by one."""

# --- The tree the rules are checked over ------------------------------------

MODULE_ROOT: Final[Path] = Path("src")
"""The directory module names derive from: ``src/foundry/http/session.py`` is ``foundry.http``."""

SOURCE_ROOT: Final[Path] = MODULE_ROOT / "foundry"
"""The package every layering rule is checked over (AAP 0.4.8)."""

PLUGIN_RELATIVE: Final[Path] = SOURCE_ROOT / "plugin.py"
"""The single hook module, whose module scope carries the startup budget (AAP 0.4.2, 0.14.2)."""

CONFTEST_RELATIVE: Final[Path] = Path("tests") / "conftest.py"
"""The suite's top-level conftest, which must never import the plugin (AAP 0.4.2)."""

KNOWN_MODULE_FILES: Final[tuple[str, ...]] = (
    "src/foundry/__init__.py",
    "src/foundry/cli/main.py",
    "src/foundry/core/exceptions.py",
    "src/foundry/http/session.py",
    "src/foundry/obs/redaction.py",
    "src/foundry/plugin.py",
    "src/foundry/reporting/html.py",
)
"""One module per layer a working walk must reach, so a broken glob cannot pass as a clean tree."""

MINIMUM_MODULE_COUNT: Final[int] = 16
"""A floor on the scanned module count: the package root, the plugin and the fifteen units."""

CACHE_DIRECTORY_NAMES: Final[frozenset[str]] = frozenset(
    {"__pycache__", ".git", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".venv"}
)
"""Directories holding no first-party source, so the walk never descends into them."""


# --- What the walk reports --------------------------------------------------


@dataclass(frozen=True, order=True)
class ImportSite:
    """One module path an ``import`` statement names, resolved against the importing package.

    ``unit`` is the Foundry unit of the importing module (``http``, or ``foundry`` for the package
    root) and ``target`` is an absolute path the statement names, so ``from foundry.execution import
    state`` yields both ``foundry.execution`` and ``foundry.execution.state``. ``module_scope`` is
    true when the statement runs as the module is imported, and the statement is kept as written so
    a message points at something the reader can find.
    """

    path: str
    line: int
    unit: str
    target: str
    module_scope: bool
    statement: str


@dataclass(frozen=True, order=True)
class Edge:
    """One unit-to-unit edge, ordered by path and line so a report reads identically each run."""

    path: str
    line: int
    importer: str
    imported: str
    module_scope: bool
    statement: str

    @property
    def label(self) -> str:
        """Return the edge as ``foundry.<importer> -> foundry.<imported>``."""
        return f"{_qualify(self.importer)} -> {_qualify(self.imported)}"

    def describe(self) -> str:
        """Return the one-line report: where the edge stands, the edge, and its statement."""
        return f"{self.path}:{self.line}: {self.label} ({self.statement})"


def _qualify(unit: str) -> str:
    return unit if unit == ROOT_PACKAGE else f"{ROOT_PACKAGE}.{unit}"


# --- Resolving a statement to the paths and units behind it ------------------


def _is_type_checking(test: ast.expr) -> bool:
    """Return True when an ``if`` guard is ``TYPE_CHECKING`` or ``typing.TYPE_CHECKING``."""
    name = test.attr if isinstance(test, ast.Attribute) else getattr(test, "id", "")
    return bool(name == "TYPE_CHECKING")


class _ImportCollector(ast.NodeVisitor):
    """Collect every ``import`` statement of one module as an :class:`ImportSite`.

    ``_type_checking`` counts enclosing ``if TYPE_CHECKING:`` blocks, whose imports never run, and
    ``_function_depth`` counts enclosing function bodies: a class body executes as the module is
    imported and stays in module scope, while a function body's import happens when the function is
    called, the deferral the startup rule is about.
    """

    def __init__(self, *, package: str, unit: str, path: str) -> None:
        self._package = package
        self._unit = unit
        self._path = path
        self._type_checking = 0
        self._function_depth = 0
        self.sites: list[ImportSite] = []

    def visit_If(self, node: ast.If) -> None:
        if not _is_type_checking(node.test):
            self.generic_visit(node)
            return
        self._type_checking += 1
        try:
            for statement in node.body:
                self.visit(statement)
        finally:
            self._type_checking -= 1
        for statement in node.orelse:
            self.visit(statement)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._descend(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._descend(node)

    def visit_Import(self, node: ast.Import) -> None:
        self._record(node, (alias.name for alias in node.names))

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        base = self._absolute(node)
        if base is None:
            return
        names = [alias.name for alias in node.names if alias.name != "*"]
        self._record(node, (base, *(f"{base}.{name}" for name in names)))

    def _descend(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._function_depth += 1
        try:
            self.generic_visit(node)
        finally:
            self._function_depth -= 1

    def _absolute(self, node: ast.ImportFrom) -> str | None:
        """Return the absolute module path a ``from`` clause names, or None when it names none."""
        if not node.level:
            return node.module or None
        parts = self._package.split(".")
        keep = len(parts) - (node.level - 1)
        if keep < 1:
            return None
        base = ".".join(parts[:keep])
        return f"{base}.{node.module}" if node.module else base

    def _record(self, node: ast.Import | ast.ImportFrom, targets: Iterable[str]) -> None:
        if self._type_checking:
            return
        statement = ast.unparse(node)
        for target in targets:
            self.sites.append(
                ImportSite(
                    path=self._path,
                    line=node.lineno,
                    unit=self._unit,
                    target=target,
                    module_scope=self._function_depth == 0,
                    statement=statement,
                )
            )


def _unit_label(relative: Path) -> str:
    """Return the Foundry unit a module path belongs to, or the package root's own name."""
    parts = relative.relative_to(MODULE_ROOT).parts
    if len(parts) > 2:
        return parts[1]
    stem = Path(parts[-1]).stem
    return ROOT_PACKAGE if stem == "__init__" else stem


def _context(relative: Path) -> tuple[str, str]:
    """Return the ``(package, unit)`` an in-package module path stands for."""
    parts = relative.with_suffix("").relative_to(MODULE_ROOT).parts
    module = ".".join(parts[:-1] if parts[-1] == "__init__" else parts)
    package = module if relative.name == "__init__.py" else module.rpartition(".")[0]
    return package, _unit_label(relative)


def _collect(base: Path, relative: Path, *, package: str, unit: str) -> tuple[ImportSite, ...]:
    path = base / relative
    display = relative.as_posix()
    collector = _ImportCollector(package=package, unit=unit, path=display)
    collector.visit(ast.parse(path.read_text(encoding="utf-8"), filename=display))
    return tuple(sorted(collector.sites))


def _library_modules(base: Path) -> tuple[Path, ...]:
    source = base / SOURCE_ROOT
    if not source.is_dir():
        return ()
    modules = [
        path.relative_to(base)
        for path in source.rglob("*.py")
        if not CACHE_DIRECTORY_NAMES.intersection(path.relative_to(source).parts[:-1])
    ]
    return tuple(sorted(modules))


def _scan_tree(base: Path) -> tuple[ImportSite, ...]:
    """Walk ``base``'s copy of the package and return every import site it holds, sorted."""
    sites: list[ImportSite] = []
    for relative in _library_modules(base):
        package, unit = _context(relative)
        sites.extend(_collect(base, relative, package=package, unit=unit))
    return tuple(sorted(sites))


def _plugin_module_scope_sites(base: Path) -> tuple[ImportSite, ...]:
    package, unit = _context(PLUGIN_RELATIVE)
    return _collect(base, PLUGIN_RELATIVE, package=package, unit=unit)


def _unit_of(target: str) -> str | None:
    """Return the Foundry unit an absolute module path belongs to, or None when it names none."""
    parts = target.split(".")
    if not parts or parts[0] != ROOT_PACKAGE:
        return None
    if len(parts) == 1:
        return ROOT_PACKAGE
    return parts[1] if parts[1] in PACKAGE_NAMES else None


def _edges(sites: Sequence[ImportSite]) -> tuple[Edge, ...]:
    """Return one :class:`Edge` per statement and imported unit, sorted, duplicates dropped."""
    edges: dict[tuple[str, int, str], Edge] = {}
    for site in sites:
        imported = _unit_of(site.target)
        # The import package itself is not an edge: ``import foundry`` and
        # ``from foundry import __version__`` are allowed for every module (AAP 0.4.8).
        if imported is None or imported in {site.unit, ROOT_PACKAGE}:
            continue
        edges.setdefault(
            (site.path, site.line, imported),
            Edge(
                path=site.path,
                line=site.line,
                importer=site.unit,
                imported=imported,
                module_scope=site.module_scope,
                statement=site.statement,
            ),
        )
    return tuple(sorted(edges.values()))


# --- The checks ------------------------------------------------------------


def _may_import(unit: str) -> frozenset[str]:
    return frozenset(PACKAGE_NAMES) if unit == ROOT_PACKAGE else ALLOWED[unit]


def _violations(edges: Iterable[Edge]) -> tuple[Edge, ...]:
    return tuple(sorted(edge for edge in edges if edge.imported not in _may_import(edge.importer)))


def _prohibited(edges: Iterable[Edge]) -> tuple[Edge, ...]:
    return tuple(sorted(edge for edge in edges if edge.imported in NEVER.get(edge.importer, ())))


def _statement_is_sanctioned(targets: Sequence[str]) -> bool:
    """Return True when every module path one statement names is permitted at module scope.

    A path passes when it lies under an allowed root, or when it is an ancestor package of one:
    ``from foundry.execution import state`` names ``foundry.execution`` as well as
    ``foundry.execution.state``, and that package's own module is a PEP 562 lazy table, so reaching
    it costs nothing (AAP 0.4.8, "Import-time discipline"). Any other alias fails the statement.
    """
    roots = PLUGIN_MODULE_SCOPE_ROOTS
    return all(
        any(target == root or target.startswith(f"{root}.") for root in roots)
        or any(root.startswith(f"{target}.") for root in roots)
        for target in targets
    )


def _module_scope_offenders(sites: Sequence[ImportSite]) -> tuple[tuple[str, int, str], ...]:
    """Return ``(path, line, statement)`` for each module-scope statement the budget refuses."""
    grouped: dict[tuple[str, int], tuple[str, list[str]]] = {}
    for site in sites:
        if site.module_scope:
            grouped.setdefault((site.path, site.line), (site.statement, []))[1].append(site.target)
    offenders = [
        (path, line, statement)
        for (path, line), (statement, targets) in grouped.items()
        if not _statement_is_sanctioned(targets)
    ]
    return tuple(sorted(offenders))


# --- Failure messages and synthetic trees ----------------------------------


def _format_edges(edges: Sequence[Edge], heading: str) -> str:
    """Render ``edges`` as a failure message: a count and the heading, then one line per edge."""
    lines = [f"  {edge.describe()}" for edge in edges]
    return "\n".join([f"{len(edges)} {heading}", *lines])


def _format_module_scope(offenders: Sequence[tuple[str, int, str]]) -> str:
    """Render refused module-scope statements as a failure message, sorted."""
    lines = [f"  {path}:{line}: {statement}" for path, line, statement in offenders]
    return "\n".join(
        [f"{len(offenders)} module-scope import(s) breach the 150 ms startup budget:", *lines]
    )


def _format_sites(sites: Sequence[ImportSite]) -> str:
    lines = sorted({f"  {site.path}:{site.line}: {site.target}" for site in sites})
    return "\n".join([f"{len(lines)} import(s):", *lines])


def _write_module(base: Path, relative: str, *lines: str) -> Path:
    """Write ``lines`` to ``base/relative``, creating parent directories, and return the path."""
    path = base / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    return path


# --- The matrix, over the real tree ----------------------------------------


def test_no_module_reverses_the_import_matrix(repo_root: Path) -> None:
    """No edge may point at a unit its importer's row does not list (AAP 0.4.8)."""
    edges = _edges(_scan_tree(repo_root))
    violations = _violations(edges)
    assert violations == (), _format_edges(
        violations, heading="import-direction violation(s) against AAP 0.4.8"
    )
    prohibited = _prohibited(edges)
    assert prohibited == (), _format_edges(
        prohibited, heading="edge(s) the 'Must never import' column forbids"
    )


def test_the_scan_reaches_the_package_and_its_known_modules(repo_root: Path) -> None:
    """A scan that finds nothing must fail, so these floors are part of the proof."""
    modules = tuple(relative.as_posix() for relative in _library_modules(repo_root))
    assert len(modules) >= MINIMUM_MODULE_COUNT, (
        f"the layering scan reached {len(modules)} module(s) under {SOURCE_ROOT.as_posix()}, "
        f"fewer than the {MINIMUM_MODULE_COUNT} a real package holds: the walk is broken"
    )
    missing = sorted(set(KNOWN_MODULE_FILES) - set(modules))
    assert missing == [], f"the layering scan never reached these modules: {missing}"
    units = {_unit_label(Path(module)) for module in modules}
    assert units == set(PACKAGE_NAMES) | {ROOT_PACKAGE}, sorted(units)
    assert "tests/unit/test_layering.py" not in modules, "this module sits outside its own scan"


def test_fixtures_raises_the_only_upward_edge(repo_root: Path) -> None:
    """``fixtures -> config`` is the one rung-crossing edge, and only ``fixtures`` raises it."""
    edges = _edges(_scan_tree(repo_root))
    rung = CAPABILITY_RUNG | FOUNDATION
    upward = sorted(
        {
            (edge.importer, edge.imported)
            for edge in edges
            if edge.importer in CAPABILITY_RUNG and edge.imported not in rung
        }
    )
    assert upward == [("fixtures", "config")], f"the capability rung crosses at {upward}"
    raised = [edge for edge in edges if (edge.importer, edge.imported) == ("fixtures", "config")]
    assert raised != [], "the fixtures -> config edge is missing: the rule would pass on nothing"
    prefix = f"{SOURCE_ROOT.as_posix()}/fixtures/"
    strays = tuple(edge for edge in raised if not edge.path.startswith(prefix))
    assert strays == (), _format_edges(strays, heading=f"fixtures -> config edge(s) from {prefix}")


def test_the_reporting_boundary_holds(repo_root: Path) -> None:
    """``foundry.http`` never imports ``foundry.reporting``: the reporting boundary (AAP 0.4.8)."""
    crossings = tuple(
        edge
        for edge in _edges(_scan_tree(repo_root))
        if (edge.importer, edge.imported) == ("http", "reporting")
    )
    assert crossings == (), _format_edges(
        crossings, heading="edge(s) across the reporting boundary: http -> reporting"
    )


def test_no_module_imports_the_plugin(repo_root: Path) -> None:
    """The plugin activates by entry point, so nothing in the package imports it (AAP 0.4.2).

    ``tests/`` is the documented allowance -- a test may import ``foundry.plugin`` to exercise a
    collaborator directly -- and this scan never reaches that tree.
    """
    importers = tuple(edge for edge in _edges(_scan_tree(repo_root)) if edge.imported == "plugin")
    assert importers == (), _format_edges(
        importers, heading="module(s) importing foundry.plugin, which activates by entry point"
    )


def test_the_plugin_stays_within_its_module_scope_budget(repo_root: Path) -> None:
    """Only ``__future__``, ``typing``, ``pytest`` and the two foundation modules (AAP 0.14.2)."""
    sites = _plugin_module_scope_sites(repo_root)
    assert sites, f"the startup-budget scan found no import in {PLUGIN_RELATIVE.as_posix()}"
    offenders = _module_scope_offenders(sites)
    assert offenders == (), _format_module_scope(offenders)
    deferred = tuple(
        site
        for site in sites
        if site.module_scope
        and any(
            site.target == name or site.target.startswith(f"{name}.")
            for name in HEAVY_MODULE_SCOPE_NEIGHBOURS
        )
    )
    assert deferred == (), _format_sites(deferred)


def test_the_test_conftest_stays_plugin_neutral(repo_root: Path) -> None:
    """The suite's conftest parses and imports no plugin: activation is the entry point's job."""
    path = repo_root / CONFTEST_RELATIVE
    assert path.is_file(), f"{CONFTEST_RELATIVE.as_posix()} is missing"
    assert ast.parse(path.read_text(encoding="utf-8")) is not None
    sites = _collect(repo_root, CONFTEST_RELATIVE, package="tests", unit="conftest")
    plugin_sites = [site for site in sites if site.target.startswith("foundry.plugin")]
    assert plugin_sites == [], _format_sites(plugin_sites)


# --- The matrix's own consistency ------------------------------------------


def test_the_matrix_is_the_plan_plus_its_one_documented_departure() -> None:
    """The enforced rung is the plan's column plus the one departure AAP 0.3.2 requires."""
    assert frozenset({("contract", "obs")}) == DOCUMENTED_DEPARTURES
    assert set(MAY_IMPORT) == set(PACKAGE_NAMES) == set(NEVER)
    assert [unit for unit in PACKAGE_NAMES if ALLOWED[unit] & NEVER[unit]] == []
    assert sorted(unit for unit in CAPABILITY_RUNG if "obs" not in MAY_IMPORT[unit]) == ["contract"]
    for unit in CAPABILITY_RUNG:
        assert "obs" in ALLOWED[unit], f"{unit} omits obs, which the capability rung grants it"
    rebuilt = {
        unit: frozenset(
            MAY_IMPORT[unit]
            | {imported for source, imported in DOCUMENTED_DEPARTURES if source == unit}
        )
        for unit in PACKAGE_NAMES
    }
    assert rebuilt == dict(ALLOWED)


# --- The resolver, over synthetic shapes -----------------------------------


@pytest.mark.parametrize(
    ("source", "targets", "edges"),
    [
        ("import foundry.http.session", {"foundry.http.session"}, ()),
        ("import foundry", {"foundry"}, ()),
        ("from foundry import http", {"foundry", "foundry.http"}, ()),
        ("from foundry import __version__", {"foundry", "foundry.__version__"}, ()),
        ("from . import url", {"foundry.http", "foundry.http.url"}, ()),
        ("from foundry.http import FoundrySession", {"foundry.http"}, ()),
        ("from ..core import exceptions", {"foundry.core"}, (("http", "core"),)),
        ("from ..reporting import html", {"foundry.reporting"}, (("http", "reporting"),)),
    ],
)
def test_every_import_shape_resolves_to_its_units(
    tmp_path: Path, source: str, targets: set[str], edges: tuple[tuple[str, str], ...]
) -> None:
    """Each shape names its absolute targets, and only a crossing one raises an edge."""
    _write_module(tmp_path, "src/foundry/http/session.py", source)
    sites = _scan_tree(tmp_path)
    assert {site.target for site in sites} >= targets
    assert tuple((edge.importer, edge.imported) for edge in _edges(sites)) == edges


def test_type_checking_blocks_raise_no_edge(tmp_path: Path) -> None:
    """The block never runs, so its imports are not edges between units."""
    _write_module(
        tmp_path,
        "src/foundry/http/session.py",
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import foundry.reporting",
    )
    assert {site.target for site in _scan_tree(tmp_path)} == {"typing", "typing.TYPE_CHECKING"}


def test_a_function_body_import_is_recorded_as_deferred(tmp_path: Path) -> None:
    """The startup rule has to tell a module-scope import from a hook body's one."""
    _write_module(
        tmp_path,
        "src/foundry/http/session.py",
        "import foundry.cassette\ndef deferred() -> None:\n    import foundry.reporting",
    )
    edges = _edges(_scan_tree(tmp_path))
    assert [(edge.label, edge.module_scope) for edge in edges] == [
        ("foundry.http -> foundry.cassette", True),
        ("foundry.http -> foundry.reporting", False),
    ]


# --- The scanner still fires: three synthetic trees ------------------------


def test_the_scan_reports_an_injected_violation(tmp_path: Path) -> None:
    """A reversed edge is reported with its path, line and edge, and never only the first one."""
    _write_module(tmp_path, "src/foundry/http/session.py", "import foundry.reporting")
    _write_module(tmp_path, "src/foundry/core/ids.py", "from foundry.data import cases")
    violations = _violations(_edges(_scan_tree(tmp_path)))
    message = _format_edges(violations, heading="import-direction violation(s) against AAP 0.4.8")
    assert "2 import-direction violation(s)" in message, message
    assert "src/foundry/http/session.py:1: foundry.http -> foundry.reporting" in message, message
    assert "src/foundry/core/ids.py:1: foundry.core -> foundry.data" in message, message


def test_the_plugin_scope_rule_reports_a_heavy_module_scope_import(tmp_path: Path) -> None:
    """A heavy module at module scope is refused, and a hook body's import is not."""
    _write_module(
        tmp_path,
        "src/foundry/plugin.py",
        "from __future__ import annotations\nimport typing\nfrom foundry.core import exceptions\n"
        "from foundry.execution import state\nimport foundry.config\n"
        "def hook() -> None:\n    import foundry.reporting",
    )
    offenders = _module_scope_offenders(_plugin_module_scope_sites(tmp_path))
    assert [line for _, line, _ in offenders] == [5], _format_module_scope(offenders)


def test_the_plugin_import_rule_reports_an_importer(tmp_path: Path) -> None:
    """Importing the plugin from inside the package is an edge the matrix has to refuse."""
    _write_module(tmp_path, "src/foundry/http/session.py", "from foundry import plugin")
    importers = [edge.path for edge in _edges(_scan_tree(tmp_path)) if edge.imported == "plugin"]
    assert importers == ["src/foundry/http/session.py"]
