"""The ``assert_json_schema`` helper: validate a decoded JSON value against a JSON Schema.

:func:`assert_json_schema` returns ``None`` when the value conforms, and otherwise raises the
:class:`foundry.core.exceptions.ContractError` that :func:`foundry.contract.validate_instance`
builds, carrying the first violation's ``json_path`` and redacted ``detail`` (AAP 0.4.3); which
violation is first is Foundry's own rule in :mod:`foundry.contract.violations`. Fixed by AAP 0.4.9.
"""

from __future__ import annotations

from foundry.contract import SchemaSource, validate_instance

__all__ = ["assert_json_schema"]


def assert_json_schema(instance: object, schema: SchemaSource) -> None:
    """Assert that ``instance`` satisfies the JSON Schema ``schema``.

    Args:
        instance: The decoded JSON value under test, typically ``FoundryResponse.json()`` or a
            loaded data file: ``None``, ``bool``, ``int``, ``float``, ``str``, ``list`` or a
            mapping with ``str`` keys, as :func:`json.loads` produces. A value outside that shape
            is reported as a violation by the schema keyword that rejects it, rather than refused
            here with a second kind of error.
        schema: The schema document, or the path to the ``.json``, ``.yaml`` or ``.yml`` file that
            holds it, resolved relative to the working directory when it is not absolute.

    Returns:
        None when the instance satisfies the schema. The call reports its result through the
        exception it raises, which is what lets a test use it as a bare statement.

    Raises:
        ContractError: When the instance violates the schema -- carrying the first violation's
            ``json_path`` and its redacted ``detail`` -- or when the schema itself is unusable: an
            unreadable path, a document that is not a valid schema, a draft this build of
            ``jsonschema`` cannot supply, or a reference to a subschema that does not exist.

    Example:
        Report the offending property rather than the document root::

            schema = {
                "type": "object",
                "required": ["id"],
                "properties": {"id": {"type": "integer"}},
            }
            assert_json_schema({"id": 7}, schema)        # returns None
            assert_json_schema({"id": "seven"}, schema)  # raises, json_path is "$.id"
    """
    validate_instance(instance, schema)
