"""The tree-wide proof that only ``core/exceptions.py`` defines Foundry's exceptions, and that a
caller only ever meets the types AAP 0.4.3 fixes.

AAP 0.14.3 names this module as the mechanism for the boundary rule AAP 0.13.1 states: only the
types of AAP 0.4.3 cross a module boundary, and error handling never swallows. Three structural
checks run over every module of ``src/foundry``, and each one is also driven on its own against a
synthetic tree in ``tmp_path``, so a check that stopped finding anything fails loudly instead of
leaving a green suite.

1. **One definition site.** Outside ``src/foundry/core/exceptions.py`` no module defines a class
   deriving from ``Exception``, ``BaseException``, a builtin exception or one of the eight
   contracted types -- a name imported from the definition site under an alias included.
2. **No swallowing handler.** A bare ``except:`` and an ``except Exception: pass`` are AAP 0.13.1's
   own prohibitions. An ``except BaseException:`` handler is reported too unless its last statement
   is a bare ``raise``: releasing a resource and re-raising propagates the exception unchanged,
   which is the idiom ``foundry.db.session`` and ``foundry.fixtures.db`` rely on to release a
   session on the interrupt path (AAP 0.4.8, "Fixture lifecycle rule"), and it masks nothing.
3. **No builtin crosses the boundary.** Outside the definition site a ``raise`` of a builtin is a
   finding when it sits in a public callable -- at module level, or in a function or method whose
   name does not start with ``_`` -- because that is the only place the exception can reach a
   caller. Two exemptions are documented below: the three assertion helpers AAP 0.4.3 fixes as
   raising ``AssertionError``, and the internal or protocol sites, where Python itself requires the
   builtin (``__getattr__`` must raise ``AttributeError`` for ``hasattr`` to answer ``False``) or a
   framework converts it (a pydantic validator raises ``ValueError``, which
   ``foundry.config.validation`` turns into ``ConfigError`` before it leaves the module).

Detection is structural rather than textual: every module is parsed with :mod:`ast`, so a docstring
quoting ``raise ValueError("boom")`` is not a finding while the statement is one. Two routes stay
out of reach -- a base class name rebound to an exception after its first binding, and a builtin
raised by a private helper that its public caller does not wrap -- which is why check 3 also bounds
the types an internal site may raise and the functions ``AttributeError`` may come from.
"""

from __future__ import annotations

import ast
import builtins
import functools
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pytest

_SOURCE_PACKAGE: Final[str] = "src/foundry"
"""The package every check scans, relative to the repository root."""

_EXCEPTION_DEFINITION_SITE: Final[str] = "src/foundry/core/exceptions.py"
"""The single module allowed to define an exception type (AAP 0.4.3, AAP 0.14.3)."""

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
"""The eight types AAP 0.4.3 fixes, alphabetical; all eight must be defined at the site."""

_ANCHOR_MODULES: Final[tuple[str, ...]] = (
    _EXCEPTION_DEFINITION_SITE,
    "src/foundry/cli/main.py",
    "src/foundry/config/merge.py",
    "src/foundry/http/session.py",
)
"""Modules the walk must reach, so a truncated or empty scan cannot pass the suite."""

_MINIMUM_SCANNED_MODULES: Final[int] = 16
"""The floor the walk must clear before its findings mean anything."""

_CACHE_DIRECTORY_NAMES: Final[frozenset[str]] = frozenset(
    {"__pycache__", ".git", ".mypy_cache", ".pytest_cache", ".ruff_cache"}
)
"""Directories holding no first-party source, so the walk does not descend into them."""

_BUILTIN_EXCEPTION_NAMES: Final[frozenset[str]] = frozenset(
    name
    for name in dir(builtins)
    if isinstance(getattr(builtins, name), type)
    and issubclass(getattr(builtins, name), BaseException)
)
"""Every builtin exception class, read from the interpreter that runs the suite."""

_BARE_HANDLER: Final[str] = "bare except:"
"""Report name of a handler with no type at all, which AAP 0.13.1 forbids outright."""

_MASKING_BASE_HANDLER: Final[str] = "except BaseException: without a re-raise"
"""Report name of a handler that can absorb ``KeyboardInterrupt`` or ``SystemExit``."""

_PASS_ONLY_HANDLER: Final[str] = "except Exception: pass"
"""Report name of the silent handler AAP 0.13.1 names explicitly."""

_INTERNAL_SITE_BUILTINS: Final[frozenset[str]] = frozenset(
    {"AssertionError", "AttributeError", "TypeError", "ValueError"}
)
"""The builtin types a private helper or protocol method may raise.

These four are the ones Python and the framework hooks of this project require at an internal site:
``AttributeError`` for the attribute protocol, and ``ValueError``/``AssertionError``/``TypeError``
for a pydantic validator, whose failure the configuration layer translates into ``ConfigError``. A
``RuntimeError`` or a ``KeyError`` from a private helper is reportable, which is what keeps the
exemption from hiding a builtin that a public caller would let escape.
"""

_ATTRIBUTE_PROTOCOL_FUNCTIONS: Final[frozenset[str]] = frozenset(
    {"__getattr__", "__getattribute__"}
)
"""The only functions ``AttributeError`` may come from: the data model requires it there."""

_ASSERTION_ALLOWLIST: Final[dict[str, frozenset[str]]] = {
    # AAP 0.4.3 fixes the contract of exactly these three helpers as raising ``AssertionError``, a
    # builtin: ``assert_status`` raises it "naming status, expected set and redacted body excerpt",
    # ``assert_json_subset`` "reports the first mismatching JSON path", and ``eventually`` "raises
    # ``AssertionError`` naming the last value or exception and the number of polls". The
    # assigned-folder brief therefore allowlists these three modules for this one type, and the
    # shape assertion below (`test_the_assertion_allowlist_holds_exactly_the_three_helpers`) makes
    # any widening a visible diff. ``json_schema.py`` is absent on purpose: it raises
    # ``ContractError``.
    "src/foundry/assertions/eventually.py": frozenset({"AssertionError"}),
    "src/foundry/assertions/json_subset.py": frozenset({"AssertionError"}),
    "src/foundry/assertions/status.py": frozenset({"AssertionError"}),
}
"""The one documented exemption from check 3, keyed by repository-relative POSIX path."""


@dataclass(frozen=True, order=True)
class Site:
    """One reported location: a repository-relative POSIX path, a line, a name and its function.

    ``function`` is the innermost enclosing function or method name, empty at module level, so a
    report says where in the file the finding sits. Frozen and ordered, so every report is sorted
    and two runs read identically.
    """

    path: str
    line: int
    name: str
    function: str = ""


def _is_in_cache_directory(path: Path, root: Path) -> bool:
    """Return True when a directory between ``root`` and ``path`` holds no first-party source."""
    return any(part in _CACHE_DIRECTORY_NAMES for part in path.relative_to(root).parts[:-1])


def scanned_modules(root: Path) -> tuple[Path, ...]:
    """Return every module of ``src/foundry`` under ``root``, sorted, with tool caches skipped."""
    package = root / _SOURCE_PACKAGE
    return tuple(sorted(p for p in package.rglob("*.py") if not _is_in_cache_directory(p, root)))


@functools.cache
def _read_tree(path: Path) -> ast.Module:
    """Return one module's syntax tree, parsed once per path for the whole session.

    Every check walks the same 100-odd modules, so parsing them once keeps the module inside the
    suite's wall-clock budget. The trees are read-only: nothing in this suite writes under
    ``src/foundry``, and a synthetic tree lives in its own ``tmp_path``.
    """
    return ast.parse(path.read_text(encoding="utf-8"))


def _parsed_modules(root: Path, modules: tuple[Path, ...]) -> Iterator[tuple[str, ast.Module]]:
    """Yield each module's repository-relative POSIX path and its parsed syntax tree."""
    for path in modules:
        yield path.relative_to(root).as_posix(), _read_tree(path)


def _trailing_name(node: ast.expr) -> str:
    """Return the name a base class or a raised exception is written as.

    ``ValueError``, ``builtins.ValueError`` and ``exceptions.FoundryError`` all reduce to the
    attribute Python resolves them to, and a generic alias reduces to its origin; a computed
    expression reduces to the empty string, which no name set contains.
    """
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript):
        return _trailing_name(node.value)
    return ""


def _handler_type_names(handler_type: ast.expr) -> frozenset[str]:
    """Return the exception names one ``except`` clause names, a tuple clause included."""
    if isinstance(handler_type, ast.Tuple):
        return frozenset(
            name for name in (_trailing_name(element) for element in handler_type.elts) if name
        )
    name = _trailing_name(handler_type)
    return frozenset({name}) if name else frozenset()


def _ends_in_reraise(body: list[ast.stmt]) -> bool:
    """Return True when a handler's last statement is a bare ``raise``, so it propagates.

    A handler that releases a resource and then writes ``raise`` masks nothing: the exception it
    caught leaves the handler unchanged. Only ``try``/``except`` can express "release on the failing
    path and keep going on the passing one", which is why this shape is the one ``BaseException``
    handler the check accepts (AAP 0.4.8, "Fixture lifecycle rule").
    """
    return bool(body) and isinstance(body[-1], ast.Raise) and body[-1].exc is None


def _is_pass_only(body: list[ast.stmt]) -> bool:
    """Return True when a handler's body is exactly ``pass``: the swallowing AAP 0.13.1 forbids."""
    return len(body) == 1 and isinstance(body[0], ast.Pass)


def _innermost_function_names(tree: ast.Module) -> dict[int, str]:
    """Map each node's id to the innermost function enclosing it, ``""`` at module level."""
    names: dict[int, str] = {}

    def visit(node: ast.AST, enclosing: str) -> None:
        for child in ast.iter_child_nodes(node):
            inner = (
                child.name
                if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef)
                else enclosing
            )
            names[id(child)] = inner
            visit(child, inner)

    visit(tree, "")
    return names


def _exception_base_names(tree: ast.Module) -> frozenset[str]:
    """Return every name in ``tree`` that can denote an exception type in a base-class list.

    The builtin exception names and the eight contracted names are candidates everywhere, and a
    ``from foundry.core.exceptions import ConfigError as Fault`` binds ``Fault`` as well, so a
    re-exported alias cannot slip a subclass past the check.
    """
    names = set(_BUILTIN_EXCEPTION_NAMES) | set(_CONTRACTED_EXCEPTION_NAMES)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in _CONTRACTED_EXCEPTION_NAMES:
                    names.add(alias.asname or alias.name)
    return frozenset(names)


def _raised_type_name(exception: ast.expr) -> str:
    """Return the exception name a ``raise`` statement raises, ``X`` and ``X(...)`` alike."""
    if isinstance(exception, ast.Call):
        return _trailing_name(exception.func)
    return _trailing_name(exception)


def _is_public(site: Site) -> bool:
    """Return True when the site sits where an exception can leave the module for a caller."""
    return not site.function.startswith("_")


def find_exception_definitions(root: Path, modules: tuple[Path, ...]) -> tuple[Site, ...]:
    """Return every class in ``modules`` deriving from an exception type (check 1, unfiltered)."""
    definitions: list[Site] = []
    for relative, tree in _parsed_modules(root, modules):
        exception_names = _exception_base_names(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            if any(_trailing_name(base) in exception_names for base in node.bases):
                definitions.append(Site(relative, node.lineno, node.name))
    return tuple(sorted(definitions))


def find_handler_violations(root: Path, modules: tuple[Path, ...]) -> tuple[Site, ...]:
    """Return every ``except`` clause that can swallow an exception (check 2)."""
    violations: list[Site] = []
    for relative, tree in _parsed_modules(root, modules):
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            if node.type is None:
                violations.append(Site(relative, node.lineno, _BARE_HANDLER))
                continue
            names = _handler_type_names(node.type)
            if "BaseException" in names and not _ends_in_reraise(node.body):
                violations.append(Site(relative, node.lineno, _MASKING_BASE_HANDLER))
            elif names == frozenset({"Exception"}) and _is_pass_only(node.body):
                violations.append(Site(relative, node.lineno, _PASS_ONLY_HANDLER))
    return tuple(sorted(violations))


def find_raise_sites(root: Path, modules: tuple[Path, ...]) -> tuple[Site, ...]:
    """Return every ``raise`` of a builtin exception outside the definition site (check 3, raw).

    Unfiltered by design: the allowlist and the internal-site bounds below are applied by
    :func:`find_raise_violations` and :func:`find_internal_raise_sites`, and comparing the raw set
    with the filtered one is what proves those rules are load-bearing rather than decorative.
    """
    sites: list[Site] = []
    for relative, tree in _parsed_modules(root, modules):
        if relative == _EXCEPTION_DEFINITION_SITE:
            continue
        enclosing = _innermost_function_names(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Raise) or node.exc is None:
                continue
            name = _raised_type_name(node.exc)
            if name in _BUILTIN_EXCEPTION_NAMES:
                sites.append(Site(relative, node.lineno, name, enclosing.get(id(node), "")))
    return tuple(sorted(sites))


def find_raise_violations(root: Path, modules: tuple[Path, ...]) -> tuple[Site, ...]:
    """Return the builtin raises that can reach a caller unpermitted (check 3, violations)."""
    violations: list[Site] = []
    for site in find_raise_sites(root, modules):
        if not _is_public(site):
            continue
        if site.name in _ASSERTION_ALLOWLIST.get(site.path, frozenset()):
            continue
        violations.append(site)
    return tuple(sorted(violations))


def find_internal_raise_sites(root: Path, modules: tuple[Path, ...]) -> tuple[Site, ...]:
    """Return the builtin raises inside a private helper or a protocol method (check 3, exempt)."""
    return tuple(sorted(site for site in find_raise_sites(root, modules) if not _is_public(site)))


def _format_sites(headline: str, sites: tuple[Site, ...]) -> str:
    """Render a report as its count and headline, then one ``path:line`` line per site."""
    lines: list[str] = []
    for site in sites:
        where = f"  {site.path}:{site.line}: {site.name}"
        lines.append(f"{where} in {site.function}" if site.function else where)
    return "\n".join([f"{len(sites)} {headline}:", *lines])


def _write(root: Path, relative: str, *lines: str) -> Path:
    """Write one synthetic module at ``relative`` under ``root`` and return its path."""
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.fixture(scope="module")
def production_modules(repo_root: Path) -> tuple[Path, ...]:
    """Module scope: the modules of ``src/foundry``, walked once for every check in this module."""
    return scanned_modules(repo_root)


def test_the_scan_reaches_the_promised_modules(
    repo_root: Path, production_modules: tuple[Path, ...]
) -> None:
    """The walk covers the package and reaches the four anchors, so no check runs on nothing."""
    scanned = {path.relative_to(repo_root).as_posix() for path in production_modules}
    missing = [anchor for anchor in _ANCHOR_MODULES if anchor not in scanned]
    assert missing == [], f"anchor modules outside the scan: {missing} of {sorted(scanned)}"
    assert len(scanned) >= _MINIMUM_SCANNED_MODULES, f"the walk found only {len(scanned)} modules"
    assert not any("__pycache__" in name for name in scanned), "a tool cache is inside the walk"


def test_every_contracted_exception_type_is_defined_at_the_definition_site(
    repo_root: Path, production_modules: tuple[Path, ...]
) -> None:
    """The scan finds all eight types where AAP 0.4.3 puts them, so check 1 reads a real site."""
    definitions = find_exception_definitions(repo_root, production_modules)
    defined = {site.name for site in definitions if site.path == _EXCEPTION_DEFINITION_SITE}
    missing = sorted(set(_CONTRACTED_EXCEPTION_NAMES) - defined)
    assert missing == [], (
        f"{_EXCEPTION_DEFINITION_SITE} does not define {missing}; the scan found {sorted(defined)}"
    )


def test_no_exception_type_is_defined_outside_the_definition_site(
    repo_root: Path, production_modules: tuple[Path, ...]
) -> None:
    """A subclass defined anywhere else is a finding (AAP 0.4.3, AAP 0.14.3)."""
    foreign = tuple(
        site
        for site in find_exception_definitions(repo_root, production_modules)
        if site.path != _EXCEPTION_DEFINITION_SITE
    )
    assert foreign == (), _format_sites(
        "exception type(s) defined outside the definition site", foreign
    )


def test_no_handler_swallows_an_exception(
    repo_root: Path, production_modules: tuple[Path, ...]
) -> None:
    """No bare ``except:``, no ``except Exception: pass``, no masking ``BaseException`` handler."""
    violations = find_handler_violations(repo_root, production_modules)
    assert violations == (), _format_sites("handler(s) that can swallow an exception", violations)


def test_no_builtin_exception_crosses_a_module_boundary(
    repo_root: Path, production_modules: tuple[Path, ...]
) -> None:
    """A public callable raises a contracted type, or the site is documented and exempt."""
    violations = find_raise_violations(repo_root, production_modules)
    assert violations == (), _format_sites(
        "builtin exception(s) raised across the module boundary", violations
    )


def test_internal_raise_sites_stay_within_the_documented_protocol_types(
    repo_root: Path, production_modules: tuple[Path, ...]
) -> None:
    """The internal-site exemption is bounded by type, and ``AttributeError`` by function."""
    internal = find_internal_raise_sites(repo_root, production_modules)
    unexpected = tuple(site for site in internal if site.name not in _INTERNAL_SITE_BUILTINS)
    assert unexpected == (), _format_sites(
        "builtin exception(s) raised from an internal site outside the documented types", unexpected
    )
    misnamed = tuple(
        site
        for site in internal
        if site.name == "AttributeError" and site.function not in _ATTRIBUTE_PROTOCOL_FUNCTIONS
    )
    assert misnamed == (), _format_sites(
        "AttributeError raised outside the attribute protocol", misnamed
    )


def test_the_assertion_allowlist_holds_exactly_the_three_helpers() -> None:
    """The one exemption is three modules wide and one type deep, so widening shows as a diff."""
    assert sorted(_ASSERTION_ALLOWLIST) == [
        "src/foundry/assertions/eventually.py",
        "src/foundry/assertions/json_subset.py",
        "src/foundry/assertions/status.py",
    ]
    for path, permitted in _ASSERTION_ALLOWLIST.items():
        assert permitted == frozenset({"AssertionError"}), f"{path} permits {sorted(permitted)}"
    assert "src/foundry/assertions/json_schema.py" not in _ASSERTION_ALLOWLIST


def test_the_allowlist_is_what_keeps_the_assertion_helpers_clean(
    repo_root: Path, production_modules: tuple[Path, ...]
) -> None:
    """The three helpers do raise ``AssertionError``: the allowlist, not the tree, absorbs it."""
    raw = find_raise_sites(repo_root, production_modules)
    allowlisted = {site.path for site in raw if site.path in _ASSERTION_ALLOWLIST}
    assert allowlisted == set(_ASSERTION_ALLOWLIST), (
        f"the allowlist is not load-bearing: raw builtin raises found in {sorted(allowlisted)}"
    )
    violations = find_raise_violations(repo_root, production_modules)
    assert not any(site.path in _ASSERTION_ALLOWLIST for site in violations), _format_sites(
        "allowlisted helper(s) reported as violations", violations
    )


def test_the_positive_control_reports_each_offender_on_its_own(tmp_path: Path) -> None:
    """A synthetic tree yields exactly its own offender per check, and none for the clean file."""
    _write(
        tmp_path,
        _EXCEPTION_DEFINITION_SITE,
        '"""The synthetic definition site."""',
        "",
        "class FoundryError(Exception):",
        '    """The base every contracted type derives from."""',
    )
    _write(
        tmp_path,
        "src/foundry/defines_its_own.py",
        '"""A module that defines an exception of its own."""',
        "",
        "class MyError(Exception):",
        '    """Reported by check 1."""',
    )
    _write(
        tmp_path,
        "src/foundry/swallows.py",
        '"""A module that swallows whatever it catches."""',
        "",
        "def swallow() -> None:",
        '    """Reported by check 2."""',
        "    try:",
        "        compact()",
        "    except:",
        "        pass",
    )
    _write(
        tmp_path,
        "src/foundry/raises_builtin.py",
        '"""A module whose public callable raises a builtin."""',
        "",
        "def public_raiser() -> None:",
        '    raise ValueError("boom")',
    )
    _write(
        tmp_path,
        "src/foundry/clean.py",
        '"""A clean module: it only mentions ``raise ValueError("boom")`` in prose."""',
        "",
        "def _private_raiser(fault: str) -> None:",
        '    """Raise a builtin its caller converts; a private helper crosses no boundary."""',
        "    raise ValueError(fault)",
        "",
        "class Proxy:",
        '    """A proxy whose ``__getattr__`` must raise ``AttributeError``."""',
        "",
        "    def __getattr__(self, name: str) -> object:",
        "        raise AttributeError(name)",
        "",
        "def releasing() -> None:",
        '    """Release, then re-raise, which masks nothing."""',
        "    try:",
        "        compact()",
        "    except BaseException:",
        "        release()",
        "        raise",
    )
    modules = scanned_modules(tmp_path)
    assert len(modules) == 5, sorted(path.name for path in modules)

    definitions = find_exception_definitions(tmp_path, modules)
    assert definitions == (
        Site(_EXCEPTION_DEFINITION_SITE, 3, "FoundryError"),
        Site("src/foundry/defines_its_own.py", 3, "MyError"),
    )
    assert find_handler_violations(tmp_path, modules) == (
        Site("src/foundry/swallows.py", 7, _BARE_HANDLER),
    )
    assert find_raise_violations(tmp_path, modules) == (
        Site("src/foundry/raises_builtin.py", 4, "ValueError", "public_raiser"),
    )


def test_the_handler_check_separates_a_masking_handler_from_a_releasing_one(
    tmp_path: Path,
) -> None:
    """The three handler shapes are told apart: bare, silent, and the allowlisted re-raise."""
    _write(
        tmp_path,
        "src/foundry/handlers.py",
        '"""Three handlers: two faults and one idiom."""',
        "",
        "def bare() -> None:",
        '    """No type at all."""',
        "    try:",
        "        compact()",
        "    except:",
        "        pass",
        "",
        "def silent() -> None:",
        '    """Swallows every ordinary failure."""',
        "    try:",
        "        compact()",
        "    except Exception:",
        "        pass",
        "",
        "def masking() -> None:",
        '    """Absorbs an interrupt, which is the shape the check refuses."""',
        "    try:",
        "        compact()",
        "    except BaseException:",
        "        pass",
        "",
        "def releasing() -> None:",
        '    """Releases, then re-raises: the interrupt still leaves the handler."""',
        "    try:",
        "        compact()",
        "    except BaseException:",
        "        release()",
        "        raise",
    )
    violations = find_handler_violations(tmp_path, scanned_modules(tmp_path))
    assert violations == (
        Site("src/foundry/handlers.py", 7, _BARE_HANDLER),
        Site("src/foundry/handlers.py", 14, _PASS_ONLY_HANDLER),
        Site("src/foundry/handlers.py", 21, _MASKING_BASE_HANDLER),
    )


def test_the_raise_check_separates_the_allowlist_from_an_unpermitted_helper(
    tmp_path: Path,
) -> None:
    """Only the allowlisted helper and the internal sites escape check 3, and the bounds report."""
    _write(
        tmp_path,
        "src/foundry/assertions/status.py",
        '"""An allowlisted helper."""',
        "",
        "def assert_status() -> None:",
        '    """AAP 0.4.3 fixes AssertionError as this helper\'s contract."""',
        '    raise AssertionError("the documented contract")',
    )
    _write(
        tmp_path,
        "src/foundry/data/schema.py",
        '"""A module whose public callable must not raise a builtin."""',
        "",
        "def enforce_row() -> None:",
        '    raise AssertionError("a public raise outside the allowlist")',
    )
    _write(
        tmp_path,
        "src/foundry/fixtures/factory.py",
        '"""A proxy whose __getattr__ must raise AttributeError."""',
        "",
        "class FoundryFactory:",
        '    """The factory fixture."""',
        "",
        "    def __getattr__(self, name: str) -> object:",
        "        raise AttributeError(name)",
    )
    _write(
        tmp_path,
        "src/foundry/data/escapes.py",
        '"""Two internal raises the bounds below catch."""',
        "",
        "def _reject(column: str) -> None:",
        "    raise KeyError(column)",
        "",
        "def _raise_missing(name: str) -> None:",
        "    raise AttributeError(name)",
    )
    modules = scanned_modules(tmp_path)

    assert find_raise_violations(tmp_path, modules) == (
        Site("src/foundry/data/schema.py", 4, "AssertionError", "enforce_row"),
    )
    internal = find_internal_raise_sites(tmp_path, modules)
    assert internal == (
        Site("src/foundry/data/escapes.py", 4, "KeyError", "_reject"),
        Site("src/foundry/data/escapes.py", 7, "AttributeError", "_raise_missing"),
        Site("src/foundry/fixtures/factory.py", 7, "AttributeError", "__getattr__"),
    )
    unexpected = tuple(site for site in internal if site.name not in _INTERNAL_SITE_BUILTINS)
    assert unexpected == (Site("src/foundry/data/escapes.py", 4, "KeyError", "_reject"),)
    misnamed = tuple(
        site
        for site in internal
        if site.name == "AttributeError" and site.function not in _ATTRIBUTE_PROTOCOL_FUNCTIONS
    )
    assert misnamed == (Site("src/foundry/data/escapes.py", 7, "AttributeError", "_raise_missing"),)
