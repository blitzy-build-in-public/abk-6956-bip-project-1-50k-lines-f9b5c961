"""Tree-wide proof that Foundry reaches no unsafe deserialiser: YAML only via safe_load."""

import ast
import inspect
import re
from collections import namedtuple
from collections.abc import Mapping
from itertools import chain
from pathlib import Path
from typing import Final

import pytest

_YAML_MODULE: Final[str] = "yaml"
_BUILTINS_MODULE: Final[str] = "builtins"
_FUNCTOOLS_MODULE: Final[str] = "functools"
_IMPORTLIB_MODULE: Final[str] = "importlib"
_SAFE_YAML_LOADER: Final[str] = "safe_load"
_UNSAFE_YAML_LOADERS: Final[frozenset[str]] = frozenset(
    {"load", "load_all", "full_load", "full_load_all", "unsafe_load", "unsafe_load_all"}
) | frozenset({"compose", "compose_all"})
_UNSAFE_BUILTINS: Final[frozenset[str]] = frozenset({"eval", "exec"})
_UNSAFE_SERIALISERS: Final[frozenset[str]] = frozenset(
    {"pickle", "_pickle", "cPickle", "marshal", "dill", "jsonpickle", "shelve"}
)
_YAML_LOADER_NAMES: Final[frozenset[str]] = _UNSAFE_YAML_LOADERS | {_SAFE_YAML_LOADER}
_BUILTIN_ROUTES: Final[frozenset[str]] = _UNSAFE_BUILTINS | {"getattr", "__import__"}
_PATROLLED_MODULES: Final[frozenset[str]] = _UNSAFE_SERIALISERS | frozenset(
    {_YAML_MODULE, _BUILTINS_MODULE, _FUNCTOOLS_MODULE, _IMPORTLIB_MODULE}
)
# eval in command position, so evaluate and foundry.execution stay out; shell exec is not patrolled
# because it replaces the process, and Python's exec is caught by the AST pass.
_SHELL_EVAL_TOKEN: Final[re.Pattern[str]] = re.compile(r"(?<![\w./-])eval(?![\w.-])")
_QUOTED: Final[re.Pattern[str]] = re.compile(r"'[^']*'|\"[^\"]*\"")
_SHELL_COMMENT: Final[re.Pattern[str]] = re.compile(r"(?:^|\s)#.*$")
_DATA_YAML_LOADER: Final[str] = "src/foundry/data/loaders/yaml.py"
_CASSETTE_STORE: Final[str] = "src/foundry/cassette/store.py"
_PYTHON_PATTERNS: Final[tuple[str, ...]] = ("*.py", "src/foundry/**/*.py", "examples/**/*.py")
_SCAN_PATTERNS: Final[tuple[str, ...]] = (*_PYTHON_PATTERNS, "scripts/**/*")


Hit = namedtuple("Hit", "path line name")
ModuleScan = namedtuple("ModuleScan", "hits yaml_loader_calls")


def _write(root: Path, relative: str, *lines: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")


def _string_constant(node: ast.expr) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _resolve_member(root: str, name: str) -> tuple[str, str] | None:
    if root == _BUILTINS_MODULE:
        qualified = f"{_BUILTINS_MODULE}.{name}" if name in _UNSAFE_BUILTINS else name
        return ("builtin", qualified) if name in _BUILTIN_ROUTES else None
    if root == _YAML_MODULE:
        return ("yaml", name) if name in _YAML_LOADER_NAMES else None
    if root == _FUNCTOOLS_MODULE:
        return ("functools", name) if name == "partial" else None
    if root == _IMPORTLIB_MODULE:
        return ("importlib", name) if name == "import_module" else None
    return ("serialiser", root) if root in _UNSAFE_SERIALISERS else None


def _dynamic_import(node: ast.Call) -> tuple[str, str] | None:
    named = [keyword.value for keyword in node.keywords if keyword.arg == "name"]
    module = _string_constant(named[0] if named else node.args[0] if node.args else node)
    root = None if module is None else module.split(".", 1)[0]
    return ("module", root) if root in _PATROLLED_MODULES else None


def _resolve_module(node: ast.expr, bound: Mapping[str, tuple[str, str]]) -> str:
    resolved = _resolve_reference(node, bound) or ("", "")
    return resolved[1] if resolved[0] == "module" else ""


def _resolve_reference(
    node: ast.expr, bound: Mapping[str, tuple[str, str]]
) -> tuple[str, str] | None:
    if isinstance(node, ast.Call):
        callee = _resolve_reference(node.func, bound)
        if callee == ("builtin", "getattr") and len(node.args) > 1:
            attribute = _string_constant(node.args[1]) or ""
            return _resolve_member(_resolve_module(node.args[0], bound), attribute)
        if callee == (_FUNCTOOLS_MODULE, "partial"):
            return _resolve_reference(node.args[0], bound) if node.args else None
        if callee in {("builtin", "__import__"), (_IMPORTLIB_MODULE, "import_module")}:
            return _dynamic_import(node)
        return None
    if isinstance(node, ast.Name):
        return ("builtin", node.id) if node.id in _BUILTIN_ROUTES else bound.get(node.id)
    if isinstance(node, ast.Attribute):
        return _resolve_member(_resolve_module(node.value, bound), node.attr)
    return None


def _collect_aliases(tree: ast.AST) -> dict[str, tuple[str, str]]:
    bound: dict[str, tuple[str, str]] = {n: ("module", n) for n in (_YAML_MODULE, _BUILTINS_MODULE)}
    while True:
        before = len(bound)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".", 1)[0]
                    if root in _PATROLLED_MODULES:
                        bound.setdefault(alias.asname or root, ("module", root))
            elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
                for alias in node.names:
                    member = _resolve_member(node.module.split(".", 1)[0], alias.name)
                    if member is not None:
                        bound.setdefault(alias.asname or alias.name, member)
            elif isinstance(node, ast.Assign | ast.AnnAssign):
                value = _resolve_reference(node.value, bound) if node.value is not None else None
                for target in node.targets if isinstance(node, ast.Assign) else [node.target]:
                    if value is not None and isinstance(target, ast.Name):
                        bound.setdefault(target.id, value)
        if len(bound) == before:
            return bound


def _collect_hits(tree: ast.AST, path: str, bound: Mapping[str, tuple[str, str]]) -> ModuleScan:
    hits: list[Hit] = []
    loader_calls: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots = [alias.name.split(".", 1)[0] for alias in node.names]
            hits.extend(Hit(path, node.lineno, r) for r in roots if r in _UNSAFE_SERIALISERS)
        elif isinstance(node, ast.ImportFrom):
            root = None if node.level or node.module is None else node.module.split(".", 1)[0]
            if root in _UNSAFE_SERIALISERS:
                hits.append(Hit(path, node.lineno, root))
            elif node.module == _YAML_MODULE and not node.level:
                named = [alias.name for alias in node.names if alias.name in _UNSAFE_YAML_LOADERS]
                hits.extend(Hit(path, node.lineno, f"{_YAML_MODULE}.{name}") for name in named)
        elif isinstance(node, ast.Call):
            kind, name = _resolve_reference(node.func, bound) or ("", "")
            if kind == "serialiser" or name.rpartition(".")[2] in _UNSAFE_BUILTINS:
                hits.append(Hit(path, node.lineno, name))
            elif kind == "yaml":
                loader_calls.append((node.lineno, name))
                if name != _SAFE_YAML_LOADER:
                    hits.append(Hit(path, node.lineno, f"{_YAML_MODULE}.{name}"))
            dynamic = _resolve_reference(node, bound) or ("", "")
            if dynamic[0] == "module" and dynamic[1] in _UNSAFE_SERIALISERS:
                hits.append(Hit(path, node.lineno, dynamic[1]))
    return ModuleScan(tuple(dict.fromkeys(sorted(hits))), tuple(sorted(loader_calls)))


def _scan_shell(source: str, path: str) -> ModuleScan:
    hits = [
        Hit(path, number, found.group(0))
        for number, line in enumerate(source.splitlines(), start=1)
        for found in _SHELL_EVAL_TOKEN.finditer(_SHELL_COMMENT.sub("", _QUOTED.sub(" ", line)))
    ]
    return ModuleScan(tuple(sorted(hits)), ())


def _scan_file(path: Path, display: str) -> ModuleScan:
    if path.suffix == ".py":
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=display)
        return _collect_hits(tree, display, _collect_aliases(tree))
    return _scan_shell(path.read_text(encoding="utf-8", errors="replace"), display)


def _is_first_party(path: Path, root: Path) -> bool:
    caches = {"__pycache__", ".git", ".venv", ".mypy_cache", ".pytest_cache", ".ruff_cache"}
    parts = path.relative_to(root).parts[:-1]
    return not any(p in caches or p.endswith(".egg-info") for p in parts)


def _scanned_files(root: Path) -> tuple[Path, ...]:
    found = (path for pattern in _SCAN_PATTERNS for path in root.glob(pattern))
    return tuple(sorted(path for path in found if path.is_file() and _is_first_party(path, root)))


def _scan_tree(root: Path) -> tuple[Hit, ...]:
    scans = (_scan_file(path, path.relative_to(root).as_posix()) for path in _scanned_files(root))
    return tuple(sorted(chain.from_iterable(scan.hits for scan in scans)))


def _format_hits(hits: tuple[Hit, ...]) -> str:
    detail = "".join(f"\n  {hit.path}:{hit.line}: {hit.name}" for hit in hits)
    return f"{len(hits)} unsafe parsing call site(s):{detail}"


def test_the_scanned_tree_holds_no_unsafe_parsing(repo_root: Path, tmp_path: Path) -> None:
    hits = _scan_tree(repo_root)
    assert hits == (), _format_hits(hits)
    _write(tmp_path, "src/foundry/offending_yaml.py", "import yaml", "yaml.load(text)")
    _write(tmp_path, "examples/offending_eval.py", "eval(expression)")
    _write(tmp_path, "scripts/offending_pickle.py", "import pickle")
    planted = _scan_tree(tmp_path)
    message = _format_hits(planted)
    assert message.startswith("3 unsafe parsing call site(s)"), message
    assert all(f"{hit.path}:{hit.line}: {hit.name}" in message for hit in planted), message


def test_the_walk_and_the_store_proof_are_not_vacuous(repo_root: Path, tmp_path: Path) -> None:
    scanned = {path.relative_to(repo_root).as_posix() for path in _scanned_files(repo_root)}
    missing = [name for name in (_DATA_YAML_LOADER, _CASSETTE_STORE) if name not in scanned]
    assert missing == [], f"the walk misses {missing}"
    assert len(scanned) > 20, f"the walk found only {len(scanned)} files"
    assert not any(name.startswith("tests/") for name in scanned), "tests/ is outside the roots"
    # The data reader must call safe_load; the store half is one-sided, but its control can fail.
    for relative, exact in {_DATA_YAML_LOADER: True, _CASSETTE_STORE: False}.items():
        scan = _scan_file(repo_root / relative, relative)
        assert scan.hits == (), _format_hits(scan.hits)
        loaders = {name for _, name in scan.yaml_loader_calls}
        assert loaders <= {_SAFE_YAML_LOADER}, f"{relative} calls {sorted(loaders)}"
        assert not exact or loaders == {_SAFE_YAML_LOADER}, f"{relative} shows {sorted(loaders)}"
    _write(tmp_path, _CASSETTE_STORE, "import yaml", "yaml.unsafe_load(text)")
    control = _scan_file(tmp_path / _CASSETTE_STORE, _CASSETTE_STORE)
    assert [hit.name for hit in control.hits] == ["yaml.unsafe_load"]
    assert not {name for _, name in control.yaml_loader_calls} <= {_SAFE_YAML_LOADER}


@pytest.mark.parametrize(
    ("relative", "code", "expected"),
    [
        ("src/foundry/direct.py", "import yaml\nyaml.load(t)", ["yaml.load"]),
        ("offending_root.py", "import yaml\nyaml.load(t)", ["yaml.load"]),
        ("src/foundry/alias.py", "import yaml as y\ny.unsafe_load(t)", ["yaml.unsafe_load"]),
        ("src/foundry/from_alias.py", "from yaml import full_load as p\np(t)", ["yaml.full_load"]),
        ("src/foundry/load_all.py", "import yaml\nyaml.load_all(s)", ["yaml.load_all"]),
        ("examples/builtins.py", "import builtins as b\nb.exec(s)", ["builtins.exec"]),
        ("src/foundry/serials.py", "import marshal\nimport _pickle as m", ["marshal", "_pickle"]),
        ("scripts/eval_script.sh", 'set -eu\neval "$command"', ["eval"]),
        ("src/foundry/assigned.py", "e = eval\nf = yaml.load\ne(t)\nf(t)", ["eval", "yaml.load"]),
        ("src/foundry/getattr_loader.py", 'import yaml\ngetattr(yaml, "load")(t)', ["yaml.load"]),
        (
            "src/foundry/partial_loader.py",
            "import functools\nimport yaml\nf = functools.partial(yaml.unsafe_load)\nf(t)",
            ["yaml.unsafe_load"],
        ),
        (
            "src/foundry/importlib_loader.py",
            'import importlib\nimportlib.import_module(name="pickle")',
            ["pickle"],
        ),
        ("src/foundry/dunder_import.py", '__import__("marshal")', ["marshal"]),
        (
            "src/foundry/quiet.py",
            '"""A module naming yaml.load(t) and eval(t) in prose."""\nimport functools\n'
            "import importlib\nimport yaml\n"
            '_MENTION = "never call yaml.unsafe_load( or eval( here"\nyaml.safe_load(t)\n'
            'getattr(yaml, "safe_load")(t)\nfunctools.partial(yaml.safe_load)(t)\n'
            'importlib.import_module("json")\n__import__("json", fromlist=("pickle",))\n'
            'getattr(object(), "load")(t)\nyaml.dump(object())',
            [],
        ),
        ("scripts/quiet.sh", "printf '%s\\n' \"no eval here, and no exec either\"", []),
        ("scripts/talking.sh", "#!/usr/bin/env bash\n# never eval its arguments.", []),
    ],
)
def test_synthetic_findings(tmp_path: Path, relative: str, code: str, expected: list[str]) -> None:
    _write(tmp_path, relative, *code.splitlines())
    hits = _scan_tree(tmp_path)
    assert {hit.name for hit in hits} == set(expected), _format_hits(hits)


def test_the_scanner_offers_no_exemption() -> None:
    markers = ("allow", "exempt", "permit", "whitelist", "ignore", "skip", "bypass")
    names = [name for name in globals() if not name.startswith(("test_", "__"))]
    assert [name for name in names if any(m in name.casefold() for m in markers)] == []
    for name, function in ((n, globals()[n]) for n in names):
        if inspect.isfunction(function) and function.__module__ == __name__:
            parameters = inspect.signature(function).parameters
            found = [item for item in parameters if any(m in item.casefold() for m in markers)]
            assert found == [], f"{name} takes an exemption parameter: {found}"
