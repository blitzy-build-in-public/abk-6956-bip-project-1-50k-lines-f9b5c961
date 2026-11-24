"""Unit tests for the ``foundry.core`` foundation package.

The four modules under test define the vocabulary every other package speaks: the identifier
and timestamp formats, the node id encoding and its containment rule, the exception hierarchy
and the shared enums (AAP 0.4.3, AAP 0.7.2, AAP 0.4.8). The assertions here are therefore
contract assertions rather than implementation snapshots -- a renamed attribute, a weakened
pattern or a widened traversal rule fails this module before it reaches a behavioural test.

Two expectations differ from the brief this file was written against, because the module on
disk is authoritative (AAP 0.4.3 "persisted formats", AAP 0.7.2 "supporting records"):

* ``parse_iso8601`` reads a naive timestamp as UTC rather than rejecting it, so
  ``test_parse_iso8601_reads_naive_text_as_utc`` pins that behaviour and the rejection case is
  covered by text that is not a timestamp at all.
* ``sanitize_nodeid`` truncates a long encoding to ``MAX_NODEID_NAME_LENGTH`` characters *plus*
  a nine-character ``-<digest8>`` suffix, so a capped name is ``MAX_NODEID_NAME_LENGTH + 9``
  characters long rather than capped at the constant itself.

Determinism (AAP 0.14.3): every clock and randomness input is injected, no wall-clock value is
asserted and no delay is introduced, so the module is safe under ``-n auto`` and under
``tests/unit/test_sleep_policy.py``. Filesystem inputs are built in ``tmp_path`` and nothing
outside it is touched.
"""

from __future__ import annotations

import dataclasses
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import get_args, get_origin
from urllib.parse import unquote

import pytest

from foundry import core
from foundry.core import exceptions, ids, paths, types

_RUN_ID_LITERAL = re.compile(r"\d{8}T\d{6}-[0-9a-f]{6}")
"""A run-id shape written independently, so a broken pattern cannot satisfy its own test."""

_FIXED_MOMENT = datetime(2025, 1, 2, 3, 4, 5, 678000, tzinfo=UTC)
"""A fixed instant with milliseconds, used instead of the wall clock everywhere."""

_FIXED_RUN_ID = "20250102T030405-abc123"
"""What ``run_id`` must return for ``_FIXED_MOMENT`` and the suffix ``abc123``."""

_SAMPLE_NODEIDS = (
    "tests/test_a.py::test_b",
    "tests/test_a.py::test_b[1-2]",
    "tests/test_a.py::test_b[a/b]",
    "tests/test_a.py::test_c[param[0]]",
    "tests/test_a.py::TestClass::test_method",
)
"""Realistic node ids: each carries a separator, so each must encode to a single component."""

_EXCEPTION_CASES: tuple[
    tuple[type[exceptions.FoundryError], dict[str, object], dict[str, object]], ...
] = (
    (exceptions.FoundryError, {"message": "boom"}, {"message": "boom", "hint": None}),
    (
        exceptions.ConfigError,
        {
            "message": "unknown key: htpp",
            "key": "htpp",
            "layer": "file",
            "path": Path("foundry.toml"),
        },
        {"key": "htpp", "layer": "file", "path": Path("foundry.toml")},
    ),
    (
        exceptions.DataError,
        {
            "message": "row 4 is missing column 'price'",
            "file": Path("testdata/cart.csv"),
            "line": 4,
            "column": "price",
        },
        {"file": Path("testdata/cart.csv"), "line": 4, "column": "price"},
    ),
    (
        exceptions.ContractError,
        {
            "message": "$.items[0].id: not of type integer",
            "json_path": "$.items[0].id",
            "detail": "not of type integer",
        },
        {"json_path": "$.items[0].id", "detail": "not of type integer"},
    ),
    (
        exceptions.TransportError,
        {
            "message": "GET /cart: connection refused",
            "method": "GET",
            "path": "/cart",
            "cause": "connect",
        },
        {"method": "GET", "path": "/cart", "cause": "connect"},
    ),
    (
        exceptions.CassetteMissError,
        {
            "message": "POST /cart: no recorded interaction",
            "method": "POST",
            "path": "/cart",
            "cassette_path": Path("cassettes/tests%2Ftest_api.py%3A%3Atest_cart.json"),
        },
        {
            "method": "POST",
            "path": "/cart",
            "cassette_path": Path("cassettes/tests%2Ftest_api.py%3A%3Atest_cart.json"),
        },
    ),
    (
        exceptions.ArtifactError,
        {
            "message": "path escapes the run directory",
            "attempted_path": Path("/tmp/elsewhere/request.json"),
            "run_dir": Path("/tmp/run"),
        },
        {"attempted_path": Path("/tmp/elsewhere/request.json"), "run_dir": Path("/tmp/run")},
    ),
)
"""One constructor call per exception class, with the attributes AAP 0.4.3 documents for it."""


# --- Run ids (AAP 0.4.3, AAP 0.7.1) ----------------------------------------


def test_run_id_matches_the_published_and_an_independent_pattern() -> None:
    value = ids.run_id()
    assert ids.RUN_ID_PATTERN.fullmatch(value) is not None
    assert _RUN_ID_LITERAL.fullmatch(value) is not None
    assert ids.is_valid_run_id(value) is True


def test_run_id_is_unique_across_calls() -> None:
    values = [ids.run_id() for _ in range(20)]
    assert len(set(values)) == len(values)
    assert all(_RUN_ID_LITERAL.fullmatch(value) is not None for value in values)


def test_run_id_uses_the_injected_clock_and_suffix() -> None:
    requested: list[int] = []

    def fixed_token_hex(byte_count: int) -> str:
        requested.append(byte_count)
        return "abc123"

    value = ids.run_id(now=_FIXED_MOMENT, token_hex=fixed_token_hex)
    assert value == _FIXED_RUN_ID
    assert value.startswith(_FIXED_MOMENT.strftime(ids.RUN_ID_FORMAT))
    assert requested == [ids.RUN_ID_TOKEN_HEX_DIGITS // 2]


def test_run_id_converts_an_aware_offset_to_utc() -> None:
    offset_moment = datetime(2025, 1, 2, 5, 4, 5, tzinfo=timezone(timedelta(hours=2)))
    value = ids.run_id(now=offset_moment, token_hex=lambda _count: "abc123")
    assert value == _FIXED_RUN_ID


@pytest.mark.parametrize("suffix", ["", "abc12", "abc1234", "ABC123", "abcxyz", "abc123 "])
def test_run_id_rejects_a_malformed_suffix(suffix: str) -> None:
    with pytest.raises(exceptions.FoundryError) as excinfo:
        ids.run_id(now=_FIXED_MOMENT, token_hex=lambda _count: suffix)
    assert "suffix" in excinfo.value.message


@pytest.mark.parametrize(
    "value",
    [
        "",
        "20250102T030405",
        "20250102T030405-9f3c1",
        "20250102T030405-9f3c1a1",
        "20250102T030405-9F3C1A",
        "20250102T030405-9f3c1a\n",
        "2025-01-02T03:04:05-9f3c1a",
    ],
)
def test_is_valid_run_id_rejects_malformed_values(value: str) -> None:
    assert ids.is_valid_run_id(value) is False


def test_run_id_constants_describe_the_documented_shape() -> None:
    assert ids.RUN_ID_TOKEN_HEX_DIGITS == 6
    assert ids.RUN_ID_FORMAT == "%Y%m%dT%H%M%S"
    assert isinstance(ids.RUN_ID_PATTERN, re.Pattern)
    assert ids.RUN_ID_PATTERN.fullmatch(_FIXED_RUN_ID) is not None
    assert ids.is_valid_run_id(_FIXED_RUN_ID) is True


# --- ISO 8601 timestamps ---------------------------------------------------


def test_to_iso8601_renders_millisecond_precision_in_utc() -> None:
    text = ids.to_iso8601(_FIXED_MOMENT)
    assert text == "2025-01-02T03:04:05.678+00:00"
    assert text.endswith("+00:00")
    assert not text.endswith("Z")


def test_to_iso8601_converts_an_aware_offset_to_utc() -> None:
    offset_moment = datetime(2025, 1, 2, 5, 4, 5, 678000, tzinfo=timezone(timedelta(hours=2)))
    assert ids.to_iso8601(offset_moment) == ids.to_iso8601(_FIXED_MOMENT)


def test_to_iso8601_reads_a_naive_datetime_as_utc() -> None:
    naive = datetime(2025, 1, 2, 3, 4, 5, 678000)
    assert ids.to_iso8601(naive) == ids.to_iso8601(_FIXED_MOMENT)


@pytest.mark.parametrize(
    "moment",
    [
        _FIXED_MOMENT,
        datetime(2024, 2, 29, 23, 59, 59, 999000, tzinfo=UTC),
        datetime(1970, 1, 1, 0, 0, 0, tzinfo=UTC),
    ],
)
def test_iso8601_survives_a_round_trip(moment: datetime) -> None:
    assert ids.parse_iso8601(ids.to_iso8601(moment)) == moment


def test_parse_iso8601_accepts_a_trailing_z_suffix() -> None:
    assert ids.parse_iso8601("2025-01-02T03:04:05.678Z") == _FIXED_MOMENT


def test_parse_iso8601_normalises_an_offset_to_utc() -> None:
    parsed = ids.parse_iso8601("2025-01-02T05:04:05.678+02:00")
    assert parsed == _FIXED_MOMENT
    assert parsed.utcoffset() == timedelta(0)
    assert parsed.hour == 3


def test_parse_iso8601_reads_naive_text_as_utc() -> None:
    parsed = ids.parse_iso8601("2025-01-02T03:04:05.678")
    assert parsed == _FIXED_MOMENT
    assert parsed.utcoffset() == timedelta(0)


@pytest.mark.parametrize("text", ["", "not a timestamp", "2025-13-02T03:04:05+00:00"])
def test_parse_iso8601_rejects_text_that_is_not_a_timestamp(text: str) -> None:
    with pytest.raises(exceptions.FoundryError) as excinfo:
        ids.parse_iso8601(text)
    assert repr(text) in excinfo.value.message
    assert excinfo.value.hint is not None


def test_utc_now_returns_an_aware_utc_datetime() -> None:
    moment = ids.utc_now()
    assert isinstance(moment, datetime)
    assert moment.utcoffset() == timedelta(0)


# --- Stable case ids (AAP 0.7.2, implicit requirement I6) ------------------


def test_case_id_for_row_is_the_stem_and_the_row_index() -> None:
    assert ids.case_id_for_row("cases", 3) == "cases-3"
    assert ids.case_id_for_row("cart", 0) == "cart-0"


def test_case_id_for_row_rejects_a_negative_index() -> None:
    with pytest.raises(exceptions.FoundryError):
        ids.case_id_for_row("cases", -1)


@pytest.mark.parametrize("stem", ["", "   "])
def test_case_id_helpers_require_a_stem(stem: str) -> None:
    with pytest.raises(exceptions.FoundryError):
        ids.case_id_for_row(stem, 0)
    with pytest.raises(exceptions.FoundryError):
        ids.case_id_for_matrix(stem, {"size": "L"})


def test_case_id_for_matrix_sorts_the_axes() -> None:
    first = {"size": "large", "colour": "red"}
    second = dict(reversed(list(first.items())))
    assert ids.case_id_for_matrix("matrix", first) == ids.case_id_for_matrix("matrix", second)
    assert ids.case_id_for_matrix("matrix", first) == (
        f"matrix-colour={json.dumps('red')}-size={json.dumps('large')}"
    )


def test_case_id_for_matrix_canonicalises_values_that_need_quoting() -> None:
    label = 'a "b" c'
    assert ids.case_id_for_matrix("m", {"label": label}) == f"m-label={json.dumps(label)}"
    assert ids.case_id_for_matrix("m", {"nested": {"b": 1, "a": 2}}) == 'm-nested={"a":2,"b":1}'


def test_case_id_for_matrix_without_axes_is_the_stem() -> None:
    assert ids.case_id_for_matrix("matrix", {}) == "matrix"


def test_case_id_for_matrix_rejects_a_non_string_axis_name() -> None:
    with pytest.raises(exceptions.FoundryError) as excinfo:
        ids.case_id_for_matrix("m", {1: "L"})
    assert "axis names" in excinfo.value.message


def test_case_id_for_matrix_rejects_a_value_json_cannot_render() -> None:
    class Unrenderable:
        def __str__(self) -> str:
            raise TypeError("cannot render")

    with pytest.raises(exceptions.FoundryError) as excinfo:
        ids.case_id_for_matrix("m", {"axis": Unrenderable()})
    assert "not serialisable" in excinfo.value.message
    assert excinfo.value.hint is not None


# --- Node id sanitisation (AAP 0.3.2 "Path safety") ------------------------


@pytest.mark.parametrize("nodeid", _SAMPLE_NODEIDS)
def test_sanitize_nodeid_yields_one_safe_component(nodeid: str) -> None:
    name = paths.sanitize_nodeid(nodeid)
    assert "/" not in name
    assert "\\" not in name
    assert "::" not in name
    assert name not in {".", ".."}
    assert Path(name).parts == (name,)


def test_sanitize_nodeid_is_injective_over_the_sample() -> None:
    names = [paths.sanitize_nodeid(nodeid) for nodeid in _SAMPLE_NODEIDS]
    assert len(set(names)) == len(names)
    assert [unquote(name) for name in names] == list(_SAMPLE_NODEIDS)


def test_sanitize_nodeid_truncates_a_long_id_deterministically() -> None:
    shared = "tests/test_long.py::test_" + "x" * 400
    first = paths.sanitize_nodeid(shared)
    other = paths.sanitize_nodeid(shared + "y")
    assert len(first) == paths.MAX_NODEID_NAME_LENGTH + 9
    assert first.startswith(paths.sanitize_nodeid(shared[:40]))
    assert first[-9] == "-"
    assert re.fullmatch(r"[0-9a-f]{8}", first[-8:]) is not None
    assert first != other
    assert paths.sanitize_nodeid(shared) == first


@pytest.mark.parametrize("nodeid", ["", "   "])
def test_sanitize_nodeid_rejects_a_blank_node_id(nodeid: str) -> None:
    with pytest.raises(exceptions.FoundryError):
        paths.sanitize_nodeid(nodeid)


# --- The traversal guard (AAP 0.14.3 "No traversal escape") ----------------


def test_resolve_under_returns_a_path_inside_the_root(tmp_path: Path) -> None:
    resolved = paths.resolve_under(tmp_path, "requests/one.json")
    assert resolved == tmp_path.resolve() / "requests" / "one.json"
    assert resolved.is_relative_to(tmp_path.resolve())


def test_resolve_under_accepts_the_root_itself(tmp_path: Path) -> None:
    assert paths.resolve_under(tmp_path, tmp_path) == tmp_path.resolve()


def test_resolve_under_rejects_a_parent_escape(tmp_path: Path) -> None:
    with pytest.raises(exceptions.ArtifactError) as excinfo:
        paths.resolve_under(tmp_path, "../escaped.json")
    error = excinfo.value
    assert error.run_dir == tmp_path.resolve()
    assert error.attempted_path == (tmp_path.parent / "escaped.json").resolve()
    assert Path(error.attempted_path) == error.attempted_path
    assert str(error.attempted_path) in str(error)


def test_resolve_under_rejects_an_absolute_path_outside_the_root(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.json"
    with pytest.raises(exceptions.ArtifactError) as excinfo:
        paths.resolve_under(tmp_path, outside)
    assert excinfo.value.attempted_path == outside.resolve()
    assert excinfo.value.run_dir == tmp_path.resolve()


def test_resolve_under_rejects_a_symlink_pointing_outside(tmp_path: Path) -> None:
    root = tmp_path / "run"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(exceptions.ArtifactError) as excinfo:
        paths.resolve_under(root, "link/request.json")
    assert excinfo.value.run_dir == root.resolve()
    assert excinfo.value.attempted_path == (outside / "request.json").resolve()


def test_artifact_dir_for_builds_the_run_layout(tmp_path: Path) -> None:
    run_dir = tmp_path / "artifacts" / _FIXED_RUN_ID
    nodeid = "tests/test_a.py::test_b[a/b]"
    directory = paths.artifact_dir_for(run_dir, nodeid)
    assert directory == run_dir.resolve() / paths.sanitize_nodeid(nodeid)
    assert paths.relative_artifact_path(directory, run_dir) == paths.sanitize_nodeid(nodeid)


def test_relative_artifact_path_is_posix_and_free_of_parent_segments(tmp_path: Path) -> None:
    run_dir = tmp_path / "artifacts" / _FIXED_RUN_ID
    nested = run_dir / paths.sanitize_nodeid("tests/test_a.py::test_b") / "request.json"
    absolute = paths.relative_artifact_path(nested, run_dir)
    relative = paths.relative_artifact_path(
        Path(paths.sanitize_nodeid("tests/test_a.py::test_b")) / "request.json",
        run_dir,
    )
    assert absolute == f"{paths.sanitize_nodeid('tests/test_a.py::test_b')}/request.json"
    assert relative == absolute
    assert "\\" not in absolute
    assert ".." not in absolute.split("/")


def test_relative_artifact_path_of_the_run_directory_is_dot(tmp_path: Path) -> None:
    assert paths.relative_artifact_path(tmp_path, tmp_path) == "."


def test_relative_artifact_path_rejects_an_escape(tmp_path: Path) -> None:
    with pytest.raises(exceptions.ArtifactError):
        paths.relative_artifact_path(tmp_path.parent / "stray.log", tmp_path)


# --- The exception hierarchy (AAP 0.4.3, AAP 0.4.9) ------------------------


@pytest.mark.parametrize(("error_class", "kwargs", "attributes"), _EXCEPTION_CASES)
def test_each_exception_is_a_foundry_error_carrying_its_context(
    error_class: type[exceptions.FoundryError],
    kwargs: dict[str, object],
    attributes: dict[str, object],
) -> None:
    error = error_class(**kwargs)
    assert isinstance(error, exceptions.FoundryError)
    assert isinstance(error, Exception)
    assert error_class.__doc__ is not None
    assert error_class.__doc__.strip() != ""
    assert str(error) == kwargs["message"]
    assert error.args == (kwargs["message"],)
    for name, expected in attributes.items():
        assert getattr(error, name) == expected
    assert repr(error).startswith(f"{error_class.__name__}(message=")
    assert error.format_message().startswith(str(kwargs["message"]))


def test_the_eight_documented_exception_types_are_published() -> None:
    assert sorted(exceptions.__all__) == sorted(
        [
            "ArtifactError",
            "CassetteMissError",
            "ConfigError",
            "ContractError",
            "DataError",
            "FoundryError",
            "RetryExhaustedError",
            "TransportError",
        ]
    )
    for name in exceptions.__all__:
        published = getattr(exceptions, name)
        assert issubclass(published, exceptions.FoundryError)


def test_foundry_error_renders_the_message_alone_and_the_hint_on_request() -> None:
    error = exceptions.FoundryError("boom", hint="do better")
    assert str(error) == "boom"
    assert error.message == "boom"
    assert error.hint == "do better"
    assert error.format_message() == "boom\n  do better"
    assert exceptions.FoundryError("boom").format_message() == "boom"
    assert exceptions.FoundryError("boom").hint is None


def test_retry_exhausted_error_extends_transport_error_and_copies_its_attempts() -> None:
    first = types.AttemptRecord(
        number=1,
        outcome="timeout",
        elapsed_ms=1.5,
        retry_on="timeout",
        message="GET /cart timed out",
    )
    attempts = [first]
    error = exceptions.RetryExhaustedError(
        "GET /cart: 1 attempt failed",
        method="GET",
        path="/cart",
        cause="timeout",
        attempts=attempts,
    )
    assert isinstance(error, exceptions.TransportError)
    assert (error.method, error.path, error.cause) == ("GET", "/cart", "timeout")
    attempts.append(types.AttemptRecord(number=2, outcome="failed", elapsed_ms=2.0))
    assert error.attempts == [first]
    record = error.attempts[0]
    assert (record.number, record.outcome, record.elapsed_ms) == (1, "timeout", 1.5)
    assert (record.retry_on, record.message) == ("timeout", "GET /cart timed out")
    assert record.as_dict() == {
        "number": 1,
        "outcome": "timeout",
        "elapsed_ms": 1.5,
        "retry_on": "timeout",
        "message": "GET /cart timed out",
    }
    assert list(record.as_dict()) == ["number", "outcome", "elapsed_ms", "retry_on", "message"]
    assert "attempts=" in repr(error)


# --- Shared vocabularies (types.py) ---------------------------------------


def test_the_shared_vocabularies_are_fixed() -> None:
    assert types.PHASES == ("setup", "call", "teardown")
    assert types.ARTIFACT_KINDS == (
        "request",
        "response",
        "stdout",
        "stderr",
        "traceback",
        "log_tail",
        "manifest",
    )
    assert types.QUARANTINE_STATES == ("none", "failed", "passed")


def test_each_literal_alias_lists_exactly_its_vocabulary() -> None:
    assert get_args(types.Phase) == types.PHASES
    assert get_args(types.ArtifactKind) == types.ARTIFACT_KINDS
    assert get_args(types.QuarantineState) == types.QUARANTINE_STATES


def test_attempt_record_is_frozen_and_serialises_in_declaration_order() -> None:
    record = types.AttemptRecord(number=2, outcome="status_5xx", elapsed_ms=4.25)
    assert record.retry_on is None
    assert record.message is None
    with pytest.raises(dataclasses.FrozenInstanceError):
        record.number = 3
    assert list(record.as_dict()) == ["number", "outcome", "elapsed_ms", "retry_on", "message"]
    assert record.as_dict()["outcome"] == "status_5xx"


def test_the_injectable_collaborator_shapes_are_protocols() -> None:
    assert types.HttpResponseLike._is_protocol is True
    assert types.ClockLike._is_protocol is True
    assert isinstance(types.HttpResponseLike.status_code, property)
    assert isinstance(types.HttpResponseLike.headers, property)
    assert isinstance(types.HttpResponseLike.text, property)
    assert callable(types.ClockLike.now)
    assert get_origin(types.Sleep) is Callable


# --- Barrel fidelity -------------------------------------------------------


def test_the_core_barrel_exports_exactly_the_siblings_public_names() -> None:
    published: set[str] = set()
    for module in (exceptions, ids, paths, types):
        published |= set(module.__all__)
    assert set(core.__all__) == published
    assert list(core.__all__) == sorted(core.__all__)
    for name in core.__all__:
        assert getattr(core, name) is not None


def test_the_core_barrel_serves_the_sibling_objects_themselves() -> None:
    assert core.run_id is ids.run_id
    assert core.sanitize_nodeid is paths.sanitize_nodeid
    assert core.FoundryError is exceptions.FoundryError
    assert core.ArtifactError is exceptions.ArtifactError
    assert core.PHASES is types.PHASES
    assert core.AttemptRecord is types.AttemptRecord
