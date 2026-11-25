"""End-to-end absence proof: the configured secret reaches none of the six sinks a run produces.
AAP 0.14.3: the token travels only through the variable ``http.auth.token_env`` names (I9); every
sink -- results.json, junit.xml, report.html, the cassette, artifacts, the terminal -- is clean."""

import base64
import json
import pathlib
import re
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Final
from urllib.parse import quote, urlparse

import pytest

from foundry.config.models import FoundryConfig
from foundry.http.auth import AuthSettings
from foundry.http.response import FoundryResponse
from foundry.http.session import FoundrySession, SessionSettings
from foundry.obs.artifacts import ARTIFACT_FILENAMES, ArtifactWriter
from foundry.obs.redaction import REDACTED, Redactor

TOKEN_VARIABLE: Final[str] = "ACME_API_TOKEN"
USER_VARIABLE: Final[str] = "ACME_API_USER"
BASIC_USER: Final[str] = "foundry-probe-user"
# Fake, and matching no provider's credential pattern; "+" and "/" belong to the base64 alphabet,
# so a sink that encoded the value instead of masking it is caught by _secret_forms.
SENTINEL_TOKEN: Final[str] = "rdx-sentinel.T0KEN+4f8a/1c"
TRACE_VALUE: Final[str] = "trace-not-a-secret-1"
# The two shapes the reports name, which the probe peer echoes into every reply body it writes.
QUERIED_LINK: Final[str] = "https://api.example.test/a?p=1"
ESCAPED_PAIR: Final[str] = '\\"client_secret\\"'
CHILD_MODULE: Final[str] = "test_redaction.py"
NODEID: Final[str] = f"{CHILD_MODULE}::test_leaks_nothing"
RUN_ID: Final[str] = "20250102T030405-9f3c1a"
PROFILE: Final[str] = "default"
# The artifact kinds the sink walk requires, resolved through the writer's own ARTIFACT_FILENAMES.
_KINDS: Final[tuple[str, ...]] = ("request", "response", "stdout", "log_tail", "manifest")
# INFO, never WARNING: pytest prints WARNING records itself, a residual channel AAP 0.2.1 names.
_CHILD_TEST_SOURCE: Final[str] = """
import logging, os
from foundry.assertions import assert_status
def test_leaks_nothing(foundry_api):
    token = os.environ["ACME_API_TOKEN"]
    logging.getLogger("foundry").info("trace=%s authorization=%s", "trace-not-a-secret-1", token)
    response = foundry_api.get("/echo", params={"access_token": token})
    assert_status(response, 500)
"""


def _secret_forms() -> tuple[str, ...]:
    # The plain value, URL-encoded, base64, and the base64 of the whole basic credential.
    credential = base64.b64encode(f"{BASIC_USER}:{SENTINEL_TOKEN}".encode()).decode()
    encoded = base64.b64encode(SENTINEL_TOKEN.encode()).decode()
    forms = (SENTINEL_TOKEN, quote(SENTINEL_TOKEN, safe=""), encoded, credential)
    return tuple(dict.fromkeys(forms))


def _mapping(value: object, what: str) -> Mapping[str, object]:
    assert isinstance(value, dict), f"{what} is not a JSON object: {type(value).__name__}"
    return value


def _sequence(value: object, what: str) -> Sequence[object]:
    assert isinstance(value, list), f"{what} is not a JSON array: {type(value).__name__}"
    return value


class _EchoHandler(BaseHTTPRequestHandler):
    # Lower-cased names let key-based redaction reach the body's "authorization" object.
    def do_GET(self) -> None:
        length = self.headers.get("Content-Length")
        if length is not None and length.isdigit():
            self.rfile.read(int(length))
        headers = {name.lower(): value for name, value in self.headers.items()}
        payload = {"headers": headers, "method": self.command, "path": urlparse(self.path).path}
        token = headers.get("authorization", "").rpartition(" ")[2]
        # The two shapes the reports name, spliced after the sorted payload: the queried URL stands
        # adjacent to a secret-bearing pair, and a JSON document is carried inside a JSON string.
        shapes = {"link": QUERIED_LINK, "client_secret": token}
        shapes["echo"] = json.dumps({"client_secret": token})
        head = json.dumps(payload, sort_keys=True)[:-1]
        reply = f"{head}, {json.dumps(shapes)[1:-1]}}}".encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)

    def do_POST(self) -> None:
        self.do_GET()

    def log_message(self, format: str, *args: object) -> None:
        return None


@pytest.fixture(scope="module")
def echo_server() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _EchoHandler)
    thread = threading.Thread(target=server.serve_forever, name="redaction-echo", daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()
    thread.join(timeout=5.0)


@dataclass(frozen=True, slots=True)
class RunOutputs:
    results: pathlib.Path
    junit: pathlib.Path
    report: pathlib.Path
    artifacts_root: pathlib.Path
    cassette_root: pathlib.Path
    returncode: int
    stdout: str
    stderr: str

    def sinks(self) -> tuple[tuple[str, str], ...]:
        # Every file a run leaves, then the child's terminal, so a new capture is covered unchanged.
        named = {"results.json": self.results, "junit.xml": self.junit, "report.html": self.report}
        named |= {f"cassette {p.name}": p for p in sorted(self.cassette_root.rglob("*.json"))}
        files = sorted(p for p in self.artifacts_root.rglob("*") if p.is_file())
        named |= {f"artifact {p.relative_to(self.artifacts_root).as_posix()}": p for p in files}
        texts = [(label, p.read_text(encoding="utf-8")) for label, p in named.items() if p.exists()]
        return (*texts, ("stdout", self.stdout), ("stderr", self.stderr))

    def result_records(self) -> tuple[Mapping[str, object], ...]:
        elements = _sequence(json.loads(self.results.read_text(encoding="utf-8")), "results.json")
        records = [_mapping(element, "a results.json element") for element in elements]
        return tuple(record for record in records if record.get("record_type") == "result")


@pytest.fixture
def redaction_run(
    tmp_path: pathlib.Path,
    write_project: Callable[..., pathlib.Path],
    run_pytest: Callable[..., object],
    echo_server: str,
) -> RunOutputs:
    # base_url goes in foundry.toml, shadowing no --foundry-* option (AAP 0.4.4); token_env (I9).
    project = write_project(
        tmp_path / "project",
        files={CHILD_MODULE: _CHILD_TEST_SOURCE},
        foundry_toml=(
            f'[http]\nbase_url = "{echo_server}"\ntimeout = 5.0\n'
            f'[http.headers]\nx-trace-id = "{TRACE_VALUE}"\n[http.auth]\n'
            f'strategy = "bearer"\ntoken_env = "{TOKEN_VARIABLE}"\n[cassette]\nmode = "record"\n'
        ),
    )
    results = tmp_path / "results.json"
    options = (
        f"--foundry-json-report={results}",
        f"--foundry-artifacts.dir={tmp_path / 'artifacts'}",
        f"--foundry-cassette.dir={tmp_path / 'cassettes'}",
    )
    env = {TOKEN_VARIABLE: SENTINEL_TOKEN}
    completed = run_pytest("-p", "no:cacheprovider", *options, cwd=project, env=env)
    return RunOutputs(
        results=results,
        junit=results.with_name("junit.xml"),
        report=results.with_name("report.html"),
        artifacts_root=tmp_path / "artifacts",
        cassette_root=tmp_path / "cassettes",
        returncode=int(getattr(completed, "returncode", -1)),
        stdout=str(getattr(completed, "stdout", "")),
        stderr=str(getattr(completed, "stderr", "")),
    )


def _assert_carries_no_secret(label: str, text: str) -> None:
    for form in _secret_forms():
        assert form not in text, f"{label} carries the configured secret as {form!r} (AAP 0.3.2)"


def _assert_carries_a_secret(label: str, text: str) -> None:
    # The control: the payload really carried the value, so a sink that wrote nothing cannot pass.
    assert [f for f in _secret_forms() if f in text], f"{label} carries no form of the secret"


def _write_artifact_files(
    server_url: str, redact_keys: Sequence[str], root: pathlib.Path, nodeid: str, *, strategy: str
) -> tuple[FoundryResponse, Mapping[str, pathlib.Path]]:
    # The engine AAP 0.4.8 allows under artifacts/ must scrub its own stdout.log and log_tail.log.
    if strategy == "basic":
        auth = AuthSettings(strategy="basic", user_env=USER_VARIABLE, password_env=TOKEN_VARIABLE)
        environ = {USER_VARIABLE: BASIC_USER, TOKEN_VARIABLE: SENTINEL_TOKEN}
    else:
        auth = AuthSettings(strategy="bearer", token_env=TOKEN_VARIABLE)
        environ = {TOKEN_VARIABLE: SENTINEL_TOKEN}
    headers = (("x-trace-id", TRACE_VALUE),)
    settings = SessionSettings(
        base_url=server_url, headers=headers, auth=auth, redact_keys=tuple(redact_keys)
    )
    with FoundrySession(settings, environ=environ) as session:
        response = session.get("/echo", params={"access_token": SENTINEL_TOKEN})
    writer = ArtifactWriter(
        root, run_id=RUN_ID, profile=PROFILE, redactor=Redactor(settings.redact_keys), nodeid=nodeid
    )
    tail = f"foundry.redaction INFO authorization={SENTINEL_TOKEN} x-trace-id={TRACE_VALUE}"
    written = [
        writer.write_json("request", response.request_dump()),
        writer.write_json("response", response.response_dump()),
        writer.write_text("stdout", response.text),
        writer.write_log_tail([tail]),
    ]
    writer.finalize(passed=False)
    files = {str(ref.kind): writer.run_dir / ref.path for ref in written if ref}
    assert len(files) == len(written), f"the artifact writer refused a write for {nodeid}"
    return response, files


def _artifact_texts(directory: pathlib.Path) -> Mapping[str, str]:
    want = {ARTIFACT_FILENAMES[kind] for kind in _KINDS}
    texts = {p.name: p.read_text(encoding="utf-8") for p in directory.iterdir() if p.is_file()}
    assert want <= set(texts), f"{directory.name} is missing {sorted(want - set(texts))}"
    return texts


def test_no_sink_carries_the_configured_secret(redaction_run: RunOutputs) -> None:
    output = f"{redaction_run.stdout[-2000:]}{redaction_run.stderr[-2000:]}"
    assert redaction_run.returncode == 1, output
    sinks = redaction_run.sinks()
    labels = {label for label, _ in sinks}
    for required in ("results.json", "junit.xml", "report.html", "stdout", "stderr"):
        assert required in labels, f"the run left no {required} sink to grep: {sorted(labels)}"
    assert any(label.startswith("cassette ") for label in labels), sorted(labels)
    assert redaction_run.artifacts_root.is_dir(), redaction_run.artifacts_root
    for label, text in sinks:
        _assert_carries_no_secret(label, text)


def test_the_run_shows_the_non_secret_content_it_must_keep(redaction_run: RunOutputs) -> None:
    records = redaction_run.result_records()
    assert [(r.get("nodeid"), r.get("outcome")) for r in records] == [(NODEID, "failed")]
    failures = _sequence(records[0].get("failures"), "the record's failures")
    assert len(failures) == 1, f"the failing test recorded {len(failures)} failures, not one"
    message = str(_mapping(failures[0], "a failure record").get("message"))
    assert TRACE_VALUE in message, f"the message lost the non-secret value: {message[:200]!r}"
    # Criterion 9's mask, case-insensitive: the body echoed the credential under "authorization".
    mask = re.search(rf'"?authorization"?\s*:\s*"{re.escape(REDACTED)}"', message, re.IGNORECASE)
    assert mask is not None, f"no {REDACTED!r} mask for the authorization header: {message[:400]!r}"
    report = redaction_run.report.read_text(encoding="utf-8")
    assert NODEID in report and TRACE_VALUE in report, "report.html lost the test or trace value"
    # The cassette's shape and masks belong to test_cassette_roundtrip.py; the walk above covers it.


def test_the_artifact_sink_redacts_every_file_it_writes(
    echo_server: str, foundry_config: FoundryConfig, tmp_path: pathlib.Path
) -> None:
    root = tmp_path / "artifacts"
    redact_keys = tuple(foundry_config.secrets.redact_keys)
    assert redact_keys, "secrets.redact_keys is empty, so no key-based mask could apply"
    proofs = {
        strategy: _write_artifact_files(
            echo_server, redact_keys, root, f"{CHILD_MODULE}::{strategy}", strategy=strategy
        )
        for strategy in ("bearer", "basic")
    }
    for strategy, (response, files) in proofs.items():
        directory = next(iter(files.values())).parent
        texts = _artifact_texts(directory)
        # The control: the payload the writer was handed really carries the secret, in both shapes.
        _assert_carries_a_secret(f"the raw {strategy} response body", response.text)
        assert QUERIED_LINK in response.text and ESCAPED_PAIR in response.text, strategy
        for name, text in texts.items():
            _assert_carries_no_secret(f"{strategy} {name}", text)
        request = texts["request.json"]
        assert TRACE_VALUE in request, f"the {strategy} request dump lost the non-secret header"
        mask = re.search(rf'"authorization"\s*:\s*"{re.escape(REDACTED)}"', request, re.IGNORECASE)
        assert mask is not None, f"the {strategy} request dump stored the credential"
        assert TRACE_VALUE in texts["response.json"], f"{strategy} response dump lost the value"
        tail = texts["log_tail.log"]
        assert TRACE_VALUE in tail and REDACTED in tail, strategy
        manifest_path = directory / ARTIFACT_FILENAMES["manifest"]
        manifest = _mapping(json.loads(manifest_path.read_text(encoding="utf-8")), "manifest")
        artifacts = _sequence(manifest.get("artifacts"), "the manifest's artifacts")
        entries = [_mapping(entry, "a manifest entry") for entry in artifacts]
        rewritten = {str(entry.get("kind")) for entry in entries if entry.get("redacted") is True}
        assert {"stdout", "log_tail"} <= rewritten, sorted(rewritten)
