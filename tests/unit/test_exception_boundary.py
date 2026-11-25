"""The tree-wide proof that only ``core/exceptions.py`` defines Foundry's exception types."""

from __future__ import annotations

import ast
import builtins
import functools
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Final, NamedTuple

import pytest

_EXCEPTION_DEFINITION_SITE: Final[str] = "src/foundry/core/exceptions.py"
_CONTRACTED_EXCEPTION_NAMES: Final[tuple[str, ...]] = (
    "ArtifactError",
    "CassetteMissError",
    "ConfigError",
    "ContractError",
    "DataError",
    "FoundryError",
    "RetryExhaustedError",
    "TransportError",
)
_ANCHOR_MODULES: Final[tuple[str, ...]] = (
    _EXCEPTION_DEFINITION_SITE,
    "src/foundry/cli/main.py",
    "src/foundry/config/merge.py",
    "src/foundry/http/session.py",
)
_MINIMUM_SCANNED_MODULES: Final[int] = 16
_BUILTIN_EXCEPTION_NAMES: Final[frozenset[str]] = frozenset(
    n
    for n in dir(builtins)
    if isinstance(named := getattr(builtins, n), type) and issubclass(named, BaseException)
)
_BARE_HANDLER: Final[str] = "bare except:"
_MASKING_BASE_HANDLER: Final[str] = "except BaseException: without a re-raise"
_PASS_ONLY_HANDLER: Final[str] = "except Exception: pass"
# A private helper or a protocol method may raise these; any other builtin stays reportable.
_INTERNAL_SITE_BUILTINS: Final[frozenset[str]] = frozenset(
    {"AssertionError", "AttributeError", "TypeError", "ValueError"}
)
# AAP 0.4.3 fixes these three helpers' contract as raising AssertionError: the one exemption.
_ASSERTION_ALLOWLIST: Final[dict[str, frozenset[str]]] = {
    f"src/foundry/assertions/{helper}.py": frozenset({"AssertionError"})
    for helper in ("eventually", "json_subset", "status")
}


class Site(NamedTuple):
    path: str
    line: int
    name: str
    function: str = ""


_Finder = Callable[[Path, tuple[Path, ...]], tuple[Site, ...]]


def scanned_modules(root: Path) -> tuple[Path, ...]:
    package = root / "src/foundry"
    caches = {"__pycache__", ".git", ".mypy_cache", ".pytest_cache", ".ruff_cache"}
    kept = (p for p in package.rglob("*.py") if not caches.intersection(p.parts))
    return tuple(sorted(kept))


@functools.cache
def _read_tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _parsed_modules(root: Path, modules: tuple[Path, ...]) -> Iterator[tuple[str, ast.Module]]:
    for path in modules:
        yield path.relative_to(root).as_posix(), _read_tree(path)


def _trailing_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Subscript):
        return _trailing_name(node.value)
    return node.attr if isinstance(node, ast.Attribute) else ""


def _handler_label(node: ast.ExceptHandler) -> str:
    """The label of a handler that can swallow an exception, or "" for one that cannot."""
    if node.type is None:
        return _BARE_HANDLER
    elements = node.type.elts if isinstance(node.type, ast.Tuple) else [node.type]
    names = frozenset(name for name in map(_trailing_name, elements) if name)
    last = node.body[-1] if node.body else None
    if "BaseException" in names and not (isinstance(last, ast.Raise) and last.exc is None):
        return _MASKING_BASE_HANDLER
    passes = len(node.body) == 1 and isinstance(last, ast.Pass)
    return _PASS_ONLY_HANDLER if names == frozenset({"Exception"}) and passes else ""


def _exception_base_names(tree: ast.Module) -> frozenset[str]:
    aliases = {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
        if alias.name in _CONTRACTED_EXCEPTION_NAMES
    }
    return frozenset(set(_BUILTIN_EXCEPTION_NAMES) | set(_CONTRACTED_EXCEPTION_NAMES) | aliases)


def _raise_sites(relative: str, nodes: Iterable[ast.AST], function: str = "") -> Iterator[Site]:
    """Builtin raises under ``nodes``, each attributed to the innermost function holding it."""
    for node in nodes:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            yield from _raise_sites(relative, node.body, node.name)
        elif isinstance(node, ast.Raise) and node.exc is not None:
            raised = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
            if (name := _trailing_name(raised)) in _BUILTIN_EXCEPTION_NAMES:
                yield Site(relative, node.lineno, name, function)
        else:
            yield from _raise_sites(relative, ast.iter_child_nodes(node), function)


def find_exception_definitions(root: Path, modules: tuple[Path, ...]) -> tuple[Site, ...]:
    found = [
        Site(relative, node.lineno, node.name)
        for relative, tree in _parsed_modules(root, modules)
        if (bases := _exception_base_names(tree))
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and set(map(_trailing_name, node.bases)) & bases
    ]
    return tuple(sorted(found))


def find_handler_violations(root: Path, modules: tuple[Path, ...]) -> tuple[Site, ...]:
    found = [
        Site(relative, node.lineno, label)
        for relative, tree in _parsed_modules(root, modules)
        for node in ast.walk(tree)
        if isinstance(node, ast.ExceptHandler) and (label := _handler_label(node))
    ]
    return tuple(sorted(found))


def find_raise_sites(root: Path, modules: tuple[Path, ...]) -> tuple[Site, ...]:
    """Every builtin raise outside the definition site, unfiltered so the filters stay provable."""
    sites: list[Site] = []
    for relative, tree in _parsed_modules(root, modules):
        if relative != _EXCEPTION_DEFINITION_SITE:
            sites.extend(_raise_sites(relative, [tree]))
    return tuple(sorted(sites))


def find_raise_violations(root: Path, modules: tuple[Path, ...]) -> tuple[Site, ...]:
    public = (s for s in find_raise_sites(root, modules) if not s.function.startswith("_"))
    return tuple(s for s in public if s.name not in _ASSERTION_ALLOWLIST.get(s.path, frozenset()))


def find_internal_raise_sites(root: Path, modules: tuple[Path, ...]) -> tuple[Site, ...]:
    return tuple(s for s in find_raise_sites(root, modules) if s.function.startswith("_"))


def _foreign_definitions(root: Path, modules: tuple[Path, ...]) -> tuple[Site, ...]:
    found = find_exception_definitions(root, modules)
    return tuple(site for site in found if site.path != _EXCEPTION_DEFINITION_SITE)


def _unbounded_internal(sites: tuple[Site, ...]) -> tuple[Site, ...]:
    return tuple(site for site in sites if site.name not in _INTERNAL_SITE_BUILTINS)


def _misnamed_attribute_raises(sites: tuple[Site, ...]) -> tuple[Site, ...]:
    named = (s for s in sites if s.name == "AttributeError")
    return tuple(s for s in named if s.function not in {"__getattr__", "__getattribute__"})


def _format_sites(headline: str, sites: tuple[Site, ...]) -> str:
    lines = [
        f"  {s.path}:{s.line}: {s.name}" + (f" in {s.function}" if s.function else "")
        for s in sites
    ]
    return "\n".join([f"{len(sites)} {headline}:", *lines])


def _write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_the_scan_reaches_every_module(repo_root: Path) -> None:
    modules = scanned_modules(repo_root)
    seen = {path.relative_to(repo_root).as_posix() for path in modules}
    assert set(_ANCHOR_MODULES) <= seen, f"anchor modules missing from {sorted(seen)}"
    assert len(seen) >= _MINIMUM_SCANNED_MODULES, f"the walk found only {len(seen)} modules"
    assert not any("__pycache__" in name for name in seen), "a tool cache is inside the walk"
    found = find_exception_definitions(repo_root, modules)
    defined = {site.name for site in found if site.path == _EXCEPTION_DEFINITION_SITE}
    missing = sorted(set(_CONTRACTED_EXCEPTION_NAMES) - defined)
    assert missing == [], f"{_EXCEPTION_DEFINITION_SITE} misses {missing}: {sorted(defined)}"


@pytest.mark.parametrize(
    ("finder", "headline"),
    [
        (_foreign_definitions, "exception type(s) defined outside the definition site"),
        (find_handler_violations, "handler(s) that can swallow an exception"),
        (find_raise_violations, "builtin exception(s) raised across the module boundary"),
    ],
)
def test_the_tree_breaks_no_boundary_rule(repo_root: Path, finder: _Finder, headline: str) -> None:
    offenders = finder(repo_root, scanned_modules(repo_root))
    assert offenders == (), _format_sites(headline, offenders)


def test_the_protocol_raise_filters_and_the_allowlist_are_both_load_bearing(
    repo_root: Path,
) -> None:
    modules = scanned_modules(repo_root)
    internal = find_internal_raise_sites(repo_root, modules)
    unexpected = _unbounded_internal(internal)
    headline = "builtin exception(s) raised from an internal site outside the documented types"
    assert unexpected == (), _format_sites(headline, unexpected)
    misnamed = _misnamed_attribute_raises(internal)
    assert misnamed == (), _format_sites("AttributeError outside the attribute protocol", misnamed)
    # Both filters are load-bearing: each names the site its rule catches, and leaves the rest.
    helper = Site("src/foundry/data/raw.py", 1, "AttributeError", "_helper")
    protocol = Site("src/foundry/data/raw.py", 2, "AttributeError", "__getattr__")
    keyed = Site("src/foundry/data/raw.py", 3, "KeyError", "_reject")
    assert _misnamed_attribute_raises((helper, protocol)) == (helper,)
    assert _unbounded_internal((helper, keyed)) == (keyed,)
    keys = {f"src/foundry/assertions/{n}.py" for n in ("eventually", "json_subset", "status")}
    assert set(_ASSERTION_ALLOWLIST) == keys
    assert set(_ASSERTION_ALLOWLIST.values()) == {frozenset({"AssertionError"})}
    assert "src/foundry/assertions/json_schema.py" not in _ASSERTION_ALLOWLIST
    # Remove an entry and the helper is reported: the allowlist, not the tree, absorbs the raise.
    raw = find_raise_sites(repo_root, modules)
    allowlisted = {site.path for site in raw if site.path in _ASSERTION_ALLOWLIST}
    assert allowlisted == keys, f"not load-bearing: {sorted(allowlisted)}"
    violations = find_raise_violations(repo_root, modules)
    stray = tuple(site for site in violations if site.path in _ASSERTION_ALLOWLIST)
    assert stray == (), _format_sites("allowlisted helper(s) reported as violations", stray)


def test_the_positive_control_reports_each_offender_on_its_own(tmp_path: Path) -> None:
    _write(tmp_path, _EXCEPTION_DEFINITION_SITE, "class FoundryError(Exception): pass\n")
    # A subclass outside the definition site, a public builtin raise, and the handler shapes.
    _write(
        tmp_path,
        "src/foundry/engine.py",
        "class MyError(FoundryError): pass\ndef public_raiser() -> None:\n"
        '    raise ValueError("boom")\ndef bare() -> None:\n    try:\n        call()\n'
        "    except:\n        pass\ndef silent() -> None:\n    try:\n        call()\n"
        "    except Exception:\n        pass\ndef masking() -> None:\n    try:\n        call()\n"
        "    except BaseException:\n        pass",
    )
    _write(
        tmp_path,
        "src/foundry/clean.py",
        "def _private_raiser(fault: str) -> None:\n    raise ValueError(fault)\n"
        'def releasing() -> None:\n    """Mentions raise KeyError("boom") in prose only."""\n'
        "    try:\n        call()\n    except BaseException:\n        let_go()\n        raise\n",
    )
    modules = scanned_modules(tmp_path)
    assert len(modules) == 3, sorted(path.name for path in modules)
    assert find_exception_definitions(tmp_path, modules) == (
        Site(_EXCEPTION_DEFINITION_SITE, 1, "FoundryError"),
        Site("src/foundry/engine.py", 1, "MyError"),
    )
    assert find_handler_violations(tmp_path, modules) == (
        Site("src/foundry/engine.py", 7, _BARE_HANDLER),
        Site("src/foundry/engine.py", 12, _PASS_ONLY_HANDLER),
        Site("src/foundry/engine.py", 17, _MASKING_BASE_HANDLER),
    )
    assert find_raise_sites(tmp_path, modules) == (
        Site("src/foundry/clean.py", 2, "ValueError", "_private_raiser"),
        Site("src/foundry/engine.py", 3, "ValueError", "public_raiser"),
    )
    assert find_raise_violations(tmp_path, modules) == (
        Site("src/foundry/engine.py", 3, "ValueError", "public_raiser"),
    )
