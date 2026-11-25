"""Unit tests for the artifact writer and the per-test manifest of ``foundry.obs``.

``artifacts`` writes the per-test layout AAP 0.4.6 publishes and disables capture with one
``WARNING`` and no exception, the AAP 0.4.7 state row proven here at unit level; ``manifest``
publishes the field set, the size and the digest of every file written. Redaction is asserted on
the bytes of a written artifact rather than on the object graph, because a writer that redacts
nothing passes the graph and leaks the file (AAP 0.3.2, 0.14.3, implicit requirement I4).

Split from ``tests/unit/test_obs.py`` when that module grew past the 700-line ceiling of AAP 0.14.2,
so the run id, profile, node id and secret sentinels are imported from it rather than restated and
the suite still describes one run.

Isolation (AAP 0.14.3): every input lives in ``tmp_path``, log capture goes through pytest's
``caplog``, and nothing sleeps, reads the wall clock or opens a socket.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

import pytest
from test_obs import _NODEID, _PROFILE, _RUN_ID, _SECRET_SENTINELS, _foundry_records

from foundry.core.types import ARTIFACT_KINDS
from foundry.obs import artifacts, manifest, redaction
from foundry.obs import logging as obs_logging

# The fixed creation time the manifest cases stamp their document with, so nothing reads the clock.
_CREATED_AT = "2025-01-02T03:04:05.678+00:00"


def _writer(root: Path, *, nodeid: str | None = _NODEID) -> artifacts.ArtifactWriter:
    """Return an artifact writer rooted at ``root`` and bound to one test as ``nodeid`` names it."""
    return artifacts.ArtifactWriter(
        root,
        run_id=_RUN_ID,
        profile=_PROFILE,
        redactor=redaction.default_redactor(),
        nodeid=nodeid,
    )


def _document(entries: tuple[manifest.ManifestEntry, ...] = ()) -> manifest.ManifestDocument:
    """Return a manifest document for this module's fixed run, profile and creation time."""
    return manifest.build_document(
        run_id=_RUN_ID,
        nodeid=_NODEID,
        profile=_PROFILE,
        created_at=_CREATED_AT,
        artifacts_disabled=False,
        artifacts=entries,
    )


def test_redaction_precedes_serialisation_of_the_written_artifact(tmp_path: Path) -> None:
    writer = _writer(tmp_path)

    reference = writer.write_json(
        "request",
        {
            "method": "POST",
            "url": "https://cart-user:hunter2secret@shop.example/p?token=tok_live_9f3",
            "headers": {"Authorization": "Bearer bearer-credential"},
        },
    )

    assert reference is not None
    assert reference.redacted is True
    written = (writer.run_dir / reference.path).read_text(encoding="utf-8")
    for sentinel in (*_SECRET_SENTINELS, "bearer-credential", "cart-user"):
        assert sentinel not in written
    assert written.count(redaction.REDACTED) == 2
    assert json.loads(written)["method"] == "POST"


def test_the_manifest_document_carries_the_published_field_set() -> None:
    document = _document()

    assert manifest.MANIFEST_SCHEMA_VERSION == 1
    assert manifest.MANIFEST_FILENAME == "manifest.json"
    assert document.schema_version == manifest.MANIFEST_SCHEMA_VERSION
    assert tuple(document.as_dict()) == (
        "schema_version",
        "run_id",
        "nodeid",
        "profile",
        "created_at",
        "artifacts_disabled",
        "artifacts",
    )
    assert document.as_dict()["nodeid"] == _NODEID
    assert document.as_dict()["artifacts_disabled"] is False
    # One entry per artifact file: its size, and the lowercase digest of the bytes on disk.
    payload = b'{"cart": []}\n'
    entry = manifest.entry_for("request", "node/request.json", payload, redacted=True)
    assert (entry.kind, entry.file) == ("request", "node/request.json")
    assert entry.bytes == len(payload)
    assert entry.sha256 == hashlib.sha256(payload).hexdigest() == entry.sha256.lower()
    assert entry.redacted is True
    assert tuple(entry.as_dict()) == ("kind", "file", "bytes", "sha256", "redacted")


def test_every_artifact_kind_has_one_published_file_name() -> None:
    assert len(ARTIFACT_KINDS) == 7
    assert set(artifacts.ARTIFACT_FILENAMES) == set(ARTIFACT_KINDS)
    assert artifacts.ARTIFACT_FILENAMES["log_tail"] == "log_tail.log"
    assert len(set(artifacts.ARTIFACT_FILENAMES.values())) == len(ARTIFACT_KINDS)
    assert frozenset({manifest.MANIFEST_FILENAME}) == artifacts.KEPT_WHEN_PASSED


def test_a_rendered_manifest_is_valid_json_ordered_and_stable() -> None:
    entries = (
        manifest.entry_for("stdout", "node/stdout.log", b"out\n", redacted=False),
        manifest.entry_for("request", "node/request.json", b"{}\n", redacted=False),
    )
    document = _document(entries)

    first = manifest.render(document)

    assert manifest.render(document) == first
    assert first.endswith("\n")
    assert not first.endswith("\n\n")
    payload = json.loads(first)
    assert [entry["kind"] for entry in payload["artifacts"]] == ["request", "stdout"]


def test_a_manifest_path_outside_the_run_directory_is_rejected() -> None:
    escaping = manifest.ManifestEntry(
        kind="request", file="../elsewhere/request.json", bytes=0, sha256="0" * 64, redacted=False
    )

    with pytest.raises(manifest.ArtifactError):
        _document((escaping,))


def test_dir_for_sanitises_the_node_id_into_one_path_component(tmp_path: Path) -> None:
    writer = _writer(tmp_path)

    directory = writer.dir_for(_NODEID)

    assert directory.is_dir()
    assert directory.is_relative_to(tmp_path / _RUN_ID)
    assert directory == artifacts.node_dir(writer.run_dir, _NODEID)
    assert "%2F" in directory.name
    assert "%3A" in directory.name
    for separator in ("/", "\\", "::", "[", "]"):
        assert separator not in directory.name


@pytest.mark.parametrize(
    "nodeid",
    [
        "test_auth.py::test_login",
        "test_secrets.py::test_alpha",
        "tests/test_api_key_flow.py::test_login",
    ],
)
def test_a_secret_shaped_node_id_names_its_own_directory(tmp_path: Path, nodeid: str) -> None:
    """A node id is an identifier: percent-encoded as pytest reports it, and never redacted."""
    writer = _writer(tmp_path)
    sibling = nodeid.replace("::test_", "::test_second_")

    directory = writer.dir_for(nodeid)

    assert directory == artifacts.node_dir(writer.run_dir, nodeid)
    assert "%2A" not in directory.name
    assert writer.dir_for(sibling).name != directory.name


def test_write_records_a_reference_to_the_bytes_on_disk(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    payload = '{"cart": []}\n'

    reference = writer.write("request", None, payload)

    assert reference is not None
    assert reference.kind == "request"
    assert reference.redacted is False
    assert reference.bytes == len(payload.encode("utf-8"))
    assert reference.sha256 == hashlib.sha256(payload.encode("utf-8")).hexdigest()
    assert (writer.run_dir / reference.path).read_bytes() == payload.encode("utf-8")
    assert not Path(reference.path).is_absolute()
    assert ".." not in Path(reference.path).parts
    # The log tail is one line per captured record, and an empty tail writes nothing at all.
    tail = writer.write_log_tail(["WARNING foundry: one", "INFO foundry: two"])
    assert tail is not None
    assert Path(tail.path).name == "log_tail.log"
    written = (writer.run_dir / tail.path).read_text(encoding="utf-8")
    assert written.splitlines() == ["WARNING foundry: one", "INFO foundry: two"]
    assert writer.write_log_tail([]) is None


def test_the_log_tail_keeps_a_foundry_records_label(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    secret = _SECRET_SENTINELS[0]
    lines = [f"INFO foundry.qa.secrets: token={secret}", f"INFO mytool.auth: token={secret}"]

    tail = writer.write_log_tail(lines)

    assert tail is not None
    written = (writer.run_dir / tail.path).read_text(encoding="utf-8")
    assert written.splitlines() == ["INFO foundry.qa.secrets: token=***", "INFO mytool.auth: ***"]


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("request", "request.json"),
        ("response", "response.json"),
        ("stdout", "stdout.log"),
        ("stderr", "stderr.log"),
        ("traceback", "traceback.txt"),
    ],
)
def test_write_uses_the_published_file_name_of_each_kind(
    tmp_path: Path, kind: str, expected: str
) -> None:
    writer = _writer(tmp_path)

    reference = writer.write_text(kind, "payload\n")  # type: ignore[arg-type]

    assert reference is not None
    assert Path(reference.path).name == expected
    assert (writer.run_dir / reference.path).read_text(encoding="utf-8") == "payload\n"


@pytest.mark.parametrize("nodeid", ["test_auth.py::test_login", "test_secret.py::test_alpha"])
def test_a_secret_shaped_node_id_writes_a_valid_manifest(tmp_path: Path, nodeid: str) -> None:
    writer = _writer(tmp_path, nodeid=nodeid)
    writer.write("request", None, '{"cart": []}')

    document = writer.write_manifest()

    assert document is not None
    payload = json.loads((writer.run_dir / document.path).read_text(encoding="utf-8"))
    assert payload["nodeid"] == nodeid
    assert all((writer.run_dir / entry["file"]).is_file() for entry in payload["artifacts"])


def test_the_manifest_lists_one_entry_per_written_file(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    request = writer.write("request", None, '{"cart": []}')
    stdout = writer.write_text("stdout", "picked 2 items\n")
    assert request is not None
    assert stdout is not None
    assert {reference.path for reference in writer.manifest} == {request.path, stdout.path}

    document = writer.write_manifest()

    assert document is not None
    assert Path(document.path).name == manifest.MANIFEST_FILENAME
    payload = json.loads((writer.run_dir / document.path).read_text(encoding="utf-8"))
    assert payload["schema_version"] == manifest.MANIFEST_SCHEMA_VERSION
    assert payload["artifacts_disabled"] is False
    assert [entry["file"] for entry in payload["artifacts"]] == sorted((request.path, stdout.path))
    for entry in payload["artifacts"]:
        written = writer.run_dir / entry["file"]
        assert entry["bytes"] == written.stat().st_size
        assert entry["sha256"] == hashlib.sha256(written.read_bytes()).hexdigest()
    # Finishing a passing test prunes every artifact but the manifest it just wrote.
    kept = writer.finalize(passed=True)
    assert [Path(reference.path).name for reference in kept] == [manifest.MANIFEST_FILENAME]
    assert sorted(path.name for path in writer.dir_for(_NODEID).iterdir()) == [
        manifest.MANIFEST_FILENAME
    ]


@pytest.mark.parametrize("capturing", [True, False])
def test_the_session_writer_answers_for_the_test_in_flight(tmp_path: Path, capturing: bool) -> None:
    """AAP 0.4.3: the unbound writer writes and reports through the test's own bound writer.

    ``foundry_artifacts`` hands out a session-scoped writer, so the binding the driver publishes for
    the test that is running is what makes ``write`` and ``manifest`` usable from a test body: the
    reference names a file inside that test's directory, and the manifest of both writers lists it.
    With no test capturing, both members refuse instead of guessing a directory.
    """
    session = _writer(tmp_path, nodeid=None)
    item = session.for_node(_NODEID)
    artifacts.set_active_writer(item if capturing else None)
    try:
        if not capturing:
            with pytest.raises(artifacts.ArtifactError):
                session.write("stdout", None, "published write")
            with pytest.raises(artifacts.ArtifactError):
                _ = session.manifest
            return
        reference = session.write("stdout", None, "published write")
        assert reference is not None
        written = session.run_dir / reference.path
        assert written.parent == item.dir_for(_NODEID)
        assert written.read_text(encoding="utf-8") == "published write"
        assert session.manifest == (reference,)
        assert item.manifest == (reference,)
    finally:
        artifacts.set_active_writer(None)


def test_an_uncreatable_run_directory_disables_capture_with_one_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root = tmp_path / "artifacts"
    root.write_text("a regular file where the artifacts root should be\n", encoding="utf-8")
    writer = _writer(root)
    caplog.set_level(logging.WARNING, logger=obs_logging.LOGGER_NAME)

    directory = writer.dir_for(_NODEID)

    assert not directory.exists()
    assert writer.disabled is True
    assert writer.manifest == ()
    # A writer derived for one test shares the run's fault state, which is the plugin's path.
    assert writer.for_node(_NODEID).disabled is True
    assert writer.write("request", None, '{"cart": []}') is None
    assert writer.write_json("response", {"status": 200}) is None
    assert writer.write_text("stdout", "out\n") is None
    assert writer.write_log_tail(["INFO foundry: x"]) is None
    assert writer.write_manifest() is None
    warnings = [record for record in _foundry_records(caplog) if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "cannot create artifact directory" in warnings[0].getMessage()


def test_a_failed_write_disables_capture_and_the_manifest_records_it(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    writer = _writer(tmp_path)
    request = writer.write("request", None, '{"cart": []}')
    assert request is not None
    blocker = writer.run_dir / Path(request.path).parent / artifacts.ARTIFACT_FILENAMES["response"]
    blocker.mkdir()
    caplog.set_level(logging.WARNING, logger=obs_logging.LOGGER_NAME)

    assert writer.write_json("response", {"status": 500}) is None
    assert writer.disabled is True

    document = writer.write_manifest()

    assert document is not None
    payload = json.loads((writer.run_dir / document.path).read_text(encoding="utf-8"))
    assert payload["artifacts_disabled"] is True
    assert [entry["kind"] for entry in payload["artifacts"]] == ["request"]
    assert writer.write_text("stdout", "after the fault\n") is None
    warnings = [record for record in _foundry_records(caplog) if record.levelno == logging.WARNING]
    assert len(warnings) == 1
