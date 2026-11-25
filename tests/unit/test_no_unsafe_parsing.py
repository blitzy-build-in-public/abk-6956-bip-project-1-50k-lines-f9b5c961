"""The tree-wide proof that Foundry never reaches an unsafe deserialiser.

AAP 0.3.2 permits YAML only through ``yaml.safe_load`` and forbids ``eval``, ``exec`` and
``pickle``; AAP 0.14.3 names this module as the mechanism for it.

Detection is structural: every module is parsed with :mod:`ast` and its calls, imports and
aliases resolved, so a mention in prose is not a finding while the call is one.

Four routes stay out of reach and review of the call site closes them: a computed attribute name,
an alias rebound after its first binding, a loader fetched from a dict, and code inside a string.

Every hit is reported once, with its path, line and name, sorted, so two runs read identically.

The permitted path is proven: both designated readers are scanned and call ``safe_load``.

There is no exemption list: a hit anywhere under the roots is a finding. The roots are the
repository root's own ``*.py`` modules, ``src/foundry``, ``examples`` and every file under
``scripts``. This module lives under ``tests/``, outside every root.
"""

from __future__ import annotations

import ast
import inspect
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pytest

_YAML_MODULE: Final[str] = "yaml"
_BUILTINS_MODULE: Final[str] = "builtins"

_SAFE_YAML_LOADER: Final[str] = "safe_load"
"""The one YAML loader name the tree may call: the whole of AAP 0.3.2's YAML rule."""

_UNSAFE_YAML_LOADERS: Final[frozenset[str]] = frozenset(
    {
        # Every loader PyYAML documents as unsafe, plus the two composers: a node graph rebuilds
        # an object just as an unchecked loader does.
        "load",
        "load_all",
        "full_load",
        "full_load_all",
        "unsafe_load",
        "unsafe_load_all",
        "compose",
        "compose_all",
    }
)
"""Every ``yaml`` attribute this scan refuses, by attribute name."""

_UNSAFE_BUILTINS: Final[frozenset[str]] = frozenset({"eval", "exec"})
"""The two builtins that turn text into executed code."""

_UNSAFE_SERIALISERS: Final[frozenset[str]] = frozenset(
    {
        # The four modules AAP 0.3.2 names.
        "pickle",
        "_pickle",
        "cPickle",
        "marshal",
        # The optional extension to the same finding: three more modules whose purpose is to
        # rebuild an object graph, which is the capability the control forbids.
        "dill",
        "jsonpickle",
        "shelve",
    }
)
"""Import roots that rebuild an object graph, which is ``pickle``'s power under any name."""

_FUNCTOOLS_MODULE: Final[str] = "functools"
_IMPORTLIB_MODULE: Final[str] = "importlib"

_GETATTR_BUILTIN: Final[str] = "getattr"
"""The builtin that reaches a patrolled attribute under a name written as a string."""

_DYNAMIC_IMPORT_BUILTIN: Final[str] = "__import__"
"""The builtin that imports a module by name, which is ``importlib.import_module``'s other face."""

_YAML_LOADER_NAMES: Final[frozenset[str]] = _UNSAFE_YAML_LOADERS | frozenset({_SAFE_YAML_LOADER})
"""Every ``yaml`` attribute yielding a parsed document, the permitted loader included."""

_BUILTIN_ROUTES: Final[frozenset[str]] = _UNSAFE_BUILTINS | frozenset(
    {_GETATTR_BUILTIN, _DYNAMIC_IMPORT_BUILTIN}
)
"""Every builtin name this scan follows, the two that only dispatch a call included."""

_FUNCTOOLS_ROUTES: Final[frozenset[str]] = frozenset({"partial"})
"""The ``functools`` attribute that wraps a patrolled callable behind another name."""

_IMPORTLIB_ROUTES: Final[frozenset[str]] = frozenset({"import_module"})
"""The ``importlib`` attribute that imports a module named by a string."""

_PATROLLED_MODULES: Final[frozenset[str]] = (
    frozenset({_YAML_MODULE, _BUILTINS_MODULE, _FUNCTOOLS_MODULE, _IMPORTLIB_MODULE})
    | _UNSAFE_SERIALISERS
)
"""Every module root this scan follows by name, in an import or in a dynamic import."""

_CACHE_DIRECTORY_NAMES: Final[frozenset[str]] = frozenset(
    {"__pycache__", ".git", ".venv", ".mypy_cache", ".pytest_cache", ".ruff_cache"}
)
"""Directories holding no first-party source, so the walk does not descend into them."""

_SHELL_EVAL_TOKEN: Final[re.Pattern[str]] = re.compile(r"(?<![\w./-])eval(?![\w.-])")
"""``eval`` in command position in a shell script.

The lookarounds are what keep ``evaluate``, ``evaluated`` and ``foundry.execution`` out of the
report, while the word after ``;``, ``&&``, ``|``, ``$(`` or a newline is a hit. ``exec`` is
deliberately absent: in a shell it replaces the process rather than evaluating code, so a wrapper
script ending in ``exec pytest "$@"`` is legitimate; Python's ``exec`` is caught by the AST pass.
"""

_DATA_YAML_LOADER: Final[str] = "src/foundry/data/loaders/yaml.py"
"""The data-file YAML reader AAP 0.3.2 designates."""

_CASSETTE_STORE: Final[str] = "src/foundry/cassette/store.py"
"""The cassette module AAP 0.3.2 designates as the other YAML reader.

As generated it persists the JSON cassette document AAP 0.4.6 fixes and names no YAML loader at
all, so the assertion below is one-sided: ``safe_load`` is the only loader named today, which is
vacuous but becomes a finding the moment a recording path parses YAML another way.
"""

_SCANNED_PYTHON_ROOTS: Final[tuple[str, ...]] = ("src/foundry", "examples")
"""Roots whose ``.py`` modules are parsed."""

_SCANNED_ROOT_MODULES: Final[str] = "*.py"
"""The repository root's own modules: a ``.py`` added there is scanned like any other."""

_SCANNED_TEXT_ROOT: Final[str] = "scripts"
"""The root whose files are all read: ``.py`` by parse, everything else as shell text."""

_MINIMUM_INVENTORY: Final[int] = 20
"""A floor on the scanned file count, so an empty or broken walk cannot pass the suite."""


@dataclass(frozen=True, order=True)
class Hit:
    """One offending statement: where it is and the name that makes it a finding.

    The field order is the sort order -- path, then line, then name -- so a report of several hits
    reads the same way on every run.
    """

    path: str
    line: int
    name: str


@dataclass(frozen=True)
class ModuleScan:
    """What one file yielded: its hits, and every YAML loader it calls, safe ones included."""

    path: str
    hits: tuple[Hit, ...]
    yaml_loader_calls: tuple[tuple[int, str], ...]


@dataclass(frozen=True)
class _Aliases:
    """The local names one module binds to the patrolled modules and to their callables.

    ``modules`` maps a name to the module root it denotes and ``callables`` a name to the attribute.
    """

    modules: Mapping[str, str]
    callables: Mapping[str, tuple[str, str]]


def _write(root: Path, relative: str, *lines: str) -> Path:
    """Write ``lines`` to ``root/relative``, creating parent directories, and return the path."""
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    return path


def _string_constant(node: ast.expr) -> str | None:
    """Return the value of ``node`` when it is a string constant, else ``None``."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _resolve_member(root: str, name: str) -> tuple[str, str] | None:
    """Resolve ``root.name`` when it names a loader, a builtin route or a serialiser member.

    A root this scan does not follow -- ``os``, ``cursor``, ``json`` -- resolves to ``None``, and so
    does an attribute of ``yaml`` that parses nothing, such as ``yaml.dump``.
    """
    if root == _BUILTINS_MODULE:
        if name in _UNSAFE_BUILTINS:
            return "builtin", f"{_BUILTINS_MODULE}.{name}"
        return ("builtin", name) if name in _BUILTIN_ROUTES else None
    if root == _YAML_MODULE:
        return ("yaml", name) if name in _YAML_LOADER_NAMES else None
    if root == _FUNCTOOLS_MODULE:
        return ("functools", name) if name in _FUNCTOOLS_ROUTES else None
    if root == _IMPORTLIB_MODULE:
        return ("importlib", name) if name in _IMPORTLIB_ROUTES else None
    return ("serialiser", root) if root in _UNSAFE_SERIALISERS else None


def _dynamic_import(node: ast.Call) -> tuple[str, str] | None:
    """Resolve the module ``import_module`` or ``__import__`` names, by ``name=`` or positionally.

    Only the root segment counts, so a call naming ``json`` resolves to ``None``.
    """
    named = [keyword.value for keyword in node.keywords if keyword.arg == "name"]
    argument = named[0] if named else (node.args[0] if node.args else None)
    module = None if argument is None else _string_constant(argument)
    if module is None:
        return None
    root = module.split(".", 1)[0]
    return ("module", root) if root in _PATROLLED_MODULES else None


def _resolve_module(node: ast.expr, aliases: _Aliases) -> str | None:
    """Return the patrolled module root ``node`` denotes, or ``None`` for any other expression."""
    resolved = _resolve_reference(node, aliases)
    return resolved[1] if resolved is not None and resolved[0] == "module" else None


def _resolve_getattr(node: ast.Call, aliases: _Aliases) -> tuple[str, str] | None:
    """Resolve ``getattr(<module>, "<name>")`` when the name is a string constant.

    A name computed at run time and an object that is not a patrolled module resolve to ``None``.
    """
    if len(node.args) < 2:
        return None
    attribute = _string_constant(node.args[1])
    if attribute is None:
        return None
    root = _resolve_module(node.args[0], aliases)
    return None if root is None else _resolve_member(root, attribute)


def _resolve_reference(node: ast.expr, aliases: _Aliases) -> tuple[str, str] | None:
    """Classify ``node`` by what it denotes, following every name ``aliases`` records.

    A call resolves to what it yields, not what it calls, so one resolver serves alias collection
    and call detection; anything else, including a computed attribute name, resolves to ``None``.
    """
    if isinstance(node, ast.Call):
        callee = _resolve_reference(node.func, aliases)
        if callee == ("builtin", _GETATTR_BUILTIN):
            return _resolve_getattr(node, aliases)
        if callee == (_FUNCTOOLS_MODULE, "partial"):
            return None if not node.args else _resolve_reference(node.args[0], aliases)
        if callee in {("builtin", _DYNAMIC_IMPORT_BUILTIN), (_IMPORTLIB_MODULE, "import_module")}:
            return _dynamic_import(node)
        return None
    if isinstance(node, ast.Name):
        if node.id in _BUILTIN_ROUTES:
            return "builtin", node.id
        bound = aliases.callables.get(node.id)
        if bound is not None:
            return bound
        root = aliases.modules.get(node.id)
        return None if root is None else ("module", root)
    if isinstance(node, ast.Attribute):
        root = _resolve_module(node.value, aliases)
        return None if root is None else _resolve_member(root, node.attr)
    return None


def _record_assignments(
    tree: ast.AST, modules: dict[str, str], callables: dict[str, tuple[str, str]]
) -> bool:
    """Record every assignment target whose value resolves to a patrolled name, in every scope.

    Returns True when a name was added, so the caller re-reads until an alias of an alias resolves;
    a name keeps its first binding and the maps only grow, so the loop ends.
    """
    aliases = _Aliases(modules, callables)
    added = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign):
            targets, value = [node.target], node.value
        else:
            continue
        resolved = None if value is None else _resolve_reference(value, aliases)
        if resolved is None:
            continue
        for target in targets:
            if not isinstance(target, ast.Name):
                continue
            if resolved[0] == "module" and target.id not in modules:
                modules[target.id] = resolved[1]
                added = True
            elif resolved[0] != "module" and target.id not in callables:
                callables[target.id] = resolved
                added = True
    return added


def _collect_aliases(tree: ast.AST) -> _Aliases:
    """Return the names ``tree`` binds to the patrolled modules and to their callables."""
    modules: dict[str, str] = {_YAML_MODULE: _YAML_MODULE, _BUILTINS_MODULE: _BUILTINS_MODULE}
    callables: dict[str, tuple[str, str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                if root in _PATROLLED_MODULES:
                    modules[alias.asname or root] = root
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module is not None:
            root = node.module.split(".", 1)[0]
            for alias in node.names:
                resolved = _resolve_member(root, alias.name)
                if resolved is not None:
                    callables[alias.asname or alias.name] = resolved
    growing = True
    while growing:
        growing = _record_assignments(tree, modules, callables)
    return _Aliases(modules, callables)


def _serialiser_root(module: str | None, level: int) -> str | None:
    """Return the serialiser a ``from``-import names, or ``None`` for any other module."""
    if level or module is None:
        return None
    root = module.split(".", 1)[0]
    return root if root in _UNSAFE_SERIALISERS else None


def _collect_hits(tree: ast.AST, path: str, aliases: _Aliases) -> ModuleScan:
    """Return every finding in ``tree``, with the YAML loader calls it records on the way."""
    hits: list[Hit] = []
    loader_calls: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                if root in _UNSAFE_SERIALISERS:
                    hits.append(Hit(path, node.lineno, root))
        elif isinstance(node, ast.ImportFrom):
            serialiser = _serialiser_root(node.module, node.level)
            if serialiser is not None:
                hits.append(Hit(path, node.lineno, serialiser))
            elif node.module == _YAML_MODULE and not node.level:
                hits.extend(
                    Hit(path, node.lineno, f"{_YAML_MODULE}.{alias.name}")
                    for alias in node.names
                    if alias.name in _UNSAFE_YAML_LOADERS
                )
        elif isinstance(node, ast.Call):
            resolved = _resolve_reference(node.func, aliases)
            if resolved is not None:
                kind, name = resolved
                is_unsafe = kind == "serialiser" or (
                    kind == "builtin" and name.rsplit(".", 1)[-1] in _UNSAFE_BUILTINS
                )
                if is_unsafe:
                    hits.append(Hit(path, node.lineno, name))
                elif kind == "yaml":
                    loader_calls.append((node.lineno, name))
                    if name != _SAFE_YAML_LOADER:
                        hits.append(Hit(path, node.lineno, f"{_YAML_MODULE}.{name}"))
            imported = _resolve_reference(node, aliases)
            if (
                imported is not None
                and imported[0] == "module"
                and imported[1] in _UNSAFE_SERIALISERS
            ):
                hits.append(Hit(path, node.lineno, imported[1]))
    return ModuleScan(path, tuple(dict.fromkeys(sorted(hits))), tuple(sorted(loader_calls)))


def _shell_code(line: str) -> str:
    """Return ``line`` with its quoted literals and its trailing comment removed.

    A command can only occupy an unquoted position, so prose and printed text stay out of the
    report while ``eval "$command"`` stays.
    """
    code: list[str] = []
    quote: str | None = None
    previous = " "
    for char in line:
        if quote is not None:
            if char == quote:
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
            code.append(" ")
        elif char == "#" and previous in {" ", "\t"}:
            break
        else:
            code.append(char)
        previous = char
    return "".join(code)


def _scan_shell(source: str, path: str) -> ModuleScan:
    """Return the ``eval`` findings in a non-Python text file."""
    hits = [
        Hit(path, number, match.group(0))
        for number, line in enumerate(source.splitlines(), start=1)
        for match in _SHELL_EVAL_TOKEN.finditer(_shell_code(line))
    ]
    return ModuleScan(path, tuple(sorted(hits)), ())


def _scan_file(path: Path, display: str) -> ModuleScan:
    """Scan one file, parsing ``.py`` by AST and reading every other file as shell text."""
    if path.suffix == ".py":
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=display)
        return _collect_hits(tree, display, _collect_aliases(tree))
    return _scan_shell(path.read_text(encoding="utf-8", errors="replace"), display)


def _is_in_cache_directory(path: Path, root: Path) -> bool:
    """Return True when a directory between ``root`` and ``path`` holds no first-party source."""
    parents = path.relative_to(root).parts[:-1]
    return any(part in _CACHE_DIRECTORY_NAMES or part.endswith(".egg-info") for part in parents)


def _scanned_files(root: Path) -> tuple[Path, ...]:
    """Return every file the scan reads, sorted, with cache directories skipped.

    The repository root's own ``*.py`` modules are read first, so a module beside ``pyproject.toml``
    cannot escape the walk, then the Python roots and the text root.
    """
    files: list[Path] = [path for path in root.glob(_SCANNED_ROOT_MODULES) if path.is_file()]
    for relative in _SCANNED_PYTHON_ROOTS:
        base = root / relative
        if base.is_dir():
            files.extend(p for p in base.rglob("*.py") if not _is_in_cache_directory(p, root))
    scripts = root / _SCANNED_TEXT_ROOT
    if scripts.is_dir():
        files.extend(
            p for p in scripts.rglob("*") if p.is_file() and not _is_in_cache_directory(p, root)
        )
    return tuple(sorted(files))


def _scan_tree(root: Path) -> tuple[Hit, ...]:
    """Scan every file of the tree at ``root`` and return all hits, sorted."""
    hits: list[Hit] = []
    for path in _scanned_files(root):
        hits.extend(_scan_file(path, path.relative_to(root).as_posix()).hits)
    return tuple(sorted(hits))


def _format_hits(hits: tuple[Hit, ...]) -> str:
    """Render ``hits`` as the failure message: a count, then one ``path:line: name`` per line."""
    lines = [f"  {hit.path}:{hit.line}: {hit.name}" for hit in hits]
    return "\n".join([f"{len(hits)} unsafe parsing call site(s):", *lines])


def test_the_scanned_tree_holds_no_unsafe_parsing(repo_root: Path) -> None:
    """A hit anywhere under the scanned roots is a finding (AAP 0.3.2, AAP 0.14.3)."""
    hits = _scan_tree(repo_root)
    assert hits == (), _format_hits(hits)


def test_the_scan_reaches_the_designated_yaml_readers(repo_root: Path) -> None:
    """Both modules AAP 0.3.2 designates are inside the scanned inventory."""
    scanned = {path.relative_to(repo_root).as_posix() for path in _scanned_files(repo_root)}
    for relative in (_DATA_YAML_LOADER, _CASSETTE_STORE):
        assert relative in scanned, f"{relative} is not scanned: {sorted(scanned)}"
    assert len(scanned) > _MINIMUM_INVENTORY, f"the walk found only {len(scanned)} files"
    assert not any(name.startswith("tests/") for name in scanned), "tests/ is outside the roots"


def test_the_data_yaml_loader_parses_only_through_safe_load(repo_root: Path) -> None:
    """The data reader holds a real ``yaml.safe_load`` call and names no other loader."""
    scan = _scan_file(repo_root / _DATA_YAML_LOADER, _DATA_YAML_LOADER)
    assert scan.hits == (), _format_hits(scan.hits)
    loaders = {name for _, name in scan.yaml_loader_calls}
    assert loaders == {_SAFE_YAML_LOADER}, f"{_DATA_YAML_LOADER} calls {sorted(loaders)}"


def test_the_cassette_store_names_no_loader_but_safe_load(repo_root: Path) -> None:
    """The cassette module AAP 0.3.2 designates names no YAML loader other than ``safe_load``."""
    scan = _scan_file(repo_root / _CASSETTE_STORE, _CASSETTE_STORE)
    assert scan.hits == (), _format_hits(scan.hits)
    loaders = {name for _, name in scan.yaml_loader_calls}
    assert loaders <= {_SAFE_YAML_LOADER}, f"{_CASSETTE_STORE} calls {sorted(loaders)}"


def test_the_cassette_store_guard_catches_an_unsafe_loader(tmp_path: Path) -> None:
    """The one-sided store assertion is not vacuous: a loader other than ``safe_load`` trips it."""
    _write(tmp_path, _CASSETTE_STORE, "import yaml", "yaml.unsafe_load(text)")

    scan = _scan_file(tmp_path / _CASSETTE_STORE, _CASSETTE_STORE)
    loaders = {name for _, name in scan.yaml_loader_calls}
    assert loaders == {"unsafe_load"}
    assert not loaders <= {_SAFE_YAML_LOADER}
    assert [hit.name for hit in scan.hits] == ["yaml.unsafe_load"]


def test_the_positive_control_reports_every_offender(tmp_path: Path) -> None:
    """A synthetic tree yields exactly its three offenders, with path, line and name."""
    _write(tmp_path, "src/foundry/offending_yaml.py", "import yaml", "yaml.load(text)")
    _write(tmp_path, "examples/offending_eval.py", "eval(expression)")
    _write(tmp_path, "scripts/offending_pickle.py", "import pickle")
    _write(tmp_path, "src/foundry/clean_reader.py", "import yaml", "yaml.safe_load(text)")

    hits = _scan_tree(tmp_path)
    assert {(hit.path, hit.line) for hit in hits} == {
        ("examples/offending_eval.py", 1),
        ("scripts/offending_pickle.py", 1),
        ("src/foundry/offending_yaml.py", 2),
    }
    assert {hit.name for hit in hits} == {"eval", "pickle", "yaml.load"}
    message = _format_hits(hits)
    assert message.startswith("3 unsafe parsing call site(s)")
    for hit in hits:
        assert f"{hit.path}:{hit.line}: {hit.name}" in message, message


def test_docstring_and_string_mentions_are_not_findings(tmp_path: Path) -> None:
    """A mention in a docstring, a comment or a string constant is not a call."""
    _write(
        tmp_path,
        "src/foundry/talking_about_it.py",
        '"""A module that names yaml.load(text) and eval(text) in prose only."""',
        "import yaml",
        '_MENTION = "never call yaml.unsafe_load( or eval( here"',
        "# yaml.load(text) here would be a finding; yaml.safe_load is what this calls.",
        "yaml.safe_load(text)",
    )
    _write(
        tmp_path,
        "scripts/talking_about_it.sh",
        "#!/usr/bin/env bash",
        "# This script must never eval its arguments.",
        "printf '%s\\n' \"no eval here, and no exec either\"",
    )

    hits = _scan_tree(tmp_path)
    assert hits == (), _format_hits(hits)


@pytest.mark.parametrize(
    ("relative", "source", "expected"),
    [
        ("src/foundry/alias_mod.py", "import yaml as y\ny.unsafe_load(t)", "yaml.unsafe_load"),
        ("src/foundry/alias_name.py", "from yaml import full_load as p\np(t)", "yaml.full_load"),
        ("src/foundry/load_all.py", "import yaml\nyaml.load_all(s)", "yaml.load_all"),
        ("examples/alias_builtins.py", "import builtins as b\nb.exec(s)", "builtins.exec"),
        ("src/foundry/__init__.py", "import marshal", "marshal"),
        ("examples/conftest.py", "import _pickle as m", "_pickle"),
        ("scripts/eval_script.sh", 'set -eu\neval "$command"', "eval"),
        ("src/foundry/assigned_loader.py", "import yaml\nf = yaml.load\nf(t)", "yaml.load"),
        ("src/foundry/assigned_builtin.py", "e = eval\ne(t)", "eval"),
        ("src/foundry/getattr_loader.py", 'import yaml\ngetattr(yaml, "load")(t)', "yaml.load"),
        (
            "src/foundry/partial_loader.py",
            "import functools\nimport yaml\nf = functools.partial(yaml.unsafe_load)\nf(t)",
            "yaml.unsafe_load",
        ),
        (
            "src/foundry/importlib_loader.py",
            'import importlib\nimportlib.import_module("pickle")',
            "pickle",
        ),
        (
            "src/foundry/import_module_named.py",
            'import importlib\nimportlib.import_module(name="pickle")',
            "pickle",
        ),
        ("src/foundry/dunder_import_named.py", '__import__(name="marshal")', "marshal"),
        ("src/foundry/dunder_import.py", '__import__("marshal")', "marshal"),
        ("offending_root.py", "import yaml\nyaml.load(t)", "yaml.load"),
    ],
)
def test_every_route_to_an_unsafe_loader_is_detected(
    tmp_path: Path, relative: str, source: str, expected: str
) -> None:
    """Each alias, each unsafe loader name, each file name and a shell ``eval`` reach the report."""
    _write(tmp_path, relative, *source.splitlines())
    hits = _scan_tree(tmp_path)
    assert expected in {hit.name for hit in hits}, _format_hits(hits)


def test_the_indirect_routes_report_nothing_on_safe_code(tmp_path: Path) -> None:
    """The same routes reached for a permitted loader or an ordinary object are not findings."""
    _write(
        tmp_path,
        "src/foundry/safe_indirection.py",
        "import functools",
        "import importlib",
        "import yaml",
        'assigned = getattr(yaml, "safe_load")',
        "wrapped = functools.partial(yaml.safe_load)",
        "yaml.safe_load(t)",
        'getattr(yaml, "safe_load")(t)',
        "functools.partial(yaml.safe_load)(t)",
        'importlib.import_module("json")',
        'importlib.import_module(name="json")',
        '__import__("json", fromlist=("pickle",))',
        "plain = object()",
        'getattr(plain, "load")(t)',
        "getattr(plain, attribute)(t)",
        "yaml.dump(plain)",
    )

    hits = _scan_tree(tmp_path)
    assert hits == (), _format_hits(hits)


def test_the_scanner_offers_no_exemption() -> None:
    """No scanner entry point takes an exemption and no module-level name holds one."""
    markers = ("allow", "exempt", "permit", "whitelist", "ignore", "skip", "bypass")
    names = [name for name in globals() if not name.startswith(("test_", "__"))]
    flagged = [name for name in names if any(marker in name.casefold() for marker in markers)]
    assert flagged == [], flagged
    for name in names:
        function = globals()[name]
        if inspect.isfunction(function) and function.__module__ == __name__:
            parameters = [
                parameter
                for parameter in inspect.signature(function).parameters
                if any(marker in parameter.casefold() for marker in markers)
            ]
            assert parameters == [], f"{name} takes an exemption parameter: {parameters}"
