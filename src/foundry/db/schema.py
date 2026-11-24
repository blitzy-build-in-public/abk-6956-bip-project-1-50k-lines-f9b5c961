"""Schema helpers behind the ``foundry_db`` fixture.

Foundry's database support is deliberately thin. The framework ships no ORM model of
its own -- AAP 0.7.2 defines Foundry's own data as pydantic and dataclass records, none
of which is a table -- so every schema this package creates is one the *test* supplies.
This module turns "the models the test registers" into a created sqlite schema and back
again, and it is the only place that decides which metadata objects are created and in
which order (AAP 0.3.2, AAP 0.7.1, AAP 0.4.3).

The mapping style it assumes is SQLAlchemy 2.0 declarative: a model is a
:class:`~sqlalchemy.orm.DeclarativeBase` subclass whose columns are declared with
``Mapped[...]`` and ``mapped_column``. A plain :class:`~sqlalchemy.Table` is accepted as
well, because a project that maps imperatively or builds an auxiliary table still wants
its schema created, and :func:`normalize_models` treats both forms as one vocabulary --
this module calls either of them a "model-like" value.

Two rules shape the implementation.

*Models travel as a value, never as module state.* "Declarative model registration" here
means normalising the argument of the current call. There is no global ``Base``, no
registry and no cache: the layering contract (AAP 0.4.8) permits import-time mutation
only in the two registration tables owned by :mod:`foundry.data.registry` and
:mod:`foundry.reporting.renderers`, and a shared model registry would additionally leak
one test's schema into the next.

*SQLAlchemy is imported lazily.* The plugin has a 150 ms startup budget (implicit
requirement I5), so no SQLAlchemy name is bound at module scope: every name used in an
annotation is imported under ``TYPE_CHECKING``, and every name used at runtime is
imported inside the function that needs it. Importing this module therefore costs the
standard library and :mod:`foundry.core.exceptions` and nothing else.

Layering and behaviour follow the contract as well. The module imports only
:mod:`foundry.core.exceptions` from Foundry (AAP 0.4.8 places ``foundry.db`` on the
foundation rung), defines no exception type of its own, and raises nothing but
:class:`~foundry.core.exceptions.FoundryError` (AAP 0.14.3). It does no work at import
time: no I/O, no environment read, no logger configuration and no module-level mutable
state beyond ``__all__``.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Final, TypeAlias, TypeGuard

from foundry.core.exceptions import FoundryError

if TYPE_CHECKING:
    from sqlalchemy import MetaData, Table
    from sqlalchemy.engine import Engine
    from sqlalchemy.orm import DeclarativeBase

    ModelLike: TypeAlias = type[DeclarativeBase] | Table
    """A value a schema can be created from: a mapped class, or a ``Table``.

    The alias exists for annotations only, which is why it lives under
    ``TYPE_CHECKING``: naming it at runtime would bind a SQLAlchemy
    :class:`~sqlalchemy.Table` import into the module's import cost and break the
    startup budget. Code never inspects it -- there is no ``isinstance(x, ModelLike)``
    anywhere -- because a union of a class type and a metaclass-built instance has no
    meaningful runtime check; :func:`_as_model` performs the two tests separately.
    """

    ModelSpec: TypeAlias = ModelLike | Iterable[ModelLike] | None
    """Everything a caller may pass where a set of models is expected.

    ``None`` for no models, one model-like value for the common single-``Base`` case, or
    any iterable of them. A set is accepted and sorted, so it is a legitimate way to
    pass models that carry no order of their own.
    """

__all__: Final[tuple[str, ...]] = (
    "create_all",
    "drop_all",
    "metadata_for",
    "normalize_models",
)
"""The four runtime calls this module publishes, sorted.

``ModelLike`` and ``ModelSpec`` are deliberately absent. Both are unions over SQLAlchemy
types, so they are bound only under ``TYPE_CHECKING``; listing them here would make
``from foundry.db.schema import *`` raise ``AttributeError`` at runtime, and binding them
for real would put a SQLAlchemy import on this module's import cost and take the plugin
back over its 150 ms startup budget (AAP 0.13.1). A consumer that needs one of the
annotations imports it under ``if TYPE_CHECKING:``, which is what
:mod:`foundry.db.session` does.
"""

_NOT_A_MODEL_HINT: Final[str] = (
    "pass declarative model classes (DeclarativeBase subclasses) or Table objects"
)
"""The hint attached to every rejected value.

A caller who reaches this message has almost always passed a mapped *instance* -- the
object a query returned -- rather than the class itself, so the hint names the two forms
that are accepted instead of repeating what the message already says.
"""


# --- Public API --------------------------------------------------------------


def normalize_models(models: ModelSpec) -> tuple[ModelLike, ...]:
    """Return the model-like values ``models`` names, as a tuple.

    This is the single entry point the rest of the module goes through, so the accepted
    vocabulary is stated once: a declarative model class, a
    :class:`~sqlalchemy.Table`, an iterable of either, or ``None`` for "no models at
    all". A single value is accepted as a convenience because the most common argument
    is one class from one ``Base``.

    A ``set`` or ``frozenset`` argument is sorted before use, by
    ``(__module__, __qualname__)`` for a class and by ``(metadata.schema or "",
    fullname)`` for a table. A set's iteration order varies between processes -- it
    follows string hash randomisation, which ``PYTHONHASHSEED`` controls -- and two xdist
    workers that created the same models in different orders would be building two
    different databases from one declaration. The order is therefore pinned here rather
    than inherited from the interpreter, which is what AAP 0.13.1 asks of anything that
    reaches a schema decision. Any other iterable keeps the caller's order, because a
    list is already an order the caller chose.

    Args:
        models: ``None``, one model-like value, or an iterable of them.

    Returns:
        One tuple holding the model-like values, in a deterministic order.

    Raises:
        FoundryError: If a value is neither a declarative model class nor a
            ``Table``. The message names the offending type and never its value, so a
            rejected secret cannot reach the terminal. A string argument is rejected by
            its characters' type for the same reason: it is iterable, so each character
            is examined and the first one fails the check.
    """
    if models is None:
        return ()
    if _is_model(models):
        return (_as_model(models),)
    if isinstance(models, (set, frozenset)):
        return tuple(sorted((_as_model(member) for member in models), key=_sort_key))
    if isinstance(models, Iterable):
        return tuple(_as_model(member) for member in models)
    raise _not_a_model(models)


def metadata_for(models: ModelSpec) -> tuple[MetaData, ...]:
    """Return the distinct metadata objects ``models`` belongs to, in first-seen order.

    Several declarative classes that share one ``Base`` share one
    :class:`~sqlalchemy.MetaData`, so a caller passing five models from one ``Base``
    names one schema, not five. Creating that schema once per class instead of once per
    metadata would be wasted work at best and an ``OperationalError`` at worst, so the
    metadata objects are de-duplicated by identity -- identity, not equality, because
    :class:`~sqlalchemy.MetaData` does not define a value comparison and two distinct
    instances are two distinct schemas even when their tables have the same names.

    A caller may legitimately pass models from more than one ``Base``; two declarative
    bases in one process build two independent databases on one engine, which is why the
    result is a tuple :func:`create_all` loops over rather than a single value.

    Args:
        models: ``None``, one model-like value, or an iterable of them.

    Returns:
        One entry per distinct metadata object, in the order the models first name it.

    Raises:
        FoundryError: If ``models`` holds a value that is not a declarative model class
            or a ``Table``, or if a mapped class is not backed by a ``Table``.
    """
    distinct: list[MetaData] = []
    for model in normalize_models(models):
        metadata = _metadata_of(model)
        if not any(existing is metadata for existing in distinct):
            distinct.append(metadata)
    return tuple(distinct)


def create_all(engine: Engine, models: ModelSpec = None) -> None:
    """Create every table of every model's metadata on ``engine``.

    One :meth:`~sqlalchemy.MetaData.create_all` call runs per distinct metadata, in the
    order :func:`metadata_for` returns them, which is the order the caller listed their
    models in. ``create_all`` already orders the tables inside one metadata by
    foreign-key dependency, so the only ordering this module has to pin is the one
    *across* metadata objects.

    ``checkfirst=False`` is deliberate. This helper exists for the ``foundry_db``
    fixture, whose engine is a fresh in-memory sqlite database (see
    :mod:`foundry.db.engine`), so its tables cannot already exist and
    ``checkfirst=True`` would only add a reflection query per table to a suite that has
    a 150 ms startup budget and a six-minute wall-clock ceiling. The flag is safe
    *because* that database is fresh: a caller who reuses this helper against a
    long-lived engine with pre-existing tables gets an error from the dialect rather
    than a silent no-op, which is the failure mode to want.

    Args:
        engine: The engine to create the schema on.
        models: ``None``, one model-like value, or an iterable of them.

    Raises:
        FoundryError: If ``models`` holds a value that is not a declarative model class
            or a ``Table``, or if a mapped class is not backed by a ``Table``.
    """
    for metadata in metadata_for(models):
        metadata.create_all(bind=engine, checkfirst=False)


def drop_all(engine: Engine, models: ModelSpec = None) -> None:
    """Drop every table of every model's metadata from ``engine``.

    The metadata objects are visited in the reverse of the order :func:`metadata_for`
    returns them, so a schema created after another is removed before it and a foreign
    key that crosses two metadata objects cannot block the drop. ``checkfirst=False``
    for the reason :func:`create_all` documents: the engines this helper is written for
    hold only the schema Foundry itself created, so the reflection query would find
    nothing and cost time.

    This is the explicit teardown helper for a caller that needs a clean schema inside a
    live engine -- a test class that reuses one engine across its methods, for instance.
    The ``foundry_db`` fixture does not call it: disposing the engine destroys an
    in-memory sqlite database outright, which :mod:`foundry.db.session` relies on.

    Args:
        engine: The engine to drop the schema from.
        models: ``None``, one model-like value, or an iterable of them.

    Raises:
        FoundryError: If ``models`` holds a value that is not a declarative model class
            or a ``Table``, or if a mapped class is not backed by a ``Table``.
    """
    for metadata in reversed(metadata_for(models)):
        metadata.drop_all(bind=engine, checkfirst=False)


# --- Private helpers ---------------------------------------------------------


def _not_a_model(value: object) -> FoundryError:
    """Build the error a rejected value is raised with.

    Returning the error instead of raising it keeps every ``raise`` in the functions
    that validate, and composes the text once for both the element-wise rejection in
    :func:`_as_model` and the whole-argument rejection at the end of
    :func:`normalize_models`.

    Args:
        value: The value that is neither a declarative model class nor a ``Table``.

    Returns:
        A :class:`~foundry.core.exceptions.FoundryError` naming the value's type.
    """
    return FoundryError(
        f"not a SQLAlchemy model or table: {type(value).__qualname__}",
        hint=_NOT_A_MODEL_HINT,
    )


def _is_model_class(value: object) -> TypeGuard[type[DeclarativeBase]]:
    """Report whether ``value`` is a declaratively mapped class.

    A class qualifies by carrying ``__table__``, which SQLAlchemy attaches to every
    declaratively mapped class and to nothing else a caller is likely to pass. The test
    is structural on purpose: requiring a ``DeclarativeBase`` subclass would reject a
    class produced by the legacy ``declarative_base()`` factory, which is a mapped class
    with a table like any other.

    The guard form lets a caller narrow ``value`` to the class side of
    :data:`ModelLike` without a ``cast``, so the module contains none -- a cast here
    would be asserting to the type checker something the runtime check has already
    proven.

    Args:
        value: The candidate to inspect.

    Returns:
        ``True`` when ``value`` is a class carrying ``__table__``.
    """
    return isinstance(value, type) and hasattr(value, "__table__")


def _is_model(value: object) -> bool:
    """Report whether ``value`` is a single model-like value, not a collection of them.

    Kept separate from :func:`_as_model` because :func:`normalize_models` needs the
    answer before it decides whether to iterate: a mapped class is not iterable, but
    nothing about the annotation tells a reader that a ``Table`` is not either.

    Args:
        value: The candidate to inspect.

    Returns:
        ``True`` for a ``Table`` or a mapped class, ``False`` for anything else.
    """
    from sqlalchemy import Table

    return isinstance(value, Table) or _is_model_class(value)


def _as_model(value: object) -> ModelLike:
    """Narrow one candidate to a model-like value, or raise.

    Args:
        value: The candidate, normally one element of the caller's iterable.

    Returns:
        The value, typed as :data:`ModelLike`.

    Raises:
        FoundryError: If the value is neither a declarative model class nor a
            ``Table``.
    """
    from sqlalchemy import Table

    if isinstance(value, Table):
        return value
    if _is_model_class(value):
        return value
    raise _not_a_model(value)


def _sort_key(model: ModelLike) -> tuple[str, str]:
    """Return the key a set of models is sorted by.

    A class sorts by its defining module and qualified name; a table sorts by its schema
    (the empty string when it has none, so the key is always two strings) and its full
    name. Both keys come from the model's declaration rather than from its position in
    the argument, which is what makes the order identical in two processes even though
    Python's string hashing differs between them.

    Args:
        model: A model-like value.

    Returns:
        Two strings whose lexicographic order is stable across processes.
    """
    from sqlalchemy import Table

    if isinstance(model, Table):
        return (model.metadata.schema or "", model.fullname)
    return (model.__module__, model.__qualname__)


def _metadata_of(model: ModelLike) -> MetaData:
    """Return the metadata that owns ``model``'s table.

    A ``Table`` owns its metadata directly; a declarative class reaches it through the
    ``__table__`` SQLAlchemy attaches at mapping time. That attribute is typed as a
    generic :class:`~sqlalchemy.sql.FromClause`, which carries no metadata of its own,
    so the value is narrowed to a ``Table`` before its metadata is read rather than cast
    into one.

    Args:
        model: A model-like value.

    Returns:
        The metadata the model's table belongs to.

    Raises:
        FoundryError: If a mapped class carries a ``__table__`` that is not a ``Table``
            -- a class mapped onto a join or a subquery. Foundry cannot derive a schema
            from such a mapping, and naming the class and the construct is more useful
            than the attribute error SQLAlchemy would raise later.
    """
    from sqlalchemy import Table

    if isinstance(model, Table):
        return model.metadata
    table = model.__table__
    if isinstance(table, Table):
        return table.metadata
    raise FoundryError(
        f"no schema metadata for model {model.__qualname__}: "
        f"its __table__ is a {type(table).__qualname__}",
        hint="map the declarative class to a Table so foundry_db can create its schema",
    )
