"""End-to-end absence proof: the configured secret reaches none of the six sinks a run produces.

AAP 0.14.1 criterion 12 observes that a configured secret header appears as ``***`` in the HTML,
``results.json``, ``junit.xml`` and console output; AAP 0.14.3 makes "No secret reaches a file, the
terminal or a report" a constraint whose mechanism is :mod:`foundry.obs.redaction`. This module is
its evidence at integration scope (AAP 0.13.1): a real ``pytest`` child process drives a real
loopback echo server, and every file it leaves plus the terminal it printed is grepped as raw text.
``tests/unit/test_obs.py`` owns the engine's own rules.

The six sinks, each with the AAP row that makes it one:

1. ``results.json`` -- the JSON store renders the result records, failure messages included (0.4.6).
2. ``junit.xml`` -- the JUnit writer rewrites every message it emits (0.4.6, 0.7.3).
3. ``report.html`` -- the renderer escapes and redacts every interpolated value (0.7.4).
4. ``cassettes/<sanitized-nodeid>.json`` -- the store redacts at *capture* time, as implicit
   requirement I4 and the residual-channel analysis of AAP 0.2.1 require (0.3.2).
5. ``artifacts/<run_id>/<sanitized-nodeid>/`` -- the artifact writer scrubs every payload before a
   byte reaches the disk (0.3.2, 0.4.6).
6. the terminal -- pytest's own rendering of the child's stdout and stderr, the route AAP 0.2.1
   names as a leak channel and AAP 0.4.3 closes by making the assertion helpers redact the body
   excerpt they quote, so the only sanctioned failure message carries no credential.

**The secret travels by indirection only** (requirement I9, AAP 0.4.5): the token lives in the
variable :data:`TOKEN_VARIABLE`, which ``foundry.toml`` names through ``http.auth.token_env`` and
which holds no value itself. That name sits outside the ``FOUNDRY_`` prefix on purpose -- the child
environment strips every ``FOUNDRY_*`` name, so such a token would be gone before the child ran.

**Why the artifact sink is driven directly.** The per-test capture AAP 0.4.6 plans is not yet wired
into ``foundry.plugin``, so a run leaves ``artifacts/<run_id>/`` empty and walking it would prove
nothing. The sink is driven the way the framework writes it -- a real loopback exchange, then
:class:`foundry.obs.artifacts.ArtifactWriter`, the one module AAP 0.4.8 lets write under
``artifacts/`` -- and the run's own tree is walked too, so a plugin that starts writing those files
is held to the same assertions. Writing the tail through ``write_log_tail`` also proves the log tail
is *written* rather than suppressed (AAP 0.14.2). Every planted value is fake, no test waits, no
wall clock is read, the only socket is a loopback server shut down and joined, and every file lives
under ``tmp_path``.
"""

from __future__ import annotations

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

__all__: Final[tuple[str, ...]] = ("echo_server", "redaction_run")

# --- The planted values -----------------------------------------------------

TOKEN_VARIABLE: Final[str] = "ACME_API_TOKEN"
"""Name of the variable the bearer token travels in, never a value (AAP 0.4.5)."""

BASIC_USER_VARIABLE: Final[str] = "ACME_API_USER"
"""Name of the variable the basic-auth user id travels in, for the base64 route of the secret."""

BASIC_USER: Final[str] = "foundry-probe-user"
"""The basic-auth user id; with :data:`SENTINEL_TOKEN` it forms the base64-encoded credential."""

SENTINEL_TOKEN: Final[str] = "rdx-sentinel.T0KEN+4f8a/1c"
"""The credential every sink is grepped for.

``+`` and ``/`` are URL-escaped and both belong to the base64 alphabet, so a sink that encoded the
value instead of masking it is caught by :func:`_secret_forms`. Fake, and matching no provider's
credential pattern.
"""

TRACE_VALUE: Final[str] = "trace-not-a-secret-1"
"""The non-secret ``http.headers`` value that must survive every sink, so absence is not vacuous."""

CHILD_MODULE: Final[str] = "test_redaction.py"
"""File name of the child project's only test module."""

NODEID: Final[str] = f"{CHILD_MODULE}::test_leaks_nothing"
"""The child item's node id, the identity every sink has to carry."""

BEARER_NODEID: Final[str] = "test_redaction.py::test_bearer_is_masked"
"""Node id the bearer proof's artifact writer is bound to."""

BASIC_NODEID: Final[str] = "test_redaction.py::test_basic_is_masked"
"""Node id the basic proof's artifact writer is bound to."""

RUN_ID: Final[str] = "20250102T030405-9f3c1a"
"""Fixed run id, ``YYYYmmddTHHMMSS-<6 hex>``: a literal, so no clock names the sink tree."""

PROFILE: Final[str] = "default"
"""The profile the artifact writer stamps into ``manifest.json`` (AAP 0.4.5)."""

ABSENT_VARIABLE: Final[str] = "ACME_ABSENT_TOKEN"
"""The variable the absent-credential project names and never sets (AAP 0.4.7)."""

LOOPBACK_HOST: Final[str] = "127.0.0.1"
"""The only address the echo server binds, and one the suite's guard permits (AAP 0.14.3)."""

SHUTDOWN_TIMEOUT: Final[float] = 5.0
"""Seconds the teardown waits for the serving thread, so a wedged loop cannot hang the run."""

_ECHO_PATH: Final[str] = "/echo"
"""The path the child test requests; the handler echoes whatever path it is given."""

_WRITTEN_ARTIFACTS: Final[tuple[str, ...]] = (
    "request.json",
    "response.json",
    "stdout.log",
    "log_tail.log",
    "manifest.json",
)
"""The artifact files one proof writes: the names AAP 0.4.6 publishes for the kinds it captures."""

_CHILD_TEST_SOURCE: Final[str] = '''
"""A suite that leaks nothing by accident: one request, one log line, one failing assertion."""

import logging
import os

from foundry.assertions import assert_status

TRACE_ID = "trace-not-a-secret-1"


def test_leaks_nothing(foundry_api):
    """Request the echo endpoint with the configured credential, then fail through Foundry."""
    # Read from the variable foundry.toml names, so no credential is written in this file (I9).
    token = os.environ["ACME_API_TOKEN"]
    # INFO, not WARNING: pytest prints captured records at WARNING and above on a failure, and a
    # WARNING line carrying the header value would be rendered by the host rather than by a Foundry
    # sink -- the residual channel AAP 0.2.1 names and no Foundry writer can intercept.
    logging.getLogger("foundry").info(
        "caller sent x-trace-id=%s authorization=%s", TRACE_ID, token
    )
    response = foundry_api.get("/echo", params={"access_token": token})
    # The one sanctioned route for a body value into a failure message: assert_status quotes a
    # redacted, bounded excerpt (AAP 0.4.3), which a hand-written assert could not.
    assert_status(response, 500)
'''

_UNSET_TEST_SOURCE: Final[str] = '''
"""A suite whose configuration names a credential variable the run does not set."""


def test_never_runs():
    """Collection never starts: the resolution fault ends the session in pytest_configure."""
    raise AssertionError("the configuration fault had to end the session before this ran")
'''


# --- Value helpers ----------------------------------------------------------


def _secret_forms() -> tuple[str, ...]:
    """Return every spelling :data:`SENTINEL_TOKEN` must never take in a sink.

    Four forms, de-duplicated in order: the plain value, its URL-encoded form (the query a request
    line would carry, per the userinfo and token-parameter rules of :mod:`foundry.http.url`), its
    base64 form, and the base64 of the ``user:password`` credential a ``basic`` strategy sends.
    """
    credential = f"{BASIC_USER}:{SENTINEL_TOKEN}"
    forms = (
        SENTINEL_TOKEN,
        quote(SENTINEL_TOKEN, safe=""),
        base64.b64encode(SENTINEL_TOKEN.encode("utf-8")).decode("ascii"),
        base64.b64encode(credential.encode("utf-8")).decode("ascii"),
    )
    return tuple(dict.fromkeys(forms))


def _foundry_toml(base_url: str, *, token_env: str = TOKEN_VARIABLE) -> str:
    """Return a fixture project's ``foundry.toml``: bearer auth over a loopback base URL.

    ``http.auth.token_env`` names a variable rather than holding a value (requirement I9);
    ``http.headers`` carries the trace id the sinks must keep; ``cassette.mode`` is ``record``, the
    row of the mode table that writes a document at teardown (AAP 0.4.5, 0.7.3).
    """
    return (
        "[http]\n"
        f'base_url = "{base_url}"\n'
        "timeout = 5.0\n"
        "\n"
        "[http.headers]\n"
        f'x-trace-id = "{TRACE_VALUE}"\n'
        "\n"
        "[http.auth]\n"
        'strategy = "bearer"\n'
        f'token_env = "{token_env}"\n'
        "\n"
        "[cassette]\n"
        'mode = "record"\n'
    )


def _credential_settings(strategy: str) -> tuple[AuthSettings, dict[str, str]]:
    """Return the auth settings one strategy needs and the environment that satisfies them."""
    if strategy == "basic":
        settings = AuthSettings(
            strategy="basic", user_env=BASIC_USER_VARIABLE, password_env=TOKEN_VARIABLE
        )
        return settings, {BASIC_USER_VARIABLE: BASIC_USER, TOKEN_VARIABLE: SENTINEL_TOKEN}
    return (
        AuthSettings(strategy="bearer", token_env=TOKEN_VARIABLE),
        {TOKEN_VARIABLE: SENTINEL_TOKEN},
    )


def _load_json(path: pathlib.Path) -> object:
    """Return the document ``path`` holds, decoded with the standard library only."""
    return json.loads(path.read_text(encoding="utf-8"))


def _mapping(value: object, what: str) -> Mapping[str, object]:
    """Return ``value`` as a JSON object, failing the test when it is something else."""
    assert isinstance(value, dict), f"{what} is not a JSON object: {type(value).__name__}"
    return value


def _sequence(value: object, what: str) -> Sequence[object]:
    """Return ``value`` as a JSON array, failing the test when it is something else."""
    assert isinstance(value, list), f"{what} is not a JSON array: {type(value).__name__}"
    return value


def _text_key(record: Mapping[str, object], key: str, what: str) -> str:
    """Return one text field of a JSON object, failing the test when it is absent or not text."""
    value = record.get(key)
    assert isinstance(value, str) and value, f"{what} has no text {key!r}: {value!r}"
    return value


# --- The echo server --------------------------------------------------------


class _EchoHandler(BaseHTTPRequestHandler):
    """Answer every request with its own headers, path and method as one JSON object.

    Lower-cased header names are what make key-based redaction apply to the response *body*: the
    credential returns inside an object keyed ``authorization``, a name the deny-list masks.
    """

    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        """Answer a ``GET`` with the echo document."""
        self._echo()

    def do_POST(self) -> None:
        """Answer a ``POST`` with the echo document."""
        self._echo()

    def log_message(self, format: str, *args: object) -> None:
        """Write no access log: the echo is a fixture, not a service (AAP 0.13.1)."""

    def _echo(self) -> None:
        """Write the echo document for the request this handler was dispatched for."""
        raw_length = self.headers.get("Content-Length")
        length = int(raw_length) if raw_length is not None and raw_length.isdigit() else 0
        if length:
            self.rfile.read(length)
        payload = {
            "headers": {name.lower(): value for name, value in self.headers.items()},
            "method": self.command,
            "path": urlparse(self.path).path,
        }
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(scope="module")
def echo_server() -> Iterator[str]:
    """Module scope: the base URL of an in-process echo server bound to ``127.0.0.1:0``.

    A real :class:`http.server.ThreadingHTTPServer` on a kernel-assigned ephemeral port, served from
    a daemon thread, so every exchange here is a loopback round trip and no port outlives the
    module: the teardown shuts the server down, closes the socket and joins the thread within
    :data:`SHUTDOWN_TIMEOUT` (AAP 0.14.3), and yields ``http://127.0.0.1:<port>``.
    """
    server = ThreadingHTTPServer((LOOPBACK_HOST, 0), _EchoHandler)
    thread = threading.Thread(target=server.serve_forever, name="redaction-echo", daemon=True)
    thread.start()
    try:
        yield f"http://{LOOPBACK_HOST}:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=SHUTDOWN_TIMEOUT)


# --- The child run and the sinks it leaves ----------------------------------


@dataclass(frozen=True, slots=True)
class RunOutputs:
    """One child run's outputs, and the sinks read back out of them."""

    results: pathlib.Path
    junit: pathlib.Path
    report: pathlib.Path
    artifacts_root: pathlib.Path
    cassette_root: pathlib.Path
    returncode: int
    stdout: str
    stderr: str

    def files(self) -> tuple[tuple[str, pathlib.Path], ...]:
        """Return ``(label, path)`` for every file sink the run left behind, sorted by path.

        The report files first, then the cassette documents, then every file under ``artifacts/``
        walked recursively, so a capture the plugin starts writing is covered unchanged.
        """
        labelled: list[tuple[str, pathlib.Path]] = [
            ("results.json", self.results),
            ("junit.xml", self.junit),
            ("report.html", self.report),
        ]
        labelled.extend((f"cassette {path.name}", path) for path in self.cassette_documents())
        labelled.extend(
            (f"artifact {path.relative_to(self.artifacts_root).as_posix()}", path)
            for path in sorted(self.artifacts_root.rglob("*"))
            if path.is_file()
        )
        return tuple((label, path) for label, path in labelled if path.is_file())

    def sinks(self) -> tuple[tuple[str, str], ...]:
        """Return ``(label, text)`` for every sink, the terminal's two streams included."""
        texts = [(label, path.read_text(encoding="utf-8")) for label, path in self.files()]
        texts.append(("stdout", self.stdout))
        texts.append(("stderr", self.stderr))
        return tuple(texts)

    def cassette_documents(self) -> tuple[pathlib.Path, ...]:
        """Return the cassette documents the run recorded, sorted (AAP 0.4.6)."""
        return tuple(sorted(self.cassette_root.rglob("*.json")))

    def result_records(self) -> tuple[Mapping[str, object], ...]:
        """Return the run's ``record_type = "result"`` elements, which AAP 0.4.6 discriminates."""
        elements = _sequence(_load_json(self.results), "results.json")
        records = [
            record
            for record in (_mapping(element, "a results.json element") for element in elements)
            if record.get("record_type") == "result"
        ]
        return tuple(records)


@pytest.fixture
def redaction_run(
    tmp_path: pathlib.Path,
    write_project: Callable[..., pathlib.Path],
    run_pytest: Callable[..., object],
    echo_server: str,
) -> RunOutputs:
    """Function scope: one real ``pytest`` child run whose outputs all live under ``tmp_path``.

    The project is written by :func:`tests.conftest.write_project` with no ``conftest.py`` of its
    own: ``http.base_url`` goes into ``foundry.toml`` with the echo server's ephemeral port, so
    nothing inside the project can shadow the ``--foundry-*`` options passed here (AAP 0.4.4).
    ``--foundry-json-report`` relocates the result document with ``junit.xml`` and ``report.html``
    beside it (assumption A5); the artifact and cassette options keep both trees inside
    ``tmp_path``; and the token reaches the child through the environment only (requirement I9).
    """
    project = write_project(
        tmp_path / "project",
        files={CHILD_MODULE: _CHILD_TEST_SOURCE},
        foundry_toml=_foundry_toml(echo_server),
    )
    results = tmp_path / "results.json"
    completed = run_pytest(
        "-q",
        "-p",
        "no:cacheprovider",
        f"--foundry-artifacts.dir={tmp_path / 'artifacts'}",
        f"--foundry-cassette.dir={tmp_path / 'cassettes'}",
        f"--foundry-json-report={results}",
        cwd=project,
        env={TOKEN_VARIABLE: SENTINEL_TOKEN},
    )
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


# --- Assertion helpers ------------------------------------------------------


def _assert_carries_no_secret(label: str, text: str) -> None:
    """Assert ``text``, the content of sink ``label``, carries no form of the configured secret."""
    for form in _secret_forms():
        assert form not in text, (
            f"{label} carries the configured secret in its {form!r} form, so the sink was written "
            "without the redaction engine (AAP 0.3.2, 0.14.3)"
        )


def _assert_carries_a_secret(label: str, text: str) -> None:
    """Assert ``text`` really carries the secret, so a sink that wrote nothing cannot pass.

    The plain word for a ``Bearer`` header, or the base64 of ``user:password`` once
    :func:`foundry.http.auth.resolve_auth` has built a ``Basic`` one.
    """
    forms = [form for form in _secret_forms() if form in text]
    assert forms, (
        f"{label} carries no form of the configured secret, so absence there proves nothing: "
        f"none of {len(_secret_forms())} spellings appears"
    )


def _failed_record(run: RunOutputs) -> Mapping[str, object]:
    """Return the single result record of the child run, asserting it failed as designed."""
    records = run.result_records()
    assert len(records) == 1, f"the child run reported {len(records)} result records, not one"
    record = records[0]
    assert _text_key(record, "nodeid", "the result record") == NODEID, (
        f"the result record names another test: {record.get('nodeid')!r}"
    )
    outcome = _text_key(record, "outcome", "the result record")
    assert outcome == "failed", f"the child test failed by design, but its outcome is {outcome!r}"
    return record


def _write_artifact_files(
    server_url: str,
    redact_keys: Sequence[str],
    root: pathlib.Path,
    nodeid: str,
    *,
    strategy: str,
) -> tuple[FoundryResponse, Mapping[str, pathlib.Path]]:
    """Send one authenticated loopback exchange and write its dumps through the artifact writer.

    The writer is :class:`foundry.obs.artifacts.ArtifactWriter`, the single module AAP 0.4.8 allows
    to write under ``artifacts/``, and its engine is built from the keys the resolved configuration
    publishes, exactly as :func:`foundry.fixtures.artifacts.foundry_artifacts` builds it.
    ``stdout.log`` and ``log_tail.log`` are written from the *raw* echoed body and a *raw* log line,
    so the engine -- not the session's own dump -- is what has to scrub them. The response comes
    back with each written artifact's kind mapped to its absolute path.
    """
    auth, environ = _credential_settings(strategy)
    settings = SessionSettings(
        base_url=server_url,
        headers=(("x-trace-id", TRACE_VALUE),),
        auth=auth,
        redact_keys=tuple(redact_keys),
    )
    with FoundrySession(settings, environ=environ) as session:
        response = session.get(_ECHO_PATH, params={"access_token": SENTINEL_TOKEN})
    writer = ArtifactWriter(
        root,
        run_id=RUN_ID,
        profile=PROFILE,
        redactor=Redactor(settings.redact_keys),
        nodeid=nodeid,
    )
    # The line carries the credential beside a non-secret marker, so the written tail proves both
    # halves: the secret is masked and the line itself survives.
    tail = f"foundry.redaction INFO authorization={SENTINEL_TOKEN} x-trace-id={TRACE_VALUE}"
    references = (
        writer.write_json("request", response.request_dump()),
        writer.write_json("response", response.response_dump()),
        writer.write_text("stdout", response.text),
        writer.write_log_tail([tail]),
    )
    writer.finalize(passed=False)
    files: dict[str, pathlib.Path] = {}
    for reference in references:
        assert reference is not None, (
            f"the artifact writer refused one of its own writes for {nodeid}: capture disabled"
        )
        files[str(reference.kind)] = writer.run_dir / reference.path
    return response, files


def _artifact_texts(directory: pathlib.Path) -> Mapping[str, str]:
    """Assert one test's directory holds every artifact of this proof, and return text by name."""
    known = set(ARTIFACT_FILENAMES.values())
    unpublished = [name for name in _WRITTEN_ARTIFACTS if name not in known]
    assert not unpublished, (
        f"this proof names artifacts the writer does not publish: {unpublished}; "
        f"the writer's table holds {sorted(known)}"
    )
    texts = {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(directory.iterdir())
        if path.is_file()
    }
    missing = sorted(set(_WRITTEN_ARTIFACTS) - set(texts))
    assert not missing, f"the artifact directory {directory.name} is missing {missing}"
    return texts


# --- The absence proof ------------------------------------------------------


def test_no_sink_carries_the_configured_secret(redaction_run: RunOutputs) -> None:
    """The token reaches no sink of the run, in its plain, URL-encoded or base64 spelling.

    Criterion 12 and the AAP 0.14.3 constraint in one observation: the child failed by design, so
    every sink exists, and each is grepped as raw text rather than as parsed values, so a nested,
    URL-embedded or re-encoded leak is caught too.
    """
    assert redaction_run.returncode == 1, (
        "the child test fails by design, so its exit code is pytest's 1; it said:\n"
        f"{redaction_run.stdout[-2000:]}{redaction_run.stderr[-2000:]}"
    )
    sinks = redaction_run.sinks()
    labels = {label for label, _ in sinks}
    for required in ("results.json", "junit.xml", "report.html", "stdout", "stderr"):
        assert required in labels, f"the run left no {required} sink to grep: {sorted(labels)}"
    assert any(label.startswith("cassette ") for label in labels), (
        f"cassette.mode = record recorded no document, so that sink is unproven: {sorted(labels)}"
    )
    assert redaction_run.artifacts_root.is_dir(), (
        f"the run never created {redaction_run.artifacts_root}, so that sink is unproven"
    )
    for label, text in sinks:
        _assert_carries_no_secret(label, text)


def test_the_run_shows_the_non_secret_content_it_must_keep(redaction_run: RunOutputs) -> None:
    """The same run carries the trace id and the ``***`` mask, so nothing was suppressed.

    Absence alone can be met by writing nothing: these assertions pin the positive half of criterion
    12 -- the redacted excerpt, the node id in the report, the outcome in the JUnit document and the
    cassette's own record of what it scrubbed.
    """
    record = _failed_record(redaction_run)
    failures = _sequence(record.get("failures"), "the record's failures")
    assert len(failures) == 1, f"the failing test recorded {len(failures)} failures, not one"
    message = _text_key(_mapping(failures[0], "a failure record"), "message", "the failure record")
    assert TRACE_VALUE in message, (
        "the failure message lost the non-secret header value, so the excerpt was dropped: "
        f"{message[:200]!r}"
    )
    # Criterion 9's mask, matched case-insensitively: the echoed body carried the credential under
    # ``authorization``, and the excerpt AAP 0.4.3 publishes shows *** in its place.
    masked = re.search(rf'"?authorization"?\s*:\s*"{re.escape(REDACTED)}"', message, re.IGNORECASE)
    assert masked is not None, (
        f"the failure message carries no {REDACTED!r} mask for the authorization header: "
        f"{message[:400]!r}"
    )
    report = redaction_run.report.read_text(encoding="utf-8")
    assert NODEID in report, f"report.html does not name the failing test {NODEID!r}"
    assert TRACE_VALUE in report, "report.html lost the non-secret value of the failure panel"
    junit = redaction_run.junit.read_text(encoding="utf-8")
    # AAP 0.4.6 splits the node id: the module path is the class name, the test id the case name.
    for part in (f'classname="{CHILD_MODULE.removesuffix(".py")}"', 'name="test_leaks_nothing"'):
        assert part in junit, f"junit.xml lost the identity of the failing test: {part!r} is absent"
    assert 'name="foundry.outcome" value="failed"' in junit, (
        "junit.xml lost the outcome property AAP 0.4.6 publishes for the test case"
    )
    documents = redaction_run.cassette_documents()
    assert len(documents) == 1, f"the run recorded {len(documents)} cassette documents, not one"
    document = _mapping(_load_json(documents[0]), "the cassette document")
    redacted_keys = _sequence(document.get("redacted_keys"), "the cassette's redacted_keys")
    assert "authorization" in redacted_keys, (
        f"the cassette does not record scrubbing the authorization header: {redacted_keys!r}"
    )
    interactions = _sequence(document.get("interactions"), "the cassette's interactions")
    assert interactions, "the recorded cassette holds no interaction, so capture never ran"
    for element in interactions:
        request = _mapping(_mapping(element, "a cassette interaction").get("request"), "a request")
        headers = _mapping(request.get("headers_subset"), "the recorded request headers")
        assert headers.get("authorization") == REDACTED, (
            f"the cassette stored the credential verbatim: {headers.get('authorization')!r}"
        )
        assert headers.get("x-trace-id") == TRACE_VALUE, (
            "the cassette dropped the non-secret header the request carried"
        )
        pairs = [
            list(_sequence(pair, "a recorded query pair"))
            for pair in _sequence(request.get("query"), "the recorded request query")
        ]
        assert ["access_token", REDACTED] in pairs, (
            f"the cassette stored the access-token query value verbatim: {pairs!r}"
        )


def test_the_artifact_sink_redacts_every_file_it_writes(
    echo_server: str,
    foundry_config: FoundryConfig,
    tmp_path: pathlib.Path,
) -> None:
    """``request.json``, ``response.json``, ``stdout.log``, ``log_tail.log`` and ``manifest.json``.

    The artifact sink is driven the way the framework writes it -- a real loopback exchange, then
    :class:`foundry.obs.artifacts.ArtifactWriter` with the keys the configuration publishes -- for
    both credential strategies. ``stdout.log`` and ``log_tail.log`` are written from the raw echo
    body and a raw log line, which is where a sink that skipped the engine would show.
    """
    root = tmp_path / "artifacts"
    redact_keys = tuple(foundry_config.secrets.redact_keys)
    assert redact_keys, "secrets.redact_keys is empty, so no key-based mask could apply"
    proofs = {
        "bearer": _write_artifact_files(
            echo_server, redact_keys, root, BEARER_NODEID, strategy="bearer"
        ),
        "basic": _write_artifact_files(
            echo_server, redact_keys, root, BASIC_NODEID, strategy="basic"
        ),
    }
    for strategy, (response, files) in proofs.items():
        directory = next(iter(files.values())).parent
        texts = _artifact_texts(directory)
        # The control that keeps this proof honest: the payload the writer was handed really carries
        # the credential in the echoed body, so a writer that suppressed its output is caught by the
        # presence assertions below rather than passing by writing nothing.
        _assert_carries_a_secret(f"the raw {strategy} response body", response.text)
        for name, text in texts.items():
            _assert_carries_no_secret(f"{strategy} {name}", text)
        request = texts["request.json"]
        assert TRACE_VALUE in request, (
            f"the {strategy} request dump lost the non-secret header value it carried"
        )
        masked = re.search(
            rf'"authorization"\s*:\s*"{re.escape(REDACTED)}"', request, re.IGNORECASE
        )
        assert masked is not None, (
            f"the {strategy} request dump stored the credential verbatim: {request!r}"
        )
        assert TRACE_VALUE in texts["response.json"], (
            f"the {strategy} response dump lost the non-secret value the echo returned"
        )
        tail = texts["log_tail.log"]
        assert TRACE_VALUE in tail, f"the {strategy} log tail was suppressed rather than redacted"
        assert REDACTED in tail, f"the {strategy} log tail carries no mask for the credential"
        manifest = _mapping(_load_json(directory / ARTIFACT_FILENAMES["manifest"]), "manifest.json")
        entries = [
            _mapping(entry, "a manifest entry")
            for entry in _sequence(manifest.get("artifacts"), "the manifest's artifacts")
        ]
        rewritten = {str(entry.get("kind")) for entry in entries if entry.get("redacted") is True}
        assert {"stdout", "log_tail"} <= rewritten, (
            f"the {strategy} manifest does not record the redaction the engine performed: "
            f"{sorted(rewritten)}"
        )


def test_an_absent_credential_variable_exits_two_naming_only_the_variable(
    tmp_path: pathlib.Path,
    write_project: Callable[..., pathlib.Path],
    run_pytest: Callable[..., object],
) -> None:
    """A resolution that cannot find its variable ends the session with exit 2 (AAP 0.4.7).

    The message names the variable and never a value, and no report file is written at all -- the
    "Configured secret environment variable absent" row, observed rather than assumed.
    """
    project = write_project(
        tmp_path / "unset",
        files={CHILD_MODULE: _UNSET_TEST_SOURCE},
        # Port 1 is never dialled: the resolution fault ends the session before any fixture runs, so
        # the base URL only has to be a loopback URL the configuration model accepts.
        foundry_toml=_foundry_toml(f"http://{LOOPBACK_HOST}:1", token_env=ABSENT_VARIABLE),
    )
    results = tmp_path / "unset-results.json"
    completed = run_pytest(
        "-q",
        "-p",
        "no:cacheprovider",
        f"--foundry-json-report={results}",
        cwd=project,
        env={},
    )
    output = f"{getattr(completed, 'stdout', '')}{getattr(completed, 'stderr', '')}"
    assert int(getattr(completed, "returncode", -1)) == 2, (
        f"a configuration fault exits 2 (assumption A4); the terminal said:\n{output[-2000:]}"
    )
    assert ABSENT_VARIABLE in output, (
        f"the message does not name the unset variable {ABSENT_VARIABLE!r}:\n{output[-2000:]}"
    )
    _assert_carries_no_secret("the absent-variable terminal", output)
    assert not results.exists(), "a run that could not resolve writes no result document"
    assert not (project / "results.json").exists(), (
        "the run wrote a report into the project although no test was collected"
    )
