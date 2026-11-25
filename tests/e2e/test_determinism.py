"""Acceptance criterion 14: two consecutive example runs normalise to one document.

After exactly four values are replaced -- ``run_id``, ``started_at``, ``finished_at`` and every
per-record ``duration_ms`` -- two consecutive runs of ``examples/checkout`` must produce
byte-identical ``results.json`` and ``report.html`` (AAP 0.14.1, criterion 14). Every other value --
node ids, outcomes, markers, attempts, quarantine state, artifact kinds and relative paths, and all
counts -- must match exactly, so a divergence the product emits is reported as a diff instead of
being absorbed by a wider normalisation.

Both runs drive the real entry point, ``python -m pytest examples/checkout`` from the repository
root with both outputs aimed inside the run's own directory (AAP 0.4.6, A5), so neither writes into
the repository; a record's ``duration_ms`` is normalised through the report's rendering of it alone.
"""

from __future__ import annotations

import difflib
import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import pytest

# The example project, its count, and the file names each run writes (AAP 0.4.6, A5, AAP 0.9.1).
EXAMPLE_DIR: Final[str] = "examples/checkout"
EXPECTED_TOTAL: Final[int] = 24
RESULTS_FILENAME: Final[str] = "results.json"
HTML_FILENAME: Final[str] = "report.html"
ARTIFACTS_DIRNAME: Final[str] = "artifacts"

# What each of the four permitted values becomes, and the floors and widths a comparison relies on.
SENTINEL_RUN_ID: Final[str] = "RUN_ID"
SENTINEL_TIMESTAMP: Final[str] = "TIMESTAMP"
SENTINEL_DURATION: Final[str] = "DURATION"
SENTINEL_DURATION_MS: Final[float] = 0.0
RUN_ID_HEX_DIGITS: Final[int] = 6
MIN_HTML_LENGTH: Final[int] = 2_000
DIFF_LINES: Final[int] = 60

# A run id's shape (AAP 0.4.6), an ISO timestamp as foundry.core.ids renders it, a duration token.
RUN_ID_RE: Final[re.Pattern[str]] = re.compile(r"\d{8}T\d{6}-[0-9a-f]{6}")
ISO_TIMESTAMP_RE: Final[re.Pattern[str]] = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})"
)
DURATION_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"\d+(?:\.\d+)?\s*(?:µs|us|ms|s)\b")

# The element discriminators, of which a green run writes only the first, and the offline schemes.
RESULT_RECORD_TYPE: Final[str] = "result"
RUN_ERROR_RECORD_TYPE: Final[str] = "run_error"
SCHEMES: Final[tuple[str, ...]] = ("http://", "https://")


def _run_once(run_pytest: Callable[..., Any], repo_root: Path, out_dir: Path) -> str:
    """Run the example project once into ``out_dir`` and return the child's combined output."""
    out_dir.mkdir(parents=True, exist_ok=True)
    args = (
        "-q",
        EXAMPLE_DIR,
        "--foundry-json-report",
        str(out_dir / RESULTS_FILENAME),
        "--foundry-artifacts.dir",
        str(out_dir / ARTIFACTS_DIRNAME),
    )
    result = run_pytest(*args, cwd=repo_root)
    output = f"{result.stdout}\n{result.stderr}"
    assert result.returncode == 0, f"the run of {EXAMPLE_DIR} must exit 0:\n{output}"
    assert f"{EXPECTED_TOTAL} passed" in output, (
        f"the run into {out_dir} collected a suite other than the {EXPECTED_TOTAL} example tests "
        f"criterion 14 compares:\n{output}"
    )
    return output


def _load_json(path: Path) -> list[dict[str, Any]]:
    """Return the ``results.json`` elements at ``path``, each checked to be a JSON object."""
    assert path.is_file(), f"the run wrote no result document at {path} (AAP 0.4.6, A5)"
    document: Any = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(document, list), f"{path} holds {type(document).__name__}, not an array"
    elements: list[dict[str, Any]] = []
    for element in document:
        assert isinstance(element, dict), f"{path} holds the non-object element {element!r}"
        elements.append(element)
    return elements


def _load_text(path: Path) -> str:
    """Return the decoded text at ``path``, asserting the report was written at all."""
    assert path.is_file(), f"the run wrote no {path.name} at {path} (AAP 0.4.6)"
    return path.read_text(encoding="utf-8")


def _run_id_of(document: Sequence[dict[str, Any]]) -> str:
    """Return the one run id a run's result document carries."""
    identifiers = {str(element["run_id"]) for element in document if "run_id" in element}
    assert len(identifiers) == 1, f"one run id belongs in a document, found {sorted(identifiers)}"
    return identifiers.pop()


def _split(
    document: Sequence[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split a result document into its ``result`` records and its ``run_error`` elements."""
    known = (RESULT_RECORD_TYPE, RUN_ERROR_RECORD_TYPE)
    records = [element for element in document if element.get("record_type") == known[0]]
    errors = [element for element in document if element.get("record_type") == known[1]]
    unexpected = [element for element in document if element.get("record_type") not in known]
    assert not unexpected, f"a document holds only result and run_error elements: {unexpected}"
    return records, errors


def _normalise_results(
    document: Sequence[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Replace the four values criterion 14 permits, returning the document and the counts."""
    counts = {"run_id": 0, "timestamps": 0, "duration_ms": 0}
    normalised: list[dict[str, Any]] = []
    for element in document:
        replacement = dict(element)
        if "run_id" in replacement:
            replacement["run_id"] = SENTINEL_RUN_ID
            counts["run_id"] += 1
        for field in ("started_at", "finished_at"):
            if field in replacement:
                replacement[field] = SENTINEL_TIMESTAMP
                counts["timestamps"] += 1
        if "duration_ms" in replacement:
            replacement["duration_ms"] = SENTINEL_DURATION_MS
            counts["duration_ms"] += 1
        normalised.append(replacement)
    return normalised, counts


def _normalise_html(text: str) -> tuple[str, dict[str, int]]:
    """Replace the report's renderings of the four values, in the order criterion 14 fixes."""
    counts: dict[str, int] = {}
    text, counts["run_id"] = RUN_ID_RE.subn(SENTINEL_RUN_ID, text)
    text, counts["timestamps"] = ISO_TIMESTAMP_RE.subn(SENTINEL_TIMESTAMP, text)
    text, counts["durations"] = DURATION_TOKEN_RE.subn(SENTINEL_DURATION, text)
    return text.replace("\r\n", "\n"), counts


def _first_diff_lines(left: str, right: str, *, left_name: str, right_name: str) -> str:
    """Return the first :data:`DIFF_LINES` lines of a unified diff of two documents."""
    diff = difflib.unified_diff(
        left.splitlines(), right.splitlines(), left_name, right_name, n=2, lineterm=""
    )
    return "\n".join(list(diff)[:DIFF_LINES]) or "(the documents differ only in their ending)"


def _outcome_counts(records: Sequence[dict[str, Any]]) -> dict[str, int]:
    """Return the number of records per outcome value, so the run's outcome mix is compared."""
    counts: dict[str, int] = {}
    for record in records:
        outcome = str(record["outcome"])
        counts[outcome] = counts.get(outcome, 0) + 1
    return counts


def _record_invariants(records: Sequence[dict[str, Any]]) -> list[tuple[Any, ...]]:
    """Return the per-record values criterion 14 never normalises, in node-id order."""
    return [
        (
            record["nodeid"],
            record["outcome"],
            record["attempts"],
            tuple(record["markers"]),
            record["quarantine"]["state"],
            tuple((item["kind"], item["path"]) for item in record["artifacts"]),
        )
        for record in sorted(records, key=lambda record: str(record["nodeid"]))
    ]


def _nodeid_fragments(nodeid: str) -> tuple[str, str]:
    """Return the two quote-free fragments of a node id the report must still render.

    The report escapes every interpolated value, so a parametrised id is absent verbatim; the module
    path and the ``::name`` head carry no character Jinja2 rewrites, so erasure stays observable.
    """
    module, _, rest = nodeid.partition("::")
    return module, f"::{rest.partition('[')[0]}"


@dataclass(frozen=True)
class TwoRuns:
    """What two consecutive runs of the example project produced."""

    results_a: list[dict[str, Any]]
    results_b: list[dict[str, Any]]
    html_a: str
    html_b: str
    run_id_a: str
    run_id_b: str


@pytest.fixture(scope="module")
def two_runs(
    tmp_path_factory: pytest.TempPathFactory,
    repo_root: Path,
    run_pytest: Callable[..., Any],
) -> TwoRuns:
    """Module scope: run the example project twice, each into a directory of the run's own."""
    base = tmp_path_factory.mktemp("determinism")
    documents: list[list[dict[str, Any]]] = []
    reports: list[str] = []
    for name in ("run-a", "run-b"):
        out_dir = base / name
        _run_once(run_pytest, repo_root, out_dir)
        documents.append(_load_json(out_dir / RESULTS_FILENAME))
        reports.append(_load_text(out_dir / HTML_FILENAME))
    return TwoRuns(
        results_a=documents[0],
        results_b=documents[1],
        html_a=reports[0],
        html_b=reports[1],
        run_id_a=_run_id_of(documents[0]),
        run_id_b=_run_id_of(documents[1]),
    )


def test_two_runs_produce_identical_normalised_results(two_runs: TwoRuns) -> None:
    """Two consecutive runs' result documents match once the four permitted values are replaced."""
    records_a, errors_a = _split(two_runs.results_a)
    records_b, errors_b = _split(two_runs.results_b)
    for name, document, records, errors in (
        ("run-a", two_runs.results_a, records_a, errors_a),
        ("run-b", two_runs.results_b, records_b, errors_b),
    ):
        assert (len(document), len(records), errors) == (EXPECTED_TOTAL, EXPECTED_TOTAL, []), (
            f"{name} wrote {len(document)} elements holding {len(records)} records and the run "
            f"errors {errors}; the {EXPECTED_TOTAL}-test example suite writes one result record "
            "per test and no run error (AAP 0.4.6, AAP 0.9.1)"
        )

    assert two_runs.run_id_a != two_runs.run_id_b, (
        f"both runs report the run id {two_runs.run_id_a!r}, so this could be one run's document "
        f"read twice; a run id is YYYYmmddTHHMMSS-<{RUN_ID_HEX_DIGITS} hex> (AAP 0.4.6)"
    )
    nodeids_a = tuple(sorted(str(record["nodeid"]) for record in records_a))
    nodeids_b = tuple(sorted(str(record["nodeid"]) for record in records_b))
    assert nodeids_a == nodeids_b, (
        "the two runs collected different node ids, so they are not runs of one suite: only in "
        f"run-a {sorted(set(nodeids_a) - set(nodeids_b))}, only in run-b "
        f"{sorted(set(nodeids_b) - set(nodeids_a))}"
    )
    assert _outcome_counts(records_a) == _outcome_counts(records_b), (
        "the two runs disagree on their outcome mix before any normalisation: run-a "
        f"{_outcome_counts(records_a)}, run-b {_outcome_counts(records_b)} (AAP 0.4.9)"
    )
    assert _record_invariants(records_a) == _record_invariants(records_b), (
        "the two runs disagree on a value criterion 14 never normalises: a node id, outcome, "
        "attempt count, marker, quarantine state, or an artifact's kind and relative path"
    )

    normalised_a, counts_a = _normalise_results(records_a)
    normalised_b, counts_b = _normalise_results(records_b)
    assert counts_a == counts_b, f"the documents' shapes differ: run-a {counts_a}, run-b {counts_b}"
    assert counts_a["run_id"] >= 1, "no run_id was replaced, so this comparison is vacuous"
    assert counts_a["timestamps"] >= 2, f"no started_at/finished_at pair per record: {counts_a}"
    assert counts_a["duration_ms"] == EXPECTED_TOTAL, f"not one duration per record: {counts_a}"

    dumped_a = json.dumps(normalised_a, sort_keys=True, indent=2, ensure_ascii=False)
    dumped_b = json.dumps(normalised_b, sort_keys=True, indent=2, ensure_ascii=False)
    assert dumped_a == dumped_b, (
        "two consecutive runs differ in results.json in something other than the four values "
        "criterion 14 normalises (AAP 0.13.1, AAP 0.14.1):\n"
        + _first_diff_lines(dumped_a, dumped_b, left_name="run-a", right_name="run-b")
    )


def test_two_runs_produce_identical_normalised_html(two_runs: TwoRuns) -> None:
    """Two consecutive runs' reports match once the four permitted values are replaced."""
    for name, text in (("run-a", two_runs.html_a), ("run-b", two_runs.html_b)):
        assert len(text) >= MIN_HTML_LENGTH, (
            f"{name}'s report.html holds {len(text)} characters, below the {MIN_HTML_LENGTH} one "
            "report of this suite occupies, so it is truncated or empty"
        )
        assert not [scheme for scheme in SCHEMES if scheme in text], (
            f"{name}'s report.html references an external scheme, so it cannot open offline "
            "(AAP 0.4.6, implicit requirement I10)"
        )

    normalised_a, counts_a = _normalise_html(two_runs.html_a)
    normalised_b, counts_b = _normalise_html(two_runs.html_b)
    assert counts_a == counts_b, f"the reports' shapes differ: run-a {counts_a}, run-b {counts_b}"
    assert counts_a["run_id"] >= 1, "no run id was replaced, so this comparison is vacuous"
    assert counts_a["timestamps"] >= 1, "no timestamp was replaced, but finished_at is rendered"
    assert counts_a["durations"] >= EXPECTED_TOTAL, (
        f"fewer duration tokens than the {EXPECTED_TOTAL} tests the report renders: {counts_a}"
    )

    records, _ = _split(two_runs.results_a)
    outcome_words = tuple(sorted(_outcome_counts(records)))
    for name, raw, normalised in (
        ("run-a", two_runs.html_a, normalised_a),
        ("run-b", two_runs.html_b, normalised_b),
    ):
        missing = [
            fragment
            for record in records
            for fragment in _nodeid_fragments(str(record["nodeid"]))
            if fragment not in normalised
        ]
        assert not missing, (
            f"{name}'s normalised report.html no longer carries the node-id fragments {missing}, "
            "so the normalisation erased content rather than replacing the four permitted values"
        )
        raw_words = {word: raw.count(word) for word in outcome_words}
        rendered = {word: normalised.count(word) for word in outcome_words}
        assert raw_words == rendered, f"{name}'s outcome words changed: {raw_words} -> {rendered}"
        assert sum(rendered.values()) >= EXPECTED_TOTAL, f"{name}'s table is gone: {rendered}"

    assert normalised_a == normalised_b, (
        "two consecutive runs differ in report.html in something other than the four values "
        "criterion 14 normalises (AAP 0.13.1, AAP 0.14.1):\n"
        + _first_diff_lines(normalised_a, normalised_b, left_name="run-a", right_name="run-b")
    )
