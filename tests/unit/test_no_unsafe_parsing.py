"""The tree-wide proof that Foundry never reaches an unsafe deserialiser.

AAP 0.3.2's "Deserialisation safety" control is one rule -- YAML is parsed with ``safe_load``
only, and configuration, data and cassette content never reach ``eval``, ``exec`` or ``pickle`` --
and AAP 0.14.3 makes it a mechanism by naming this module. What it guards is the failure the rule
exists for: one call added later that reads a project-authored file through an unchecked loader
executes whatever that file names.

*Detection is structural.* Every Python module is parsed with :mod:`ast` and its calls and imports
are resolved, so a docstring, a comment or a string constant that merely mentions ``yaml.load`` is
not a finding while the call is one, and ``import yaml as y``, ``from yaml import load as parse``
and ``import builtins as b`` are followed to the names they bind.

*Every hit is reported in one message*, with its path, line and the name that made it a finding,
sorted so two runs over one tree read identically.

*The permitted path is proven.* The two modules AAP 0.3.2 designates as YAML readers must be
inside the scanned inventory and the data reader must hold a real ``yaml.safe_load`` call, so a
rename cannot leave the scan above passing on an empty list.

*There is no exemption list.* AAP 0.3.2 permits none, and a hit anywhere under the roots is a
finding. The roots are ``src/foundry`` and ``examples`` for Python and ``scripts`` for Python plus
every other text file, because a shell script must not ``eval`` either. This module lives under
``tests/``, outside every root, so the names it polices are safe to spell here.
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

_CACHE_DIRECTORY_NAMES: Final[frozenset[str]] = frozenset(
    {"__pycache__", ".git", ".venv", ".mypy_cache", ".pytest_cache", ".ruff_cache"}
)
"""Directories holding no first-party source, so the walk does not descend into them."""

_SHELL_EVAL_TOKEN: Final[re.Pattern[str]] = re.compile(r"(?<![\w./-])eval(?![\w.-])")
"""``eval`` in command position in a shell script.

The lookarounds are what keep ``evaluate``, ``evaluated`` and ``foundry.execution`` out of the
report, while the word after ``;``, ``&&``, ``|``, ``$(`` or a newline is a hit. ``exec`` is
deliberately absent: in a shell it replaces the process rather than evaluating code, and a wrapper
script that ends in ``exec pytest "$@"`` is a legitimate pattern. Python's ``exec`` is caught by
the AST pass instead, where it really does run code.
"""

_DATA_YAML_LOADER: Final[str] = "src/foundry/data/loaders/yaml.py"
"""The data-file YAML reader AAP 0.3.2 designates."""

_CASSETTE_STORE: Final[str] = "src/foundry/cassette/store.py"
"""The cassette module AAP 0.3.2 designates as the other YAML reader.

As generated it persists the JSON cassette document AAP 0.4.6 fixes and names no YAML loader at
all, so its assertion below is one-sided: it names no loader other than ``safe_load`` -- vacuous
today, and a finding the moment a recording path parses YAML another way.
"""

_SCANNED_PYTHON_ROOTS: Final[tuple[str, ...]] = ("src/foundry", "examples")
"""Roots whose ``.py`` modules are parsed."""

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
    """The local names one module binds to the patrolled modules and to their loaders."""

    yaml_modules: frozenset[str]
    yaml_loaders: Mapping[str, str]
    builtins_modules: frozenset[str]


def _write(root: Path, relative: str, *lines: str) -> Path:
    """Write ``lines`` to ``root/relative``, creating parent directories, and return the path."""
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    return path


def _dotted_name(node: ast.expr) -> str | None:
    """Return ``node`` as a dotted name -- ``yaml`` -- or ``None`` for anything else."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted_name(node.value)
        return None if base is None else f"{base}.{node.attr}"
    return None


def _collect_aliases(tree: ast.AST) -> _Aliases:
    """Return the names ``tree`` binds to the patrolled modules and to their loaders."""
    yaml_modules = {_YAML_MODULE}
    builtins_modules = {_BUILTINS_MODULE}
    yaml_loaders: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == _YAML_MODULE:
                    yaml_modules.add(alias.asname or _YAML_MODULE)
                elif alias.name == _BUILTINS_MODULE:
                    builtins_modules.add(alias.asname or _BUILTINS_MODULE)
        elif isinstance(node, ast.ImportFrom) and node.module == _YAML_MODULE and not node.level:
            for alias in node.names:
                if alias.name == _SAFE_YAML_LOADER or alias.name in _UNSAFE_YAML_LOADERS:
                    yaml_loaders[alias.asname or alias.name] = alias.name
    return _Aliases(frozenset(yaml_modules), yaml_loaders, frozenset(builtins_modules))


def _resolve_callee(func: ast.expr, aliases: _Aliases) -> tuple[str, str] | None:
    """Classify ``func`` as ``("builtin", name)`` or ``("yaml", loader)``, else ``None``.

    Only the patrolled names resolve: a bare ``eval`` or ``exec``, the same two behind an alias of
    ``builtins``, a YAML loader behind an alias of ``yaml``, and a loader imported under a name of
    its own. Every other attribute call -- ``os.path.join``, ``cursor.execute``, ``yaml.dump`` --
    is left alone, which is what keeps the report to real findings.
    """
    if isinstance(func, ast.Name):
        if func.id in _UNSAFE_BUILTINS:
            return "builtin", func.id
        loader = aliases.yaml_loaders.get(func.id)
        return None if loader is None else ("yaml", loader)
    if isinstance(func, ast.Attribute):
        base = _dotted_name(func.value)
        if base is not None and base in aliases.builtins_modules and func.attr in _UNSAFE_BUILTINS:
            return "builtin", f"{_BUILTINS_MODULE}.{func.attr}"
        is_yaml = base is not None and base in aliases.yaml_modules
        if is_yaml and (func.attr == _SAFE_YAML_LOADER or func.attr in _UNSAFE_YAML_LOADERS):
            return "yaml", func.attr
    return None


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
            resolved = _resolve_callee(node.func, aliases)
            if resolved is None:
                continue
            kind, name = resolved
            if kind == "builtin":
                hits.append(Hit(path, node.lineno, name))
                continue
            loader_calls.append((node.lineno, name))
            if name != _SAFE_YAML_LOADER:
                hits.append(Hit(path, node.lineno, f"{_YAML_MODULE}.{name}"))
    return ModuleScan(path, tuple(sorted(hits)), tuple(sorted(loader_calls)))


def _shell_code(line: str) -> str:
    """Return ``line`` with its quoted literals and its trailing comment removed.

    A shell script is scanned as text, and the positions a command can occupy are unquoted ones.
    Dropping quoted literals and comments keeps ``printf '%s' "never eval this"`` and ``# do not
    eval the arguments`` out of the report while ``eval "$command"`` -- the finding -- stays. The
    residual gap is code carried inside a string handed to another interpreter, which no token
    scan can see.
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
    """Return every file the scan reads, sorted, with cache directories skipped."""
    files: list[Path] = []
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
    """A hit anywhere under the scanned roots is a finding (AAP 0.3.2, AAP 0.14.3).

    YAML may be parsed only through ``yaml.safe_load``, and no module may call ``eval`` or ``exec``
    or import a module that rebuilds a Python object graph. The assertion is on the whole sorted
    hit list, so one failure names every offending statement rather than the first.
    """
    hits = _scan_tree(repo_root)
    assert hits == (), _format_hits(hits)


def test_the_scan_reaches_the_designated_yaml_readers(repo_root: Path) -> None:
    """Both modules AAP 0.3.2 designates are inside the scanned inventory.

    Without this, a rename that took a reader out of the walk would leave the scan above passing
    on an empty list -- the vacuous pass this whole module exists to prevent.
    """
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
    """The one-sided store assertion is not vacuous: a loader other than ``safe_load`` trips it.

    ``src/foundry/cassette/store.py`` names no YAML loader today, so the assertion in
    :func:`test_the_cassette_store_names_no_loader_but_safe_load` holds on an empty set. This case
    runs the same condition against a store that parses YAML another way, which is what proves the
    guard reports a regression instead of passing on nothing.
    """
    _write(tmp_path, _CASSETTE_STORE, "import yaml", "yaml.unsafe_load(text)")

    scan = _scan_file(tmp_path / _CASSETTE_STORE, _CASSETTE_STORE)
    loaders = {name for _, name in scan.yaml_loader_calls}
    assert loaders == {"unsafe_load"}
    assert not loaders <= {_SAFE_YAML_LOADER}
    assert [hit.name for hit in scan.hits] == ["yaml.unsafe_load"]


def test_the_positive_control_reports_every_offender(tmp_path: Path) -> None:
    """A synthetic tree yields exactly its three offenders, with path, line and name.

    The control is what proves the scanner can fail at all: a resolver that had stopped resolving
    would leave the real tree's clean scan passing for the wrong reason.
    """
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
    """A mention in a docstring, a comment or a string constant is not a call.

    This is the property an AST scan has and a text grep does not, and the reason the guard stays
    usable in a codebase that documents the rule it enforces.
    """
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
    ],
)
def test_every_route_to_an_unsafe_loader_is_detected(
    tmp_path: Path, relative: str, source: str, expected: str
) -> None:
    """Each alias, each unsafe loader name, each file name and a shell ``eval`` reach the report.

    ``src/foundry/__init__.py``, ``examples/conftest.py`` and a ``.sh`` file are listed on purpose:
    no file name and no file type is exempt from the scan.
    """
    _write(tmp_path, relative, *source.splitlines())
    hits = _scan_tree(tmp_path)
    assert expected in {hit.name for hit in hits}, _format_hits(hits)


def test_the_scanner_offers_no_exemption() -> None:
    """No scanner entry point takes an exemption and no module-level name holds one.

    AAP 0.3.2 permits no allowlist, so a future edit that added one would be invisible as a
    behaviour change and visible only as this failure.
    """
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
