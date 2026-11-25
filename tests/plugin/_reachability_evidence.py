"""What a ``REACHES`` declaration (AAP 0.14.1) is read against: one module's own code, as text.
``Evidence.of`` returns the fixtures, call arguments and files a module traces, and a docstring is
prose: nothing here imports, executes or clocks a scanned module.
"""

from __future__ import annotations

import ast
import pathlib
from dataclasses import dataclass
from typing import Final

__all__: Final[tuple[str, ...]] = ("Evidence", "parsed_sources")

_FIXTURE_REQUEST_CALLS: Final[frozenset[str]] = frozenset(
    {"getfixturevalue", "indirect", "usefixtures"}
)
_SEQUENCE_LITERALS: Final[tuple[type[ast.expr], ...]] = (ast.List, ast.Tuple, ast.Set)
_DEFINITION_NODES: Final[tuple[type[ast.AST], ...]] = (
    ast.Module,
    ast.ClassDef,
    ast.FunctionDef,
    ast.AsyncFunctionDef,
)
_FUNCTION_NODES: Final[tuple[type[ast.AST], ...]] = (ast.FunctionDef, ast.AsyncFunctionDef)


def parsed_sources(source: str) -> tuple[ast.Module, ...]:
    # A literal that is not Python (TOML, JSON, a fragment) contributes no tree and is passed over.
    tree = ast.parse(source)
    docstrings = _docstring_value_ids(tree)
    trees = [tree]
    for node in ast.walk(tree):
        if id(node) in docstrings or (text := _literal_text(node)) is None:
            continue
        try:
            trees.append(ast.parse(text))
        except (SyntaxError, ValueError):
            continue
    return tuple(trees)


def _docstring_value_ids(tree: ast.Module) -> frozenset[int]:
    # Identity, not value: two docstrings may be spelled alike, and ast.walk yields these nodes.
    firsts = [n.body[0] for n in ast.walk(tree) if isinstance(n, _DEFINITION_NODES) and n.body]
    return frozenset(
        id(first.value)
        for first in firsts
        if isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
    )


def _literal_text(node: ast.AST) -> str | None:
    # An f-string is rebuilt with each value unparsed back to its expression, so its child parses.
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if not isinstance(node, ast.JoinedStr):
        return None
    parts: list[str] = []
    for part in node.values:
        if isinstance(part, ast.FormattedValue):
            parts.append(ast.unparse(part.value))
        elif isinstance(part, ast.Constant) and isinstance(part.value, str):
            parts.append(part.value)
        else:
            return None
    return "".join(parts)


def _trailing_name(node: ast.expr) -> str:
    while isinstance(node, ast.Call):
        node = node.func
    return node.attr if isinstance(node, ast.Attribute) else getattr(node, "id", "")


def _string_arguments(call: ast.Call) -> frozenset[str]:
    found: set[str] = set()
    for argument in (*call.args, *(keyword.value for keyword in call.keywords)):
        candidates = argument.elts if isinstance(argument, _SEQUENCE_LITERALS) else (argument,)
        for element in candidates:
            if isinstance(element, ast.Constant) and isinstance(element.value, str):
                found.add(element.value)
    return frozenset(found)


def _requested(node: ast.FunctionDef | ast.AsyncFunctionDef) -> frozenset[str]:
    provider = any(_trailing_name(d) == "fixture" for d in node.decorator_list)
    if not (node.name.startswith("test_") or provider):
        return frozenset()
    names = (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
    return frozenset(argument.arg for argument in names)


def _facts(tree: ast.Module) -> tuple[frozenset[str], frozenset[str], frozenset[str]]:
    docstrings = _docstring_value_ids(tree)
    fixtures, commands, files = set(), set(), set()
    for node in ast.walk(tree):
        if isinstance(node, _FUNCTION_NODES):
            fixtures |= _requested(node)
        elif isinstance(node, ast.Call):
            arguments = _string_arguments(node)
            if _trailing_name(node) in _FIXTURE_REQUEST_CALLS:
                fixtures |= arguments
            commands |= arguments
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
            and pathlib.PurePosixPath(node.value).suffix
        ):
            files.add(node.value)
    return frozenset(fixtures), frozenset(commands), frozenset(files)


@dataclass(frozen=True, slots=True)
class Evidence:
    fixtures: frozenset[str]
    commands: frozenset[str]
    files: frozenset[str]

    @classmethod
    def of(cls, source: str) -> Evidence:
        totals: tuple[set[str], set[str], set[str]] = (set(), set(), set())
        for tree in parsed_sources(source):
            for total, facts in zip(totals, _facts(tree), strict=True):
                total |= facts
        return cls(*(frozenset(total) for total in totals))

    def backs(self, kind: str, name: str) -> bool:
        # A kind this scan cannot read answers False: a check fails closed rather than back a claim.
        if kind == "fixture":
            return name in self.fixtures
        if kind == "command":
            return name in self.commands
        if kind == "loader":
            return any(pathlib.PurePosixPath(c).suffix == f".{name}" for c in self.files)
        if kind != "format":
            return False
        # A stem that is the format's own or begins with it and an underscore is that document:
        # results_base.json backs format:results.json.
        wanted = pathlib.PurePosixPath(name)
        return any(
            path.suffix == wanted.suffix
            and (path.stem == wanted.stem or path.stem.startswith(f"{wanted.stem}_"))
            for path in map(pathlib.PurePosixPath, self.files)
        )
