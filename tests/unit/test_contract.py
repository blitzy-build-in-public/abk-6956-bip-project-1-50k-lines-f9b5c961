"""Unit tests for the ``foundry.contract`` capability package."""

from __future__ import annotations

import copy
import json
import socket
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Final, Never

import jsonschema
import pytest
from jsonschema.exceptions import ValidationError

from foundry.contract import (
    Violation,
    excerpt,
    find_violation,
    first_violation,
    iter_violations,
    load_schema,
    render_violation,
    validate_instance,
)
from foundry.contract.violations import to_contract_error, violation_from_error
from foundry.core.exceptions import ContractError
from foundry.obs.redaction import MAX_EXCERPT_BYTES, REDACTED, default_redactor

_FIXTURES: Final[Path] = Path(__file__).resolve().parents[1] / "fixtures"
_RS, _CR = _FIXTURES / "response.schema.json", _FIXTURES / "case_row.schema.json"
_EXCERPT_MARKER: Final[str] = "..."
_SECRET: Final[str] = "hunter2secret"
_YAML_TEXT = "type: object\nrequired:\n  - order_id\nproperties:\n  order_id:\n    type: string\n"
_YAML_DOC: Final[dict[str, object]] = {"type": "object", "required": ["order_id"]}
_YAML_DOC["properties"] = {"order_id": {"type": "string"}}
_TOTALS: Final[dict[str, object]] = {"currency": "EUR", "grand_total_cents": 4200}
_LOW_TOTALS: Final[dict[str, object]] = {"currency": "EUR", "grand_total_cents": 0}
_ITEMS: Final[list[dict[str, object]]] = [{"sku": "SKU-1234", "quantity": 2}]
_BAD_ITEMS: Final[list[dict[str, object]]] = [*_ITEMS, {"sku": "BAD", "quantity": 1}]
_RESPONSE: Final[dict[str, object]] = {"order_id": "ORD-000123", "status": "paid"}
_RESPONSE.update(items=_ITEMS, totals=_TOTALS)
_ROW: Final[dict[str, object]] = {"case_id": "C-001", "currency": "EUR", "tier": "gold"}
_ROW.update(quantity="3", unit_price="10.00", expected_total="30.00")
_DEPTH_SCHEMA: Final[dict[str, object]] = {
    "properties": {"a": {"type": "object"}, "b": {"properties": {"x": {"type": "integer"}}}}
}
_DEPTH_INSTANCE: Final[dict[str, object]] = {"b": {"x": "not-an-int"}, "a": "not-an-object"}
_SIBLINGS = {"properties": {"a": {"type": "object"}, "b": {"type": "object"}}}
_MESSAGES: Final[dict[str, object]] = {"properties": {"a": {"enum": ["x"], "type": "string"}}}
_NOTE_SCHEMA: Final[dict[str, object]] = {"properties": {"note": {"pattern": "^[a-z]+$"}}}
_SENTINEL: Final[str] = "_".join(("QA_BEARER", "9f3ab1c77d2e4ff0a5c3d81b"))
_NESTED_PASSWORD_SCHEMA: Final[dict[str, object]] = {
    "type": "object",
    "properties": {"account": {"type": "object", "properties": {"password": {"type": "integer"}}}},
}


def _integer_key(key: str) -> dict[str, object]:
    """Return a schema requiring ``key`` to be an integer, so a string there is one type fault."""
    return {"type": "object", "properties": {key: {"type": "integer"}}}


def _errors(schema: Mapping[str, object], instance: object) -> list[ValidationError]:
    return list(jsonschema.Draft202012Validator(dict(schema)).iter_errors(instance))


def _replaced(body: Mapping[str, object], key: str, value: object) -> dict[str, object]:
    updated = copy.deepcopy(dict(body))
    updated[key] = value
    return updated


def _written(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


@pytest.mark.parametrize("suffix", (".json", ".yaml", ".yml"))
def test_load_schema_reads_a_mapping_or_a_file(tmp_path: Path, suffix: str) -> None:
    text = json.dumps(_YAML_DOC) if suffix == ".json" else _YAML_TEXT
    path = _written(tmp_path, f"row.schema{suffix}", text)
    assert load_schema(path) == load_schema(str(path)) == _YAML_DOC
    assert load_schema(_YAML_DOC) is _YAML_DOC
    violation = find_violation({"order_id": 3}, path)
    assert violation is not None and violation.json_path == "$.order_id"


@pytest.mark.parametrize(
    ("name", "text", "fragment"),
    (
        pytest.param("absent.schema.json", None, "absent.schema.json", id="missing"),
        pytest.param("row.schema.txt", "{}", ".json", id="unsupported-extension"),
        pytest.param("list.schema.json", "[{}]", "JSON Schema object", id="not-an-object"),
    ),
)
def test_load_schema_refuses_a_file_it_cannot_use(
    tmp_path: Path, name: str, text: str | None, fragment: str
) -> None:
    path = tmp_path / name if text is None else _written(tmp_path, name, text)
    with pytest.raises(ContractError) as caught:
        load_schema(path)
    error = caught.value
    assert fragment in error.detail and error.json_path == "$" and error.hint is not None
    assert text is not None or isinstance(error.__cause__, FileNotFoundError)


@pytest.mark.parametrize("keyword", ("$ref", "$dynamicRef", "$recursiveRef"))
def test_a_reference_is_refused_unless_it_is_a_fragment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, keyword: str
) -> None:
    def _refuse(*args: object, **kwargs: object) -> Never:
        raise AssertionError(f"network access attempted: {args!r} {kwargs!r}")

    for name in ("getaddrinfo", "create_connection"):
        monkeypatch.setattr(socket, name, _refuse)
    url = "https://example.com/price.schema.json"
    document = {"properties": {"price": {keyword: url}}}
    with pytest.raises(ContractError) as caught:
        load_schema(_written(tmp_path, "remote.schema.json", json.dumps(document)))
    assert "remote schema reference" in caught.value.message and url in caught.value.message
    assert f"$.properties.price.{keyword}" in caught.value.message
    local: dict[str, object] = {"$defs": {"price": {"type": "integer"}}}
    local["properties"] = {"p": {"$ref": "#/$defs/price"}}
    assert find_violation({"p": 3}, local) is None
    violation = find_violation({"p": "free"}, local)
    assert violation is not None and violation.json_path == "$.p"


def test_a_violation_is_a_typed_rendered_error(tmp_path: Path) -> None:
    assert load_schema(_FIXTURES / "invalid_schema.json") == {"type": 42}
    schema_path = _written(tmp_path, "bad.schema.json", json.dumps({"type": 42}))
    reason = "42 is not valid under any of the given schemas"
    with pytest.raises(ContractError) as caught:
        validate_instance({}, schema_path)
    error = caught.value
    assert error.json_path == "$.type" and error.hint is not None
    assert error.detail.startswith(f"schema is invalid: {reason} (schema path: #")
    assert error.message == f"{schema_path}: {error.detail}"
    with pytest.raises(ContractError):
        find_violation({}, schema_path)
    with pytest.raises(ContractError) as deep:
        validate_instance(_DEPTH_INSTANCE, _DEPTH_SCHEMA)
    assert deep.value.json_path == "$.a" and "validator: type" in deep.value.detail
    assert (violation := find_violation(_DEPTH_INSTANCE, _DEPTH_SCHEMA)) is not None
    rendered = render_violation(violation)
    assert rendered.startswith("$.a: ") and violation.message in rendered
    assert "schema path: #/properties/a/type" in rendered
    constructed = to_contract_error(violation)
    assert constructed.message == f"contract validation failed at {violation.json_path}"
    assert constructed.detail == rendered and constructed.hint is None


@pytest.mark.parametrize(
    ("schema", "instance"), ((_RS, _RESPONSE), (_CR, _ROW)), ids=["response", "case-row"]
)
def test_a_valid_document_has_no_violation(schema: Path, instance: dict[str, object]) -> None:
    assert find_violation(instance, schema) is None and iter_violations(instance, schema) == ()
    assert validate_instance(instance, schema) is None
    assert load_schema(schema)["additionalProperties"] is False


@pytest.mark.parametrize(
    ("schema", "instance", "json_path", "validator"),
    (
        (_RS, {k: v for k, v in _RESPONSE.items() if k != "order_id"}, "$", "required"),
        (_RS, _replaced(_RESPONSE, "items", "none"), "$.items", "type"),
        (_RS, _replaced(_RESPONSE, "order_id", "ORD-1"), "$.order_id", "pattern"),
        (_RS, _replaced(_RESPONSE, "status", "refunded"), "$.status", "enum"),
        (_RS, _replaced(_RESPONSE, "totals", _LOW_TOTALS), "$.totals.grand_total_cents", "minimum"),
        (_RS, _replaced(_RESPONSE, "items", _BAD_ITEMS), "$.items[1].sku", "pattern"),
        (_RS, _replaced(_RESPONSE, "extra", 1), "$", "additionalProperties"),
        (_CR, _replaced(_ROW, "quantity", 3), "$.quantity", "type"),
        (_CR, _replaced(_ROW, "tier", "bronze-ish"), "$.tier", "enum"),
        (_CR, _replaced(_ROW, "note", "x"), "$", "additionalProperties"),
    ),
)
def test_a_documented_violation_is_reported_at_its_json_path(
    schema: Path, instance: dict[str, object], json_path: str, validator: str
) -> None:
    assert isinstance(violation := find_violation(instance, schema), Violation)
    assert (violation.json_path, violation.validator) == (json_path, validator)
    assert violation.message and violation.schema_path.startswith("#")
    with pytest.raises(ContractError) as caught:
        validate_instance(instance, schema)
    assert caught.value.json_path == json_path and caught.value.detail.startswith(f"{json_path}: ")


def test_first_violation_orders_by_depth_then_path_then_message() -> None:
    errors = _errors(_DEPTH_SCHEMA, _DEPTH_INSTANCE)
    assert [violation_from_error(error).json_path for error in errors] == ["$.a", "$.b.x"]
    reverse = list(reversed(errors))
    assert violation_from_error(reverse[0]).json_path == "$.b.x"
    assert isinstance(chosen := first_violation(reverse), Violation) and chosen.json_path == "$.a"
    siblings = _errors(_SIBLINGS, {"a": "zzz", "b": "aaa"})
    message_by_path = {violation_from_error(error).json_path: error.message for error in siblings}
    assert message_by_path["$.b"] < message_by_path["$.a"]
    assert (by_path := first_violation(siblings)) is not None and by_path.json_path == "$.a"
    ties = [violation_from_error(error) for error in _errors(_MESSAGES, {"a": 7})]
    assert ({v.json_path for v in ties}, len({v.message for v in ties})) == ({"$.a"}, 2)
    tied = first_violation(_errors(_MESSAGES, {"a": 7}))
    assert tied is not None and tied.message == min(violation.message for violation in ties)
    assert first_violation([]) is None and first_violation(iter(())) is None
    array: dict[str, object] = {"type": "array", "items": {"type": "string"}}
    paths = [violation.json_path for violation in iter_violations(list(range(11)), array)]
    assert paths == [f"$[{index}]" for index in range(11)]


def test_redaction_and_the_excerpt_budget_hold_on_every_rendered_surface() -> None:
    violation = find_violation({"note": f"password={_SECRET}"}, _NOTE_SCHEMA)
    assert violation is not None and _SECRET in violation.message
    surfaces = (
        render_violation(violation),
        violation.render(),
        str(violation),
        to_contract_error(violation).detail,
        render_violation(violation, redact=default_redactor().text),
    )
    for surface in surfaces:
        assert f"password={REDACTED}" in surface and "hunter" not in surface
    kept = (text := excerpt("x" * 400)).removesuffix(_EXCERPT_MARKER)
    assert text.endswith(_EXCERPT_MARKER) and ("x" * 400).startswith(kept)
    assert len(("x" * 400).encode("utf-8")) > MAX_EXCERPT_BYTES
    assert len(kept.encode("utf-8")) == MAX_EXCERPT_BYTES
    assert len(text.encode("utf-8")) <= MAX_EXCERPT_BYTES + len(_EXCERPT_MARKER)
    assert excerpt(b"<html><body>" + b"y" * 300).startswith("<html><body>")
    assert excerpt("not json") == "not json"
    assert excerpt("abcdefghijklmno", limit=10) == f"abcdefghij{_EXCERPT_MARKER}"
    redacted = excerpt("x" * 180 + f" password={_SECRET}" + "z" * 50)
    assert f"password={REDACTED}" in redacted and "hunter" not in redacted
    assert _SECRET not in json.dumps(f"JSON expected but the body was: {redacted}")


@pytest.mark.parametrize(
    ("instance", "schema", "json_path", "masked"),
    (
        pytest.param(
            {"password": _SENTINEL}, _integer_key("password"), "$.password", True, id="password"
        ),
        pytest.param(
            {"api_key": _SENTINEL}, _integer_key("api_key"), "$.api_key", True, id="api-key"
        ),
        pytest.param({"token": _SENTINEL}, _integer_key("token"), "$.token", True, id="token"),
        pytest.param(
            {"account": {"password": _SENTINEL}},
            _NESTED_PASSWORD_SCHEMA,
            "$.account.password",
            True,
            id="nested-password",
        ),
        pytest.param({"note": _SENTINEL}, _integer_key("note"), "$.note", False, id="innocent-key"),
    ),
)
def test_a_violation_at_a_secret_key_masks_the_value_it_quotes(
    instance: dict[str, object], schema: dict[str, object], json_path: str, masked: bool
) -> None:
    assert isinstance(violation := find_violation(instance, schema), Violation)
    assert violation.json_path == json_path and _SENTINEL in violation.message
    error = to_contract_error(violation)
    assert error.json_path == json_path
    surfaces = (
        render_violation(violation),
        to_contract_error(violation, redact=default_redactor().text).detail,
        error.detail,
    )
    for surface in surfaces:
        assert surface.startswith(f"{json_path}: ") and "validator: type" in surface
        if masked:
            assert REDACTED in surface
            assert not any(fragment in surface for fragment in _SENTINEL.split("_"))
        else:
            assert f"'{_SENTINEL}'" in surface


def test_a_line_break_in_a_message_cannot_hide_a_credential_from_the_scan() -> None:
    violation = Violation(
        json_path="$.note",
        schema_path="#/properties/note/type",
        validator="type",
        message=f"'Bearer\n{_SENTINEL}' is not of type 'integer'",
        path_parts=("note",),
    )
    line = render_violation(violation)
    assert "\n" not in line and "Bearer ***" in line and _SENTINEL not in line


def test_importing_the_package_pulls_in_neither_jsonschema_nor_yaml(
    repo_root: Path, foundry_python: str, subprocess_env: Callable[..., dict[str, str]]
) -> None:
    probe = (
        'import sys, foundry.contract; print("jsonschema" in sys.modules, "yaml" in sys.modules)'
    )
    completed = subprocess.run(
        [foundry_python, "-c", probe],
        cwd=str(repo_root),
        env=subprocess_env({"PYTHONPATH": str(repo_root / "src")}),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert (completed.returncode, completed.stdout.strip()) == (0, "False False")
