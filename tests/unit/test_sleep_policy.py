"""The tree-wide proof that nothing waits on real time and nothing reads a raw clock.

**The sleep cap.** AAP 0.14.2 permits at most 100 ms per sleep inside ``tests/`` and ``examples/``,
and AAP 0.14.3 makes "no sleeps beyond 100 ms" a mechanism rather than a promise: a suite that waits
on real time is slow, flaky under ``-n auto`` and unable to prove the wall-clock ratio the sharding
criterion asserts. A computed delay is refused too, because nothing can bound it; the one exemption
is the body of a fake-sleep helper, which is how a retry or poll test hands a recording callable to
the code under test instead of spending the time.

**The raw wall clock.** AAP 0.14.3 also rules out wall-clock, locale and filesystem-case
dependence, and ``src/foundry`` keeps that promise by routing every timestamp through
``foundry.core.ids.utc_now``. A second raw read would make a run's output depend on when it ran, so
the producers that legitimately stamp a value are listed by hand with the AAP clause that requires
each one: widening the exemption edits a constant a reviewer reads, never a pattern.

Both scans are structural. Every module is parsed with :mod:`ast` and its imports, aliases and
enclosing functions resolved, so a docstring naming the forbidden call is not a finding while the
call itself is -- which matters, because this module lives inside the tree it scans. A ``sleep=``
keyword handed to a poller is a value reference, never a call: ``eventually`` and ``foundry_poll``
are built on that hand-off (AAP 0.4.3). Computed arguments, aliases rebound after their first
binding and code inside a string stay out of reach, as in ``tests/unit/test_no_unsafe_parsing.py``.

Every hit is reported with its path, line and offending argument, sorted, so two runs read
identically. Synthetic trees are built in ``tmp_path`` and the repository is only read.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

import pytest

_SLEEP_CAP_SECONDS: Final[float] = 0.1
"""The longest single sleep AAP 0.14.2 permits, in seconds."""

_TIME_MODULE: Final[str] = "time"
"""The module whose sleeper is capped; kept apart from its attribute so this file never spells the
forbidden call literally, which is what lets a plain grep over the source stay meaningful."""

_SLEEP_ATTRIBUTE: Final[str] = "sleep"
"""The attribute of :data:`_TIME_MODULE` that spends real time."""

_FAKE_SLEEP_FUNCTIONS: Final[frozenset[str]] = frozenset({"fake_sleep", "_fake_sleep"})
"""Helper names whose body may forward a computed delay instead of waiting.

A fake sleeper records the delay or advances a frozen clock, so it spends no real time however large
the value it is asked to wait (AAP 0.14.2's "except injected fakes").
"""

_SLEEP_ROOTS: Final[tuple[str, ...]] = ("tests", "examples")
"""Roots under the cap: the self-test suite and the example project (AAP 0.14.2)."""

_SOURCE_ROOT: Final[str] = "src/foundry"
"""The package whose wall-clock reads are patrolled (AAP 0.4.8, AAP 0.14.3)."""

_CACHE_DIRECTORY_NAMES: Final[frozenset[str]] = frozenset(
    {"__pycache__", ".git", ".venv", ".mypy_cache", ".pytest_cache", ".ruff_cache"}
)
"""Directories holding no first-party source, so neither walk descends into them."""

_WALL_CLOCK_LABELS: Final[Mapping[str, str]] = {
    "datetime.datetime.now": "datetime.now",
    "datetime.datetime.utcnow": "datetime.utcnow",
    "datetime.date.today": "date.today",
    "time.time": "time.time",
}
"""Every resolved callable that reads the wall clock, mapped to the spelling a report shows.

The keys are resolved through each module's own imports, so ``datetime.now`` behind
``from datetime import datetime``, ``import datetime as dt`` and ``from time import time`` all land
on one of these four entries.
"""

_WALL_CLOCK_PRODUCERS: Final[frozenset[str]] = frozenset(
    {
        # utc_now(): the tree's single permitted read, which every other producer calls
        # (AAP 0.7.1, AAP 0.14.3).
        "src/foundry/core/ids.py",
        # Run bootstrap: the session's started_at and finished_at (AAP 0.4.6, AAP 0.4.7).
        "src/foundry/execution/session.py",
        # Result records carry started_at and finished_at per test (AAP 0.4.6).
        "src/foundry/reporting/record.py",
        # The run document's own write timestamp (AAP 0.4.6 "results.json").
        "src/foundry/reporting/store.py",
        # Structured lifecycle events record when they happened (AAP 0.7.1 "Observability").
        "src/foundry/obs/events.py",
        # Artifact manifests carry created_at and each artifact its instant (AAP 0.4.6).
        "src/foundry/obs/artifacts.py",
        # The manifest document's created_at field is produced here (AAP 0.4.6 "manifest.json").
        "src/foundry/obs/manifest.py",
        # A recorded cassette stamps recorded_at (AAP 0.4.6 "Cassette document").
        "src/foundry/cassette/store.py",
    }
)
"""The modules allowed to read the wall clock, each with the AAP clause that requires it.

A reviewable constant, never a glob: a new producer is added here with its clause and shows up in
the diff, and the allowlist test pins the count, so an unnoticed widening fails the suite.
"""

_REVIEWED_PRODUCER_COUNT: Final[int] = 8
"""How many producers the plan sanctions; a wider exemption is a review, not an edit."""

_MINIMUM_SLEEP_FILES: Final[int] = 20
"""A floor on the files the under-cap walk visits, so a broken walk cannot pass vacuously."""

_MINIMUM_SOURCE_MODULES: Final[int] = 50
"""A floor on the modules the wall-clock walk visits, for the same reason.

A floor rather than an equality: the package grows, and an exact count would fail on every module a
later change adds without telling anyone anything about this policy.
"""

_SLEEP_CALL: Final[str] = f"{_TIME_MODULE}.{_SLEEP_ATTRIBUTE}"
"""The forbidden call as source text, assembled from the two constants above."""

_SLEEP_IMPORT: Final[str] = f"from {_TIME_MODULE} import {_SLEEP_ATTRIBUTE}"
"""The ``from`` form the scanner must follow through its per-module binding map."""


@dataclass(frozen=True, order=True)
class SleepHit:
    """One sleep that breaks the cap: where it is and the argument that breaks it.

    The field order is the sort order -- path, then line, then argument -- so a report of several
    hits reads the same way on every run.
    """

    path: str
    line: int
    argument: str


@dataclass(frozen=True, order=True)
class ClockHit:
    """One raw wall-clock read: where it is, which callable, and whether it is called or passed.

    ``form`` distinguishes a call from a bare reference such as ``clock = datetime.now``, which
    reads the clock just as surely once someone invokes it.
    """

    path: str
    line: int
    name: str
    form: Literal["call", "reference"]


def _write(root: Path, relative: str, *lines: str) -> Path:
    """Write ``lines`` to ``root/relative``, creating parent directories, and return the path."""
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    return path


def _display(path: Path, root: Path) -> str:
    """Return ``path`` relative to ``root`` as a POSIX string, the form every hit is reported in."""
    return path.relative_to(root).as_posix()


def _in_cache_directory(path: Path, root: Path) -> bool:
    """Return True when a directory between ``root`` and ``path`` holds no first-party source."""
    parents = path.relative_to(root).parts[:-1]
    return any(part in _CACHE_DIRECTORY_NAMES for part in parents)


def _scanned_files(root: Path, relatives: Sequence[str]) -> tuple[Path, ...]:
    """Return every Python module under ``relatives``, sorted, with cache directories skipped.

    A path is taken only when it is a real file inside ``root``. A walk that followed a link out of
    the repository would report on -- and read -- a file the project does not own, so a path that
    resolves elsewhere and a symlink are both dropped before anything is opened.
    """
    files: list[Path] = []
    for relative in relatives:
        base = root / relative
        if not base.is_dir():
            continue
        for path in base.rglob("*.py"):
            if not path.is_file() or path.is_symlink() or not path.is_relative_to(root):
                continue
            if not _in_cache_directory(path, root):
                files.append(path)
    return tuple(sorted(files))


def _walk_scoped(
    node: ast.AST, functions: tuple[str, ...] = ()
) -> Iterator[tuple[ast.AST, tuple[str, ...]]]:
    """Yield ``node`` and every descendant with the tuple of function names enclosing it.

    The stack answers the sleep scanner's one contextual question: is this call inside a fake-sleep
    helper? It is a tuple, so an iterator that outlives its frame cannot see a later sibling's name.
    """
    yield node, functions
    inner = functions
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
        inner = (*functions, node.name)
    for child in ast.iter_child_nodes(node):
        yield from _walk_scoped(child, inner)


def _module_bindings(tree: ast.AST) -> tuple[Mapping[str, str], Mapping[str, str]]:
    """Return the names ``tree`` binds to imported modules and to imported callables.

    ``import time as t`` records ``t -> time`` and ``from time import sleep as pause`` records
    ``pause -> time.sleep``. Only a name's first binding is recorded, so a rebound alias stays out
    of reach, as the module docstring says.
    """
    modules: dict[str, str] = {}
    callables: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                binding = alias.asname or alias.name.split(".", 1)[0]
                modules.setdefault(binding, alias.name if alias.asname else binding)
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module is not None:
            for alias in node.names:
                if alias.name != "*":
                    callables.setdefault(alias.asname or alias.name, f"{node.module}.{alias.name}")
    return modules, callables


def _resolve(
    node: ast.expr, modules: Mapping[str, str], callables: Mapping[str, str]
) -> str | None:
    """Return the dotted callable ``node`` denotes, or ``None`` for anything else.

    A name resolves through the binding maps and an attribute chain against its base, so
    ``dt.datetime.now`` behind ``import datetime as dt`` reaches ``datetime.datetime.now``, while a
    computed attribute name resolves to ``None`` and stays out of the report.
    """
    if isinstance(node, ast.Name):
        return callables.get(node.id) or modules.get(node.id)
    if isinstance(node, ast.Attribute):
        base = _resolve(node.value, modules, callables)
        return None if base is None else f"{base}.{node.attr}"
    return None


def _computed_argument(source: str, functions: Sequence[str]) -> str | None:
    """Return ``source`` unless a fake-sleep helper encloses the call, where a delay is recorded."""
    return None if functions and functions[-1] in _FAKE_SLEEP_FUNCTIONS else source


def _delay_seconds(node: ast.expr) -> float | None:
    """Return the seconds a literal delay states, or ``None`` when it states no number.

    A signed numeric literal counts as the literal it is -- ``-5`` waits for nothing -- while a
    string constant, a boolean and a computed expression state no number at all.
    """
    if isinstance(node, ast.Constant):
        value = node.value
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None
        return float(value)
    if (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, ast.USub | ast.UAdd)
        and isinstance(node.operand, ast.Constant)
    ):
        inner = node.operand.value
        if isinstance(inner, bool) or not isinstance(inner, int | float):
            return None
        return -float(inner) if isinstance(node.op, ast.USub) else float(inner)
    return None


def _sleep_argument(call: ast.Call, functions: Sequence[str]) -> str | None:
    """Return the source of the argument that breaks the cap, or ``None`` when the call complies.

    Zero arguments comply, as does a literal delay at or below the cap. A literal above the cap and
    a literal that states no number at all are findings, and so is a computed argument -- except
    inside a fake-sleep helper, whose body is exactly where a computed delay belongs.
    """
    if any(keyword.arg is None for keyword in call.keywords):
        return _computed_argument("<** expansion>", functions)
    arguments = [
        *call.args,
        *(keyword.value for keyword in call.keywords if keyword.arg is not None),
    ]
    if not arguments:
        return None
    if len(arguments) > 1:
        return _computed_argument(", ".join(ast.unparse(item) for item in arguments), functions)
    argument = arguments[0]
    delay = _delay_seconds(argument)
    if delay is not None:
        return None if delay <= _SLEEP_CAP_SECONDS else ast.unparse(argument)
    if isinstance(argument, ast.Constant):
        return ast.unparse(argument)
    return _computed_argument(ast.unparse(argument), functions)


def _scan_sleep_source(source: str, display: str) -> tuple[SleepHit, ...]:
    """Return every sleep in ``source`` that breaks the cap, sorted and de-duplicated."""
    tree = ast.parse(source, filename=display)
    modules, callables = _module_bindings(tree)
    sleepers = {
        name for name, target in callables.items() if target == f"{_TIME_MODULE}.{_SLEEP_ATTRIBUTE}"
    }
    hits: list[SleepHit] = []
    for node, functions in _walk_scoped(tree):
        if not isinstance(node, ast.Call):
            continue
        callee = node.func
        module_call = (
            isinstance(callee, ast.Attribute)
            and callee.attr == _SLEEP_ATTRIBUTE
            and isinstance(callee.value, ast.Name)
            and modules.get(callee.value.id) == _TIME_MODULE
        )
        imported_call = isinstance(callee, ast.Name) and callee.id in sleepers
        if not (module_call or imported_call):
            continue
        argument = _sleep_argument(node, functions)
        if argument is not None:
            hits.append(SleepHit(display, node.lineno, argument))
    return tuple(sorted(dict.fromkeys(hits)))


def _scan_sleep_tree(root: Path) -> tuple[SleepHit, ...]:
    """Scan every module under :data:`_SLEEP_ROOTS` from ``root`` and return all hits, sorted."""
    hits: list[SleepHit] = []
    for path in _scanned_files(root, _SLEEP_ROOTS):
        source = path.read_text(encoding="utf-8")
        hits.extend(_scan_sleep_source(source, _display(path, root)))
    return tuple(sorted(hits))


def _sleep_callable_handoffs(root: Path) -> tuple[str, ...]:
    """Return the files under :data:`_SLEEP_ROOTS` that pass a callable as a ``sleep=`` keyword.

    This is what makes the negative control meaningful on the real tree: the scan's silence there is
    only evidence if the shape it must not flag is actually present in it.
    """
    files: list[str] = []
    for path in _scanned_files(root, _SLEEP_ROOTS):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=path.as_posix())
        if any(
            isinstance(node, ast.Call)
            and any(keyword.arg == _SLEEP_ATTRIBUTE for keyword in node.keywords)
            for node in ast.walk(tree)
        ):
            files.append(_display(path, root))
    return tuple(files)


def _scan_clock_source(source: str, display: str) -> tuple[ClockHit, ...]:
    """Return every raw wall-clock read in ``source``, sorted and de-duplicated.

    A call is found through the callee; a bare reference is an attribute that resolves to one of the
    patrolled callables without being some call's callee, so ``clock = datetime.now`` is reported
    alongside ``datetime.now()``.
    """
    tree = ast.parse(source, filename=display)
    modules, callables = _module_bindings(tree)
    callees = {id(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
    hits: list[ClockHit] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Name | ast.Attribute):
            continue
        resolved = _resolve(node, modules, callables)
        if resolved not in _WALL_CLOCK_LABELS:
            continue
        form: Literal["call", "reference"] = "call" if id(node) in callees else "reference"
        hits.append(ClockHit(display, node.lineno, _WALL_CLOCK_LABELS[resolved], form))
    return tuple(sorted(dict.fromkeys(hits)))


def _scan_clock_tree(root: Path) -> tuple[ClockHit, ...]:
    """Scan every module under :data:`_SOURCE_ROOT` from ``root`` and return all hits, sorted."""
    hits: list[ClockHit] = []
    for path in _scanned_files(root, (_SOURCE_ROOT,)):
        source = path.read_text(encoding="utf-8")
        hits.extend(_scan_clock_source(source, _display(path, root)))
    return tuple(sorted(hits))


def _unapproved(hits: Sequence[ClockHit]) -> tuple[ClockHit, ...]:
    """Return the hits that stand outside :data:`_WALL_CLOCK_PRODUCERS`."""
    return tuple(hit for hit in hits if hit.path not in _WALL_CLOCK_PRODUCERS)


def _format_sleep_hits(hits: Sequence[SleepHit]) -> str:
    """Render ``hits`` as the failure message: a count, then one ``path:line: argument`` line."""
    lines = [f"  {hit.path}:{hit.line}: {hit.argument}" for hit in hits]
    return "\n".join([f"{len(hits)} sleep(s) longer than {_SLEEP_CAP_SECONDS}s:", *lines])


def _format_clock_hits(hits: Sequence[ClockHit]) -> str:
    """Render ``hits`` as the failure message, naming each read and how it is used."""
    lines = [f"  {hit.path}:{hit.line}: {hit.name} ({hit.form})" for hit in hits]
    return "\n".join(
        [f"{len(hits)} raw wall-clock read(s) outside the reviewed producers:", *lines]
    )


def test_the_test_and_example_trees_stay_under_the_sleep_cap(repo_root: Path) -> None:
    """No test and no example sleeps longer than the cap (AAP 0.14.2, AAP 0.14.3).

    One failure names every offending call, not the first: a review fixes the tree in one pass.
    """
    hits = _scan_sleep_tree(repo_root)
    assert hits == (), _format_sleep_hits(hits)


def test_the_sleep_scan_reaches_the_known_anchors(repo_root: Path) -> None:
    """The walk visits the suite it polices, so a clean scan above is evidence and not emptiness."""
    scanned = {_display(path, repo_root) for path in _scanned_files(repo_root, _SLEEP_ROOTS)}
    for anchor in ("tests/conftest.py", "examples/checkout/conftest.py"):
        assert anchor in scanned, f"{anchor} is not scanned: {sorted(scanned)}"
    assert len(scanned) >= _MINIMUM_SLEEP_FILES, f"the walk found only {len(scanned)} files"
    assert not any(name.startswith(f"{_SOURCE_ROOT}/") for name in scanned), sorted(scanned)


def test_the_tree_hands_a_sleep_callable_over_without_calling_it(repo_root: Path) -> None:
    """The shape the scan must stay silent about is present in the tree, not hypothetical.

    ``eventually`` and ``foundry_poll`` take their sleeper as a ``sleep=`` keyword (AAP 0.4.3), and
    a scan that flagged the hand-off would refuse the mechanism the cap is built on.
    """
    handoffs = _sleep_callable_handoffs(repo_root)
    assert handoffs, "no sleep= hand-off under tests/ or examples/; the negative control is vacuous"


def test_this_module_is_inside_the_scan_and_clean() -> None:
    """This file is under ``tests/``, so it obeys both policies it enforces."""
    path = Path(__file__).resolve()
    source = path.read_text(encoding="utf-8")
    display = f"tests/unit/{path.name}"
    assert _scan_sleep_source(source, display) == ()
    assert _scan_clock_source(source, display) == ()


def test_a_synthetic_tree_reports_exactly_the_non_compliant_sleep(tmp_path: Path) -> None:
    """A compliant and a non-compliant sleep in one tree yield the non-compliant one alone."""
    _write(tmp_path, "tests/unit/test_brisk.py", f"import {_TIME_MODULE}", f"{_SLEEP_CALL}(0.01)")
    _write(
        tmp_path, "examples/checkout/test_slow.py", f"import {_TIME_MODULE}", f"{_SLEEP_CALL}(0.5)"
    )

    hits = _scan_sleep_tree(tmp_path)

    assert {(hit.path, hit.line) for hit in hits} == {("examples/checkout/test_slow.py", 2)}
    assert {hit.argument for hit in hits} == {"0.5"}, _format_sleep_hits(hits)


def test_a_tree_of_compliant_sleeps_reports_nothing(tmp_path: Path) -> None:
    """A short sleep and a zero-length one are both compliant, whichever import form states them."""
    _write(tmp_path, "tests/unit/test_brisk.py", f"import {_TIME_MODULE}", f"{_SLEEP_CALL}(0.01)")
    _write(tmp_path, "examples/checkout/test_instant.py", _SLEEP_IMPORT, f"{_SLEEP_ATTRIBUTE}(0)")

    assert _scan_sleep_tree(tmp_path) == ()


def test_the_sleep_scan_reports_every_offender(tmp_path: Path) -> None:
    """Three files, two offenders: both are reported, and the message names each one."""
    _write(tmp_path, "tests/unit/test_a.py", f"import {_TIME_MODULE}", f"{_SLEEP_CALL}(0.5)")
    _write(tmp_path, "examples/checkout/test_b.py", _SLEEP_IMPORT, f"{_SLEEP_ATTRIBUTE}(2)")
    _write(tmp_path, "tests/conftest.py", f"import {_TIME_MODULE}", f"{_SLEEP_CALL}(0.05)")

    hits = _scan_sleep_tree(tmp_path)

    assert {(hit.path, hit.line) for hit in hits} == {
        ("examples/checkout/test_b.py", 2),
        ("tests/unit/test_a.py", 2),
    }
    message = _format_sleep_hits(hits)
    assert message.startswith(f"2 sleep(s) longer than {_SLEEP_CAP_SECONDS}s")
    for hit in hits:
        assert f"{hit.path}:{hit.line}: {hit.argument}" in message, message


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (f"import {_TIME_MODULE}\n{_SLEEP_CALL}()", ()),
        (f"import {_TIME_MODULE}\n{_SLEEP_CALL}(0)", ()),
        (f"import {_TIME_MODULE}\n{_SLEEP_CALL}(0.1)", ()),
        (f"import {_TIME_MODULE}\n{_SLEEP_CALL}(-5)", ()),
        (f"import {_TIME_MODULE}\n{_SLEEP_CALL}(0.100001)", ("0.100001",)),
        (f"import {_TIME_MODULE}\n{_SLEEP_CALL}(1)", ("1",)),
        (f"import {_TIME_MODULE}\n{_SLEEP_CALL}('0.01')", ("'0.01'",)),
        (f"import {_TIME_MODULE}\n{_SLEEP_CALL}(True)", ("True",)),
        (f"import {_TIME_MODULE}\n{_SLEEP_CALL}(seconds=0.5)", ("0.5",)),
        (f"import {_TIME_MODULE} as t\nt.{_SLEEP_ATTRIBUTE}(5)", ("5",)),
        (f"{_SLEEP_IMPORT}\n{_SLEEP_ATTRIBUTE}(5)", ("5",)),
        (f"{_SLEEP_IMPORT} as pause\npause(5)", ("5",)),
        (
            "class Clock:\n    def sleep(self, seconds):\n        return seconds\n\n"
            "clock = Clock()\nclock.sleep(5)",
            (),
        ),
    ],
)
def test_every_sleep_shape_is_classified(
    tmp_path: Path, source: str, expected: tuple[str, ...]
) -> None:
    """Each import form, each literal and an unrelated ``sleep`` method are classified as fixed.

    A negative literal such as ``-5`` waits for nothing, so the sign is read as part of the literal;
    a string constant states no delay the policy can compare, so it is refused; and an object's own
    ``sleep`` method has nothing to do with the module the cap governs.
    """
    _write(tmp_path, "tests/unit/test_shapes.py", *source.splitlines())
    assert tuple(hit.argument for hit in _scan_sleep_tree(tmp_path)) == expected


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (f"import {_TIME_MODULE}\n\n\ndef fake_sleep(seconds):\n    {_SLEEP_CALL}(seconds)", ()),
        (f"import {_TIME_MODULE}\n\n\ndef _fake_sleep(seconds):\n    {_SLEEP_CALL}(seconds)", ()),
        (
            f"import {_TIME_MODULE}\n\n\nasync def _fake_sleep(seconds):\n    "
            f"{_SLEEP_CALL}(seconds)",
            (),
        ),
        (
            f"import {_TIME_MODULE}\n\n\ndef helper(seconds):\n    {_SLEEP_CALL}(seconds)",
            ("seconds",),
        ),
        (
            f"import {_TIME_MODULE}\n\n\ndef fake_sleep(seconds):\n    def inner():\n        "
            f"{_SLEEP_CALL}(seconds)\n\n    return inner",
            ("seconds",),
        ),
        (f"import {_TIME_MODULE}\n\n\ndef fake_sleep(seconds):\n    {_SLEEP_CALL}(0.5)", ("0.5",)),
    ],
)
def test_a_computed_delay_is_refused_outside_a_fake_sleep_helper(
    tmp_path: Path, source: str, expected: tuple[str, ...]
) -> None:
    """The exemption belongs to the helper's own body -- and to a computed delay alone.

    A nested function inside the helper is a new scope and answers to nothing, and a literal above
    the cap is a real wait however the helper is named.
    """
    _write(tmp_path, "tests/unit/test_delay.py", *source.splitlines())
    assert tuple(hit.argument for hit in _scan_sleep_tree(tmp_path)) == expected


def test_a_sleep_keyword_is_a_value_reference_not_a_call(tmp_path: Path) -> None:
    """Passing a callable as ``sleep=``, and calling the parameter it lands on, are not findings."""
    _write(
        tmp_path,
        "tests/unit/test_polling.py",
        "def eventually(probe, *, interval, sleep):",
        "    sleep(interval)",
        "    return probe()",
        "",
        "recorded = []",
        "eventually(lambda: True, interval=0.25, sleep=recorded.append)",
    )

    assert _scan_sleep_tree(tmp_path) == ()


def test_a_mention_in_prose_is_not_a_finding(tmp_path: Path) -> None:
    """A docstring, a string constant or a comment naming either pattern is not a call.

    This is the property a structural scan has and a text grep does not, and the reason this module
    can live inside the tree it polices without exempting itself from it.
    """
    _write(
        tmp_path,
        "examples/checkout/test_prose.py",
        f'"""A docstring naming {_SLEEP_CALL} and the delay it once took."""',
        f"_MENTION = 'never call {_SLEEP_CALL} here'",
        "# A comment about it is not a call either.",
        "value = 1",
    )
    _write(
        tmp_path,
        "src/foundry/notes.py",
        '"""A module discussing datetime.now and date.today in prose."""',
        "_NOTE = 'datetime.now() and time.time() are refused in this tree'",
        "value = 1",
    )

    assert _scan_sleep_tree(tmp_path) == ()
    assert _scan_clock_tree(tmp_path) == ()


def test_the_source_tree_reads_the_wall_clock_only_in_approved_producers(repo_root: Path) -> None:
    """Every raw read outside :data:`_WALL_CLOCK_PRODUCERS` is a finding (AAP 0.14.3)."""
    hits = _unapproved(_scan_clock_tree(repo_root))
    assert hits == (), _format_clock_hits(hits)


def test_the_wall_clock_scan_reaches_the_core_modules(repo_root: Path) -> None:
    """The walk visits the package it polices, so a clean scan above is evidence, not emptiness."""
    scanned = {_display(path, repo_root) for path in _scanned_files(repo_root, (_SOURCE_ROOT,))}
    for anchor in (
        "src/foundry/core/ids.py",
        "src/foundry/obs/artifacts.py",
        "src/foundry/reporting/store.py",
        "src/foundry/http/session.py",
    ):
        assert anchor in scanned, f"{anchor} is not scanned by the wall-clock walk"
    assert len(scanned) >= _MINIMUM_SOURCE_MODULES, f"the walk found only {len(scanned)} modules"


def test_the_wall_clock_allowlist_is_the_reviewed_producer_set(repo_root: Path) -> None:
    """The exemption is the eight documented modules, each of which exists in the tree.

    The count assertion is the point: adding a producer is a reviewable edit to this constant, and a
    path that no longer exists is a stale entry that would silently excuse nothing.
    """
    assert len(_WALL_CLOCK_PRODUCERS) == _REVIEWED_PRODUCER_COUNT
    for relative in sorted(_WALL_CLOCK_PRODUCERS):
        assert relative.startswith(f"{_SOURCE_ROOT}/"), relative
        assert relative.endswith(".py"), relative
        assert "*" not in relative, f"{relative} is a glob, not a producer"
        assert (repo_root / relative).is_file(), f"{relative} is not a module in the tree"


def test_an_approved_producer_holds_the_approved_read(repo_root: Path) -> None:
    """At least one allowlisted module really does read the clock, so the list masks something."""
    approved = _WALL_CLOCK_PRODUCERS & {hit.path for hit in _scan_clock_tree(repo_root)}
    assert approved, "the allowlist excuses no real read: it is stale"


def test_an_approved_path_is_excused_only_where_it_governs(tmp_path: Path) -> None:
    """The same read is excused in ``core/ids.py`` and reported in ``http/session.py``."""
    _write(
        tmp_path,
        "src/foundry/core/ids.py",
        "from datetime import datetime",
        "moment = datetime.now()",
    )
    _write(
        tmp_path,
        "src/foundry/http/session.py",
        "from datetime import datetime",
        "moment = datetime.now()",
    )

    hits = _scan_clock_tree(tmp_path)

    assert _unapproved(hits) == (
        ClockHit("src/foundry/http/session.py", 2, "datetime.now", "call"),
    ), _format_clock_hits(_unapproved(hits))


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("from datetime import datetime\nstamp = datetime.now()", ("datetime.now", "call")),
        ("from datetime import datetime\nstamp = datetime.utcnow()", ("datetime.utcnow", "call")),
        ("from datetime import datetime as clock\nstamp = clock.now()", ("datetime.now", "call")),
        ("from datetime import date\nstamp = date.today()", ("date.today", "call")),
        ("import datetime\nstamp = datetime.datetime.now()", ("datetime.now", "call")),
        ("import datetime as dt\nstamp = dt.datetime.now()", ("datetime.now", "call")),
        ("import time\nstamp = time.time()", ("time.time", "call")),
        ("from time import time\nstamp = time()", ("time.time", "call")),
        ("from datetime import datetime\nreader = datetime.now", ("datetime.now", "reference")),
    ],
)
def test_every_wall_clock_shape_is_detected(
    tmp_path: Path, source: str, expected: tuple[str, str]
) -> None:
    """Each import form is followed, and a bare reference is reported with the call."""
    _write(tmp_path, "src/foundry/offending.py", *source.splitlines())
    hits = _scan_clock_tree(tmp_path)
    assert [(hit.name, hit.form) for hit in hits] == [expected], _format_clock_hits(hits)


def test_the_datetime_constructors_are_not_readings(tmp_path: Path) -> None:
    """Constructing a date, a datetime or a timedelta, and a monotonic clock, read no wall clock."""
    _write(
        tmp_path,
        "src/foundry/clean_reader.py",
        "import time",
        "from datetime import date, datetime, timedelta, timezone",
        "elapsed = time.monotonic()",
        "epoch = datetime(2024, 1, 1, tzinfo=timezone.utc)",
        "day = date(2024, 1, 1)",
        "later = epoch + timedelta(days=1)",
    )

    assert _scan_clock_tree(tmp_path) == ()
