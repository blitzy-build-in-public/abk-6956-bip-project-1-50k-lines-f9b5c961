"""Acceptance criteria 3 and 8: the example project, run the way a reviewer runs it.

The observation this module exists for is the one AAP 0.14.1 calls the only proof that
configuration, HTTP, cassettes, artifacts and reporting are wired together along the path a user
actually runs: ``pytest examples/checkout`` from the repository root, through pytest itself in a
child process (AAP 0.14.5), reporting ``24 passed`` inside AAP 0.14.2's 60 s and leaving
``artifacts/<run_id>/``, ``results.json``, ``junit.xml`` and ``report.html`` in the invocation
directory. That invocation is made once and its outputs are left in place for a reviewer.

Criterion 8 -- record and replay -- is :func:`test_record_then_replay`: the suite records one
cassette per request-making test into a temporary directory, then replays the committed cassettes
with the echo server absent, and ``examples/checkout`` is asserted byte-identical afterwards
(AAP 0.14.3: a run never mutates the example). :func:`test_replay_miss_is_a_typed_cassette_error`
pins the replay-miss row of AAP 0.4.7: an unrecorded request fails with ``CassetteMissError``
naming the method, the path and the cassette document, and no socket is opened.

Every run except the documented one redirects its outputs into ``tmp_path``
(``--foundry-json-report`` takes ``results.json`` with ``junit.xml`` and ``report.html`` beside it,
``--foundry-artifacts.dir`` the artifact tree), so parallel workers cannot collide and the
repository root only ever holds the documented run's outputs. Nothing here imports ``foundry`` in
process or starts a server: the example's own ``conftest.py`` owns both, and this module reads the
documents each run wrote.
"""

from __future__ import annotations

import datetime
import hashlib
import html
import json
import re
import shutil
import time
import xml.etree.ElementTree as ElementTree
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any, Final, NamedTuple
from urllib.parse import quote_from_bytes, unquote

import pytest

EXAMPLE_DIR: Final[str] = "examples/checkout"
EXPECTED_TOTAL: Final[int] = 24
"""The tests the example collects, one result record each (AAP 0.8.1, criteria 3 and 8)."""

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
"""Per-module counts AAP 0.8.1 fixes: six pricing rows and three matrix cases complete the 24."""

RECORD_CASSETTE_MODULES: Final[Mapping[str, int]] = {
    "test_checkout_api.py": 4,
    "test_contracts.py": 3,
}
"""The modules whose tests issue requests, so the ones a record run writes a cassette for."""

EXPECTED_CASSETTE_DOCUMENTS: Final[int] = sum(RECORD_CASSETTE_MODULES.values())
"""7: one cassette per request-making test, which is what the committed set holds."""

RESULTS_FILENAME, JUNIT_FILENAME, HTML_FILENAME = "results.json", "junit.xml", "report.html"
ARTIFACTS_DIRNAME, CASSETTES_DIRNAME = "artifacts", "cassettes"
MANIFEST_FILENAME: Final[str] = "manifest.json"

WALL_CLOCK_BUDGET_MS: Final[float] = 60_000.0
"""AAP 0.14.2's ceiling on one run of the 24-test suite, measured around the child process alone."""

JUNIT_PROPERTY_NAMES: Final[tuple[str, ...]] = (
    "foundry.attempts",
    "foundry.outcome",
    "foundry.profile",
    "foundry.quarantine",
)
"""The four per-testcase properties AAP 0.4.6 makes part of the published contract."""

PASSED: Final[str] = "passed"
QUARANTINE_PASSED: Final[str] = "quarantine_passed"
RESULT_RECORD_TYPE: Final[str] = "result"
RUN_ERROR_RECORD_TYPE: Final[str] = "run_error"
KNOWN_RECORD_TYPES: Final[tuple[str, ...]] = (RESULT_RECORD_TYPE, RUN_ERROR_RECORD_TYPE)

CASSETTE_KEYS: Final[frozenset[str]] = frozenset({"interactions", "recorded_at", "redacted_keys"})
REQUEST_KEYS: Final[frozenset[str]] = frozenset(
    {"body_sha256", "headers_subset", "method", "path", "query"}
)
"""Exactly the request fields a cassette holds: no host and no port, since matching ignores them."""

RESPONSE_KEYS: Final[frozenset[str]] = frozenset({"body", "headers", "status"})

OFFLINE_SCHEMES: Final[tuple[str, ...]] = ("http://", "https://")
"""No rendered report may reference either scheme: it has to open offline (requirement I10)."""

IGNORED_TREE_NAMES: Final[frozenset[str]] = frozenset({"__pycache__", ".pytest_cache"})
"""What the child interpreter writes rather than the suite: excluded from a repository snapshot."""

GUARD_LINE_RE: Final[re.Pattern[str]] = re.compile(
    r"socket-guard non-loopback=(\d+) loopback=(\d+)"
)

CHECKOUT_ENDPOINTS: Final[tuple[str, ...]] = ("/health", "/orders", "/private/profile")
"""Paths ``test_checkout_api.py`` drives, at least one of which a record run has to capture."""

CONNECTION_FAILURE_MARKERS: Final[tuple[str, ...]] = ("TransportError", "ConnectError", "refused")
"""Proof that replay never fell through to a socket: none may appear in a miss diagnostic."""

MISS_PROJECT_TOML: Final[str] = (
    '[http]\nbase_url = "http://127.0.0.1:9"\n\n[cassette]\ndir = "cassettes"\n'
)
"""A throwaway project's configuration: a closed loopback port and its own cassette directory."""

MISS_TEST_SOURCE: Final[str] = (
    "def test_unrecorded_request_fails(foundry_api):\n"
    '    response = foundry_api.get("/missing")\n'
    "    assert response.status_code == 200\n"
)
MISS_NODEID: Final[str] = "tests/test_missing.py::test_unrecorded_request_fails"
"""The item the miss run collects, whose sanitised node id names the cassette it looked for."""


def _read_text(path: Path) -> str:
    """Return the decoded text at ``path``, failing with the path when the run wrote nothing."""
    assert path.is_file(), f"the run wrote no {path.name} at {path} (AAP 0.4.6)"
    return path.read_text(encoding="utf-8")


def _load_results(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split the document at ``path`` into its result records and its ``run_error`` elements."""
    document: Any = json.loads(_read_text(path))
    assert isinstance(document, list) and all(isinstance(item, dict) for item in document), (
        f"{path} is not the array of JSON objects AAP 0.4.6 specifies: {document!r}"
    )
    records = [item for item in document if item.get("record_type") == RESULT_RECORD_TYPE]
    errors = [item for item in document if item.get("record_type") == RUN_ERROR_RECORD_TYPE]
    unknown = [item for item in document if item.get("record_type") not in KNOWN_RECORD_TYPES]
    assert not unknown, f"{path} holds element(s) of no known record_type: {unknown}"
    return records, errors


def _module_of(nodeid: str) -> str:
    """Return the example module a node id names, for example ``test_checkout_api.py``."""
    return PurePosixPath(nodeid.partition("::")[0]).name


def _module_counts(records: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """Return the number of records per example module, keyed by the module's file name."""
    counts: dict[str, int] = {}
    for record in records:
        name = _module_of(str(record["nodeid"]))
        counts[name] = counts.get(name, 0) + 1
    return counts


def _sanitized(nodeid: str) -> str:
    """Return the artifact directory name AAP 0.4.6 derives from ``nodeid``: one encoding."""
    return quote_from_bytes(nodeid.encode("utf-8", "surrogatepass"), safe="")


def _tree_snapshot(root: Path) -> tuple[tuple[str, int, str], ...]:
    """Return ``(relative posix path, size, sha256)`` for every file under ``root``, sorted."""
    entries: list[tuple[str, int, str]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if path.is_file() and not IGNORED_TREE_NAMES.intersection(relative.parts):
            content = path.read_bytes()
            entries.append((relative.as_posix(), len(content), hashlib.sha256(content).hexdigest()))
    return tuple(entries)


def _cassette_snapshot(directory: Path) -> tuple[tuple[str, str], ...]:
    """Return ``(file name, sha256)`` for every cassette document in ``directory``, sorted."""
    return tuple(
        (path.name, hashlib.sha256(path.read_bytes()).hexdigest())
        for path in sorted(directory.glob("*.json"))
    )


def _escaped_forms(text: str) -> tuple[str, ...]:
    """Return every spelling a template engine may give ``text``, most literal first.

    The report escapes each interpolated value with ``autoescape=True``, and Jinja2 renders ``"``
    and ``'`` as numeric entities where :func:`html.escape` spells them ``&quot;`` and ``&#x27;``,
    so a node id carrying a quote is matched against each spelling rather than one dialect.
    """
    numeric = html.escape(text, quote=False).replace('"', "&#34;").replace("'", "&#39;")
    return tuple(dict.fromkeys((text, html.escape(text, quote=False), html.escape(text), numeric)))


def _guard(output: str) -> tuple[int, int]:
    """Return the example's ``(non-loopback, loopback)`` counts, failing when it states neither."""
    match = GUARD_LINE_RE.search(output)
    assert match is not None, f"the terminal summary carries no socket-guard line:\n{output}"
    return int(match.group(1)), int(match.group(2))


def _redirect(out_dir: Path) -> tuple[str, ...]:
    """Return the two redirections that keep a run's reports and artifacts out of the root."""
    return (
        "--foundry-json-report",
        str(out_dir / RESULTS_FILENAME),
        "--foundry-artifacts.dir",
        str(out_dir / ARTIFACTS_DIRNAME),
    )


def _execute(
    run_pytest: Callable[..., Any], *, cwd: Path, out_dir: Path, arguments: Sequence[str]
) -> _Run:
    """Run pytest once in a child process from ``cwd`` and time that child process alone."""
    out_dir.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    result = run_pytest(*arguments, cwd=cwd)
    return _Run(result=result, out_dir=out_dir, elapsed_ms=(time.perf_counter() - started) * 1e3)


class _Run(NamedTuple):
    """One child run: the completed process, its output directory and its wall clock."""

    result: Any
    out_dir: Path
    elapsed_ms: float

    @property
    def output(self) -> str:
        """Return the child's stdout and stderr together, for an assertion message."""
        return f"{self.result.stdout}\n{self.result.stderr}"


class _Documented(NamedTuple):
    """The documented invocation, with the example tree as it stood before and after it."""

    run: _Run
    example_before: tuple[tuple[str, int, str], ...]
    example_after: tuple[tuple[str, int, str], ...]


class _Transition(NamedTuple):
    """The two criterion-8 runs, with every snapshot they have to leave unchanged."""

    record: _Run
    replay: _Run
    cassette_dir: Path
    cassettes_before: tuple[tuple[str, str], ...]
    cassettes_after_record: tuple[tuple[str, str], ...]
    cassettes_after_replay: tuple[tuple[str, str], ...]
    example_before: tuple[tuple[str, int, str], ...]
    example_after: tuple[tuple[str, int, str], ...]


def _assert_cassette_shape(path: Path, paths: set[str]) -> None:
    """Assert one recorded cassette matches AAP 0.4.6, collecting the request paths it holds."""
    document: Any = json.loads(_read_text(path))
    assert isinstance(document, dict) and set(document) == CASSETTE_KEYS, (
        f"{path.name} holds {sorted(document) if isinstance(document, dict) else document!r}, not "
        f"the fields {sorted(CASSETTE_KEYS)}"
    )
    recorded_at = str(document["recorded_at"]).replace("Z", "+00:00")
    try:
        datetime.datetime.fromisoformat(recorded_at)
    except ValueError:
        pytest.fail(f"{path.name} records the non-ISO recorded_at {recorded_at!r}")
    redacted = document["redacted_keys"]
    assert isinstance(redacted, list) and redacted == sorted(redacted), f"{path.name}: {redacted}"
    interactions = document["interactions"]
    assert isinstance(interactions, list) and interactions, f"{path.name} records no interaction"
    for interaction in interactions:
        assert set(interaction) == {"request", "response"}, f"{path.name}: {sorted(interaction)}"
        request, response = interaction["request"], interaction["response"]
        assert set(request) == REQUEST_KEYS, f"{path.name} stores {sorted(request)}"
        query = request["query"]
        assert isinstance(query, list) and all(len(pair) == 2 for pair in query), f"{query!r}"
        assert set(response) == RESPONSE_KEYS, f"{path.name} stores {sorted(response)}"
        paths.add(str(request["path"]))


def _documented_records(root: Path) -> tuple[list[dict[str, Any]], str]:
    """Assert the result document's census and return its records and the run id they carry."""
    records, errors = _load_results(root / RESULTS_FILENAME)
    assert len(records) == EXPECTED_TOTAL, f"{len(records)} record(s), not {EXPECTED_TOTAL}"
    assert errors == [], f"{len(errors)} run_error element(s): {errors}"
    nodeids = [str(record["nodeid"]) for record in records]
    assert nodeids == sorted(set(nodeids)), f"not one sorted record per item: {nodeids}"
    counts = _module_counts(records)
    assert counts == dict(EXPECTED_MODULE_COUNTS), f"per-module counts {counts}"
    outcomes = sorted(str(record["outcome"]) for record in records)
    assert outcomes.count(QUARANTINE_PASSED) == 1, (
        f"outcomes {sorted(set(outcomes))}, one quarantine"
    )
    assert set(outcomes) <= {PASSED, QUARANTINE_PASSED}, f"a green run's outcomes: {outcomes}"
    attempts = sorted(int(record["attempts"]) for record in records)
    assert attempts[0] >= 1 and attempts[-1] >= 2, f"attempts {attempts}: the retry never ran"
    for record in records:
        markers = list(record["markers"])
        assert float(record["duration_ms"]) >= 0.0, f"{record['nodeid']} has a negative duration"
        assert markers == sorted(set(markers)), f"{record['nodeid']} markers {markers}"
    run_ids = {str(record["run_id"]) for record in records}
    assert len(run_ids) == 1, f"the records carry {len(run_ids)} run ids: {sorted(run_ids)}"
    return records, run_ids.pop()


def _assert_artifacts(root: Path, run_id: str, records: Sequence[Mapping[str, Any]]) -> None:
    """Assert one sanitised, traversal-safe directory per record, holding its manifest."""
    run_dir = root / ARTIFACTS_DIRNAME / run_id
    assert run_dir.is_dir(), f"no {run_dir} for the run id the records carry"
    for record in records:
        nodeid, name = str(record["nodeid"]), _sanitized(str(record["nodeid"]))
        illegal = sorted({character for character in name if character in "/:[]"})
        assert illegal == [], f"{name!r} holds {illegal}, which lets a node id escape (AAP 0.4.6)"
        directory = run_dir / name
        assert directory.is_dir(), f"no artifact directory {directory} for {nodeid}"
        assert (directory / MANIFEST_FILENAME).is_file(), f"{directory} holds no manifest.json"
        artifacts = list(record["artifacts"])
        assert any(item["kind"] == "manifest" for item in artifacts), f"{nodeid}: {artifacts}"
        for item in artifacts:
            path = PurePosixPath(str(item["path"]))
            assert not path.is_absolute() and ".." not in path.parts, f"{nodeid} records {path}"
            assert (run_dir / path).is_file(), f"{nodeid} records the missing {path}"
            assert path.parts[0] == name, f"{nodeid} records {path} outside {name!r}"


def _assert_junit(root: Path, run_id: str) -> None:
    """Assert one enriched testcase per record, the suite properties and the quarantine note."""
    suite = ElementTree.parse(root / JUNIT_FILENAME).getroot()
    assert suite.tag == "testsuite", f"{JUNIT_FILENAME} starts with <{suite.tag}>"
    cases = suite.findall(".//testcase")
    assert len(cases) == EXPECTED_TOTAL, f"{len(cases)} <testcase> element(s), not {EXPECTED_TOTAL}"

    def properties(element: ElementTree.Element) -> dict[str, str]:
        """Return the ``<property>`` entries of one JUnit element, keyed by property name."""
        return {
            str(entry.get("name")): str(entry.get("value"))
            for entry in element.findall("./properties/property")
        }

    suite_properties = properties(suite)
    assert suite_properties.get("foundry.run_id") == run_id, f"the suite reports {suite_properties}"
    assert "foundry.profile" in suite_properties, f"no suite profile: {suite_properties}"
    by_outcome: dict[str, ElementTree.Element] = {}
    for case in cases:
        entries = properties(case)
        missing = [name for name in JUNIT_PROPERTY_NAMES if name not in entries]
        assert missing == [], f"the testcase {case.get('name')!r} lacks {missing}"
        by_outcome[entries["foundry.outcome"]] = case
    quarantined = by_outcome.get(QUARANTINE_PASSED)
    assert quarantined is not None, f"no testcase reports {QUARANTINE_PASSED}: {sorted(by_outcome)}"
    note = quarantined.find("system-out")
    assert note is not None and "remove" in "".join(note.itertext()).lower(), (
        "the passing quarantined testcase carries no marker-removal note (A11)"
    )


def _assert_report(root: Path, records: Sequence[Mapping[str, Any]]) -> None:
    """Assert the report is self-contained, lists every test escaped, and shows the quarantine."""
    report = _read_text(root / HTML_FILENAME)
    for scheme in OFFLINE_SCHEMES:
        assert scheme not in report, f"{HTML_FILENAME} references {scheme}, so it needs a network"
    assert "<link" not in report, f"{HTML_FILENAME} links an external asset"
    assert str(records[0]["run_id"]) in report, f"{HTML_FILENAME} does not name its run id"
    unlisted = [
        str(record["nodeid"])
        for record in records
        if not any(form in report for form in _escaped_forms(str(record["nodeid"])))
    ]
    assert unlisted == [], f"{HTML_FILENAME} lists no such test: {unlisted}"
    assert "Quarantined" in report, f"{HTML_FILENAME} has no Quarantined section (AAP 0.7.4)"


@pytest.fixture(scope="module")
def documented_run(
    repo_root: Path, example_project: Path, run_pytest: Callable[..., Any]
) -> _Documented:
    """Make the documented invocation once from the repository root, and time it.

    Setup rather than the call phase, because ``execution.timeout`` bounds the call phase while the
    suite spends AAP 0.14.2's 12 s of aggregate work by design. The outputs are removed first, so
    every assertion reads what this run wrote, and left in place afterwards for a reviewer.
    """
    assert (repo_root / EXAMPLE_DIR).resolve() == example_project.resolve(), f"{EXAMPLE_DIR} moved"
    for name in (RESULTS_FILENAME, JUNIT_FILENAME, HTML_FILENAME):
        (repo_root / name).unlink(missing_ok=True)
    shutil.rmtree(repo_root / ARTIFACTS_DIRNAME, ignore_errors=True)
    before = _tree_snapshot(example_project)
    run = _execute(run_pytest, cwd=repo_root, out_dir=repo_root, arguments=(EXAMPLE_DIR,))
    return _Documented(run, before, _tree_snapshot(example_project))


@pytest.fixture(scope="module")
def record_and_replay(
    repo_root: Path,
    example_project: Path,
    run_pytest: Callable[..., Any],
    tmp_path_factory: pytest.TempPathFactory,
) -> _Transition:
    """Record the example into a temporary directory, then replay the committed cassettes.

    Both runs start from the repository root, so their node ids keep the ``examples/checkout/``
    prefix the committed cassettes are named by. The record run names its cassette directory
    explicitly; the replay run names none, which points it at the example's own ``cassettes/``.
    Snapshots bracket both runs, so a write into the committed fixtures fails here.
    """
    base = tmp_path_factory.mktemp("record-then-replay")
    committed, example_before = example_project / CASSETTES_DIRNAME, _tree_snapshot(example_project)
    cassettes_before = _cassette_snapshot(committed)
    target, record_dir, replay_dir = base / CASSETTES_DIRNAME, base / "record", base / "replay"
    record = _execute(
        run_pytest,
        cwd=repo_root,
        out_dir=record_dir,
        arguments=(
            EXAMPLE_DIR,
            "-q",
            "--foundry-record",
            "--foundry-cassette.dir",
            str(target),
            *_redirect(record_dir),
        ),
    )
    cassettes_after_record = _cassette_snapshot(committed)
    replay = _execute(
        run_pytest,
        cwd=repo_root,
        out_dir=replay_dir,
        arguments=(EXAMPLE_DIR, "-q", "--foundry-replay", *_redirect(replay_dir)),
    )
    return _Transition(
        record=record,
        replay=replay,
        cassette_dir=target,
        cassettes_before=cassettes_before,
        cassettes_after_record=cassettes_after_record,
        cassettes_after_replay=_cassette_snapshot(committed),
        example_before=example_before,
        example_after=_tree_snapshot(example_project),
    )


def test_example_project_collects_the_documented_suite(
    repo_root: Path, run_pytest: Callable[..., Any], tmp_path: Path
) -> None:
    """Collection alone yields the 24 items AAP 0.8.1 fixes, grouped by module as stated."""
    run = _execute(
        run_pytest, cwd=repo_root, out_dir=tmp_path, arguments=(EXAMPLE_DIR, "--collect-only", "-q")
    )
    assert run.result.returncode == 0, f"collecting {EXAMPLE_DIR} failed:\n{run.output}"
    collected = [line.strip() for line in run.result.stdout.splitlines() if "::" in line]
    counts: dict[str, int] = {}
    for nodeid in collected:
        name = _module_of(nodeid)
        counts[name] = counts.get(name, 0) + 1
    assert counts == dict(EXPECTED_MODULE_COUNTS), (
        f"collected {len(collected)} item(s) as {counts}, not {dict(EXPECTED_MODULE_COUNTS)}"
    )
    duplicates = sorted({nodeid for nodeid in collected if collected.count(nodeid) > 1})
    assert duplicates == [], f"collection yielded duplicate node ids: {duplicates}"


def test_documented_invocation_is_green(documented_run: _Documented) -> None:
    """``pytest examples/checkout`` reports 24 passed, inside the budget, with all four outputs."""
    run, root = documented_run.run, documented_run.run.out_dir
    assert run.result.returncode == 0, f"the documented invocation failed:\n{run.output}"
    assert f"{EXPECTED_TOTAL} passed" in run.result.stdout, f"not passed:\n{run.output}"
    refused, loopback = _guard(run.output)
    assert refused == 0, f"{refused} non-loopback connection attempt(s):\n{run.output}"
    assert loopback > 0, (
        f"the guard counted no loopback call, so no request was served:\n{run.output}"
    )
    assert run.elapsed_ms < WALL_CLOCK_BUDGET_MS, (
        f"{run.elapsed_ms:.0f} ms over AAP 0.14.2's {WALL_CLOCK_BUDGET_MS:.0f} ms budget"
    )
    for name in (RESULTS_FILENAME, JUNIT_FILENAME, HTML_FILENAME):
        assert (root / name).is_file(), f"the run wrote no {name} in its invocation directory"
    assert (root / ARTIFACTS_DIRNAME).is_dir(), f"the run created no {ARTIFACTS_DIRNAME}/ in {root}"
    records, run_id = _documented_records(root)
    _assert_artifacts(root, run_id, records)
    _assert_junit(root, run_id)
    _assert_report(root, records)
    assert documented_run.example_after == documented_run.example_before, (
        f"the documented run changed files under {EXAMPLE_DIR}: a run writes its outputs to the "
        "invocation directory and never into the example (AAP 0.14.3)"
    )


# --- Criterion 8: record and replay ------------------------------------------


def test_record_then_replay(record_and_replay: _Transition) -> None:
    """A record run writes one cassette per request-making test; replay then needs no server."""
    transition = record_and_replay
    committed = transition.cassettes_before
    assert len(committed) == EXPECTED_CASSETTE_DOCUMENTS, f"{len(committed)} committed cassette(s)"
    record = transition.record
    assert record.result.returncode == 0, f"the record run failed:\n{record.output}"
    assert f"{EXPECTED_TOTAL} passed" in record.result.stdout, f"recording ran:\n{record.output}"
    recorded = sorted(transition.cassette_dir.glob("*.json"))
    assert len(recorded) == EXPECTED_CASSETTE_DOCUMENTS, (
        f"the record run wrote {len(recorded)} cassette(s) into {transition.cassette_dir}. When "
        "that directory is empty, check that examples/checkout/conftest.py's foundry_config "
        "override leaves an explicitly configured cassette.dir alone: a fixture that replaced the "
        f"command-line value would record into the committed fixtures instead.\n{record.output}"
    )
    counts: dict[str, int] = {}
    paths: set[str] = set()
    for path in recorded:
        _assert_cassette_shape(path, paths)
        module = _module_of(unquote(path.stem))
        counts[module] = counts.get(module, 0) + 1
    assert counts == dict(RECORD_CASSETTE_MODULES), f"the recorded documents cover {counts}"
    assert set(CHECKOUT_ENDPOINTS) & paths, (
        f"no recorded request hits {CHECKOUT_ENDPOINTS}: {paths}"
    )
    assert "/health" in paths, f"the recorded set omits the health check: {sorted(paths)}"
    assert transition.cassettes_after_record == committed, (
        "recording rewrote the committed cassettes: a run never mutates examples/checkout"
    )
    replay = transition.replay
    assert replay.result.returncode == 0, (
        f"replaying the committed cassettes exited {replay.result.returncode}; a CassetteMissError "
        f"names the method, the path and the cassette it looked for:\n{replay.output}"
    )
    assert f"{EXPECTED_TOTAL} passed" in replay.result.stdout, f"replay ran:\n{replay.output}"
    assert _guard(replay.output) == (0, 0), f"replay opened a socket:\n{replay.output}"
    records, errors = _load_results(replay.out_dir / RESULTS_FILENAME)
    assert (len(records), errors) == (EXPECTED_TOTAL, []), f"replay wrote {len(records)} record(s)"
    assert [record for record in records if record["failures"]] == [], "a replayed test failed"
    _assert_report(replay.out_dir, records)
    assert transition.cassettes_after_replay == committed, "replay rewrote the committed cassettes"
    assert transition.example_after == transition.example_before, (
        f"recording and replay changed files under {EXAMPLE_DIR} (AAP 0.14.3)"
    )


def test_replay_miss_is_a_typed_cassette_error(
    tmp_path: Path, write_project: Callable[..., Path], run_pytest: Callable[..., Any]
) -> None:
    """A request replay cannot answer fails with ``CassetteMissError``, and opens no socket."""
    project = write_project(
        tmp_path / "project",
        files={"tests/test_missing.py": MISS_TEST_SOURCE},
        foundry_toml=MISS_PROJECT_TOML,
    )
    (project / CASSETTES_DIRNAME).mkdir(parents=True, exist_ok=True)
    out_dir = tmp_path / "miss"
    run = _execute(
        run_pytest,
        cwd=project,
        out_dir=out_dir,
        arguments=(
            "tests/test_missing.py",
            "-q",
            "--foundry-replay",
            *_redirect(out_dir),
        ),
    )
    assert run.result.returncode == 1, f"a replay miss exits 1 (AAP 0.4.7):\n{run.output}"
    records, errors = _load_results(out_dir / RESULTS_FILENAME)
    assert errors == [], f"the miss was reported as a run error: {errors}"
    failed = [record for record in records if record["failures"]]
    assert len(failed) == 1, f"{len(failed)} failing record(s) for one unrecorded request"
    message = str(failed[0]["failures"][0]["message"])
    for needle in ("CassetteMissError", "GET", "/missing", f"{_sanitized(MISS_NODEID)}.json"):
        assert needle in message, f"the miss diagnostic omits {needle!r}:\n{message}"
    leaked = [marker for marker in CONNECTION_FAILURE_MARKERS if marker in message]
    assert leaked == [], f"replay attempted a connection ({leaked}) rather than missing:\n{message}"
    assert "CassetteMissError" in f"{run.result.stdout}{run.result.stderr}", (
        f"a silent miss:\n{run.output}"
    )
