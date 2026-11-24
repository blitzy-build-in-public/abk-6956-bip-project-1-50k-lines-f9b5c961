"""The ``assert_json_schema`` helper: validate a decoded JSON value against a JSON Schema.

:func:`assert_json_schema` is the public entry point a test uses to prove that a document -- an
HTTP response body, a loaded data file, a payload the test itself just built -- conforms to a JSON
Schema (AAP 0.4.3). It validates the value against the schema and returns ``None`` when the value
conforms. When it does not, the failure is a
:class:`foundry.core.exceptions.ContractError` carrying the **first** violation and its JSON path,
which is what makes the helper usable as a bare statement in a test.

That error is fixed once, in :func:`foundry.contract.validate_instance`: the message is
``contract validation failed at <json_path>``, ``json_path`` locates the defect inside the
instance (``$.items[0].price``), and ``detail`` renders the violation as its path, message, schema
path and validator name. The rendering is redacted through :mod:`foundry.obs.redaction` before it
is shown, so a value the schema rejects cannot print a configured secret into a terminal,
``results.json``, ``junit.xml`` or ``report.html`` (AAP 0.14.3). This module adds no error type of
its own and raises nothing it did not receive: propagating the error unchanged is the contract,
because any wrapping or rewording would destroy the ``json_path`` the caller and the report read.

The violation reported is Foundry's choice, never the library's: :mod:`jsonschema` documents no
ordering for its own error iterator and the relevance ranking it offers is an explicit heuristic,
so "the first violation" is a rule Foundry fixes itself -- shallowest JSON path first, then the
path structurally, then the message -- in :mod:`foundry.contract.violations`. The same instance
and schema therefore report the same violation on every run and on every xdist worker. That rule
is also why this module never calls ``jsonschema.best_match`` and never imports :mod:`jsonschema`
at all: it cannot pick a violation, because it cannot reach the heuristic, and pulling the library
in here would put it on the plugin's startup path (implicit requirement I5).

The schema argument is a :data:`foundry.contract.SchemaSource`: a schema document in memory, or
the path to a ``.json``, ``.yaml`` or ``.yml`` file holding one. Loading belongs to
:mod:`foundry.contract.schema`, which parses YAML through ``yaml.safe_load`` alone and refuses
every ``$ref`` that is not a fragment of the same document, so no validation can retrieve a
document -- which would mean opening a socket -- and Foundry stays offline. An unreadable path, a
malformed document and a mapping that is not a schema all surface as the same
:class:`~foundry.core.exceptions.ContractError`.

``instance`` is annotated ``object`` and ``schema``
:class:`~foundry.contract.SchemaSource`. AAP 0.4.3 writes the pair as ``Any`` and
``Mapping[str, Any] | str | Path``, while AAP 0.4.1 and AAP 0.13.1 forbid ``Any`` in a public
signature and require a clean ``mypy --strict``; ``SchemaSource`` is contract's published alias for
exactly the second shape, and ``object`` is the accurate type of a decoded JSON value, whose JSON
shape -- ``None``, ``bool``, ``int``, ``float``, ``str``, ``list`` or a mapping with ``str`` keys
-- is documented here rather than enforced with checks that would duplicate the schema's own type
keyword. The name, the parameter shape and the ``None`` return are the ones AAP 0.4.3 and AAP
0.4.9 fix for ``0.1.0``.

The body is one delegation on purpose: :func:`foundry.contract.validate_instance` already resolves
the schema, walks the instance and builds the error, so this helper publishes that behaviour under
the name a test calls and nothing more. Importing this module defines the function and its
``__all__`` and nothing else -- no I/O, no environment read, no logger configuration, no
module-level mutable state (AAP 0.4.8); the only Foundry import is :mod:`foundry.contract`, the
capability rung AAP 0.4.8 permits for this package.

Example:
    Assert a response body against the schema a project keeps beside its tests::

        schema = {"type": "object", "required": ["id"]}
        assert_json_schema(response.json(), schema)      # returns None when it conforms
        assert_json_schema({"items": []}, schema)        # raises ContractError at "$"
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
