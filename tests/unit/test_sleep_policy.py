"""Both time policies: the 100 ms sleep cap over tests/ and examples/ (AAP 0.14.2), and no raw
wall-clock read over src/foundry outside the eight reviewed producers (AAP 0.14.3)."""

import ast
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Final, Literal, NamedTuple

import pytest

_SLEEP_CAP_SECONDS: Final[float] = 0.1
# Kept apart from its attribute, so this file never spells the forbidden call.
_TIME_MODULE: Final[str] = "time"
_SLEEP_ATTRIBUTE: Final[str] = "sleep"
# Helper names whose body may forward a computed delay instead of waiting (AAP 0.14.2's fakes).
_FAKE_SLEEP_FUNCTIONS: Final[frozenset[str]] = frozenset({"fake_sleep", "_fake_sleep"})
_SLEEP_ROOTS: Final[tuple[str, ...]] = ("tests", "examples")
_SOURCE_ROOT: Final[str] = "src/foundry"
_CACHE_DIRECTORY_NAMES: Final[frozenset[str]] = frozenset(
    {"__pycache__", ".git", ".venv", ".mypy_cache", ".pytest_cache", ".ruff_cache"}
)
# Every resolved callable that reads the wall clock, mapped to the spelling a report shows.
_WALL_CLOCK_LABELS: Final[Mapping[str, str]] = {
    "datetime.datetime.now": "datetime.now",
    "datetime.datetime.utcnow": "datetime.utcnow",
    "datetime.date.today": "date.today",
    "time.time": "time.time",
}
# The modules allowed to read the wall clock, each with its AAP clause; a widening shows in review.
_WALL_CLOCK_PRODUCERS: Final[frozenset[str]] = frozenset(
    {
        "src/foundry/core/ids.py",  # utc_now(): the tree's single permitted read (AAP 0.7.1)
        "src/foundry/execution/session.py",  # run started_at and finished_at (AAP 0.4.6, 0.4.7)
        "src/foundry/reporting/record.py",  # each record's started_at and finished_at (AAP 0.4.6)
        "src/foundry/reporting/store.py",  # the run document's own write timestamp (AAP 0.4.6)
        "src/foundry/obs/events.py",  # lifecycle event timestamps (AAP 0.7.1)
        "src/foundry/obs/artifacts.py",  # manifest created_at, per-artifact instant (AAP 0.4.6)
        "src/foundry/obs/manifest.py",  # manifest.json's created_at field (AAP 0.4.6)
        "src/foundry/cassette/store.py",  # a recorded cassette's recorded_at (AAP 0.4.6)
    }
)
# How many producers the plan sanctions; a wider exemption is a review, not an edit.
_REVIEWED_PRODUCER_COUNT: Final[int] = 8
# Floors rather than equalities, so ordinary growth needs no edit while a broken walk still fails.
_MINIMUM_SLEEP_FILES: Final[int] = 20
_MINIMUM_SOURCE_MODULES: Final[int] = 50
_SLEEP_CALL: Final[str] = f"{_TIME_MODULE}.{_SLEEP_ATTRIBUTE}"
_SLEEP_IMPORT: Final[str] = f"from {_TIME_MODULE} import {_SLEEP_ATTRIBUTE}"
# Each sleep shape as a module source, with the delay a report must name; () means it complies.
_SLEEP_SHAPES: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    (f"import {_TIME_MODULE}\n{_SLEEP_CALL}()", ()),
    (f'"""A docstring naming {_SLEEP_CALL}."""\n_MENTION = "never call it"', ()),
    (f"import {_TIME_MODULE}\n{_SLEEP_CALL}(0.1)", ()),
    (f"import {_TIME_MODULE}\n{_SLEEP_CALL}(-5)", ()),
    (f"import {_TIME_MODULE}\n{_SLEEP_CALL}(0.100001)", ("0.100001",)),
    (f"import {_TIME_MODULE}\n{_SLEEP_CALL}('0.01')", ("'0.01'",)),
    (f"import {_TIME_MODULE}\n{_SLEEP_CALL}(True)", ("True",)),
    (f"import {_TIME_MODULE}\n{_SLEEP_CALL}(seconds=0.5)", ("0.5",)),
    (f"import {_TIME_MODULE} as t\nt.{_SLEEP_ATTRIBUTE}(5)", ("5",)),
    (f"{_SLEEP_IMPORT}\n{_SLEEP_ATTRIBUTE}(5)", ("5",)),
    (f"{_SLEEP_IMPORT} as pause\npause(5)", ("5",)),
    (f"import {_TIME_MODULE}\n\n\ndef fake_sleep(seconds):\n    {_SLEEP_CALL}(seconds)", ()),
    (f"import {_TIME_MODULE}\n\n\nasync def _fake_sleep(s):\n    {_SLEEP_CALL}(s)", ()),
    (f"import {_TIME_MODULE}\n\n\ndef helper(s):\n    {_SLEEP_CALL}(s)", ("s",)),
    (
        f"import {_TIME_MODULE}\n\n\ndef fake_sleep(s):\n    def inner():\n        "
        f"{_SLEEP_CALL}(s)\n\n    return inner",
        ("s",),
    ),
    (f"import {_TIME_MODULE}\n\n\ndef fake_sleep(s):\n    {_SLEEP_CALL}(0.5)", ("0.5",)),
    ("class Clock:\n    def sleep(self, s):\n        return s\n\nClock().sleep(5)", ()),
)
# Each wall-clock shape, with the (name, form) pairs a report shows; () means it is not a read.
_CLOCK_SHAPES: Final[tuple[tuple[str, tuple[tuple[str, str], ...]], ...]] = (
    ("from datetime import datetime\nstamp = datetime.now()", (("datetime.now", "call"),)),
    ("from datetime import date\nstamp = date.today()", (("date.today", "call"),)),
    ("import datetime\nstamp = datetime.datetime.now()", (("datetime.now", "call"),)),
    ("import time\nstamp = time.time()", (("time.time", "call"),)),
    ("from time import time\nstamp = time()", (("time.time", "call"),)),
    ("from datetime import datetime\nreader = datetime.now", (("datetime.now", "reference"),)),
    ("from datetime import datetime\nx = datetime.utcnow()", (("datetime.utcnow", "call"),)),
    ("import time\nelapsed = time.monotonic()", ()),
    ("from datetime import datetime\nstamp = datetime(2024, 1, 1)", ()),
    ("from datetime import datetime as ck\nx = ck.now()", (("datetime.now", "call"),)),
)


class SleepHit(NamedTuple):
    path: str
    line: int
    argument: str


class ClockHit(NamedTuple):
    path: str
    line: int
    name: str
    form: Literal["call", "reference"]


def _write(root: Path, relative: str, *lines: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")


def _display(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _scanned_files(root: Path, relatives: Sequence[str]) -> tuple[Path, ...]:
    # A missing root yields nothing, so the walk is usable on a synthetic tree in tmp_path too.
    found = [
        path
        for relative in relatives
        for path in (root / relative).rglob("*.py")
        if path.is_file()
        and not path.is_symlink()
        and not _CACHE_DIRECTORY_NAMES.intersection(path.relative_to(root).parts[:-1])
    ]
    return tuple(sorted(found))


def _walk_scoped(
    node: ast.AST, functions: tuple[str, ...] = ()
) -> Iterator[tuple[ast.AST, tuple[str, ...]]]:
    # A tuple, so an iterator that outlives its frame cannot see a later sibling's name.
    yield node, functions
    nested = isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    for child in ast.iter_child_nodes(node):
        yield from _walk_scoped(child, (*functions, node.name) if nested else functions)


def _module_bindings(tree: ast.AST) -> tuple[Mapping[str, str], Mapping[str, str]]:
    # The first binding of a name wins, so a rebound alias stays out of reach.
    modules: dict[str, str] = {}
    callables: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                binding = alias.asname or alias.name.split(".", 1)[0]
                modules.setdefault(binding, alias.name if alias.asname else binding)
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module is not None:
            for alias in node.names:
                callables.setdefault(alias.asname or alias.name, f"{node.module}.{alias.name}")
    return modules, callables


def _resolve(
    node: ast.expr, modules: Mapping[str, str], callables: Mapping[str, str]
) -> str | None:
    # A name resolves through the binding maps and an attribute chain against its base.
    if isinstance(node, ast.Name):
        return callables.get(node.id) or modules.get(node.id)
    if isinstance(node, ast.Attribute):
        base = _resolve(node.value, modules, callables)
        return None if base is None else f"{base}.{node.attr}"
    return None


def _delay_seconds(node: ast.expr) -> float | None:
    # A signed literal counts as the literal it is; anything else states no number at all.
    if isinstance(node, ast.UnaryOp):
        if not isinstance(node.op, ast.USub | ast.UAdd):
            return None
        value, sign = node.operand, -1.0 if isinstance(node.op, ast.USub) else 1.0
    else:
        value, sign = node, 1.0
    if not isinstance(value, ast.Constant) or isinstance(value.value, bool):
        return None
    return sign * float(value.value) if isinstance(value.value, int | float) else None


def _sleep_argument(call: ast.Call, functions: tuple[str, ...]) -> str | None:
    # Zero arguments and a literal at or below the cap comply; a fake-sleep helper may take a delay.
    computed = not functions or functions[-1] not in _FAKE_SLEEP_FUNCTIONS
    if any(keyword.arg is None for keyword in call.keywords):
        return "<** expansion>" if computed else None
    arguments = [*call.args, *(item.value for item in call.keywords if item.arg is not None)]
    if not arguments:
        return None
    if len(arguments) > 1:
        return ", ".join(ast.unparse(item) for item in arguments) if computed else None
    delay = _delay_seconds(arguments[0])
    if delay is not None:
        return None if delay <= _SLEEP_CAP_SECONDS else ast.unparse(arguments[0])
    return ast.unparse(arguments[0]) if computed or isinstance(arguments[0], ast.Constant) else None


def _scan_sleep_source(source: str, display: str) -> tuple[SleepHit, ...]:
    tree = ast.parse(source, filename=display)
    modules, callables = _module_bindings(tree)
    sleepers = {name for name, target in callables.items() if target == _SLEEP_CALL}
    hits: list[SleepHit] = []
    for node, functions in _walk_scoped(tree):
        if not isinstance(node, ast.Call):
            continue
        callee = node.func
        owner = callee.value if isinstance(callee, ast.Attribute) else None
        call = isinstance(callee, ast.Attribute) and callee.attr == _SLEEP_ATTRIBUTE
        call = call and isinstance(owner, ast.Name) and modules.get(owner.id) == _TIME_MODULE
        imported = isinstance(callee, ast.Name) and callee.id in sleepers
        argument = _sleep_argument(node, functions) if call or imported else None
        if argument is not None:
            hits.append(SleepHit(display, node.lineno, argument))
    return tuple(sorted(dict.fromkeys(hits)))


def _scan_sleep_tree(root: Path) -> tuple[SleepHit, ...]:
    hits = [
        hit
        for path in _scanned_files(root, _SLEEP_ROOTS)
        for hit in _scan_sleep_source(path.read_text(encoding="utf-8"), _display(path, root))
    ]
    return tuple(sorted(hits))


def _sleep_callable_handoffs(root: Path) -> tuple[str, ...]:
    # The scan's silence on the real tree is evidence only if the shape it must not flag is there.
    files: list[str] = []
    for path in _scanned_files(root, _SLEEP_ROOTS):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=path.as_posix())
        keywords = [k.arg for n in ast.walk(tree) if isinstance(n, ast.Call) for k in n.keywords]
        if _SLEEP_ATTRIBUTE in keywords:
            files.append(_display(path, root))
    return tuple(files)


def _scan_clock_source(source: str, display: str) -> tuple[ClockHit, ...]:
    # A bare reference is reported alongside the call, so clock = datetime.now is a finding too.
    tree = ast.parse(source, filename=display)
    modules, callables = _module_bindings(tree)
    callees = {id(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
    hits: list[ClockHit] = []
    for node in ast.walk(tree):
        resolved = _resolve(node, modules, callables)
        if isinstance(node, ast.Name | ast.Attribute) and resolved in _WALL_CLOCK_LABELS:
            form: Literal["call", "reference"] = "call" if id(node) in callees else "reference"
            hits.append(ClockHit(display, node.lineno, _WALL_CLOCK_LABELS[resolved], form))
    return tuple(sorted(dict.fromkeys(hits)))


def _scan_clock_tree(root: Path) -> tuple[ClockHit, ...]:
    hits = [
        hit
        for path in _scanned_files(root, (_SOURCE_ROOT,))
        for hit in _scan_clock_source(path.read_text(encoding="utf-8"), _display(path, root))
    ]
    return tuple(sorted(hits))


def _unapproved(hits: Sequence[ClockHit]) -> tuple[ClockHit, ...]:
    return tuple(hit for hit in hits if hit.path not in _WALL_CLOCK_PRODUCERS)


def _format_sleep_hits(hits: Sequence[SleepHit]) -> str:
    lines = [f"  {hit.path}:{hit.line}: {hit.argument}" for hit in hits]
    return "\n".join([f"{len(hits)} sleep(s) longer than {_SLEEP_CAP_SECONDS}s:", *lines])


def _format_clock_hits(hits: Sequence[ClockHit]) -> str:
    head = f"{len(hits)} raw wall-clock read(s) outside the reviewed producers:"
    return "\n".join([head, *(f"  {h.path}:{h.line}: {h.name} ({h.form})" for h in hits)])


def test_both_policies_hold_on_the_real_tree(repo_root: Path) -> None:
    sleep_hits = _scan_sleep_tree(repo_root)
    assert sleep_hits == (), _format_sleep_hits(sleep_hits)
    clock_hits = _unapproved(_scan_clock_tree(repo_root))
    assert clock_hits == (), _format_clock_hits(clock_hits)
    tests_scanned = {_display(p, repo_root) for p in _scanned_files(repo_root, _SLEEP_ROOTS)}
    code_scanned = {_display(p, repo_root) for p in _scanned_files(repo_root, (_SOURCE_ROOT,))}
    assert {"tests/conftest.py", "examples/checkout/conftest.py"} <= tests_scanned
    assert {"src/foundry/core/ids.py", "src/foundry/obs/artifacts.py"} <= code_scanned
    assert {"src/foundry/reporting/store.py", "src/foundry/http/session.py"} <= code_scanned
    assert len(tests_scanned) >= _MINIMUM_SLEEP_FILES, f"the walk found only {len(tests_scanned)}"
    assert len(code_scanned) >= _MINIMUM_SOURCE_MODULES, f"only {len(code_scanned)} modules"
    assert not any(n.startswith(_SOURCE_ROOT + "/") for n in tests_scanned), sorted(tests_scanned)
    # A hand-off is on the tree, so that silence is evidence, and this module is no exemption.
    assert _sleep_callable_handoffs(repo_root), "no sleep= hand-off: the control would be vacuous"
    path = Path(__file__).resolve()
    source, display = path.read_text(encoding="utf-8"), f"tests/unit/{path.name}"
    assert (_scan_sleep_source(source, display), _scan_clock_source(source, display)) == ((), ())


def test_a_synthetic_tree_reports_exactly_its_offenders(tmp_path: Path) -> None:
    _write(tmp_path, "tests/unit/test_a.py", f"import {_TIME_MODULE}", f"{_SLEEP_CALL}(0.5)")
    _write(tmp_path, "examples/checkout/test_b.py", _SLEEP_IMPORT, f"{_SLEEP_ATTRIBUTE}(2)")
    _write(tmp_path, "tests/conftest.py", f"import {_TIME_MODULE}", f"{_SLEEP_CALL}(0.05)")
    hits = _scan_sleep_tree(tmp_path)
    expected = {("examples/checkout/test_b.py", 2), ("tests/unit/test_a.py", 2)}
    assert {(hit.path, hit.line) for hit in hits} == expected, _format_sleep_hits(hits)
    message = _format_sleep_hits(hits)
    assert message.startswith(f"2 sleep(s) longer than {_SLEEP_CAP_SECONDS}s"), message
    assert all(f"{hit.path}:{hit.line}: {hit.argument}" in message for hit in hits), message


@pytest.mark.parametrize(("source", "expected"), _SLEEP_SHAPES)
def test_every_sleep_shape(tmp_path: Path, source: str, expected: tuple[str, ...]) -> None:
    _write(tmp_path, "tests/unit/test_shapes.py", *source.splitlines())
    assert tuple(hit.argument for hit in _scan_sleep_tree(tmp_path)) == expected


def test_the_wall_clock_allowlist_is_eight_live_producers(repo_root: Path, tmp_path: Path) -> None:
    assert len(_WALL_CLOCK_PRODUCERS) == _REVIEWED_PRODUCER_COUNT
    for relative in sorted(_WALL_CLOCK_PRODUCERS):
        assert relative.startswith(f"{_SOURCE_ROOT}/") and relative.endswith(".py"), relative
        assert "*" not in relative and (repo_root / relative).is_file(), relative
    approved = _WALL_CLOCK_PRODUCERS & {hit.path for hit in _scan_clock_tree(repo_root)}
    assert approved, "the allowlist excuses no real read: it is stale"
    # The excuse covers the module it names and no other: the same read elsewhere is a finding.
    reader = ("from datetime import datetime", "moment = datetime.now()")
    _write(tmp_path, "src/foundry/core/ids.py", *reader)
    _write(tmp_path, "src/foundry/http/session.py", *reader)
    expected = (ClockHit("src/foundry/http/session.py", 2, "datetime.now", "call"),)
    assert _unapproved(_scan_clock_tree(tmp_path)) == expected, "an excuse spread past its module"


@pytest.mark.parametrize(("source", "expected"), _CLOCK_SHAPES)
def test_every_wall_clock_shape(
    tmp_path: Path, source: str, expected: tuple[tuple[str, str], ...]
) -> None:
    _write(tmp_path, "src/foundry/offending.py", *source.splitlines())
    hits = _scan_clock_tree(tmp_path)
    assert [(hit.name, hit.form) for hit in hits] == list(expected), _format_clock_hits(hits)
