"""The static evidence a ``REACHES`` declaration of AAP 0.14.1 is read against.

``tests/plugin/test_reachability.py`` holds the ledger that keeps AAP 0.14.1 criterion 13 true as
the surface grows: every published fixture, subcommand, data loader and report format has a test
that drives it through a real entry point, which AAP 0.14.3 states as a mechanism -- "no capability
that exists only in unit tests". The ledger reads each declaring module's ``REACHES`` tuple and
proves two things about it: that the union of the tuples covers the published surface, and that
every name in them exists in Foundry. Neither proves the third thing, which is the one that
matters -- that the module claiming an entry has a test that touches it. A tuple is a claim written
by the same author as the tests beside it, so an entry added next to a test that never uses the name
would satisfy the ledger with no run behind it. :func:`Evidence.of` reads one module's text and
returns what that module's own code traces, so the claim can be held against the code that has to
back it.

**What backs each kind.** A ``fixture:<name>`` is backed by the name appearing as a parameter of a
``test_`` function or of a fixture-decorated one -- the only way pytest resolves a fixture -- or as
a string handed to ``usefixtures``, ``getfixturevalue`` or ``indirect``, the three decorator and
request spellings that name a fixture without a parameter. A ``command:<name>`` is backed by the
name appearing as a string argument of a call, in the argv shape every CLI test in this tree uses:
``run_foundry("run", ...)``, ``_run_cli(capsys, "config", ...)`` and
``runner.invoke(group, ["report", "compare"])``. A ``loader:<ext>`` is backed by any code string
whose suffix is ``.<ext>``, which is a data file the module names and therefore loads. A
``format:<name>`` is backed by any code string with that format's suffix whose stem is the format's
stem or begins with it: ``results_base.json`` backs ``format:results.json``, while a mention in
prose or a file with another suffix does not.

**Why these rules and no stronger ones.** Each is the strongest trace available without running
anything. A report document is written by the run, so the test's own naming of it is the
observable, and a fixture request reaches pytest as the parameter name pytest itself reads.
Analysis here cannot show that a test passed -- that is the suite's job -- only that the declaring
module's code names the thing it claims to reach.

**An embedded child source is the module's own code.** Most of ``tests/plugin/`` drives pytest by
writing a project: a child test module is a string literal handed to ``makepyfile``, and the fixture
requests inside it happen in a real pytester session. That literal is therefore scanned on the same
footing as the module around it, or ``fixture:foundry_data`` -- reached only from the f-string child
of ``tests/plugin/test_data_collection_errors.py`` -- would read as unbacked. Both a plain string
and an f-string are read, the latter rebuilt from its constant parts with each formatted value
unparsed back to the expression that produced it.

**Docstrings are prose and are skipped.** A module that documents the surface in its docstring has
not tested it, and the docstring is the one string that is reliably prose in this tree -- several
carry parseable Python, a table of the five subcommand names among them. Every other non-docstring
literal that parses as Python is read as a child source; one that does not -- TOML, JSON, a
fragment, a string holding a null byte -- is passed over in silence, because the alternative is a
scanner that fails on the data its own subject happens to hold.

Nothing here imports, executes or clocks anything (AAP 0.14.3): a scanned module is read as text
and resolved through :mod:`ast`, so no sibling's import cost, side effect or failure can change a
verdict.
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
"""The calls that name a fixture in a string rather than in a signature.

``@pytest.mark.usefixtures("foundry_http")``, ``request.getfixturevalue("foundry_db")`` and
``@pytest.mark.parametrize(..., indirect=["foundry_data"])`` are the three ways a fixture reaches a
test without appearing as one of its parameters, so all three are read as fixture requests.
"""

_SEQUENCE_LITERALS: Final[tuple[type[ast.expr], ...]] = (ast.List, ast.Tuple, ast.Set)
"""The literal containers a call argument may hold its strings in: ``["report", "compare"]``."""

_FIXTURE_MARKER: Final[str] = "fixture"
"""The trailing name of the decorator that turns a function into a fixture provider."""

_TEST_PREFIX: Final[str] = "test_"
"""The prefix of the functions pytest calls with the fixtures of a test resolved."""

_DEFINITION_NODES: Final[tuple[type[ast.AST], ...]] = (
    ast.Module,
    ast.ClassDef,
    ast.FunctionDef,
    ast.AsyncFunctionDef,
)
"""The nodes that own a body, and so may open one with a docstring."""

_FUNCTION_NODES: Final[tuple[type[ast.AST], ...]] = (ast.FunctionDef, ast.AsyncFunctionDef)
"""The two nodes that carry a signature, which is where a fixture request lives."""


def parsed_sources(source: str) -> tuple[ast.Module, ...]:
    """Return the module's own tree followed by one tree per child source the module embeds.

    Args:
        source: the module's text, never its imported object.

    Returns:
        The module's tree first, then one tree for every non-docstring string literal whose text
        parses as Python, in the order :func:`ast.walk` reaches them. A literal that is not Python
        -- TOML, JSON, a fragment -- contributes no tree and is passed over without a word.

    Raises:
        SyntaxError: ``source`` itself is not Python. That is the caller's own module, which cannot
            be skipped in silence the way an embedded fragment can.
    """
    tree = ast.parse(source)
    docstrings = _docstring_value_ids(tree)
    trees = [tree]
    for node in ast.walk(tree):
        if id(node) in docstrings:
            continue
        text = _literal_text(node)
        if text is None:
            continue
        try:
            trees.append(ast.parse(text))
        except (SyntaxError, ValueError):
            continue
    return tuple(trees)


def _docstring_value_ids(tree: ast.Module) -> frozenset[int]:
    """Return the ``id`` of the constant holding every docstring of ``tree``.

    A docstring is the first statement of a module's, class's or function's body when that
    statement is an expression holding a string. Identity is the key rather than the value, because
    two docstrings may be spelled alike and :func:`ast.walk` yields the very node objects collected
    here.
    """
    found: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, _DEFINITION_NODES):
            continue
        first = node.body[0] if node.body else None
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            found.add(id(first.value))
    return frozenset(found)


def _literal_text(node: ast.AST) -> str | None:
    """Return the text ``node`` holds as a string, or ``None`` when it holds none.

    A plain string literal is returned as it is. An f-string is rebuilt from its constant parts,
    with each formatted value unparsed back to the expression that produced it, so the child module
    it spells can still be parsed: ``f'DATA_FILE = "{_NAME}"'`` reads as ``DATA_FILE = "_NAME"``, a
    valid assignment. The conversion and format spec of a formatted value only shape its text and
    are dropped with it.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if not isinstance(node, ast.JoinedStr):
        return None
    parts: list[str] = []
    for part in node.values:
        if isinstance(part, ast.Constant) and isinstance(part.value, str):
            parts.append(part.value)
        elif isinstance(part, ast.FormattedValue):
            parts.append(ast.unparse(part.value))
        else:
            return None
    return "".join(parts)


def _trailing_name(node: ast.expr) -> str:
    """Return the name a call or decorator is spelled with: ``pytest.mark.usefixtures`` is that."""
    if isinstance(node, ast.Call):
        return _trailing_name(node.func)
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _string_arguments(call: ast.Call) -> frozenset[str]:
    """Return every string ``call`` is handed, directly or as an element of a literal container."""
    found: set[str] = set()
    for argument in (*call.args, *(keyword.value for keyword in call.keywords)):
        candidates = argument.elts if isinstance(argument, _SEQUENCE_LITERALS) else (argument,)
        for element in candidates:
            if isinstance(element, ast.Constant) and isinstance(element.value, str):
                found.add(element.value)
    return frozenset(found)


def _parameters(node: ast.FunctionDef | ast.AsyncFunctionDef) -> frozenset[str]:
    """Return every parameter name of ``node``'s signature, positional and keyword-only alike."""
    arguments = node.args
    return frozenset(
        argument.arg
        for argument in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs)
    )


def _is_fixture(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Return whether ``node`` is decorated as a fixture provider."""
    return any(_trailing_name(decorator) == _FIXTURE_MARKER for decorator in node.decorator_list)


def _facts(tree: ast.Module) -> tuple[frozenset[str], frozenset[str], frozenset[str]]:
    """Return the fixtures, command strings and file strings one tree traces, in that order."""
    docstrings = _docstring_value_ids(tree)
    fixtures: set[str] = set()
    commands: set[str] = set()
    files: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, _FUNCTION_NODES) and (
            node.name.startswith(_TEST_PREFIX) or _is_fixture(node)
        ):
            fixtures |= _parameters(node)
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
    """What one module's own code traces of the surface that module declares.

    The three sets are unions over everything the module holds: its own tree and every child source
    it embeds, merged by :meth:`of`. A declaration is backed when any part of that text traces it.
    """

    fixtures: frozenset[str]
    commands: frozenset[str]
    files: frozenset[str]

    @classmethod
    def of(cls, source: str) -> Evidence:
        """Return the union of the facts every source ``source`` holds makes available."""
        fixtures: set[str] = set()
        commands: set[str] = set()
        files: set[str] = set()
        for tree in parsed_sources(source):
            tree_fixtures, tree_commands, tree_files = _facts(tree)
            fixtures |= tree_fixtures
            commands |= tree_commands
            files |= tree_files
        return cls(
            fixtures=frozenset(fixtures), commands=frozenset(commands), files=frozenset(files)
        )

    def backs(self, kind: str, name: str) -> bool:
        """Return whether this evidence backs the ``<kind>:<name>`` declaration it is asked about.

        An unrecognised kind returns ``False``: the ledger validates the declaration's vocabulary
        before it asks, and a scanner that answered ``True`` to a kind it cannot read would back a
        claim nobody checked.
        """
        if kind == "fixture":
            return name in self.fixtures
        if kind == "command":
            return name in self.commands
        if kind == "loader":
            return any(
                pathlib.PurePosixPath(candidate).suffix == f".{name}" for candidate in self.files
            )
        if kind == "format":
            return self._names_a_document(name)
        return False

    def _names_a_document(self, name: str) -> bool:
        """Return whether a file this evidence holds is the report document ``name`` names.

        The suffix has to match exactly, so a document in another format never backs this one, and
        the stem has to be the format's own or begin with it and an underscore, which is how a test
        names the several documents it builds -- ``results_base.json``, ``results_new.json``.
        """
        wanted = pathlib.PurePosixPath(name)
        for candidate in self.files:
            path = pathlib.PurePosixPath(candidate)
            if path.suffix != wanted.suffix:
                continue
            if path.stem == wanted.stem or path.stem.startswith(f"{wanted.stem}_"):
                return True
        return False
