"""Unit tests for the ``foundry.contract`` capability package.

One module pins the whole contract of loading a JSON Schema document, validating an instance against
it and reporting the violation Foundry chooses (AAP 0.3.2, 0.4.3, 0.4.7, 0.4.8, 0.7.1):

* ``load_schema`` accepts a mapping in memory, a ``.json`` path and a ``.yaml``/``.yml`` path, and
  refuses a missing file, an unsupported extension, a non-object document and a reference that would
  make a validator retrieve another document -- each as a ``ContractError``, never as the builtin
  the standard library raised.
* ``validate_instance`` raises, ``find_violation`` returns and ``iter_violations`` enumerates, all
  through one walk, so every validator the two committed fixture schemas exercise is proven.
* The reported violation is Foundry's own choice, never the library's: shallowest JSON path first,
  then the path structurally, then the message. Each ordering test therefore hands
  ``first_violation`` the library's errors in an order that contradicts that rule, which is what
  makes the assertions able to fail.
* Rendering and excerpts redact on the way out, so a secret a validator message quotes, or one a
  non-JSON body carries, reaches no message, no detail and no JSON dump.

Two expectations differ from the brief this file was written against, the modules on disk being
authoritative (AAP 0.7.1, AAP 0.4.7): ``load_schema`` is a loader, not a metaschema check, so
``tests/fixtures/invalid_schema.json`` loads as the mapping it is and the ``ContractError`` comes
from the first validation; and ``excerpt`` appends its ``...`` marker after cutting the body to
``MAX_EXCERPT_BYTES``, so the budget is asserted with the marker, never against the bare constant.

Determinism and isolation (AAP 0.14.3): every schema and body is built in ``tmp_path``, the
committed fixtures are only read, nothing sleeps and no socket is opened -- the remote-reference
test closes the two name-resolution seams. The single subprocess is the sanctioned lazy-import probe
(implicit requirement I5): importing ``foundry.contract`` pulls in neither ``jsonschema`` nor
``yaml``.
"""

from __future__ import annotations

import copy
import itertools
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
"""The committed fixtures: read here, never written."""

_RESPONSE_SCHEMA: Final[Path] = _FIXTURES / "response.schema.json"
_CASE_ROW_SCHEMA: Final[Path] = _FIXTURES / "case_row.schema.json"
_INVALID_SCHEMA: Final[Path] = _FIXTURES / "invalid_schema.json"

_EXCERPT_MARKER: Final[str] = "..."
"""What :func:`foundry.contract.excerpt` appends once it has cut a body to its budget."""

_SECRET: Final[str] = "hunter2secret"
"""A value no rendered violation, detail or excerpt may carry; distinctive enough to be a leak."""

_YAML_SCHEMA_TEXT: Final[str] = (
    "type: object\n"
    "required:\n"
    "  - order_id\n"
    "properties:\n"
    "  order_id:\n"
    "    type: string\n"
    "additionalProperties: false\n"
)
"""A schema written as YAML, whose parsed form is spelled out independently below."""

_YAML_SCHEMA_DOCUMENT: Final[dict[str, object]] = {
    "type": "object",
    "required": ["order_id"],
    "properties": {"order_id": {"type": "string"}},
    "additionalProperties": False,
}
"""What ``_YAML_SCHEMA_TEXT`` must parse to, so the loader cannot grade its own output."""

_VALID_RESPONSE: Final[dict[str, object]] = {
    "order_id": "ORD-000123",
    "status": "paid",
    "items": [{"sku": "SKU-1234", "quantity": 2}, {"sku": "SKU-0001", "quantity": 1}],
    "totals": {"currency": "EUR", "grand_total_cents": 4200},
}
"""A body the committed response schema accepts in full."""

_VALID_ROW: Final[dict[str, object]] = {
    "case_id": "C-001",
    "currency": "EUR",
    "tier": "gold",
    "quantity": "3",
    "unit_price": "10.00",
    "expected_total": "30.00",
}
"""A data row the row schema accepts; every value is a string, as the csv loader yields."""

_DEPTH_SCHEMA: Final[dict[str, object]] = {
    "type": "object",
    "properties": {
        "a": {"type": "object"},
        "b": {"type": "object", "properties": {"x": {"type": "integer"}}},
    },
}
"""A schema whose instance below breaks ``$.a`` at depth 1 and ``$.b.x`` at depth 2."""

_DEPTH_INSTANCE: Final[dict[str, object]] = {"b": {"x": "not-an-int"}, "a": "not-an-object"}
"""``iter_errors`` yields ``$.a`` first, so the test reverses the list to prove the ordering."""

_SIBLING_SCHEMA: Final[dict[str, object]] = {
    "type": "object",
    "properties": {"a": {"type": "object"}, "b": {"type": "object"}},
}
"""Two violations of equal depth, at ``$.a`` and ``$.b``."""

_SIBLING_INSTANCE: Final[dict[str, object]] = {"a": "zzz", "b": "aaa"}
"""Values chosen so the message order (``aaa`` first) contradicts the path order (``$.a`` first)."""

_MESSAGE_SCHEMA: Final[dict[str, object]] = {
    "type": "object",
    "properties": {"a": {"enum": ["x"], "type": "string"}},
}
"""Two violations of one path, ``$.a``, differing only in their message."""

_MESSAGE_INSTANCE: Final[dict[str, object]] = {"a": 7}
"""An integer at ``$.a``, so both the ``enum`` and the ``type`` keyword reject it."""

_NOTE_SCHEMA: Final[dict[str, object]] = {
    "type": "object",
    "properties": {"note": {"pattern": "^[a-z]+$"}},
}
"""A schema whose violation message quotes the offending value verbatim."""

_NOTE_INSTANCE: Final[dict[str, object]] = {"note": f"password={_SECRET}"}
"""An offending value carrying a credential-shaped secret, so rendering has to scrub it."""


# --- Helpers and fixtures ---------------------------------------------------


def _write(path: Path, text: str) -> Path:
    """Write ``text`` to ``path`` as UTF-8 with LF newlines and return the path."""
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def _errors(schema: Mapping[str, object], instance: object) -> list[ValidationError]:
    """Return the library's own errors for one schema and instance, in the order it yields them."""
    return list(jsonschema.Draft202012Validator(dict(schema)).iter_errors(instance))


def _without(body: Mapping[str, object], key: str) -> dict[str, object]:
    """Return ``body`` without ``key``, which is how a missing required key is built."""
    return {name: value for name, value in body.items() if name != key}


def _replaced(body: Mapping[str, object], key: str, value: object) -> dict[str, object]:
    """Return a copy of ``body`` with ``key`` set to ``value``."""
    updated = copy.deepcopy(dict(body))
    updated[key] = value
    return updated


@pytest.fixture
def forbid_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make any name resolution or connection attempt fail this test loudly.

    A schema carrying a remote ``$ref`` must be refused before a validator can retrieve it (AAP
    0.3.2, AAP 0.14.3). With these two seams closed, a retrieval attempt surfaces as
    ``AssertionError`` rather than as the ``ContractError`` an assertion below expects, so a loader
    that ever fetched a document could not pass.
    """

    def _refuse(*args: object, **kwargs: object) -> Never:
        raise AssertionError(f"network access attempted: {args!r} {kwargs!r}")

    monkeypatch.setattr(socket, "getaddrinfo", _refuse)
    monkeypatch.setattr(socket, "create_connection", _refuse)


# --- load_schema: the three accepted sources --------------------------------


def test_load_schema_returns_a_mapping_it_was_given() -> None:
    """A document in memory is returned by identity, because the caller owns it."""
    document: dict[str, object] = {"type": "object", "required": ["order_id"]}

    assert load_schema(document) is document


def test_load_schema_reads_a_json_file_given_as_a_path_or_a_string(tmp_path: Path) -> None:
    """A ``.json`` path parses to the document it holds, whether given as ``Path`` or ``str``."""
    document: dict[str, object] = {
        "type": "object",
        "required": ["order_id"],
        "properties": {"order_id": {"type": "string"}},
    }
    path = _write(tmp_path / "row.schema.json", json.dumps(document))

    assert load_schema(path) == document
    assert load_schema(str(path)) == document

    violation = find_violation({"order_id": 3}, path)
    assert violation is not None
    assert violation.json_path == "$.order_id"


@pytest.mark.parametrize("suffix", (".yaml", ".yml"))
def test_load_schema_reads_a_yaml_file(tmp_path: Path, suffix: str) -> None:
    """Both YAML extensions parse through ``safe_load`` to the document the text spells."""
    path = _write(tmp_path / f"row.schema{suffix}", _YAML_SCHEMA_TEXT)

    assert load_schema(path) == _YAML_SCHEMA_DOCUMENT

    violation = find_violation({"order_id": 3}, path)
    assert violation is not None
    assert (violation.json_path, violation.validator) == ("$.order_id", "type")


@pytest.mark.parametrize(
    "schema_path",
    (pytest.param(_RESPONSE_SCHEMA, id="response"), pytest.param(_CASE_ROW_SCHEMA, id="case-row")),
)
def test_the_committed_fixture_schemas_load(schema_path: Path) -> None:
    """Both committed schemas are objects that reject unknown keys and declare one draft."""
    document = load_schema(schema_path)

    assert isinstance(document, Mapping)
    assert document["additionalProperties"] is False
    assert document["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert isinstance(document["required"], list)
    assert document["required"]


def test_load_schema_reports_a_missing_file_as_a_contract_error(tmp_path: Path) -> None:
    """A path that does not exist escapes as ``ContractError``, not as ``FileNotFoundError``."""
    with pytest.raises(ContractError) as caught:
        load_schema(tmp_path / "absent.schema.json")

    error = caught.value
    assert not isinstance(error, FileNotFoundError)
    assert isinstance(error.__cause__, FileNotFoundError)
    assert error.json_path == "$"
    assert "absent.schema.json" in error.detail
    assert error.hint is not None


def test_load_schema_refuses_an_unsupported_extension(tmp_path: Path) -> None:
    """A file the loader cannot parse is refused with the accepted extensions named."""
    path = _write(tmp_path / "row.schema.txt", "{}")

    with pytest.raises(ContractError) as caught:
        load_schema(path)

    assert ".json" in caught.value.detail
    assert ".yaml" in caught.value.detail


def test_load_schema_refuses_a_document_that_is_not_an_object(tmp_path: Path) -> None:
    """A list is not a schema: the loader refuses it rather than passing it to a validator."""
    path = _write(tmp_path / "list.schema.json", json.dumps([{"type": "object"}]))

    with pytest.raises(ContractError) as caught:
        load_schema(path)

    assert "JSON Schema object" in caught.value.detail


@pytest.mark.parametrize("keyword", ("$ref", "$dynamicRef", "$recursiveRef"))
def test_load_schema_refuses_a_reference_that_would_be_retrieved(
    tmp_path: Path, forbid_network: None, keyword: str
) -> None:
    """Every reference keyword accepts a fragment of this document and nothing else."""
    document = {
        "type": "object",
        "properties": {"price": {keyword: "https://example.com/price.schema.json"}},
    }
    path = _write(tmp_path / "remote.schema.json", json.dumps(document))

    with pytest.raises(ContractError) as caught:
        load_schema(path)

    assert "remote schema reference" in caught.value.message
    assert "https://example.com/price.schema.json" in caught.value.message
    assert f"$.properties.price.{keyword}" in caught.value.message


def test_a_fragment_reference_validates_without_retrieval(forbid_network: None) -> None:
    """A ``#`` fragment resolves inside the document, which is the one reference form allowed."""
    schema: dict[str, object] = {
        "$defs": {"price": {"type": "integer"}},
        "type": "object",
        "properties": {"price": {"$ref": "#/$defs/price"}},
    }

    assert find_violation({"price": 3}, schema) is None

    violation = find_violation({"price": "free"}, schema)
    assert violation is not None
    assert (violation.json_path, violation.validator) == ("$.price", "type")


def test_the_invalid_schema_fixture_is_loaded_but_refused_by_a_validation() -> None:
    """The loader returns the document as written; the metaschema check is what refuses it."""
    assert load_schema(_INVALID_SCHEMA) == {"type": 42}

    with pytest.raises(ContractError) as caught:
        validate_instance({}, _INVALID_SCHEMA)

    error = caught.value
    assert error.json_path.startswith("$")
    assert error.detail
    assert "schema is invalid" in error.detail

    with pytest.raises(ContractError):
        find_violation({}, _INVALID_SCHEMA)


def test_an_invalid_schema_path_names_the_document_and_the_reason(tmp_path: Path) -> None:
    """A schema fault names the file it came from, while ``detail`` keeps the reason alone."""
    schema_path = _write(tmp_path / "malformed.schema.json", json.dumps({"type": 42}))
    reason = "42 is not valid under any of the given schemas"

    with pytest.raises(ContractError) as caught:
        validate_instance({}, schema_path)

    error = caught.value
    assert str(schema_path) in error.message
    assert reason in error.message
    assert error.json_path == "$.type"
    assert error.detail.startswith(f"schema is invalid: {reason} (schema path: #")
    assert str(schema_path) not in error.detail
    assert error.message == f"{schema_path}: {error.detail}"


# --- Validation: the keyword shapes both fixture schemas exercise -----------

# Each body below breaks exactly one keyword of the schema it is validated against; the case ids
# of ``_RESPONSE_CASES`` and ``_ROW_CASES`` name the keyword and the path it reports.
_MISSING_ORDER_ID: Final[dict[str, object]] = _without(_VALID_RESPONSE, "order_id")
_ITEMS_NOT_ARRAY: Final[dict[str, object]] = _replaced(_VALID_RESPONSE, "items", "none")
_BAD_ORDER_ID: Final[dict[str, object]] = _replaced(_VALID_RESPONSE, "order_id", "ORD-1")
_BAD_STATUS: Final[dict[str, object]] = _replaced(_VALID_RESPONSE, "status", "refunded")
_LOW_TOTALS: Final[dict[str, object]] = _replaced(
    _VALID_RESPONSE, "totals", {"currency": "EUR", "grand_total_cents": 0}
)
_BAD_ITEMS: Final[dict[str, object]] = _replaced(
    _VALID_RESPONSE,
    "items",
    [{"sku": "SKU-1234", "quantity": 2}, {"sku": "BAD", "quantity": 1}],
)
_EXTRA_RESPONSE_KEY: Final[dict[str, object]] = _replaced(_VALID_RESPONSE, "extra", 1)
_BAD_ROW_QUANTITY: Final[dict[str, object]] = _replaced(_VALID_ROW, "quantity", 3)
_BAD_ROW_TIER: Final[dict[str, object]] = _replaced(_VALID_ROW, "tier", "bronze-ish")
_EXTRA_ROW_COLUMN: Final[dict[str, object]] = _replaced(_VALID_ROW, "note", "x")

_RESPONSE_CASES: Final[tuple[pytest.ParameterSet, ...]] = (
    pytest.param(_MISSING_ORDER_ID, "$", "required", id="required"),
    pytest.param(_ITEMS_NOT_ARRAY, "$.items", "type", id="type"),
    pytest.param(_BAD_ORDER_ID, "$.order_id", "pattern", id="pattern"),
    pytest.param(_BAD_STATUS, "$.status", "enum", id="enum"),
    pytest.param(_LOW_TOTALS, "$.totals.grand_total_cents", "minimum", id="minimum"),
    pytest.param(_BAD_ITEMS, "$.items[1].sku", "pattern", id="array-element"),
    pytest.param(_EXTRA_RESPONSE_KEY, "$", "additionalProperties", id="extra-key"),
)
"""One ``response.schema.json`` case per keyword: the path and the validator it reports."""

_ROW_CASES: Final[tuple[pytest.ParameterSet, ...]] = (
    pytest.param(_BAD_ROW_QUANTITY, "$.quantity", "type", id="type"),
    pytest.param(_BAD_ROW_TIER, "$.tier", "enum", id="enum"),
    pytest.param(_EXTRA_ROW_COLUMN, "$", "additionalProperties", id="extra-key"),
)
"""One ``case_row.schema.json`` case per keyword: the path and the validator it reports."""


@pytest.mark.parametrize(
    ("schema_path", "instance"),
    (
        pytest.param(_RESPONSE_SCHEMA, _VALID_RESPONSE, id="response"),
        pytest.param(_CASE_ROW_SCHEMA, _VALID_ROW, id="case-row"),
    ),
)
def test_a_valid_document_has_no_violation(schema_path: Path, instance: dict[str, object]) -> None:
    """A valid instance is not a verdict at all: the walk returns nothing and raises nothing."""
    assert find_violation(instance, schema_path) is None
    assert iter_violations(instance, schema_path) == ()
    assert validate_instance(instance, schema_path) is None


def _assert_both_halves_report(
    schema_path: Path, instance: dict[str, object], json_path: str, validator: str
) -> None:
    """Assert both halves report ``json_path`` and ``validator`` for ``instance``."""
    violation = find_violation(instance, schema_path)
    assert isinstance(violation, Violation)
    assert (violation.json_path, violation.validator) == (json_path, validator)
    assert violation.message
    assert violation.schema_path.startswith("#")

    with pytest.raises(ContractError) as caught:
        validate_instance(instance, schema_path)
    assert caught.value.json_path == json_path
    assert caught.value.detail.startswith(f"{json_path}: ")


@pytest.mark.parametrize(("instance", "json_path", "validator"), _RESPONSE_CASES)
def test_the_response_schema_reports_a_violation_at_its_json_path(
    instance: dict[str, object], json_path: str, validator: str
) -> None:
    """Every keyword the committed response schema relies on reports its path and its name."""
    _assert_both_halves_report(_RESPONSE_SCHEMA, instance, json_path, validator)


@pytest.mark.parametrize(("instance", "json_path", "validator"), _ROW_CASES)
def test_the_row_schema_reports_a_violation_at_its_json_path(
    instance: dict[str, object], json_path: str, validator: str
) -> None:
    """Every keyword the committed row schema relies on reports its path and its name."""
    _assert_both_halves_report(_CASE_ROW_SCHEMA, instance, json_path, validator)


# --- The reported violation: Foundry's order, never the library's ----------


def test_first_violation_chooses_the_shallowest_path_over_the_library_order() -> None:
    """The library yields ``$.a`` first here; reversed, the choice must still be ``$.a``."""
    errors = _errors(_DEPTH_SCHEMA, _DEPTH_INSTANCE)
    assert [violation_from_error(error).json_path for error in errors] == ["$.a", "$.b.x"]

    reversed_errors = list(reversed(errors))
    assert violation_from_error(reversed_errors[0]).json_path == "$.b.x"

    chosen = first_violation(reversed_errors)
    assert isinstance(chosen, Violation)
    assert chosen.json_path == "$.a"


def test_first_violation_orders_by_path_before_message() -> None:
    """At equal depth the path decides, even where the message order prefers the other one."""
    errors = _errors(_SIBLING_SCHEMA, _SIBLING_INSTANCE)
    message_by_path = {violation_from_error(error).json_path: error.message for error in errors}
    assert message_by_path.keys() == {"$.a", "$.b"}
    assert message_by_path["$.b"] < message_by_path["$.a"]

    chosen = first_violation(errors)
    assert chosen is not None
    assert chosen.json_path == "$.a"


def test_first_violation_orders_by_message_when_the_path_ties() -> None:
    """Two keywords rejecting one value report one path, so the message decides."""
    errors = _errors(_MESSAGE_SCHEMA, _MESSAGE_INSTANCE)
    violations = [violation_from_error(error) for error in errors]
    assert {violation.json_path for violation in violations} == {"$.a"}
    assert len({violation.message for violation in violations}) == 2

    chosen = first_violation(errors)
    assert chosen is not None
    assert chosen.message == min(violation.message for violation in violations)


@pytest.mark.parametrize(
    ("schema", "instance"),
    (
        pytest.param(_DEPTH_SCHEMA, _DEPTH_INSTANCE, id="depth"),
        pytest.param(_SIBLING_SCHEMA, _SIBLING_INSTANCE, id="path"),
        pytest.param(_MESSAGE_SCHEMA, _MESSAGE_INSTANCE, id="message"),
    ),
)
def test_first_violation_is_independent_of_the_error_order(
    schema: dict[str, object], instance: dict[str, object]
) -> None:
    """Every permutation of the library's errors selects the same violation, twice over."""
    errors = _errors(schema, instance)
    assert len(errors) > 1

    chosen = [first_violation(order) for order in itertools.permutations(errors)]
    assert all(item == chosen[0] for item in chosen)
    assert first_violation(errors) == first_violation(errors)


def test_first_violation_returns_none_when_no_error_was_reported() -> None:
    """No error is not a violation: the return is ``None`` for a list and for an iterator."""
    assert first_violation([]) is None
    assert first_violation(iter(())) is None


def test_index_paths_order_numerically() -> None:
    """Indices compare as numbers, so ``$[2]`` precedes ``$[10]`` rather than following it."""
    schema: dict[str, object] = {"type": "array", "items": {"type": "string"}}

    paths = [violation.json_path for violation in iter_violations(list(range(11)), schema)]

    assert paths == [f"$[{index}]" for index in range(11)]


def test_validate_instance_raises_the_first_violation_as_a_contract_error() -> None:
    """The raising half reports the same violation the returning half selects."""
    with pytest.raises(ContractError) as caught:
        validate_instance(_DEPTH_INSTANCE, _DEPTH_SCHEMA)

    error = caught.value
    assert error.json_path == "$.a"
    assert error.detail.startswith("$.a: ")
    assert "validator: type" in error.detail


# --- Rendering, contract errors and excerpts -------------------------------


def test_render_violation_names_the_path_the_validator_and_the_schema_path() -> None:
    """One line carries the reportable text: path, message, schema path and validator."""
    violation = find_violation(_DEPTH_INSTANCE, _DEPTH_SCHEMA)
    assert violation is not None

    rendered = render_violation(violation)

    assert rendered.startswith("$.a: ")
    assert violation.message in rendered
    assert "schema path: #/properties/a/type" in rendered
    assert "validator: type" in rendered


def test_a_contract_error_carries_the_rendered_path_of_its_violation() -> None:
    """The path a caller branches on and the detail it reads cannot diverge."""
    violation = find_violation(_DEPTH_INSTANCE, _DEPTH_SCHEMA)
    assert violation is not None

    error = to_contract_error(violation)

    assert error.json_path == violation.json_path
    assert error.json_path.startswith("$")
    assert error.message == f"contract validation failed at {violation.json_path}"
    assert error.detail == render_violation(violation)
    assert error.detail.startswith(f"{violation.json_path}: ")
    assert error.hint is None


def test_rendering_a_violation_redacts_a_secret_quoted_from_the_instance() -> None:
    """The record keeps the raw message; every surface a sink sees is scrubbed on the way out."""
    violation = find_violation(_NOTE_INSTANCE, _NOTE_SCHEMA)
    assert violation is not None
    assert _SECRET in violation.message

    surfaces = (
        render_violation(violation),
        violation.render(),
        str(violation),
        to_contract_error(violation).detail,
        render_violation(violation, redact=default_redactor().text),
    )
    for surface in surfaces:
        assert f"password={REDACTED}" in surface
        assert "hunter" not in surface
        assert _SECRET not in json.dumps(surface)


def test_excerpt_cuts_a_body_to_the_byte_budget() -> None:
    """A long body loses its tail, keeps its head and gains the marker the contract specifies."""
    body = "x" * 400

    text = excerpt(body)

    assert len(body.encode("utf-8")) > MAX_EXCERPT_BYTES
    assert text.endswith(_EXCERPT_MARKER)
    assert len(text.encode("utf-8")) <= MAX_EXCERPT_BYTES + len(_EXCERPT_MARKER)
    kept = text.removesuffix(_EXCERPT_MARKER)
    assert body.startswith(kept)
    assert len(kept.encode("utf-8")) == MAX_EXCERPT_BYTES


def test_excerpt_redacts_before_it_truncates() -> None:
    """Redaction runs first, so the cut cannot leave the prefix of a secret behind."""
    body = "x" * 180 + f" password={_SECRET}" + "z" * 50

    text = excerpt(body)

    assert f"password={REDACTED}" in text
    # A cut applied first would have stopped inside the credential and left part of it in the text.
    assert "hunter" not in text
    assert _SECRET not in json.dumps(f"JSON expected but the body was: {text}")


def test_excerpt_accepts_bytes_and_honours_an_explicit_budget() -> None:
    """The body may arrive as bytes, and ``limit`` is the byte budget the caller chose."""
    text = excerpt(b"<html><body>" + b"y" * 300)

    assert text.startswith("<html><body>")
    assert len(text.encode("utf-8")) <= MAX_EXCERPT_BYTES + len(_EXCERPT_MARKER)
    assert excerpt("not json") == "not json"
    assert excerpt("abcdefghijklmno", limit=10) == f"abcdefghij{_EXCERPT_MARKER}"


# --- The startup-path rule (implicit requirement I5) -----------------------


def test_importing_the_package_pulls_in_neither_jsonschema_nor_yaml(
    repo_root: Path, foundry_python: str, subprocess_env: Callable[..., dict[str, str]]
) -> None:
    """A fresh interpreter imports the package and reports what came with it (AAP 0.4.8)."""
    probe = (
        "import sys, foundry.contract; print('jsonschema' in sys.modules, 'yaml' in sys.modules)"
    )

    completed = subprocess.run(
        [foundry_python, "-c", probe],
        cwd=str(repo_root),
        env=subprocess_env({"PYTHONPATH": str(repo_root / "src")}),
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "False False"
