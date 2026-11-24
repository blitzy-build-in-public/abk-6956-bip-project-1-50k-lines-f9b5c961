"""Instance validation: compile a schema, walk an instance, report the first violation.

Two callers reach the contract layer with opposite needs:
:func:`foundry.assertions.assert_json_schema` wants an exception carrying the violating JSON path,
:mod:`foundry.data.schema` a value it turns into a :class:`~foundry.core.exceptions.DataError`.
:func:`validate_instance` raises, :func:`find_violation` returns, and both run the same walk.

An *instance invalid* -- a document that breaks the schema -- is one or the other of those, built
from the first :class:`~foundry.contract.violations.Violation`. A *schema unusable* -- unloadable,
not a schema object, invalid under its metaschema, declaring an unavailable draft, or referencing
a missing subschema -- leaves no verdict about the instance, so every entry point raises.

The reported violation is Foundry's choice, never the library's: ``iter_errors`` documents no
ordering and the library's relevance ranking is an explicit heuristic whose answer can change with
the shape of the schema (AAP 0.2.1), so every result is ordered by
:func:`foundry.contract.violations.sort_violations` -- shallowest JSON path first, then the path
structurally, then the message -- and the same instance reports the same violation everywhere.

The draft is pinned rather than inferred: a document with no ``$schema`` validates as JSON Schema
2020-12, and one naming an unavailable draft is refused by name. ``format`` is annotation only,
because format checking needs extras outside the declared dependency set (AAP 0.5.1).

``format`` is annotation only: its extras sit outside the declared dependency set (AAP 0.5.1), so a
schema carrying ``"format": "email"`` accepts ``"not-an-email"``, which keeps validation offline and
reproducible with the packages the wheel declares.

Importing this module defines names and does nothing else: ``jsonschema`` is imported inside the
functions that need it, never at module scope, because :mod:`foundry.http` imports this module and
the plugin loads it on every ``pytest`` startup inside the 150 ms budget (AAP 0.4.8, implicit
requirement I5). It keeps no compiled-validator cache and no other mutable state, so a schema
edited between two calls is read by the second; at module scope it imports only
:mod:`foundry.core.exceptions`, :mod:`foundry.contract.schema` and
:mod:`foundry.contract.violations`, and it raises only
:class:`~foundry.core.exceptions.ContractError`. Its single ``DEBUG`` record per walk -- draft and
violation count, never an instance value or a violation message -- goes through
:func:`foundry.obs.events.log_event`, imported inside :func:`iter_violations` so that module-scope
imports stay inside the set AAP 0.4.8 lists for ``foundry.contract``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Final
from urllib.parse import urlsplit

from foundry.contract.schema import SchemaSource, declared_draft, load_schema
from foundry.contract.violations import (
    RedactorCallable,
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

    One construction site keeps every such failure in the same shape: the one-line summary in
    ``message``, the specifics in ``detail``, an actionable ``hint`` where one exists, and the path
    at the document root unless the fault names a location inside the schema. Returned, never
    raised here, so each caller keeps the original cause chained.
    """
    return ContractError(message, json_path=json_path, detail=detail, hint=hint)


def _invalid_schema_fault(error: SchemaError) -> ContractError:
    """Build the contract error for a document that is not a valid schema.

    Both paths are rendered by :mod:`foundry.contract.violations`, so a schema fault's JSON path is
    spelled exactly like the path of a violation, and the message is collapsed to one line and
    redacted so a schema cannot inject a newline -- or a quoted secret -- into a failure report.
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

    :func:`foundry.contract.schema.load_schema` refuses every reference that is not a fragment, so
    the only one left names a subschema that does not exist -- a ``$ref`` typo, in practice. The
    reference and the library's reason are collapsed to one line and redacted.
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

    A schema whose reference names itself, which the library follows forever, and a value nested
    deeply enough to run out of stack both reach it; either way the fault is in the input, and a
    builtin must not cross this module's boundary (AAP 0.14.3).
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
    ``#`` fragment, so normalising the same way is what lets a document declaring
    ``http://json-schema.org/draft-07/schema#`` be recognised as the draft that URI names.
    """
    return urlsplit(uri).geturl()


def _draft_uris(validator_class: type[Validator]) -> frozenset[str]:
    """Return the URIs one validator class answers to.

    A draft publishes its own URI in its meta-schema, under ``$id`` in the modern drafts and under
    ``id`` in the older ones, and the library registers the class under exactly that value; a
    class that declares neither answers to nothing.
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

    The set is read from the six draft classes the library publishes rather than from its private
    registry, so the message a refused draft produces names only what a caller can actually write
    in ``$schema``, and sorting keeps that message identical on every run.
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
    ``$schema`` validates identically on every machine. A ``$schema`` that is not a string names
    no draft at all, so the pinned default is returned and :func:`check_schema` reports the
    malformed keyword under the metaschema, rather than letting ``validator_for`` raise on it.

    Returns:
        The validator class to compile the document with.

    Raises:
        ContractError: When the document names a draft this build of ``jsonschema`` cannot supply.
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

    A schema fault is **not** an instance verdict: this reports a document that cannot be used at
    all, and never reports that an instance failed. It runs on every walk, because an unusable
    keyword would otherwise fail mid-validation, where the fault reads like a violation.

    Raises:
        ContractError: When the document is not a valid schema, when it declares a draft this
            build cannot supply, or when inspecting it exhausts the recursion limit.
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
        instance: The value to validate; a value that is not JSON is reported as a violation by the
            keyword that rejects it.
        schema: The schema document, or the path to the ``.json``, ``.yaml`` or ``.yml`` file that
            holds it.

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
    from foundry.obs.events import LEVEL_DEBUG, LifecycleEvent, log_event

    log_event(
        LifecycleEvent(
            name="contract_validation",
            level=LEVEL_DEBUG,
            fields=(
                ("draft", draft if isinstance(draft, str) else validator_class.__name__),
                ("violations", str(len(violations))),
            ),
        )
    )
    return violations


def find_violation(instance: object, schema: SchemaSource) -> Violation | None:
    """Return the first violation of the schema by the instance, or ``None`` when it is valid.

    This is the non-raising half of the split: an *invalid instance* is a value the caller decides
    what to do with -- :mod:`foundry.data.schema` turns it into a
    :class:`~foundry.core.exceptions.DataError` naming the file, line and column -- while an
    *unusable schema* raises, because no verdict about the instance exists.    Returns:
        The first violation in Foundry's order, or ``None`` when the instance is valid.

    Raises:
        ContractError: When the schema is unusable, for the reasons :func:`iter_violations` lists.
    """
    violations = iter_violations(instance, schema)
    return violations[0] if violations else None


def validate_instance(
    instance: object, schema: SchemaSource, *, redact: RedactorCallable | None = None
) -> None:
    """Raise unless the instance satisfies the schema.

    :func:`foundry.assertions.assert_json_schema` calls this function, so the failure a test reads
    is fixed here once: the message is ``contract validation failed at <json_path>``, ``json_path``
    carries that path, and ``detail`` carries the redacted path, message, schema path and validator
    line :func:`to_contract_error` builds.

    ``redact`` is normally the run's engine built from ``secrets.redact_keys``, so a configured key
    is scrubbed as well as the built-in deny-lists; ``None`` applies those deny-lists alone.

    Returns:
        None when the instance satisfies the schema.

    Raises:
        ContractError: When the instance violates the schema, carrying the first violation's path
            and detail; or when the schema itself is unusable.
    """
    violation = find_violation(instance, schema)
    if violation is not None:
        raise to_contract_error(violation, redact=redact)


def first_violation(errors: Iterable[ValidationError]) -> Violation | None:
    """Return the first violation among the errors, in Foundry's order.

    The rule that selects it -- shallowest JSON path first, then the path structurally, then the
    message -- belongs to :func:`foundry.contract.violations.choose_first`, and this function
    delegates to it rather than restating it: two copies would eventually disagree, and every
    determinism guarantee Foundry makes about a reported violation rests on there being one.

    Returns:
        The violation with the lowest order key, or ``None`` when ``errors`` is empty.
    """
    return choose_first(errors)
