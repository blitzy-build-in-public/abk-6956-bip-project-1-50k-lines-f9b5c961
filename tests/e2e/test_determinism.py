"""Acceptance criterion 14: two consecutive example runs normalise to one document.

``run_id``, ``started_at``, ``finished_at`` and every per-record ``duration_ms`` are normalised, so
a divergence the product emits is reported rather than absorbed."""

from __future__ import annotations

import difflib
import json
import re
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Final, NamedTuple

import pytest

EXAMPLE_DIR: Final[str] = "examples/checkout"
EXPECTED_TOTAL: Final[int] = 24
RESULTS, REPORT, ARTIFACTS = "results.json", "report.html", "artifacts"
NORMALISED: Final[tuple[str, ...]] = ("run_id", "started_at", "finished_at", "duration_ms")
SENTINEL_DURATION: Final[str] = "DURATION"
SENTINELS: Final[dict[str, Any]] = dict.fromkeys(NORMALISED, "TIMESTAMP")
SENTINELS.update(run_id="RUN_ID", duration_ms=0.0)
MIN_HTML_LENGTH: Final[int] = 2_000
DIFF_LINES: Final[int] = 60
RUN_ID_RE: Final[re.Pattern[str]] = re.compile(r"\d{8}T\d{6}-[0-9a-f]{6}")
ISO_TIMESTAMP_RE: Final[re.Pattern[str]] = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})"
)
DURATION_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"\d+(?:\.\d+)?\s*(?:µs|us|ms|s)\b")


class TwoRuns(NamedTuple):
    results: tuple[list[dict[str, Any]], list[dict[str, Any]]]
    reports: tuple[str, str]


def _run_once(run_pytest: Callable[..., Any], repo_root: Path, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    arguments = ["-q", EXAMPLE_DIR, "--foundry-json-report", str(out_dir / RESULTS)]
    arguments += ["--foundry-artifacts.dir", str(out_dir / ARTIFACTS)]
    result = run_pytest(*arguments, cwd=repo_root)
    output = f"{result.stdout}\n{result.stderr}"
    assert result.returncode == 0, f"the run of {EXAMPLE_DIR} must exit 0:\n{output}"
    assert f"{EXPECTED_TOTAL} passed" in output, f"not the {EXPECTED_TOTAL} tests:\n{output}"


def _load(path: Path) -> list[dict[str, Any]]:
    assert path.is_file(), f"the run wrote no {path.name} at {path}"
    document: Any = json.loads(path.read_text(encoding="utf-8"))
    objects = isinstance(document, list) and all(isinstance(item, dict) for item in document)
    assert objects, f"{path} is not the array of JSON objects AAP 0.4.6 specifies"
    return document


def _records(document: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    assert all(item.get("record_type") in ("result", "run_error") for item in document), document
    return [item for item in document if item.get("record_type") == "result"]


def _normalise_results(document: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {**element, **{field: SENTINELS[field] for field in NORMALISED if field in element}}
        for element in document
    ]


def _normalise_html(text: str) -> tuple[str, dict[str, int]]:
    counts: dict[str, int] = {}
    text, counts["run_id"] = RUN_ID_RE.subn(SENTINELS["run_id"], text)
    text, counts["timestamps"] = ISO_TIMESTAMP_RE.subn(SENTINELS["started_at"], text)
    text, counts["durations"] = DURATION_TOKEN_RE.subn(SENTINEL_DURATION, text)
    return text.replace("\r\n", "\n"), counts


def _diff(left: str, right: str) -> str:
    diff = difflib.unified_diff(left.splitlines(), right.splitlines(), "run-a", "run-b", n=2)
    return "\n".join(list(diff)[:DIFF_LINES]) or "(the documents differ only in their ending)"


def _nodeid_fragments(nodeid: str) -> tuple[str, str]:
    module, _, rest = nodeid.partition("::")
    return module, f"::{rest.partition('[')[0]}"


@pytest.fixture(scope="module")
def two_runs(
    tmp_path_factory: pytest.TempPathFactory, repo_root: Path, run_pytest: Callable[..., Any]
) -> TwoRuns:
    base = tmp_path_factory.mktemp("determinism")
    results: list[list[dict[str, Any]]] = []
    reports: list[str] = []
    for name in ("run-a", "run-b"):
        out_dir = base / name
        _run_once(run_pytest, repo_root, out_dir)
        results.append(_load(out_dir / RESULTS))
        reports.append((out_dir / REPORT).read_text(encoding="utf-8"))
    return TwoRuns(results=(results[0], results[1]), reports=(reports[0], reports[1]))


def test_two_runs_produce_identical_normalised_results(two_runs: TwoRuns) -> None:
    documents = list(two_runs.results)
    records = [_records(document) for document in documents]
    for name, document, items in zip(("run-a", "run-b"), documents, records, strict=True):
        assert (len(document), len(items)) == (EXPECTED_TOTAL, EXPECTED_TOTAL), (
            f"{name} wrote {len(document)} element(s) holding {len(items)} record(s)"
        )
    ids = [{str(record["run_id"]) for record in items} for items in records]
    assert len(ids[0]) == len(ids[1]) == 1, f"one run id belongs in a document: {ids}"
    assert ids[0] != ids[1], f"both runs report the run id {ids[0]}: this could be one run twice"
    collected = [tuple(sorted(str(record["nodeid"]) for record in items)) for items in records]
    assert collected[0] == collected[1], f"the runs collected different node ids: {collected}"
    present = [set(NORMALISED) <= set(record) for items in records for record in items]
    assert all(present), f"{present.count(False)} record(s) omit one of the four replaced values"
    normalised = [_normalise_results(document) for document in documents]
    dumped = [json.dumps(items, sort_keys=True, ensure_ascii=False) for items in normalised]
    assert dumped[0] == dumped[1], (
        "results.json differs in something other than the four normalised values:\n"
        + _diff(dumped[0], dumped[1])
    )


def test_two_runs_produce_identical_normalised_html(two_runs: TwoRuns) -> None:
    for name, text in zip(("run-a", "run-b"), two_runs.reports, strict=True):
        assert len(text) >= MIN_HTML_LENGTH, f"{name}'s report holds {len(text)} characters"
        assert not [scheme for scheme in ("http://", "https://") if scheme in text], name
    normalised = [_normalise_html(text) for text in two_runs.reports]
    assert normalised[0][1] == normalised[1][1], f"the reports' shapes differ: {normalised}"
    counts = normalised[0][1]
    assert counts["run_id"] >= 1 and counts["timestamps"] >= 1, f"nothing replaced: {counts}"
    assert counts["durations"] >= EXPECTED_TOTAL, f"fewer duration tokens than tests: {counts}"
    records = _records(two_runs.results[0])
    words = sorted({str(record["outcome"]) for record in records})
    fragments = [part for record in records for part in _nodeid_fragments(str(record["nodeid"]))]
    for name, raw, (text, _) in zip(("run-a", "run-b"), two_runs.reports, normalised, strict=True):
        assert [part for part in fragments if part not in text] == [], f"{name} lost a node id"
        rendered = {word: text.count(word) for word in words}
        assert rendered == {word: raw.count(word) for word in words}, f"{name}: {rendered}"
        assert sum(rendered.values()) >= EXPECTED_TOTAL, f"{name}'s table is gone"
    assert normalised[0][0] == normalised[1][0], (
        "report.html differs in something other than the four normalised values:\n"
        + _diff(normalised[0][0], normalised[1][0])
    )
