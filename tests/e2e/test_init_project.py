"""End-to-end proof that ``foundry init`` scaffolds a project which runs green offline.

AAP 0.14.1 criterion 10: ``foundry init <dir> && cd <dir> && pytest`` exits 0 with one passing test
and nothing listening, and a non-empty directory without ``--force`` exits 2 creating nothing."""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any, Final, NamedTuple, Protocol

import pytest

REFUSED: Final[int] = 2
EXPECTED_MINIMAL_FILES: Final[tuple[str, ...]] = (
    "foundry.toml",
    "README.md",
    ".gitignore",
    "tests/test_smoke.py",
)
IGNORE_ENTRY: Final[str] = "artifacts"
_IGNORED_TREE_PARTS: Final[frozenset[str]] = frozenset({"__pycache__", ".pytest_cache"})


class _Completed(Protocol):
    returncode: int
    stdout: str
    stderr: str


class _Scaffold(NamedTuple):
    project: Path
    printed: tuple[str, ...]


def _failure_detail(label: str, result: _Completed) -> str:
    return f"{label} exited {result.returncode}\n{result.stdout}\n{result.stderr}"


def _tree(root: Path) -> tuple[tuple[str, str | None], ...]:
    entries: list[tuple[str, str | None]] = []
    for path in sorted(root.rglob("*")):
        relative = PurePosixPath(path.relative_to(root).as_posix())
        if _IGNORED_TREE_PARTS.intersection(relative.parts) or path.suffix == ".pyc":
            continue
        digest = None if path.is_dir() else hashlib.sha256(path.read_bytes()).hexdigest()
        entries.append((relative.as_posix(), digest))
    return tuple(entries)


def _load_results(project: Path) -> tuple[tuple[dict[str, Any], ...], tuple[dict[str, Any], ...]]:
    path = project / "results.json"
    assert path.is_file(), f"the scaffolded run wrote no results.json in {project}"
    document = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(document, list), f"{path} holds a JSON document, not an array"
    results = tuple(entry for entry in document if entry.get("record_type") == "result")
    errors = tuple(entry for entry in document if entry.get("record_type") == "run_error")
    return results, errors


def _assert_cassette_shape(document: Any) -> None:
    assert isinstance(document, dict), "a cassette document is a JSON object"
    assert {"recorded_at", "redacted_keys", "interactions"} <= set(document), sorted(document)
    assert isinstance(document["recorded_at"], str), "recorded_at is an ISO 8601 timestamp"
    assert isinstance(document["redacted_keys"], list), "redacted_keys is a list of key names"
    interactions = document["interactions"]
    assert isinstance(interactions, list) and interactions, "a cassette holds an interaction"
    assert all(set(interaction) == {"request", "response"} for interaction in interactions), (
        "every interaction carries a request and a response"
    )


@pytest.fixture(scope="module")
def minimal_scaffold(
    tmp_path_factory: pytest.TempPathFactory, run_foundry: Callable[..., _Completed]
) -> _Scaffold:
    root = tmp_path_factory.mktemp("minimal")
    project = root / "demo"
    result = run_foundry("init", str(project), cwd=root)
    assert result.returncode == 0, _failure_detail("foundry init", result)
    printed = tuple(line.strip() for line in result.stdout.splitlines() if line.strip())
    return _Scaffold(project=project, printed=printed)


def test_minimal_scaffold_is_created_and_runs_green(
    minimal_scaffold: _Scaffold, run_pytest: Callable[..., _Completed]
) -> None:
    project = minimal_scaffold.project
    for relative in EXPECTED_MINIMAL_FILES:
        assert (project / relative).is_file(), f"foundry init wrote no {relative} into {project}"
    assert (project / "testdata").is_dir(), f"foundry init created no testdata/ in {project}"
    testdata = sorted(path for path in (project / "testdata").iterdir() if path.is_file())
    for suffix in (".csv", ".schema.json"):
        assert any(path.name.endswith(suffix) for path in testdata), f"testdata/ holds no {suffix}"
    assert (project / "artifacts").is_dir(), f"foundry init created no artifacts/ in {project}"
    gitignore = (project / ".gitignore").read_text(encoding="utf-8").splitlines()
    ignore = {line.strip().rstrip("/") for line in gitignore}
    assert IGNORE_ENTRY in ignore, f".gitignore carries no {IGNORE_ENTRY} entry: {sorted(ignore)}"
    tomllib.loads((project / "foundry.toml").read_text(encoding="utf-8"))
    created = [*EXPECTED_MINIMAL_FILES, *(p.relative_to(project).as_posix() for p in testdata)]
    listed = [PurePosixPath(line).as_posix() for line in minimal_scaffold.printed]
    for relative in created:
        assert any(line.endswith(relative) for line in listed), f"never printed {relative}"
    run = run_pytest(cwd=project)
    assert run.returncode == 0, _failure_detail("pytest in the scaffold", run)
    assert re.search(r"(\d+) passed", f"{run.stdout}\n{run.stderr}") is not None, run.stdout
    results, errors = _load_results(project)
    assert errors == (), f"the scaffolded run reported a run error: {errors}"
    passing = [record for record in results if record.get("outcome") == "passed"]
    assert passing, f"results.json records no passing test: {results}"


def test_second_init_into_a_non_empty_directory_is_refused_then_forced(
    minimal_scaffold: _Scaffold, run_foundry: Callable[..., _Completed]
) -> None:
    project = minimal_scaffold.project
    before = _tree(project)
    assert before, f"{project} is empty, so the refusal below would prove nothing"
    refused = run_foundry("init", str(project), cwd=project.parent)
    assert refused.returncode == REFUSED, _failure_detail("foundry init refusal", refused)
    assert refused.stderr.strip(), "the refusal printed no reason on stderr"
    assert _tree(project) == before, "a refused foundry init created, removed or modified a file"
    forced = run_foundry("init", str(project), "--force", cwd=project.parent)
    assert forced.returncode == 0, _failure_detail("foundry init --force", forced)
    assert _tree(project) == before, "foundry init --force changed a byte it had already written"


def test_api_template_adds_a_replayable_http_test(
    tmp_path: Path, run_foundry: Callable[..., _Completed], run_pytest: Callable[..., _Completed]
) -> None:
    project = tmp_path / "api-demo"
    result = run_foundry("init", str(project), "--template", "api", cwd=tmp_path)
    assert result.returncode == 0, _failure_detail("foundry init --template api", result)
    for relative in EXPECTED_MINIMAL_FILES:
        assert (project / relative).is_file(), f"the api template omits {relative}"
    api_test = project / "tests" / "test_api.py"
    assert api_test.is_file() and "foundry_api" in api_test.read_text(encoding="utf-8"), api_test
    cassettes = sorted((project / "cassettes").glob("*.json"))
    assert cassettes, f"the api template shipped no cassette in {project / 'cassettes'}"
    _assert_cassette_shape(json.loads(cassettes[0].read_text(encoding="utf-8")))
    run = run_pytest(cwd=project)
    assert run.returncode == 0, _failure_detail("pytest in the api scaffold", run)
    results, errors = _load_results(project)
    assert errors == (), f"the api scaffold's run reported a run error: {errors}"
    assert len(results) >= 2, f"{len(results)} record(s) for the smoke test and the HTTP test"
    assert {record.get("outcome") for record in results} == {"passed"}, run.stdout
    assert all(record.get("failures") == [] for record in results), results
    report = (project / "report.html").read_text(encoding="utf-8")
    assert "http://" not in report and "https://" not in report, "report.html needs a network"
