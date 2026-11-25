"""Artifacts, redaction and the bounded log tail: the three shapes ``report.html`` consumes.

AAP 0.8.1 gives this module three of the example project's 24 tests, asserting what the renderer
reads: the per-test directory ``artifacts/<run_id>/<sanitized-nodeid>/`` with its ``manifest.json``
(AAP 0.4.6), the redaction engine that scrubs a payload *before* anything is written (AAP 0.3.2),
and the ``logging.capture_lines``-sized ring behind ``tail(n)`` that becomes the report's failure
log panel (requirement I7). ``report.html`` itself is written at ``pytest_sessionfinish``, after
every test has finished, so no test here can open it -- ``tests/integration/test_report_render.py``
and ``tests/e2e/test_example_project.py`` assert that file -- and every assertion below runs
inside a test body, because ``artifacts.keep_passed`` defaults to ``false`` (AAP 0.4.5): a
passing test keeps only ``manifest.json``; each spends one ``_work.spend()`` call (AAP 0.14.2).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

import _work

if TYPE_CHECKING:
    import pytest

    from foundry.config.models import FoundryConfig
    from foundry.fixtures.logs import FoundryLogCapture
    from foundry.obs.artifacts import ArtifactWriter

_LOGGER_NAME = "foundry"  # the library's own logger; root logging is never configured here
_LOG_PREFIX = "checkout-log"  # prefix of every emitted line, so a filtered assertion stays precise
_EMITTED_RECORDS = 250  # emitted into a ring retaining 200, so the tail must drop the first 50
_CAPTURE_LINES = 200  # logging.capture_lines' default (AAP 0.4.5), the report's log-panel bound
_MASK = "***"  # the marker the redaction engine substitutes for every secret (AAP 0.3.2)
_SENTINEL = "SENTINEL-4b1d0c7e"  # one recognisable fake credential, so its absence is unambiguous
_STDOUT_PAYLOAD = "".join(f"checkout artifact line {index}\n" for index in range(3))


def test_artifact_writer_records_a_manifest(
    foundry_artifacts: ArtifactWriter,
    foundry_config: FoundryConfig,
    foundry_run_id: str,
    request: pytest.FixtureRequest,
) -> None:
    """One sanitised directory under the run root, holding a manifest that agrees with it.

    The session fixture hands out the run's writer; a test body writes through its documented
    per-test binding, because a session-scoped writer addresses no test and refuses ``write``.
    """
    directory = foundry_artifacts.dir_for(request.node.nodeid)
    assert directory.is_dir(), f"dir_for did not create the artifact directory {directory}"
    run_root = Path(foundry_config.artifacts.dir).resolve() / foundry_run_id
    assert directory.parent == run_root, f"{directory.parent} is not the run root {run_root}"
    encoded = directory.name
    for separator in ("/", "\\", "::", "[", "]"):
        assert separator not in encoded, f"unsanitised node id {encoded!r} carries {separator!r}"

    writer = foundry_artifacts.for_node(request.node.nodeid)
    reference = writer.write("stdout", None, _STDOUT_PAYLOAD)
    assert reference is not None, "the artifact writer refused a write, so capture is disabled"
    assert reference.kind == "stdout"
    assert reference.bytes == len(_STDOUT_PAYLOAD.encode("utf-8"))
    assert not reference.path.startswith("/"), f"the recorded path is absolute: {reference.path!r}"
    assert ".." not in PurePosixPath(reference.path).parts
    written = writer.run_dir / reference.path
    assert written.is_file(), f"the writer recorded {reference.path!r} but wrote no such file"
    assert written.read_text(encoding="utf-8") == _STDOUT_PAYLOAD
    assert written.parent == directory, f"{written} is not inside {directory}"

    manifest = writer.manifest
    assert isinstance(manifest, tuple)
    entry = next((item for item in manifest if item.path == reference.path), None)
    assert entry is not None, f"the written artifact is missing from the manifest: {manifest}"
    assert (entry.kind, entry.bytes, entry.sha256) == ("stdout", reference.bytes, reference.sha256)

    document = writer.write_manifest()
    assert document is not None, "the manifest could not be written, so capture was disabled"
    assert document.path.endswith("/manifest.json"), f"wrong manifest name: {document.path!r}"
    recorded = json.loads((writer.run_dir / document.path).read_text(encoding="utf-8"))
    assert recorded["schema_version"] == 1
    assert recorded["run_id"] == foundry_run_id
    assert recorded["nodeid"] == request.node.nodeid
    assert recorded["profile"] == str(foundry_config.profile)
    assert recorded["artifacts_disabled"] is False
    assert [item["file"] for item in recorded["artifacts"]] == [reference.path]
    assert {"kind", "file", "bytes", "sha256", "redacted"} == set(recorded["artifacts"][0])
    assert _work.spend() >= 0


def test_secret_headers_are_redacted_before_write(
    foundry_artifacts: ArtifactWriter, request: pytest.FixtureRequest
) -> None:
    """A request dump keeps its shape, loses every spelling of the secret, and masks both sinks."""
    writer = foundry_artifacts.for_node(request.node.nodeid)
    payload = {
        "headers": {
            "Authorization": f"Bearer {_SENTINEL}",
            "Cookie": f"session={_SENTINEL}",
            "Accept": "application/json",
        },
        "method": "GET",
        "query": {"access_token": _SENTINEL, "page": "1"},
        "url": f"http://127.0.0.1:9/orders?token={_SENTINEL}&page=1",
    }
    reference = writer.write_json("request", payload)
    assert reference is not None, "the artifact writer refused the request dump"
    assert reference.redacted is True, "the writer did not report the payload as redacted"
    text = (writer.run_dir / reference.path).read_text(encoding="utf-8")
    assert _SENTINEL not in text, f"a configured secret reached {reference.path}"

    # Exact values rather than a substring count: an engine that blanked the whole payload would
    # fail here as loudly as one that left the credential in place (AAP 0.3.2).
    dump = json.loads(text)
    masked = {name: value for name, value in dump["headers"].items() if value == _MASK}
    assert masked == {"Authorization": _MASK, "Cookie": _MASK}
    assert dump["headers"]["Accept"] == "application/json"
    assert dump["query"] == {"access_token": _MASK, "page": "1"}
    assert dump["url"] == "http://127.0.0.1:9/orders?page=1&token=***"
    assert dump["method"] == "GET"
    assert _SENTINEL not in repr(writer.manifest)
    assert _SENTINEL not in str(request.node.nodeid)
    assert _work.spend() >= 0


def test_log_capture_tail_is_bounded(foundry_log_capture: FoundryLogCapture) -> None:
    """250 records through a 200-record ring leave exactly the newest 200 in the tail."""
    logger = logging.getLogger(_LOGGER_NAME)
    assert foundry_log_capture.capacity == _CAPTURE_LINES
    foundry_log_capture.clear()
    for index in range(_EMITTED_RECORDS):
        logger.info("%s-%03d", _LOG_PREFIX, index)

    tail = foundry_log_capture.tail(_CAPTURE_LINES)
    assert len(tail) == _CAPTURE_LINES, f"the tail held {len(tail)} of {_CAPTURE_LINES} entries"
    assert tail[-1].endswith(f"{_LOG_PREFIX}-{_EMITTED_RECORDS - 1:03d}")
    indices = [int(line.rsplit("-", 1)[1]) for line in tail if f"{_LOG_PREFIX}-" in line]
    assert indices == list(range(_EMITTED_RECORDS - _CAPTURE_LINES, _EMITTED_RECORDS))
    assert len(foundry_log_capture.records) <= _CAPTURE_LINES
    assert foundry_log_capture.tail(0) == ()
    assert _work.spend() >= 0
