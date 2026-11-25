"""AAP 0.4.8's import-direction matrix over the package's own source; contract -> obs departs it."""

from __future__ import annotations

import ast
from collections.abc import Callable, Iterable, Iterator, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Final, NamedTuple

import pytest

ROOT_PACKAGE: Final[str] = "foundry"
COLUMNS: Final[Mapping[str, tuple[str, str]]] = MappingProxyType(
    {
        "core": ("", "*"),
        "obs": ("core", "http data db reporting cli plugin"),
        "config": ("core obs", "http data db reporting execution cli plugin"),
        "contract": ("core", "http reporting cli"),
        "http": ("core obs contract cassette", "reporting cli plugin execution"),
        "cassette": ("core obs contract", "reporting cli plugin http"),
        "data": ("core obs contract", "reporting cli plugin http"),
        "db": ("core obs", "reporting cli plugin"),
        "fixtures": (
            "core obs contract cassette http data db assertions config",
            "execution reporting cli plugin",
        ),
        "assertions": ("core obs contract", "reporting cli plugin http"),
        "execution": ("core obs config http cassette data", "reporting cli plugin"),
        "reporting": ("core obs", "http data db execution cli plugin"),
        "diagnostics": ("core obs config", "plugin reporting"),
        "cli": ("core obs config execution diagnostics reporting", "plugin"),
        "plugin": ("*", ""),
    }
)
PACKAGE_NAMES: Final[tuple[str, ...]] = tuple(COLUMNS)


def _column(column: str, unit: str) -> frozenset[str]:
    return frozenset(PACKAGE_NAMES if column == "*" else column.split()) - {unit}


MAY_IMPORT: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {unit: _column(may, unit) for unit, (may, _) in COLUMNS.items()}
)
NEVER: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {unit: _column(never, unit) for unit, (_, never) in COLUMNS.items()}
)
CAPABILITY_RUNG: Final[frozenset[str]] = frozenset(
    {"fixtures", "http", "data", "db", "contract", "cassette", "assertions"}
)
# The one departure: AAP 0.3.2 puts contract on the capability rung, which grants obs, and 0.4.8's
# "Must never import" cell does not forbid it, so contract.violations reaches the redaction engine.
DOCUMENTED_DEPARTURES: Final[frozenset[tuple[str, str]]] = frozenset({("contract", "obs")})
# foundry.execution.state is allowed at module scope: typed stash keys, no startup-budget cost.
PLUGIN_MODULE_SCOPE_ROOTS: Final[frozenset[str]] = frozenset(
    {"__future__", "typing", "pytest", "foundry.core", "foundry.execution.state"}
)
SOURCE_ROOT: Final[Path] = Path("src/foundry")
PLUGIN_RELATIVE: Final[Path] = SOURCE_ROOT / "plugin.py"
KNOWN_MODULE_FILES: Final[tuple[str, ...]] = (
    "src/foundry/__init__.py",
    "src/foundry/cli/main.py",
    "src/foundry/core/exceptions.py",
    "src/foundry/http/session.py",
    "src/foundry/obs/redaction.py",
    "src/foundry/plugin.py",
    "src/foundry/reporting/html.py",
)
MINIMUM_MODULE_COUNT: Final[int] = 16
CACHE_DIRECTORY_NAMES: Final[frozenset[str]] = frozenset(
    {"__pycache__", ".git", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".venv"}
)


_Context = tuple[str, str, str]


class Site(NamedTuple):
    path: str
    line: int
    unit: str
    target: str
    scope: bool
    statement: str

    @property
    def imported(self) -> str | None:
        parts = self.target.split(".")
        named = len(parts) > 1 and parts[0] == ROOT_PACKAGE and parts[1] in PACKAGE_NAMES
        return parts[1] if named else None

    @property
    def label(self) -> str:
        return f"{_qualify(self.unit)} -> {_qualify(self.imported or '')}"


def _qualify(unit: str) -> str:
    return unit if unit == ROOT_PACKAGE else f"{ROOT_PACKAGE}.{unit}"


def _is_type_checking(test: ast.expr) -> bool:
    name = test.attr if isinstance(test, ast.Attribute) else getattr(test, "id", "")
    return bool(name == "TYPE_CHECKING")


def _under(target: str, roots: Iterable[str]) -> bool:
    return any(target == root or target.startswith(f"{root}.") for root in roots)


def _qualified(node: ast.ImportFrom, package: str) -> str | None:
    # A relative import's leading dots are resolved against the module's own package.
    if not node.level:
        return node.module or None
    parts = package.split(".")
    keep = len(parts) - (node.level - 1)
    if keep < 1:
        return None
    base = ".".join(parts[:keep])
    return f"{base}.{node.module}" if node.module else base


def _targets(node: ast.Import | ast.ImportFrom, package: str) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    base = _qualified(node, package) or ""
    named = [alias.name for alias in node.names if alias.name != "*"]
    return [base, *(f"{base}.{name}" for name in named)] if base else []


def _sites(ctx: _Context, nodes: Iterable[ast.AST], scope: bool = True) -> Iterator[Site]:
    unit, path, package = ctx
    for node in nodes:
        if isinstance(node, ast.If) and _is_type_checking(node.test):
            yield from _sites(ctx, node.orelse, scope)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            yield from _sites(ctx, node.body, False)
        elif isinstance(node, ast.Import | ast.ImportFrom):
            for target in _targets(node, package):
                yield Site(path, node.lineno, unit, target, scope, ast.unparse(node))
        else:
            yield from _sites(ctx, ast.iter_child_nodes(node), scope)


def _unit_label(relative: Path) -> str:
    parts = relative.relative_to("src").parts
    if len(parts) > 2:
        return parts[1]
    return ROOT_PACKAGE if relative.name == "__init__.py" else relative.stem


def _context(relative: Path) -> _Context:
    parts = relative.with_suffix("").relative_to("src").parts
    module = ".".join(parts[:-1] if parts[-1] == "__init__" else parts)
    package = module if relative.name == "__init__.py" else module.rpartition(".")[0]
    return _unit_label(relative), relative.as_posix(), package


def _sites_of(base: Path, relative: Path) -> tuple[Site, ...]:
    ctx = _context(relative)
    tree = ast.parse((base / ctx[1]).read_text(encoding="utf-8"), filename=ctx[1])
    return tuple(sorted(_sites(ctx, [tree])))


def _library_modules(base: Path) -> tuple[Path, ...]:
    source = base / SOURCE_ROOT
    found = source.rglob("*.py") if source.is_dir() else ()
    kept = (p for p in found if not CACHE_DIRECTORY_NAMES.intersection(p.parts))
    return tuple(sorted(p.relative_to(base) for p in kept))


def _scan_tree(base: Path) -> tuple[Site, ...]:
    found = [_sites_of(base, relative) for relative in _library_modules(base)]
    return tuple(sorted(site for sites in found for site in sites))


def _edges(sites: Iterable[Site]) -> tuple[Site, ...]:
    kept = [site for site in sites if site.imported not in (None, site.unit, ROOT_PACKAGE)]
    return tuple(sorted({(s.path, s.line, s.imported): s for s in kept}.values()))


def _may_import(unit: str) -> frozenset[str]:
    if unit == ROOT_PACKAGE:
        return frozenset(PACKAGE_NAMES)
    return MAY_IMPORT[unit] | {d for s, d in DOCUMENTED_DEPARTURES if s == unit}


def _sanctioned(target: str) -> bool:
    # An ancestor package of an allowed root is a PEP 562 lazy table, so reaching it costs nothing.
    return _under(target, PLUGIN_MODULE_SCOPE_ROOTS) or any(
        root.startswith(f"{target}.") for root in PLUGIN_MODULE_SCOPE_ROOTS
    )


def _module_scope_offenders(sites: Iterable[Site]) -> tuple[Site, ...]:
    return tuple(site for site in sites if site.scope and not _sanctioned(site.target))


def _report(sites: Iterable[Site], heading: str) -> str:
    lines = sorted({f"  {s.path}:{s.line}: {s.label} ({s.statement})" for s in sites})
    return "\n".join([f"{len(lines)} {heading}", *lines])


def _write_module(base: Path, relative: str, *lines: str) -> None:
    path = base / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")


IMPORT_DIRECTION: Final[str] = "import-direction violation(s) against AAP 0.4.8"
MUST_NEVER: Final[str] = "edge(s) the 'Must never import' column forbids"
REPORTING_BOUNDARY: Final[str] = "edge(s) across the reporting boundary: http -> reporting"
PLUGIN_IMPORT: Final[str] = "module(s) importing foundry.plugin, which activates by entry point"
MODULE_SCOPE_HEADING: Final[str] = "module-scope import(s) breach the 150 ms startup budget:"
EDGE_RULES: Final[Mapping[str, Callable[[Site], bool]]] = MappingProxyType(
    {
        IMPORT_DIRECTION: lambda e: e.imported not in _may_import(e.unit),
        MUST_NEVER: lambda e: e.imported in NEVER.get(e.unit, ()),
        REPORTING_BOUNDARY: lambda e: (e.unit, e.imported) == ("http", "reporting"),
        PLUGIN_IMPORT: lambda e: e.imported == "plugin",
    }
)


@pytest.mark.parametrize("heading", list(EDGE_RULES))
def test_no_edge_breaks_a_rule_of_the_plan(repo_root: Path, heading: str) -> None:
    edges = _edges(_scan_tree(repo_root))
    offenders = tuple(edge for edge in edges if EDGE_RULES[heading](edge))
    assert offenders == (), _report(offenders, heading)


def test_the_scan_reaches_every_layer_and_stays_out_of_itself(repo_root: Path) -> None:
    modules = tuple(relative.as_posix() for relative in _library_modules(repo_root))
    assert len(modules) >= MINIMUM_MODULE_COUNT, f"the scan reached only {len(modules)} modules"
    missing = sorted(set(KNOWN_MODULE_FILES) - set(modules))
    assert missing == [], f"the layering scan never reached these modules: {missing}"
    units = {_unit_label(Path(module)) for module in modules}
    assert units == set(PACKAGE_NAMES) | {ROOT_PACKAGE}, sorted(units)
    assert "tests/unit/test_layering.py" not in modules, "this module sits outside its own scan"
    rel = {s.target for s in _scan_tree(repo_root) if s.path.endswith("cassette/__init__.py")}
    assert "foundry.cassette.matcher.RequestFingerprint" in rel, "a relative import is dropped"


def test_the_matrix_is_the_plans_and_the_tree_adds_only_its_one_departure(repo_root: Path) -> None:
    assert {("contract", "obs")} == DOCUMENTED_DEPARTURES
    assert [unit for unit in PACKAGE_NAMES if _may_import(unit) & NEVER[unit]] == []
    assert sorted(unit for unit in CAPABILITY_RUNG if "obs" not in MAY_IMPORT[unit]) == ["contract"]
    for unit in CAPABILITY_RUNG:
        assert "obs" in _may_import(unit), f"{unit} omits obs, which the rung grants it"
    edges = _edges(_scan_tree(repo_root))
    above = [(e.unit, e.imported) for e in edges if e.unit in CAPABILITY_RUNG]
    upward = sorted({pair for pair in above if pair[1] not in CAPABILITY_RUNG | {"core", "obs"}})
    assert upward == [("fixtures", "config")], f"the capability rung crosses at {upward}"
    raised = [edge for edge in edges if (edge.unit, edge.imported) == ("fixtures", "config")]
    assert raised != [], "the fixtures -> config edge is missing: the rule would pass on nothing"
    prefix = f"{SOURCE_ROOT.as_posix()}/fixtures/"
    strays = tuple(edge for edge in raised if not edge.path.startswith(prefix))
    assert strays == (), _report(strays, f"fixtures -> config edge(s) from {prefix}")


def test_the_plugin_is_reached_by_the_entry_point_alone(repo_root: Path, tmp_path: Path) -> None:
    sites = _sites_of(repo_root, PLUGIN_RELATIVE)
    assert sites, f"the startup-budget scan found no import in {PLUGIN_RELATIVE.as_posix()}"
    offenders = _module_scope_offenders(sites)
    assert offenders == (), _report(offenders, MODULE_SCOPE_HEADING)
    _write_module(
        tmp_path,
        "src/foundry/plugin.py",
        "from __future__ import annotations\nimport typing\nfrom foundry.core import exceptions\n"
        "from foundry.execution import state\nimport foundry.config\n"
        "def hook() -> None:\n    import foundry.reporting",
    )
    injected = _module_scope_offenders(_sites_of(tmp_path, PLUGIN_RELATIVE))
    assert [site.line for site in injected] == [5], _report(injected, MODULE_SCOPE_HEADING)


def test_the_scan_reports_an_injected_violation(tmp_path: Path) -> None:
    # Two reversed edges at once: the scanner must report both, not only the first.
    _write_module(tmp_path, "src/foundry/http/session.py", "import foundry.reporting")
    _write_module(tmp_path, "src/foundry/core/ids.py", "from foundry.data import cases")
    violations = [e for e in _edges(_scan_tree(tmp_path)) if EDGE_RULES[IMPORT_DIRECTION](e)]
    message = _report(violations, IMPORT_DIRECTION)
    assert f"2 {IMPORT_DIRECTION}" in message, message
    assert "src/foundry/http/session.py:1: foundry.http -> foundry.reporting" in message, message
    assert "src/foundry/core/ids.py:1: foundry.core -> foundry.data" in message, message
