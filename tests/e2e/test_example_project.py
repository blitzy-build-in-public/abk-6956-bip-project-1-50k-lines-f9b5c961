"""Acceptance criteria 3 and 8: the documented 24-test example run, then its record/replay pair."""

import hashlib
import json
import re
import shutil
import time
import xml.etree.ElementTree as ElementTree
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any, Final, NamedTuple
from urllib.parse import quote_from_bytes, unquote

import pytest

EXAMPLE_DIR: Final[str] = "examples/checkout"
EXPECTED_TOTAL: Final[int] = 24
EXPECTED_MODULE_COUNTS: Final[Mapping[str, int]] = {
    "test_artifacts_report.py": 3,
    "test_cart_pricing.py": 6,
    "test_checkout_api.py": 4,
    "test_contracts.py": 3,
    "test_factory_clock.py": 2,
    "test_inventory_db.py": 2,
    "test_polling.py": 2,
    "test_retries_quarantine.py": 2,
}
RECORDED_MODULES: Final[Mapping[str, int]] = {"test_checkout_api.py": 4, "test_contracts.py": 3}
RECORDED_TOTAL: Final[int] = sum(RECORDED_MODULES.values())
RESULTS, JUNIT, REPORT = "results.json", "junit.xml", "report.html"
ARTIFACTS, CASSETTES = "artifacts", "cassettes"
GUARD_LINE_RE: Final[re.Pattern[str]] = re.compile(
    r"socket-guard non-loopback=(\d+) loopback=(\d+)"
)
MISS_PROJECT_TOML: Final[str] = '[http]\nbase_url = "http://127.0.0.1:9"\n'
MISS_PROJECT_TOML += '[cassette]\ndir = "cassettes"\n'
MISS_TEST_SOURCE: Final[str] = (
    'def test_unrecorded_request_fails(foundry_api): foundry_api.get("/missing")\n'
)
MISS_NODEID: Final[str] = "tests/test_missing.py::test_unrecorded_request_fails"


class _Run(NamedTuple):
    result: Any
    out_dir: Path
    elapsed_ms: float

    @property
    def output(self) -> str:
        return f"{self.result.stdout}\n{self.result.stderr}"


class _Transition(NamedTuple):
    record: _Run
    replay: _Run
    cassette_dir: Path
    cassettes: tuple[tuple[tuple[str, str], ...], ...]


def _read(path: Path) -> str:
    assert path.is_file(), f"the run wrote no {path.name} at {path}"
    return path.read_text(encoding="utf-8")


def _load(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    document: Any = json.loads(_read(path))
    assert isinstance(document, list) and all(isinstance(item, dict) for item in document), path
    records = [item for item in document if item.get("record_type") == "result"]
    errors = [item for item in document if item.get("record_type") == "run_error"]
    assert len(records) + len(errors) == len(document), f"{path} holds an unknown record_type"
    return records, errors


def _sanitized(nodeid: str) -> str:
    return quote_from_bytes(nodeid.encode("utf-8", "surrogatepass"), safe="")


def _cassettes(directory: Path) -> tuple[tuple[str, str], ...]:
    files = sorted(directory.glob("*.json"))
    return tuple((path.name, hashlib.sha256(path.read_bytes()).hexdigest()) for path in files)


def _guard(output: str) -> tuple[int, int]:
    match = GUARD_LINE_RE.search(output)
    assert match is not None, f"the terminal summary carries no socket-guard line:\n{output}"
    return int(match.group(1)), int(match.group(2))


def _redirect(out_dir: Path) -> list[str]:
    arguments = ["--foundry-json-report", str(out_dir / RESULTS)]
    arguments += ["--foundry-artifacts.dir", str(out_dir / ARTIFACTS)]
    return arguments


def _execute(
    run_pytest: Callable[..., Any], *, cwd: Path, out_dir: Path, arguments: Sequence[str]
) -> _Run:
    out_dir.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    result = run_pytest(*arguments, cwd=cwd)
    return _Run(result=result, out_dir=out_dir, elapsed_ms=(time.perf_counter() - started) * 1e3)


def _assert_records(root: Path) -> tuple[list[dict[str, Any]], str]:
    records, errors = _load(root / RESULTS)
    assert (len(records), errors) == (EXPECTED_TOTAL, []), f"{len(records)} record(s), {errors}"
    nodeids = [str(record["nodeid"]) for record in records]
    assert nodeids == sorted(set(nodeids)), f"not one sorted record per item: {nodeids}"
    counts = Counter(PurePosixPath(nodeid.partition("::")[0]).name for nodeid in nodeids)
    assert counts == EXPECTED_MODULE_COUNTS, f"per-module counts {counts}"
    outcomes = sorted(str(record["outcome"]) for record in records)
    assert outcomes.count("quarantine_passed") == 1, f"outcomes {sorted(set(outcomes))}"
    ids = {str(record["run_id"]) for record in records}
    assert len(ids) == 1, f"the records carry {len(ids)} run ids: {sorted(ids)}"
    return records, ids.pop()


@pytest.fixture(scope="module")
def documented_run(repo_root: Path, run_pytest: Callable[..., Any]) -> _Run:
    for name in (RESULTS, JUNIT, REPORT):
        (repo_root / name).unlink(missing_ok=True)
    shutil.rmtree(repo_root / ARTIFACTS, ignore_errors=True)
    return _execute(run_pytest, cwd=repo_root, out_dir=repo_root, arguments=(EXAMPLE_DIR,))


@pytest.fixture(scope="module")
def record_and_replay(
    repo_root: Path,
    example_project: Path,
    run_pytest: Callable[..., Any],
    tmp_path_factory: pytest.TempPathFactory,
) -> _Transition:
    base = tmp_path_factory.mktemp("record-then-replay")
    committed = example_project / CASSETTES
    cassettes_before = _cassettes(committed)
    target, record_dir, replay_dir = base / CASSETTES, base / "record", base / "replay"
    record_args = [EXAMPLE_DIR, "-q", "--foundry-record", "--foundry-cassette.dir", str(target)]
    record = _execute(run_pytest, cwd=repo_root, out_dir=record_dir, arguments=record_args)
    cassettes_after_record = _cassettes(committed)
    replay_args = [EXAMPLE_DIR, "-q", "--foundry-replay", *_redirect(replay_dir)]
    replay = _execute(run_pytest, cwd=repo_root, out_dir=replay_dir, arguments=replay_args)
    cassettes = (cassettes_before, cassettes_after_record, _cassettes(committed))
    return _Transition(record, replay, target, cassettes)


def test_documented_invocation_is_green(
    documented_run: _Run, repo_root: Path, run_pytest: Callable[..., Any], tmp_path: Path
) -> None:
    arguments = (EXAMPLE_DIR, "--collect-only", "-q")
    census = _execute(run_pytest, cwd=repo_root, out_dir=tmp_path, arguments=arguments)
    assert census.result.returncode == 0, census.output
    collected = [line.strip() for line in census.result.stdout.splitlines() if "::" in line]
    counts = Counter(PurePosixPath(nodeid.partition("::")[0]).name for nodeid in collected)
    assert counts == EXPECTED_MODULE_COUNTS, f"collected {len(collected)} as {counts}"
    run, root = documented_run, documented_run.out_dir
    assert run.result.returncode == 0, f"the documented invocation failed:\n{run.output}"
    assert f"{EXPECTED_TOTAL} passed" in run.result.stdout, f"not passed:\n{run.output}"
    refused, loopback = _guard(run.output)
    assert refused == 0 and loopback > 0, f"socket guard refused={refused} loopback={loopback}"
    assert run.elapsed_ms < 60_000.0, f"{run.elapsed_ms:.0f} ms over AAP 0.14.2's 60 s budget"
    for name in (RESULTS, JUNIT, REPORT):
        assert (root / name).is_file(), f"the run wrote no {name} in its invocation directory"
    assert (root / ARTIFACTS).is_dir(), f"the run created no {ARTIFACTS}/ in {root}"
    records, run_id = _assert_records(root)
    run_dir = root / ARTIFACTS / run_id
    assert run_dir.is_dir(), f"no {run_dir} for the run id the records carry"
    for record in records:
        name = _sanitized(str(record["nodeid"]))
        assert "/" not in name and "::" not in name, f"{name!r} carries a raw separator"
        assert (run_dir / name / "manifest.json").is_file(), f"{run_dir / name} holds no manifest"
    suite = ElementTree.parse(root / JUNIT).getroot()
    cases = suite.findall(".//testcase")
    assert suite.tag == "testsuite" and len(cases) == EXPECTED_TOTAL, f"{len(cases)} testcase(s)"
    properties = [(entry.get("name"), entry.get("value")) for entry in suite.iter("property")]
    assert ("foundry.run_id", run_id) in properties, f"the suite reports {properties}"
    assert any("remove" in "".join(case.itertext()).lower() for case in cases), "no A11 note"
    report = _read(root / REPORT)
    assert not [scheme for scheme in ("http://", "https://") if scheme in report], REPORT
    assert run_id in report and "Quarantined" in report, f"{REPORT} omits its run id or section"


def test_record_then_replay(record_and_replay: _Transition) -> None:
    before, after_record, after_replay = record_and_replay.cassettes
    record, replay = record_and_replay.record, record_and_replay.replay
    assert len(before) == RECORDED_TOTAL, f"{len(before)} committed cassette(s)"
    assert record.result.returncode == 0, f"the record run failed:\n{record.output}"
    assert f"{EXPECTED_TOTAL} passed" in record.result.stdout, f"recording ran:\n{record.output}"
    recorded = sorted(record_and_replay.cassette_dir.glob("*.json"))
    assert len(recorded) == RECORDED_TOTAL, f"the record run wrote {len(recorded)} cassette(s)"
    documents = [json.loads(_read(path)) for path in recorded]
    assert all({"interactions", "recorded_at", "redacted_keys"} <= set(doc) for doc in documents)
    routes = {str(item["request"]["path"]) for doc in documents for item in doc["interactions"]}
    names = [PurePosixPath(unquote(path.stem).partition("::")[0]).name for path in recorded]
    assert Counter(names) == RECORDED_MODULES, f"the recorded documents cover {sorted(names)}"
    assert "/health" in routes, f"the recorded set omits the health check: {sorted(routes)}"
    assert after_record == before, "recording rewrote the committed cassettes"
    assert replay.result.returncode == 0, f"replay exited {replay.result.returncode}"
    assert f"{EXPECTED_TOTAL} passed" in replay.result.stdout, f"replay ran:\n{replay.output}"
    assert _guard(replay.output) == (0, 0), f"replay opened a socket:\n{replay.output}"
    records, errors = _load(replay.out_dir / RESULTS)
    assert (len(records), errors) == (EXPECTED_TOTAL, []), f"replay wrote {len(records)} records"
    assert not [record for record in records if record["failures"]], "a replayed test failed"
    assert after_replay == before, "replay rewrote the committed cassettes"


def test_replay_miss_is_a_typed_cassette_error(
    tmp_path: Path, write_project: Callable[..., Path], run_pytest: Callable[..., Any]
) -> None:
    project = write_project(
        tmp_path / "project",
        files={"tests/test_missing.py": MISS_TEST_SOURCE},
        foundry_toml=MISS_PROJECT_TOML,
    )
    (project / CASSETTES).mkdir(parents=True, exist_ok=True)
    out_dir = tmp_path / "miss"
    arguments = ["tests/test_missing.py", "-q", "--foundry-replay", *_redirect(out_dir)]
    run = _execute(run_pytest, cwd=project, out_dir=out_dir, arguments=arguments)
    assert run.result.returncode == 1, f"a replay miss exits 1 (AAP 0.4.7):\n{run.output}"
    records, errors = _load(out_dir / RESULTS)
    assert errors == [], f"the miss was reported as a run error: {errors}"
    failed = [record for record in records if record["failures"]]
    assert len(failed) == 1, f"{len(failed)} failing record(s) for one unrecorded request"
    message = str(failed[0]["failures"][0]["message"])
    for needle in ("CassetteMissError", "GET", "/missing", f"{_sanitized(MISS_NODEID)}.json"):
        assert needle in message, f"the miss diagnostic omits {needle!r}:\n{message}"
    assert not [m for m in ("TransportError", "ConnectError", "refused") if m in message], message
    assert "CassetteMissError" in f"{run.result.stdout}{run.result.stderr}", "a silent miss"
