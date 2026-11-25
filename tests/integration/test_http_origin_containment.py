"""A configured ``http.base_url`` contains the session's credential (AAP 0.3.2, implicit requirement
I9): an absolute target naming a second loopback listener is refused, so that peer records nothing,
while a path against the base URL carries the bearer token to the base URL's own peer."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socketserver import TCPServer
from typing import Final

import pytest
from pytest import MonkeyPatch

from foundry.core.exceptions import ConfigError
from foundry.http import AuthSettings, FoundrySession, SessionSettings

# No FOUNDRY_ prefix: the suite strips that prefix from an environment it hands on, and this must
# arrive under the name ``http.auth.token_env`` carries. The value is a scanner-safe stand-in, not
# a real credential.
TOKEN_ENV: Final[str] = "QA_SENTINEL_TOKEN_ENV"
TOKEN_SENTINEL: Final[str] = "sentinel-bearer-token-4c1e8d02a7"
STEAL_PATH: Final[str] = "/steal"
OK_PATH: Final[str] = "/ok"


class _QuietServer(ThreadingHTTPServer):
    """A loopback peer with an empty ``received`` list and no reverse lookup on bind."""

    received: list[dict[str, str]]

    def server_bind(self) -> None:
        # HTTPServer.server_bind calls socket.getfqdn, a reverse lookup the offline rule forbids.
        TCPServer.server_bind(self)
        self.server_name = str(self.server_address[0])
        self.server_port = int(self.server_address[1])


class _EchoHandler(BaseHTTPRequestHandler):
    """Answer every request 200 and keep the path and ``Authorization`` it arrived with."""

    def do_GET(self) -> None:
        self._respond()

    def log_message(self, format_string: str, *args: object) -> None:
        return None

    def _respond(self) -> None:
        server = self.server
        assert isinstance(server, _QuietServer)
        server.received.append(
            {"path": self.path, "authorization": self.headers.get("authorization", "")}
        )
        body = json.dumps({"ok": True}).encode("utf-8")
        # send_response_only: send_response would add a Date header, a wall clock in the capture.
        self.send_response_only(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@dataclass
class _Peer:
    """One running loopback peer: its URL and the requests it received."""

    server: _QuietServer
    thread: threading.Thread
    received: list[dict[str, str]] = field(default_factory=list)

    @property
    def url(self) -> str:
        """Return the base URL that reaches this peer on its ephemeral port."""
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def stop(self) -> None:
        """Shut the peer down and wait for its thread, so no test leaks a listener."""
        if self.thread.is_alive():
            self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=15.0)


def _start() -> _Peer:
    # Port 0: the kernel picks a free port per process, so clones and reruns cannot collide.
    server = _QuietServer(("127.0.0.1", 0), _EchoHandler)
    peer = _Peer(server=server, thread=threading.Thread(target=server.serve_forever, daemon=True))
    server.received = peer.received
    peer.thread.start()
    return peer


@pytest.fixture
def peers() -> Iterator[tuple[_Peer, _Peer]]:
    """A configured peer and a foreign listener, both closed when the test ends."""
    opened = [_start(), _start()]
    try:
        yield opened[0], opened[1]
    finally:
        for peer in opened:
            peer.stop()


def test_the_credential_never_travels_to_a_host_outside_the_base_origin(
    peers: tuple[_Peer, _Peer], monkeypatch: MonkeyPatch
) -> None:
    base, foreign = peers
    monkeypatch.setenv(TOKEN_ENV, TOKEN_SENTINEL)
    settings = SessionSettings(
        base_url=base.url, auth=AuthSettings(strategy="bearer", token_env=TOKEN_ENV)
    )
    target = f"{foreign.url}{STEAL_PATH}"
    with FoundrySession(settings, sleep=lambda seconds: None) as session:
        with pytest.raises(ConfigError) as raised:
            session.get(target)
        error = raised.value
        assert error.key == "http.base_url"
        # The refusal names the key and quotes neither the target nor the credential.
        surfaces = (str(error), error.format_message(), repr(error))
        assert [text for text in surfaces if target in text or TOKEN_SENTINEL in text] == []
        assert foreign.received == []
        # The session stays usable: the configured peer still answers, with the credential.
        accepted = session.get(OK_PATH)
    assert accepted.status_code == 200 and accepted.json() == {"ok": True}
    assert base.received == [{"path": OK_PATH, "authorization": f"Bearer {TOKEN_SENTINEL}"}]
    assert foreign.received == []
