"""Tree-wide scan of src, tests, examples and scripts for the spellings that enable or disable
Foundry's own pytest plugin, activation being the pytest11 entry point alone (AAP 0.4.2), plus the
Markdown documents, where only a fenced line counts. One allowlisted site: the startup cost module.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pytest

# Assembled from fragments, so this file, which the scan reads, spells none of the three needles.
_FLAG: Final[str] = "-p"
_NO: Final[str] = "no:"
_NAME: Final[str] = "foundry"
NEEDLES: Final[tuple[str, ...]] = (
    f"{_FLAG} {_NAME}",
    f"{_FLAG}{_NO}{_NAME}",
    f"{_FLAG} {_NO}{_NAME}",
)
# The one exempt site: AAP 0.14.2's subtracted startup arm, compared by exact path and pinned once.
ALLOWLIST: Final[frozenset[str]] = frozenset({"tests/integration/test_startup_cost.py"})
_SCAN_ROOTS: Final[tuple[str, ...]] = ("src", "tests", "examples", "scripts")
_SCANNED_SUFFIXES: Final[frozenset[str]] = frozenset(
    {".py", ".sh", ".toml", ".cfg", ".ini", ".md", ".txt", ".yaml", ".yml", ".json"}
)
_SKIPPED_DIRECTORIES: Final[frozenset[str]] = frozenset(
    {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "node_modules", ".venv"}
)
# The two places a maintainer copies a command from, read under the fenced-only rule, not as text.
_MARKDOWN_TREES: Final[tuple[str, ...]] = ("docs",)
_MARKDOWN_GLOB: Final[str] = "*.md"
_FENCE_CHARACTERS: Final[tuple[str, ...]] = ("`", "~")
_FENCE_MINIMUM_LENGTH: Final[int] = 3
# A count floor catches an empty walk; a named anchor per root and per document catches a miss.
_MINIMUM_INVENTORY: Final[int] = 100
_ANCHOR_FILES: Final[tuple[str, ...]] = (
    "src/foundry/plugin.py",
    "tests/conftest.py",
    "tests/integration/test_startup_cost.py",
    "examples/checkout/conftest.py",
    "scripts/verify.sh",
)
_DOCUMENT_ANCHOR_FILES: Final[tuple[str, ...]] = ("README.md", "docs/verification.md")


@dataclass(frozen=True, order=True)
class Hit:
    path: str
    line: int
    needle: str


def _skipped(path: Path, root: Path) -> bool:
    return bool(_SKIPPED_DIRECTORIES.intersection(path.relative_to(root).parts[:-1]))


def scanned_files(root: Path) -> tuple[Path, ...]:
    # An absent root contributes nothing, so the walk is usable on a synthetic tree in tmp_path too.
    bases = [root / relative for relative in _SCAN_ROOTS if (root / relative).is_dir()]
    found = [
        path
        for base in bases
        for path in base.rglob("*")
        if path.is_file() and path.suffix in _SCANNED_SUFFIXES and not _skipped(path, root)
    ]
    return tuple(sorted(found))


def documentation_files(root: Path) -> tuple[Path, ...]:
    # The root's own *.md files, then each documentation tree; an absent tree contributes none.
    found = [path for path in root.glob(_MARKDOWN_GLOB) if path.is_file()]
    for relative in _MARKDOWN_TREES:
        if (root / relative).is_dir():
            walked = (root / relative).rglob(_MARKDOWN_GLOB)
            found += [path for path in walked if path.is_file() and not _skipped(path, root)]
    return tuple(sorted(found))


def _fence_delimiter(line: str) -> tuple[str, int, str] | None:
    # Leading indentation is tolerated, so a fence inside a list still opens; two characters do not.
    stripped = line.strip()
    if not stripped or stripped[0] not in _FENCE_CHARACTERS:
        return None
    length = len(stripped) - len(stripped.lstrip(stripped[0]))
    return (stripped[0], length, stripped[length:]) if length >= _FENCE_MINIMUM_LENGTH else None


def _fenced_line_numbers(text: str) -> frozenset[int]:
    # A fence closes on the same character, at least as long, holding nothing after it.
    fenced: set[int] = set()
    opened: tuple[str, int] | None = None
    for number, line in enumerate(text.splitlines(), start=1):
        char, length, rest = _fence_delimiter(line) or ("", 0, "")
        if opened is None:
            if not char:
                continue
            opened = (char, length)
        elif char == opened[0] and length >= opened[1] and not rest:
            opened = None
        fenced.add(number)
    return frozenset(fenced)


def _scan(path: Path, root: Path, markdown: bool = False) -> tuple[Hit, ...]:
    # One hit per line and spelling; a Markdown document contributes only its fenced lines.
    text = path.read_text(encoding="utf-8", errors="replace")
    fenced = _fenced_line_numbers(text) if markdown else None
    display = path.relative_to(root).as_posix()
    hits: set[Hit] = set()
    for number, line in enumerate(text.splitlines(), start=1):
        if fenced is not None and number not in fenced:
            continue
        # The fallback covers the two-argv-element form subprocess actually passes.
        found = [needle for needle in NEEDLES if needle in line]
        found = found or ([NEEDLES[2]] if _FLAG in line and f"{_NO}{_NAME}" in line else [])
        hits.update(Hit(display, number, needle) for needle in found)
    return tuple(sorted(hits))


def scan_tree(root: Path) -> tuple[Hit, ...]:
    # Both passes, in this order: the four source trees as plain text, then the documents' fenced
    # lines only. The allowlist is the caller's, never this walk's.
    walked = [(path, False) for path in scanned_files(root)]
    walked += [(path, True) for path in documentation_files(root)]
    return tuple(sorted({hit for path, markdown in walked for hit in _scan(path, root, markdown)}))


def _format_hits(hits: tuple[Hit, ...]) -> str:
    lines = [f"  {hit.path}:{hit.line}: {hit.needle}" for hit in hits]
    return "\n".join([f"{len(hits)} disallowed plugin-option occurrence(s):", *lines])


def _write(root: Path, relative: str, *lines: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")


def test_the_scanner_and_its_fragments_are_not_findings(repo_root: Path, tmp_path: Path) -> None:
    own_text = Path(__file__).read_text(encoding="utf-8")
    found = [needle for needle in NEEDLES if needle in own_text]
    assert found == [], f"{__file__} spells {found} literally: build it from the fragments instead"
    _write(tmp_path, "scripts/clean.sh", "set -eu", "pytest -q tests")
    _write(tmp_path, "src/foundry/clean.py", '_FLAG = "-p"', '_NAME = "foundry"')
    assert scan_tree(tmp_path) == ()
    relative = Path(__file__).resolve().relative_to(repo_root).as_posix()
    scanned = {path.relative_to(repo_root).as_posix() for path in scanned_files(repo_root)}
    assert relative in scanned, f"{relative} is not scanned, so the scan would pass vacuously"
    (allowed,) = ALLOWLIST
    assert allowed == "tests/integration/test_startup_cost.py", sorted(ALLOWLIST)
    assert (repo_root / allowed).is_file() and allowed in scanned, f"{allowed} is not scanned"
    assert allowed != relative, f"{relative} exempts itself"
    needles = {hit.needle for hit in scan_tree(repo_root) if hit.path == allowed}
    assert NEEDLES[2] in needles, f"{allowed} names no {NEEDLES[2]!r}: the exemption masks nothing"


def test_no_invocation_disables_the_plugin(repo_root: Path) -> None:
    hits = tuple(hit for hit in scan_tree(repo_root) if hit.path not in ALLOWLIST)
    assert hits == (), _format_hits(hits)


def test_the_scan_reaches_every_root_and_every_document(repo_root: Path) -> None:
    walked = (*scanned_files(repo_root), *documentation_files(repo_root))
    scanned = {path.relative_to(repo_root).as_posix() for path in walked}
    assert len(scanned) > _MINIMUM_INVENTORY, f"the walk found only {len(scanned)} files"
    anchors = set(_ANCHOR_FILES) | set(_DOCUMENT_ANCHOR_FILES)
    assert anchors <= scanned, f"the walk misses {anchors - scanned}"
    assert "pyproject.toml" not in scanned, "the root pass reads Markdown, so no root config file"


@pytest.mark.parametrize("needle", NEEDLES)
def test_every_needle_is_detected(tmp_path: Path, needle: str) -> None:
    # One spelling per synthetic tree, reported with its path, its line and the spelling itself.
    _write(tmp_path, "scripts/offending.sh", "set -eu", f"pytest {needle} tests")
    hits = scan_tree(tmp_path)
    assert hits == (Hit("scripts/offending.sh", 2, needle),), _format_hits(hits)


@pytest.mark.parametrize(
    ("relative", "body", "expected"),
    [
        ("README.md", "# Foundry\n\n```console\n$ pytest {2}\n```", (4, 2)),
        ("README.md", "# Foundry\n\nNothing passes `{0}`; `{1}` turns it off.", None),
        ("docs/verification.md", "# Docs\n\n1. Run:\n\n   ~~~bash\n   pytest {1}\n   ~~~", (6, 1)),
    ],
)
def test_a_fenced_command_is_a_hit_and_prose_is_not(
    tmp_path: Path, relative: str, body: str, expected: tuple[int, int] | None
) -> None:
    # A documented command is an invocation; prose naming the spelling to state the rule is not.
    _write(tmp_path, relative, *body.format(*NEEDLES).split("\n"))
    hits = scan_tree(tmp_path)
    want = () if expected is None else (Hit(relative, expected[0], NEEDLES[expected[1]]),)
    assert hits == want, _format_hits(hits)


def test_the_report_names_every_offender_once_per_line(tmp_path: Path) -> None:
    _write(tmp_path, "scripts/one.sh", f'pytest "{NEEDLES[1]}" {NEEDLES[1]}')
    _write(tmp_path, "examples/two.py", f'ADDOPTS = "{NEEDLES[2]}"')
    _write(tmp_path, "tests/unit/three.toml", f'addopts = "{NEEDLES[2]}"')
    hits = scan_tree(tmp_path)
    message = _format_hits(hits)
    assert {(hit.path, hit.line) for hit in hits} == {
        ("examples/two.py", 1),
        ("scripts/one.sh", 1),
        ("tests/unit/three.toml", 1),
    }, message
    for hit in hits:
        assert f"{hit.path}:{hit.line}: {hit.needle}" in message, message
    assert message.startswith("3 disallowed plugin-option occurrence(s)"), message


def test_the_walk_reads_exactly_the_four_roots_and_the_text_types(tmp_path: Path) -> None:
    rows = {f"{root}/carrier{suffix}" for root in _SCAN_ROOTS for suffix in _SCANNED_SUFFIXES}
    ignored = {"scripts/carrier.png", "scripts/carrier.pyc", "scripts/carrier.log", "carrier.py"}
    ignored |= {"docs/carrier.py", "tests/__pycache__/carrier.py", "src/foundry/.venv/carrier.py"}
    ignored |= {"tests/.pytest_cache/carrier.py", "tests/.mypy_cache/carrier.py"}
    for relative in sorted(rows | ignored):
        _write(tmp_path, relative, f"pytest {NEEDLES[2]}")
    assert {hit.path for hit in scan_tree(tmp_path)} == rows


def test_undecodable_bytes_do_not_stop_the_scan(tmp_path: Path) -> None:
    path = tmp_path / "scripts" / "binary_adjacent.txt"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xff\xfe" + f"pytest {NEEDLES[2]}".encode())
    hits = scan_tree(tmp_path)
    assert hits == (Hit("scripts/binary_adjacent.txt", 1, NEEDLES[2]),), _format_hits(hits)
