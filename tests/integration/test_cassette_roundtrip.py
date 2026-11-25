"""Record then replay through a loopback echo server (AAP 0.14.1 criteria 7 and 8): one recorded
document, an order-insensitive replay that refuses every ``connect``, and the typed miss error."""

from __future__ import annotations

import hashlib
import json
import re
import socket
import subprocess
import threading
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socketserver import TCPServer
from typing import Any, Final

import httpx
import pytest

from foundry.cassette.paths import cassette_path

TEST_MODULE: Final[str] = "test_api.py"
NODEID: Final[str] = "test_api.py::test_echo"
ECHO_PATH: Final[str] = "/echo"
MISSING_PATH: Final[str] = "/not-recorded"
RECORD_QUERY: Final[tuple[tuple[str, str], ...]] = (("page", "2"), ("limit", "10"))
REPLAY_QUERY: Final[tuple[tuple[str, str], ...]] = (("limit", "10"), ("page", "2"))
POST_QUERY: Final[tuple[tuple[str, str], ...]] = (("trace", "abc"),)
POST_BODY: Final[dict[str, object]] = {"message": "hello"}
# No FOUNDRY_ prefix: the suite strips that prefix from a child's environment and this must arrive.
TOKEN_ENV: Final[str] = "ECHO_TEST_BEARER_TOKEN"
TOKEN_SENTINEL: Final[str] = "sentinel-bearer-token-5f3a9c41d7"
STATIC_HEADER_NAME: Final[str] = "x-foundry-client"
STATIC_HEADER_VALUE: Final[str] = "foundry-cassette-roundtrip"


def _pairs_literal(pairs: tuple[tuple[str, str], ...]) -> str:
    return "[" + ", ".join(f"({name!r}, {value!r})" for name, value in pairs) + "]"


def _result_records(results_path: Path) -> list[dict[str, object]]:
    elements = json.loads(results_path.read_text(encoding="utf-8"))
    assert isinstance(elements, list), f"{results_path} does not hold a JSON array"
    return [element for element in elements if element.get("record_type") == "result"]


class _QuietEchoServer(ThreadingHTTPServer):
    def server_bind(self) -> None:
        # HTTPServer.server_bind calls socket.getfqdn, a reverse lookup the offline rule forbids.
        TCPServer.server_bind(self)
        self.server_name = str(self.server_address[0])
        self.server_port = int(self.server_address[1])


class _EchoHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self._respond()

    def do_POST(self) -> None:
        self._respond()

    def log_message(self, format_string: str, *args: object) -> None:
        return None

    def _respond(self) -> None:
        body = json.dumps({"ok": True}).encode("utf-8")
        # send_response_only: send_response would add a Date header, a wall clock in the capture.
        self.send_response_only(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def echo_server() -> Iterator[tuple[str, Callable[[], None]]]:
    # Base URL plus a stop callable: the replay outlives its peer; port 0 keeps clones apart.
    server = _QuietEchoServer(("127.0.0.1", 0), _EchoHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def stop() -> None:
        if thread.is_alive():
            server.shutdown()
        server.server_close()
        thread.join(timeout=15.0)

    yield f"http://127.0.0.1:{server.server_address[1]}", stop
    stop()


# http.auth.token_env names the variable the credential comes from; no value lives in this file.
_RECORD_TOML: Final[str] = (
    f'[http]\ntimeout = 5.0\n\n[http.auth]\nstrategy = "bearer"\ntoken_env = "{TOKEN_ENV}"\n\n'
    f'[http.headers]\n"{STATIC_HEADER_NAME}" = "{STATIC_HEADER_VALUE}"\n'
)

# One child module serves both arms: the resolved mode decides the source each reply must name, and
# the record arm sends its query unsorted so the stored document proves the store sorts it.
_ECHO_MODULE: Final[str] = f"""from foundry.http import FoundrySession
def test_echo(foundry_api: FoundrySession, foundry_config) -> None:
    live = foundry_config.cassette.mode != "replay"
    query = {_pairs_literal(RECORD_QUERY)} if live else {_pairs_literal(REPLAY_QUERY)}
    trace = {_pairs_literal(POST_QUERY)}
    created = foundry_api.post({ECHO_PATH!r}, params=trace, json={POST_BODY!r})
    listing = foundry_api.get({ECHO_PATH!r}, params=query)
    assert created.status_code == listing.status_code == 200
    assert created.source == listing.source == ("live" if live else "cassette")
    assert created.json() == listing.json() == {{"ok": True}}
"""

_ECHO_FILES: Final[dict[str, str]] = {TEST_MODULE: _ECHO_MODULE}

_MISS_MODULE: Final[str] = f"""from foundry.http import FoundrySession
def test_echo(foundry_api: FoundrySession) -> None:
    foundry_api.get({MISSING_PATH!r})
"""

_MISS_FILES: Final[dict[str, str]] = {TEST_MODULE: _MISS_MODULE}

# The decisive zero-socket proof: a replay that fell back to the transport fails instead of passing.
_ALL_CONNECT_GUARD: Final[str] = """
import socket
def _refuse(address: object) -> None:
    raise RuntimeError(f"the replay project connected to {address!r}: replay opens no socket")
socket.socket.connect = lambda self, address: _refuse(address)
socket.socket.connect_ex = lambda self, address: _refuse(address)
"""


def test_record_then_replay_answers_from_the_cassette_and_a_miss_names_it(
    echo_server: tuple[str, Callable[[], None]],
    tmp_path: Path,
    write_project: Callable[..., Path],
    run_pytest: Callable[..., subprocess.CompletedProcess[str]],
    clean_foundry_env: None,
    socket_guard: Any,
) -> None:
    # Criterion 7: the token reaches the child through env only, under http.auth.token_env's name.
    peer_url, stop_peer = echo_server
    root = tmp_path.resolve()
    cassette_dir = (root / "cassettes").resolve()
    project = write_project(root / "record", files=_ECHO_FILES, foundry_toml=_RECORD_TOML)

    def arm(mode: str, directory: Path, url: str, env: dict[str, str] | None = None):
        options = (
            f"--foundry-cassette.dir={cassette_dir}",
            f"--foundry-artifacts.dir={directory / 'artifacts'}",
            f"--foundry-json-report={directory / 'results.json'}",
            f"--foundry-http.base_url={url}",
        )
        return run_pytest(mode, *options, cwd=directory, env=env)

    record = arm("--foundry-record", project, peer_url, {TOKEN_ENV: TOKEN_SENTINEL})
    assert record.returncode == 0, f"{record.stdout}\n{record.stderr}"
    # AAP 0.4.6: exactly one document per test, under the percent-encoded node id.
    expected = cassette_path(NODEID, cassette_dir=cassette_dir)
    assert sorted(cassette_dir.glob("*.json")) == [expected]
    records = _result_records(project / "results.json")
    assert [(str(r["nodeid"]), r["outcome"]) for r in records] == [(NODEID, "passed")]
    assert (project / "artifacts" / str(records[0]["run_id"])).is_dir()
    document = json.loads(text := expected.read_text(encoding="utf-8"))
    assert set(document) == {"recorded_at", "redacted_keys", "interactions"}
    assert datetime.fromisoformat(str(document["recorded_at"])).utcoffset() == timedelta(0)
    redacted_keys = document["redacted_keys"]
    assert isinstance(redacted_keys, list) and redacted_keys == sorted(redacted_keys)
    interactions = document["interactions"]
    assert isinstance(interactions, list) and len(interactions) == 2
    created, listing = interactions
    for interaction in interactions:
        request, response = interaction["request"], interaction["response"]
        assert set(interaction) == {"request", "response"}
        assert set(request) == {"method", "path", "query", "body_sha256", "headers_subset"}
        assert set(response) == {"status", "headers", "body"}
        assert response["status"] == 200 and response["body"]
    # The query is stored sorted although the GET sent it unsorted; the path has no address (A7).
    listing_request = listing["request"]
    assert (listing_request["method"], listing_request["path"]) == ("GET", ECHO_PATH)
    assert listing_request["query"] == [list(pair) for pair in REPLAY_QUERY]
    assert listing_request["query"] != [list(pair) for pair in RECORD_QUERY]
    assert listing_request["body_sha256"] is None
    created_request = created["request"]
    assert created_request["method"] == "POST"
    assert created_request["query"] == [list(pair) for pair in POST_QUERY]
    digest = created_request["body_sha256"]
    assert isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest) is not None
    sent = httpx.Request("POST", "http://127.0.0.1/", json=POST_BODY).content
    assert digest == hashlib.sha256(sent).hexdigest()
    # A7 and I4: neither the address nor the credential reaches the file; the static header does.
    assert (peer_url in text, TOKEN_SENTINEL in text) == (False, False)
    assert "authorization" in [str(name).casefold() for name in redacted_keys]
    assert listing_request["headers_subset"]["authorization"] == "***"
    assert listing_request["headers_subset"][STATIC_HEADER_NAME] == STATIC_HEADER_VALUE
    assert STATIC_HEADER_VALUE in text
    # Criterion 8: the peer is stopped and the replay port is new, so only a path key can answer.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        replay_url = f"http://127.0.0.1:{probe.getsockname()[1]}"
    stop_peer()
    before = socket_guard.non_loopback_attempts
    replay_project = write_project(root / "replay", files=_ECHO_FILES, conftest=_ALL_CONNECT_GUARD)
    replay = arm("--foundry-replay", replay_project, replay_url)
    # The parent guard cannot see the child; exit 0 under the all-connect guard is decisive.
    assert socket_guard.non_loopback_attempts == before
    # Criterion 8: both replies come from the cassette; AAP 0.7.3 matches query values, not order.
    assert replay.returncode == 0, f"{replay.stdout}\n{replay.stderr}"
    records = _result_records(replay_project / "results.json")
    rows = [(str(r["nodeid"]), r["outcome"], r["attempts"]) for r in records]
    assert rows == [(NODEID, "passed", 1)]
    miss_project = write_project(root / "miss", files=_MISS_FILES, conftest=_ALL_CONNECT_GUARD)
    missed = arm("--foundry-replay", miss_project, replay_url)
    # AAP 0.4.7: a replay miss fails that test -- exit 1 -- and never crashes the session.
    assert missed.returncode == 1, f"{missed.stdout}\n{missed.stderr}"
    miss_records = _result_records(miss_project / "results.json")
    assert [record["outcome"] for record in miss_records] == ["failed"]
    failures = miss_records[0]["failures"]
    assert isinstance(failures, list) and len(failures) == 1
    # Criterion 8: the failure names type, method, path and cassette, in the request's call phase.
    message = str(failures[0]["message"])
    named = ("CassetteMissError", f"GET {MISSING_PATH}", str(expected))
    assert [name for name in named if name not in message] == []
    assert failures[0]["phase"] == "call"
    # AAP 0.14.3: every path the three runs produced stays inside this test's own directory.
    produced = (project, replay_project, miss_project, cassette_dir)
    assert [path for path in produced if root not in path.resolve().parents] == []
