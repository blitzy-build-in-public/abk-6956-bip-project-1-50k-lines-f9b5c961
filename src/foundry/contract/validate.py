"""Instance validation: compile a schema, walk an instance, report the first violation.

Two callers reach Foundry's contract layer with opposite needs.
:func:`foundry.assertions.assert_json_schema` wants an exception that carries the violating JSON
path, and :mod:`foundry.data.schema` wants a value it can turn into a
:class:`~foundry.core.exceptions.DataError` naming the file, the line and the column of the
offending row. :func:`validate_instance` raises and :func:`find_violation` returns, and both run
the same walk, so neither has to catch and re-wrap what the other reports.

Two failure modes stay apart, because reporting one as the other misdescribes the fault:

instance invalid
    The document is well-formed JSON that breaks the schema. :func:`find_violation` returns the
    first :class:`~foundry.contract.violations.Violation`, and :func:`validate_instance` raises
    the :class:`~foundry.core.exceptions.ContractError` that
    :func:`foundry.contract.violations.to_contract_error` builds, whose ``json_path`` is that
    violation's path.
schema unusable
    The schema cannot be loaded, is not a schema object, is invalid under its metaschema,
    declares a draft this build of ``jsonschema`` cannot supply, or references a subschema that
    does not exist. No verdict about the instance exists, so every entry point --
    :func:`find_violation` included -- raises :class:`~foundry.core.exceptions.ContractError`.

A schema arrives in any of the three shapes :data:`foundry.contract.schema.SchemaSource` allows:
a mapping in memory, or the path to a ``.json``, ``.yaml`` or ``.yml`` file.
:func:`foundry.contract.schema.load_schema` turns all three into one document, so nothing here
knows which shape it was handed.

The reported violation is Foundry's choice, never the library's. ``iter_errors`` yields errors in
an order it does not document, so every result is ordered by
:func:`foundry.contract.violations.sort_violations` -- shallowest JSON path first, then the path
structurally, then the message -- and the relevance ranking the library offers is never used,
because it is an explicit heuristic whose answer can change with the shape of the schema (AAP
0.2.1). The same instance and schema therefore report the same ``message``, ``json_path`` and
``detail`` in every process and on every ``pytest-xdist`` worker.

The draft is pinned rather than inferred. A document with no ``$schema`` is validated as JSON
Schema 2020-12, and a document naming a draft this build cannot supply is refused by name instead
of being silently validated as something else.

``format`` is annotation only. Format checking needs the ``format`` extras, which sit outside the
declared dependency set (AAP 0.5.1), so a schema carrying ``"format": "email"`` accepts
``"not-an-email"``: validation stays deterministic and offline, and Foundry never reports a
failure a reviewer cannot reproduce with the packages the wheel declares.

Importing this module defines names and does nothing else. ``jsonschema`` is imported inside the
functions that need it and never at module scope, because :mod:`foundry.http` imports this
module, the plugin loads it on every ``pytest`` startup, and the 150 ms startup budget is
measured from there (AAP 0.4.8, implicit requirement I5). The module keeps no compiled-validator
cache and no other mutable state, so a schema edited between two calls is read by the second. It
imports :mod:`foundry.core.exceptions`, :mod:`foundry.contract.schema` and
:mod:`foundry.contract.violations` and nothing else from the tree, raises only
:class:`~foundry.core.exceptions.ContractError`, and logs at most one ``DEBUG`` line per walk,
naming the draft in use and the number of violations, never the instance and never a violation
message.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Final
from urllib.parse import urlsplit

from foundry.contract.schema import SchemaSource, declared_draft, load_schema
from foundry.contract.violations import (
    Violation,
    choose_first,
    excerpt,
    format_schema_path,
    json_path_from_parts,
    sort_violations,
    to_contract_error,
    violation_from_error,
)
from foundry.core.exceptions import ContractError

if TYPE_CHECKING:
    from jsonschema.exceptions import SchemaError, ValidationError
    from jsonschema.protocols import Validator
    from referencing.exceptions import Unresolvable

__all__: Final[tuple[str, ...]] = (
    "check_schema",
    "find_violation",
    "first_violation",
    "iter_violations",
    "select_validator",
    "validate_instance",
)

_LOGGER: Final[logging.Logger] = logging.getLogger(__name__)

_DOCUMENT_ROOT: Final[str] = "$"
"""JSON path of a document root: the ``json_path`` a schema fault with no location reports."""

_DETAIL_LIMIT: Final[int] = 200
"""Byte budget of the quoted part of a schema fault, matching the excerpt contract."""


# --- Schema faults: built in one place, so they read the same everywhere ----


def _schema_fault(
    *,
    message: str,
    detail: str,
    json_path: str = _DOCUMENT_ROOT,
    hint: str | None = None,
) -> ContractError:
    """Build the contract error a fault in a schema document reports.

    A fault in the schema is not a verdict about the instance, so one construction site keeps
    every such failure in the same shape: the one-line summary in ``message``, the specifics in
    ``detail``, an actionable ``hint`` where one exists, and the reported path left at the
    document root unless the fault names a location inside the schema.

    Args:
        message: The one-line summary a caller reads first.
        detail: What is wrong, in the terms of the schema document at fault.
        json_path: The path the exception reports; the document root for a fault that has no
            location inside the schema.
        hint: An actionable next step, or ``None``.

    Returns:
        The exception to raise, never raised here: each caller raises it inside its own
        ``except`` clause so the original cause stays chained.
    """
    return ContractError(message, json_path=json_path, detail=detail, hint=hint)


def _invalid_schema_fault(error: SchemaError) -> ContractError:
    """Build the contract error for a document that is not a valid schema.

    Both paths are rendered by :mod:`foundry.contract.violations`, so the ``$``-rooted JSON path
    of a schema fault is spelled exactly like the path of a violation. The message is collapsed
    to one line and redacted, so a schema cannot inject a newline -- or a quoted secret -- into a
    failure report.

    Args:
        error: The metaschema error ``jsonschema`` raised for the document.

    Returns:
        The exception to raise, with the failing location inside the schema as ``json_path``.
    """
    json_path = json_path_from_parts(tuple(error.absolute_path))
    schema_path = format_schema_path(tuple(error.absolute_schema_path))
    message = excerpt(error.message, limit=_DETAIL_LIMIT)
    return _schema_fault(
        message="schema is invalid",
        json_path=json_path,
        detail=f"schema is invalid: {message} (schema path: {schema_path})",
        hint="correct the schema document; a schema fault is not an instance verdict",
    )


def _reference_fault(error: Unresolvable) -> ContractError:
    """Build the contract error for a reference that cannot be resolved.

    :func:`foundry.contract.schema.load_schema` has already refused every reference that is not a
    fragment of the document, so the only reference left is a fragment naming a subschema that
    does not exist -- a typo in a ``$ref``, in practice. The reference and the library's reason
    are both collapsed to one line and redacted, so a report stays a single readable line
    whatever the document holds.

    Args:
        error: The reference fault ``jsonschema`` raised while walking the instance.

    Returns:
        The exception to raise, naming the unresolved reference.
    """
    reference = excerpt(str(error.ref), limit=_DETAIL_LIMIT)
    reason = excerpt(str(error), limit=_DETAIL_LIMIT)
    return _schema_fault(
        message=f"schema reference cannot be resolved: {reference}",
        detail=f"schema reference {reference} cannot be resolved: {reason}",
        hint="define the referenced subschema, or correct the fragment in the reference",
    )


def _recursion_fault() -> ContractError:
    """Build the contract error for a walk that exhausted the interpreter's recursion limit.

    Two inputs reach it: a schema whose reference names itself, which the library follows
    forever, and a value nested deeply enough that the walk runs out of stack. Either way the
    fault is in the input rather than in Foundry, and a builtin must not cross this module's
    boundary (AAP 0.14.3).

    Returns:
        The exception to raise.
    """
    return _schema_fault(
        message="schema validation could not complete",
        detail=(
            "the walk exhausted the interpreter's recursion limit: a reference that names "
            "itself, or a document nested too deeply to validate"
        ),
        hint="replace a self-referential reference with a $defs subschema, or flatten the value",
    )


# --- Draft selection: pinned, never inferred from the installed library -----


def _normalised_uri(uri: str) -> str:
    """Return a draft URI in the form ``jsonschema`` registers drafts under.

    The library keys its draft table by ``urlparse(uri).geturl()``, which drops an empty trailing
    ``#`` fragment and keeps a non-empty one. Normalising the same way here is what lets a
    document declaring ``http://json-schema.org/draft-07/schema#`` be recognised as the draft
    ``http://json-schema.org/draft-07/schema`` names.

    Args:
        uri: A ``$schema`` value or a draft's meta-schema identifier.

    Returns:
        The URI as the library would key it.
    """
    return urlsplit(uri).geturl()


def _draft_uris(validator_class: type[Validator]) -> frozenset[str]:
    """Return the URIs one validator class answers to.

    A draft publishes its own URI in its meta-schema, under ``$id`` in the modern drafts and
    under ``id`` in the older ones, and the library registers the class under exactly that value.

    Args:
        validator_class: A validator class, normally taken from :mod:`jsonschema`.

    Returns:
        The normalised meta-schema identifiers the class declares; empty for a class that
        declares none.
    """
    meta_schema = validator_class.META_SCHEMA
    uris: set[str] = set()
    for keyword in ("$id", "id"):
        value = meta_schema.get(keyword)
        if isinstance(value, str):
            uris.add(_normalised_uri(value))
    return frozenset(uris)


def _supported_draft_uris() -> tuple[str, ...]:
    """Return the draft URIs this build of ``jsonschema`` can supply, sorted.

    The set is read from the six draft classes the library publishes rather than from its
    private registry, so the message a refused draft produces names only what a caller can
    actually write in ``$schema``.

    Returns:
        The supported draft URIs, sorted so the message is identical on every run.
    """
    import jsonschema

    uris: set[str] = set()
    for validator_class in (
        jsonschema.Draft3Validator,
        jsonschema.Draft4Validator,
        jsonschema.Draft6Validator,
        jsonschema.Draft7Validator,
        jsonschema.Draft201909Validator,
        jsonschema.Draft202012Validator,
    ):
        uris.update(_draft_uris(validator_class))
    return tuple(sorted(uris))


# --- The public walk -------------------------------------------------------


def select_validator(document: Mapping[str, object]) -> type[Validator]:
    """Return the validator class the document's draft calls for.

    ``validator_for`` is given :class:`jsonschema.Draft202012Validator` as its explicit default
    rather than relying on the library's implicit one, which tracks whichever release a user
    resolved: pinning it makes the draft a property of Foundry 0.1.0, so a document carrying no
    ``$schema`` validates identically on every machine.

    A ``$schema`` that is not a string names no draft at all, so the pinned default is returned
    and :func:`check_schema` reports the malformed keyword under the metaschema -- which is the
    useful diagnosis, and avoids the builtin ``validator_for`` raises for an unhashable or a
    non-decoded value.

    Args:
        document: A schema document, as :func:`foundry.contract.schema.load_schema` returns it.

    Returns:
        The validator class to compile the document with.

    Raises:
        ContractError: When the document names a draft this build of ``jsonschema`` cannot
            supply, so no validator can honour what the document declares.
    """
    import jsonschema
    from jsonschema.validators import validator_for

    default = jsonschema.Draft202012Validator
    declared = declared_draft(document)
    if declared is None:
        return default

    selected = validator_for(document, default=default)
    if selected is not default:
        return selected
    if _normalised_uri(declared) in _draft_uris(default):
        return default

    supported = ", ".join(_supported_draft_uris())
    raise _schema_fault(
        message="schema declares an unsupported JSON Schema draft",
        detail=(
            f"schema declares $schema {declared}, which this build of jsonschema cannot supply; "
            f"supported drafts: {supported}"
        ),
        hint=(
            "declare one of the supported drafts, or drop the $schema keyword to validate as "
            "JSON Schema 2020-12"
        ),
    )


def check_schema(document: Mapping[str, object]) -> None:
    """Raise unless the document is a valid schema for the draft it declares or defaults to.

    A schema fault is **not** an instance verdict: this function reports a document that cannot
    be used at all, and never reports that an instance failed. It runs on every walk, because a
    schema with an unusable keyword would otherwise fail in the middle of validation, where the
    fault reads like a violation of the instance.

    Args:
        document: A schema document, as :func:`foundry.contract.schema.load_schema` returns it.

    Returns:
        None when the document is valid under its metaschema.

    Raises:
        ContractError: When the document is not a valid schema, when it declares a draft this
            build cannot supply, or when inspecting it exhausts the interpreter's recursion
            limit.
    """
    from jsonschema.exceptions import SchemaError

    validator_class = select_validator(document)
    try:
        # ``check_schema`` is typed as taking a dict, and the shallow copy also keeps the
        # caller's mapping out of the library's hands: this module never mutates a schema.
        validator_class.check_schema(dict(document))
    except SchemaError as exc:
        raise _invalid_schema_fault(exc) from exc
    except RecursionError as exc:
        raise _recursion_fault() from exc


def iter_violations(instance: object, schema: SchemaSource) -> tuple[Violation, ...]:
    """Return every violation of the schema by the instance, in Foundry's reporting order.

    Args:
        instance: The value to validate. Any object is accepted; a value that is not JSON is
            reported as a violation by the keyword that rejects it.
        schema: The schema document, or the path to the ``.json``, ``.yaml`` or ``.yml`` file
            that holds it.

    Returns:
        The violations, shallowest JSON path first, or an empty tuple when the instance is valid.

    Raises:
        ContractError: When the schema is unusable -- unloadable, not a schema object, invalid
            under its metaschema, declaring a draft this build cannot supply, or referencing a
            subschema that does not exist.
    """
    # ``referencing`` is a declared runtime dependency of this distribution -- see ``[project]
    # dependencies`` in pyproject.toml and the recorded reason in docs/verification.md -- not a
    # transitive import borrowed from jsonschema.
    from referencing.exceptions import Unresolvable

    document = load_schema(schema)
    check_schema(document)
    validator_class = select_validator(document)
    validator = validator_class(document)
    try:
        # Probed against the pinned jsonschema 4.26: an unresolvable fragment raises
        # jsonschema.exceptions._WrappedReferencingError, whose documented base for callers is
        # referencing.exceptions.Unresolvable -- the type jsonschema's own deprecation notice
        # says to catch. Its other base, the deprecated _RefResolutionError, is private and
        # absent from the type stubs, so it is not named here. A reference that names its own
        # document is followed until the interpreter refuses, which is a RecursionError.
        #
        # jsonschema types the instance as JSON, but a validator accepts any object and reports a
        # non-JSON value as a violation instead of rejecting it, so the public ``object`` passes.
        errors = list(validator.iter_errors(instance))  # type: ignore[arg-type]
    except Unresolvable as exc:
        raise _reference_fault(exc) from exc
    except RecursionError as exc:
        raise _recursion_fault() from exc

    violations = sort_violations(violation_from_error(error) for error in errors)
    draft = validator_class.META_SCHEMA.get("$id")
    _LOGGER.debug(
        "validated against %s: %d violation(s)",
        draft if isinstance(draft, str) else validator_class.__name__,
        len(violations),
    )
    return violations


def find_violation(instance: object, schema: SchemaSource) -> Violation | None:
    """Return the first violation of the schema by the instance, or ``None`` when it is valid.

    This is the non-raising half of the split. An *invalid instance* is a value the caller
    decides what to do with -- :mod:`foundry.data.schema` turns it into a
    :class:`~foundry.core.exceptions.DataError` naming the file, the line and the column the
    violation's ``path_parts`` lead to -- while an *unusable schema* raises, because no verdict
    about the instance exists to return.

    Args:
        instance: The value to validate.
        schema: The schema document, or the path to the file that holds it.

    Returns:
        The first violation in Foundry's order, or ``None`` when the instance is valid.

    Raises:
        ContractError: When the schema is unusable, for the reasons :func:`iter_violations`
            lists.
    """
    violations = iter_violations(instance, schema)
    return violations[0] if violations else None


def validate_instance(instance: object, schema: SchemaSource) -> None:
    """Raise unless the instance satisfies the schema.

    :func:`foundry.assertions.assert_json_schema` calls this function, so the failure a test
    reads is fixed here once: the message is ``contract validation failed at <json_path>``, the
    ``json_path`` attribute carries that path, and ``detail`` carries the redacted
    ``<json_path>: <message> (schema path: ..., validator: ...)`` line that
    :func:`foundry.contract.violations.to_contract_error` builds.

    Args:
        instance: The value to validate.
        schema: The schema document, or the path to the file that holds it.

    Returns:
        None when the instance satisfies the schema.

    Raises:
        ContractError: When the instance violates the schema, carrying the first violation's
            path and detail; or when the schema itself is unusable.
    """
    violation = find_violation(instance, schema)
    if violation is not None:
        raise to_contract_error(violation)


def first_violation(errors: Iterable[ValidationError]) -> Violation | None:
    """Return the first violation among the errors, in Foundry's order.

    The rule that selects it -- shallowest JSON path first, then the path structurally, then the
    message -- belongs to :func:`foundry.contract.violations.choose_first`, and this function
    delegates to it rather than restating it. Two copies of that rule would eventually disagree,
    and every determinism guarantee Foundry makes about a reported violation rests on there being
    exactly one.

    Args:
        errors: The errors produced by validating one instance against one schema, in any order,
            possibly none.

    Returns:
        The violation with the lowest order key, or ``None`` when ``errors`` is empty.
    """
    return choose_first(errors)
