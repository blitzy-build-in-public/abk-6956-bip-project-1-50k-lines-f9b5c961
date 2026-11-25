"""Unit tests for ``foundry.core``: naive timestamps read as UTC, ``sanitize_nodeid`` caps a long id
at ``MAX_NODEID_NAME_LENGTH`` + 9, and the eight exception types carry their published attributes.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import get_args, get_origin
from urllib.parse import unquote

import pytest

from foundry import core
from foundry.core import exceptions, ids, paths, types

# Written independently of the product's own pattern, so a broken pattern cannot satisfy its test.
_RUN_ID_LITERAL = re.compile(r"\d{8}T\d{6}-[0-9a-f]{6}")
_FIXED_MOMENT = datetime(2025, 1, 2, 3, 4, 5, 678000, tzinfo=UTC)
_OFFSET_MOMENT = datetime(2025, 1, 2, 5, 4, 5, 678000, tzinfo=timezone(timedelta(hours=2)))
_NAIVE_MOMENT = datetime(2025, 1, 2, 3, 4, 5, 678000)
_FIXED_RUN_ID = "20250102T030405-abc123"
_RENDERED = "2025-01-02T03:04:05.678+00:00"
_BAD_RUN_IDS = (
    "",
    "20250102T030405-9f3c1",
    "20250102T030405-9f3c1a1",
    "20250102T030405-9F3C1A",
    "20250102T030405-9f3c1a\n",
    "2025-01-02T03:04:05-9f3c1a",
)
_SAMPLE_NODEIDS = (
    "tests/test_a.py::test_b",
    "tests/test_a.py::test_b[1-2]",
    "tests/test_a.py::test_c[param[0]]",
    "tests/test_a.py::TestClass::test_method",
)
_EXC = exceptions
_EXCEPTION_CASES = (
    (_EXC.ConfigError, "unknown key: htpp", {"key": "htpp", "layer": "file"}),
    (_EXC.DataError, "row 4 is missing price", {"file": Path("cart.csv"), "line": 4}),
    (_EXC.ContractError, "bad id", {"json_path": "$.id", "detail": "no integer"}),
    (_EXC.TransportError, "GET /c refused", {"method": "GET", "path": "/c", "cause": "connect"}),
    (_EXC.CassetteMissError, "miss", {"method": "POST", "path": "/c", "cassette_path": Path("c")}),
    (_EXC.ArtifactError, "escapes", {"attempted_path": Path("/x"), "run_dir": Path("/run")}),
)


def test_run_id_matches_the_published_shape_and_is_unique() -> None:
    value = ids.run_id()
    assert ids.RUN_ID_PATTERN.fullmatch(value) and _RUN_ID_LITERAL.fullmatch(value)
    assert ids.RUN_ID_TOKEN_HEX_DIGITS == 6 and ids.RUN_ID_FORMAT == "%Y%m%dT%H%M%S"
    values = [ids.run_id() for _ in range(20)]
    assert len(set(values)) == len(values) and all(map(_RUN_ID_LITERAL.fullmatch, values))


@pytest.mark.parametrize("moment", [_FIXED_MOMENT, _OFFSET_MOMENT], ids=["utc", "offset"])
def test_run_id_uses_the_injected_clock_and_suffix(moment: datetime) -> None:
    sizes: list[int] = []
    value = ids.run_id(now=moment, token_hex=lambda count: sizes.append(count) or "abc123")
    assert value == _FIXED_RUN_ID
    assert value.startswith(moment.astimezone(UTC).strftime(ids.RUN_ID_FORMAT))
    assert sizes == [ids.RUN_ID_TOKEN_HEX_DIGITS // 2]


@pytest.mark.parametrize("suffix", ["", "abc12", "abc1234", "ABC123", "abcxyz", "abc123 "])
def test_run_id_rejects_a_malformed_suffix(suffix: str) -> None:
    with pytest.raises(exceptions.FoundryError) as caught:
        ids.run_id(now=_FIXED_MOMENT, token_hex=lambda _count: suffix)
    assert "suffix" in caught.value.message


@pytest.mark.parametrize("value", _BAD_RUN_IDS)
def test_is_valid_run_id_rejects_malformed_values(value: str) -> None:
    assert ids.is_valid_run_id(value) is False


@pytest.mark.parametrize("moment", [_FIXED_MOMENT, _OFFSET_MOMENT, _NAIVE_MOMENT])
def test_to_iso8601_renders_every_input_as_utc_milliseconds(moment: datetime) -> None:
    text = ids.to_iso8601(moment)
    assert text == _RENDERED and not text.endswith("Z") and ids.parse_iso8601(text) == _FIXED_MOMENT


@pytest.mark.parametrize(
    "text", ["2025-01-02T03:04:05.678Z", "2025-01-02T05:04:05.678+02:00", "2025-01-02T03:04:05.678"]
)
def test_parse_iso8601_reads_every_published_form_as_utc(text: str) -> None:
    parsed = ids.parse_iso8601(text)
    assert parsed == _FIXED_MOMENT and parsed.utcoffset() == timedelta(0)


@pytest.mark.parametrize("text", ["", "not a timestamp", "2025-13-02T03:04:05+00:00"])
def test_parse_iso8601_rejects_text_that_is_not_a_timestamp(text: str) -> None:
    with pytest.raises(exceptions.FoundryError) as caught:
        ids.parse_iso8601(text)
    assert repr(text) in caught.value.message


def test_case_id_for_matrix_sorts_axes_names_a_bare_matrix_and_refuses_odd_values() -> None:
    assert ids.case_id_for_matrix("m", {"nested": {"b": 1, "a": 2}}) == 'm-nested={"a":2,"b":1}'
    assert ids.case_id_for_matrix("matrix", {}) == "matrix"

    class Unrenderable:
        def __str__(self) -> str:
            raise TypeError("cannot render")

    with pytest.raises(exceptions.FoundryError) as caught:
        ids.case_id_for_matrix("m", {"axis": Unrenderable()})
    assert "not serialisable" in caught.value.message


@pytest.mark.parametrize("nodeid", _SAMPLE_NODEIDS)
def test_sanitize_nodeid_yields_one_safe_injective_component(nodeid: str) -> None:
    name = paths.sanitize_nodeid(nodeid)
    assert "/" not in name and "\\" not in name and "::" not in name
    assert name not in {".", ".."} and unquote(name) == nodeid
    names = [paths.sanitize_nodeid(other) for other in _SAMPLE_NODEIDS]
    assert len(set(names)) == len(names) == len(_SAMPLE_NODEIDS)


def test_sanitize_nodeid_truncates_a_long_id_deterministically() -> None:
    first = paths.sanitize_nodeid(shared := "tests/test_long.py::test_" + "x" * 400)
    assert len(first) == paths.MAX_NODEID_NAME_LENGTH + 9
    assert first[-9] == "-" and re.fullmatch(r"[0-9a-f]{8}", first[-8:]) is not None
    assert paths.sanitize_nodeid(shared) == first != paths.sanitize_nodeid(shared + "y")


def test_artifact_paths_stay_inside_the_run_directory(tmp_path: Path) -> None:
    run, outside = tmp_path / "run", tmp_path / "outside"
    run.mkdir()
    outside.mkdir()
    (run / "link").symlink_to(outside, target_is_directory=True)
    name = paths.sanitize_nodeid(nodeid := "tests/test_a.py::test_b")
    assert paths.resolve_under(run, "requests/one.json") == run.resolve() / "requests" / "one.json"
    directory = paths.artifact_dir_for(run, nodeid)
    assert directory == run.resolve() / name
    relative = paths.relative_artifact_path(directory / "request.json", run)
    assert relative == f"{name}/request.json" and "\\" not in relative
    assert paths.relative_artifact_path(Path(name) / "request.json", run) == relative
    for target in ("../escaped.json", str(tmp_path.parent / "outside.json"), "link/request.json"):
        with pytest.raises(exceptions.ArtifactError) as caught:
            paths.resolve_under(run, target)
        assert caught.value.run_dir == run.resolve()
        assert caught.value.attempted_path == (run / target).resolve()
    with pytest.raises(exceptions.ArtifactError):
        paths.relative_artifact_path(tmp_path.parent / "stray.log", run)


@pytest.mark.parametrize(("error_class", "message", "attributes"), _EXCEPTION_CASES)
def test_each_exception_is_a_foundry_error_carrying_its_context(
    error_class: type[exceptions.FoundryError], message: str, attributes: dict[str, object]
) -> None:
    error = error_class(message, **attributes)
    assert isinstance(error, exceptions.FoundryError) and str(error) == message
    for name, expected in attributes.items():
        assert getattr(error, name) == expected
    assert repr(error).startswith(f"{error_class.__name__}(message=")


@pytest.mark.parametrize(
    "call",
    (
        lambda: ids.case_id_for_matrix("", {"size": "L"}),
        lambda: ids.case_id_for_matrix("m", {1: "L"}),
        lambda: paths.sanitize_nodeid(""),
        lambda: paths.sanitize_nodeid("   "),
    ),
)
def test_case_id_and_nodeid_helpers_reject_unusable_input(call: Callable[[], str]) -> None:
    with pytest.raises(exceptions.FoundryError):
        call()


def test_foundry_error_renders_message_and_hint_and_the_eight_types_are_published() -> None:
    error = exceptions.FoundryError("boom", hint="do better")
    assert (str(error), error.message, error.hint) == ("boom", "boom", "do better")
    assert error.format_message() == "boom\n  do better"
    assert exceptions.FoundryError("boom").format_message() == "boom"
    expected = {"ArtifactError", "CassetteMissError", "ConfigError", "ContractError"}
    expected |= {"DataError", "FoundryError", "RetryExhaustedError", "TransportError"}
    assert set(exceptions.__all__) == expected
    assert all(issubclass(getattr(exceptions, name), exceptions.FoundryError) for name in expected)


def test_retry_exhausted_error_extends_transport_error_and_copies_its_attempts() -> None:
    first = types.AttemptRecord(number=1, outcome="timeout", elapsed_ms=1.5, message="timed out")
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
    assert error.attempts == [first] and "attempts=" in repr(error)


def test_the_shared_vocabulary_records_and_barrel_hold_their_published_shape() -> None:
    assert types.PHASES == ("setup", "call", "teardown")
    assert types.QUARANTINE_STATES == ("none", "failed", "passed")
    kinds = ("request", "response", "stdout", "stderr", "traceback", "log_tail", "manifest")
    assert kinds == types.ARTIFACT_KINDS
    aliases = (types.Phase, types.ArtifactKind, types.QuarantineState)
    assert [get_args(alias) for alias in aliases] == [types.PHASES, kinds, types.QUARANTINE_STATES]
    record = types.AttemptRecord(number=2, outcome="status_5xx", elapsed_ms=4.25)
    with pytest.raises(dataclasses.FrozenInstanceError):
        record.number = 3
    assert list(record.as_dict()) == ["number", "outcome", "elapsed_ms", "retry_on", "message"]
    assert types.HttpResponseLike._is_protocol is True and types.ClockLike._is_protocol is True
    members = ("status_code", "headers", "text")
    assert all(isinstance(getattr(types.HttpResponseLike, name), property) for name in members)
    assert callable(types.ClockLike.now) and get_origin(types.Sleep) is Callable
    published = {n for m in (exceptions, ids, paths, types) for n in m.__all__}
    assert set(core.__all__) == published and list(core.__all__) == sorted(core.__all__)
    assert all(getattr(core, name) is not None for name in core.__all__)
    assert (core.run_id, core.sanitize_nodeid) == (ids.run_id, paths.sanitize_nodeid)
    assert (core.PHASES, core.AttemptRecord) == (types.PHASES, types.AttemptRecord)
