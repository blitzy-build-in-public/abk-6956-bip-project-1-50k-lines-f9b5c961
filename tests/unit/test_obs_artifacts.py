"""Unit tests for the artifact writer and the per-test manifest of ``foundry.obs``.

``artifacts`` writes the per-test layout AAP 0.4.6 publishes and disables capture with one
``WARNING`` and no exception (AAP 0.4.7); ``manifest`` publishes the field set, the size and the
digest of every file written. Redaction is asserted on the bytes written, because a writer that
redacts nothing passes the object graph and leaks the file (AAP 0.3.2, 0.14.3, I4). Every input
lives in ``tmp_path``, log capture goes through pytest's ``caplog``, and nothing sleeps, reads the
wall clock or opens a socket (AAP 0.14.3).
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

import pytest
from test_obs import _NODEID, _PROFILE, _RUN_ID, _SECRET_SENTINELS

from foundry.core.types import ARTIFACT_KINDS
from foundry.obs import artifacts, manifest, redaction
from foundry.obs import logging as obs_logging

# The fixed creation time the manifest cases stamp their document with, so nothing reads the clock.
_CREATED_AT = "2025-01-02T03:04:05.678+00:00"


def _writer(root: Path, *, nodeid: str | None = _NODEID) -> artifacts.ArtifactWriter:
    # A writer rooted at ``root`` and bound to one test as ``nodeid`` names it.
    return artifacts.ArtifactWriter(
        root, run_id=_RUN_ID, profile=_PROFILE, redactor=redaction.default_redactor(), nodeid=nodeid
    )


def _document(entries: tuple[manifest.ManifestEntry, ...] = ()) -> manifest.ManifestDocument:
    # A manifest document for this module's fixed run, profile and creation time.
    return manifest.build_document(
        run_id=_RUN_ID,
        nodeid=_NODEID,
        profile=_PROFILE,
        created_at=_CREATED_AT,
        artifacts_disabled=False,
        artifacts=entries,
    )


def _warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    # The foundry tree's own WARNING lines: a host logger's warning is not Foundry's fault line.
    root = obs_logging.LOGGER_NAME
    return [
        r for r in caplog.records if r.levelno == logging.WARNING and r.name.split(".")[0] == root
    ]


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
    assert reference is not None and reference.redacted is True
    written = (writer.run_dir / reference.path).read_text(encoding="utf-8")
    assert [s for s in (*_SECRET_SENTINELS, "bearer-credential", "cart-user") if s in written] == []
    assert written.count(redaction.REDACTED) == 2
    assert json.loads(written)["method"] == "POST"


def test_the_manifest_publishes_its_fields_digests_and_refuses_an_escape() -> None:
    document = _document()
    assert (manifest.MANIFEST_SCHEMA_VERSION, manifest.MANIFEST_FILENAME) == (1, "manifest.json")
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
    assert (entry.bytes, entry.redacted) == (len(payload), True)
    assert entry.sha256 == hashlib.sha256(payload).hexdigest() == entry.sha256.lower()
    assert tuple(entry.as_dict()) == ("kind", "file", "bytes", "sha256", "redacted")
    # Entries are ordered by kind, and the rendered document is stable from one call to the next.
    stdout = manifest.entry_for("stdout", "node/stdout.log", b"out\n", redacted=False)
    rendered = manifest.render(_document((stdout, entry)))
    assert manifest.render(_document((stdout, entry))) == rendered
    assert rendered.endswith("\n") and not rendered.endswith("\n\n")
    assert [row["kind"] for row in json.loads(rendered)["artifacts"]] == ["request", "stdout"]
    escaping = manifest.ManifestEntry(
        kind="request", file="../elsewhere/request.json", bytes=0, sha256="0" * 64, redacted=False
    )
    with pytest.raises(manifest.ArtifactError):
        _document((escaping,))


def test_every_artifact_kind_has_one_published_file_name() -> None:
    assert len(ARTIFACT_KINDS) == 7
    assert set(artifacts.ARTIFACT_FILENAMES) == set(ARTIFACT_KINDS)
    assert artifacts.ARTIFACT_FILENAMES["log_tail"] == "log_tail.log"
    assert len(set(artifacts.ARTIFACT_FILENAMES.values())) == len(ARTIFACT_KINDS)
    assert frozenset({manifest.MANIFEST_FILENAME}) == artifacts.KEPT_WHEN_PASSED


@pytest.mark.parametrize(
    "nodeid",
    [
        "tests/test_api.py::test_cart[1]",
        "test_secrets.py::test_alpha",
        "tests/test_api_key_flow.py::test_login",
    ],
)
def test_dir_for_sanitises_a_node_id_into_one_unredacted_component(
    tmp_path: Path, nodeid: str
) -> None:
    """Percent-encoded as pytest reports it, and never redacted into another test's directory."""
    writer = _writer(tmp_path)
    directory = writer.dir_for(nodeid)
    assert directory.is_dir() and directory.is_relative_to(tmp_path / _RUN_ID)
    assert directory == artifacts.node_dir(writer.run_dir, nodeid)
    assert directory.name.count("%2F") == nodeid.count("/")
    assert "%3A" in directory.name and "%2A" not in directory.name
    for separator in ("/", "\\", "::", "[", "]"):
        assert separator not in directory.name
    assert writer.dir_for(nodeid.replace("::test_", "::test_second_")).name != directory.name


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
def test_write_records_a_relative_reference_to_the_published_file_name(
    tmp_path: Path, kind: str, expected: str
) -> None:
    writer = _writer(tmp_path)
    payload = '{"cart": []}\n'
    reference = writer.write_text(kind, payload)  # type: ignore[arg-type]
    assert reference is not None and reference.redacted is False
    assert (reference.kind, Path(reference.path).name) == (kind, expected)
    assert (reference.bytes, reference.sha256) == (
        len(payload.encode("utf-8")),
        hashlib.sha256(payload.encode("utf-8")).hexdigest(),
    )
    assert not Path(reference.path).is_absolute() and ".." not in Path(reference.path).parts
    assert (writer.run_dir / reference.path).read_text(encoding="utf-8") == payload


def test_the_log_tail_keeps_a_foundry_records_label(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    secret = _SECRET_SENTINELS[0]
    lines = [f"INFO foundry.qa.secrets: token={secret}", f"INFO mytool.auth: token={secret}"]
    tail = writer.write_log_tail(lines)
    assert tail is not None and Path(tail.path).name == "log_tail.log"
    written = (writer.run_dir / tail.path).read_text(encoding="utf-8")
    assert written.splitlines() == ["INFO foundry.qa.secrets: token=***", "INFO mytool.auth: ***"]
    # One line per captured record, and an empty tail writes no file at all.
    assert writer.write_log_tail([]) is None


def test_the_manifest_lists_one_entry_per_written_file(tmp_path: Path) -> None:
    # A secret-shaped node id names its own directory and is published unsanitised.
    writer = _writer(tmp_path, nodeid="test_secrets.py::test_alpha")
    request = writer.write("request", None, '{"cart": []}')
    stdout = writer.write_text("stdout", "picked 2 items\n")
    assert request is not None and stdout is not None
    assert {reference.path for reference in writer.manifest} == {request.path, stdout.path}
    document = writer.write_manifest()
    assert document is not None and Path(document.path).name == manifest.MANIFEST_FILENAME
    payload = json.loads((writer.run_dir / document.path).read_text(encoding="utf-8"))
    assert (payload["schema_version"], payload["nodeid"], payload["artifacts_disabled"]) == (
        manifest.MANIFEST_SCHEMA_VERSION,
        "test_secrets.py::test_alpha",
        False,
    )
    assert [entry["file"] for entry in payload["artifacts"]] == sorted((request.path, stdout.path))
    for entry in payload["artifacts"]:
        written = writer.run_dir / entry["file"]
        assert (entry["bytes"], entry["sha256"]) == (
            written.stat().st_size,
            hashlib.sha256(written.read_bytes()).hexdigest(),
        )
    # Finishing a passing test prunes every artifact but the manifest it just wrote.
    kept = writer.finalize(passed=True)
    assert [Path(reference.path).name for reference in kept] == [manifest.MANIFEST_FILENAME]
    assert [path.name for path in writer.dir_for("test_secrets.py::test_alpha").iterdir()] == [
        manifest.MANIFEST_FILENAME
    ]


def test_an_uncreatable_run_directory_disables_capture_with_one_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root = tmp_path / "artifacts"
    root.write_text("a regular file where the artifacts root should be\n", encoding="utf-8")
    writer = _writer(root)
    caplog.set_level(logging.WARNING, logger=obs_logging.LOGGER_NAME)
    assert not writer.dir_for(_NODEID).exists()
    assert writer.disabled is True and writer.manifest == ()
    # A writer derived for one test shares the run's fault state, which is the plugin's path.
    assert writer.for_node(_NODEID).disabled is True
    assert writer.write("request", None, '{"cart": []}') is None
    assert writer.write_json("response", {"status": 200}) is None
    assert writer.write_text("stdout", "out\n") is None
    assert writer.write_log_tail(["INFO foundry: x"]) is None
    assert writer.write_manifest() is None
    warnings = _warnings(caplog)
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
    assert len(_warnings(caplog)) == 1
