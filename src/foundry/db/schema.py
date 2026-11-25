"""Schema helpers behind the ``foundry_db`` fixture.

Foundry ships no ORM model of its own -- AAP 0.7.2 defines Foundry's own data as pydantic and
dataclass records, none of which is a table -- so every schema this package creates is one the
*test* supplies. This module turns "the models the test registers" into a created sqlite schema and
back again, and it is the only place that decides which metadata objects are created and in which
order (AAP 0.3.2, AAP 0.7.1, AAP 0.4.3). It assumes SQLAlchemy 2.0 declarative mapping -- a
:class:`~sqlalchemy.orm.DeclarativeBase` subclass with ``Mapped[...]`` columns -- and accepts a
plain :class:`~sqlalchemy.Table` too, calling either a "model-like" value. Two rules shape it.
Models travel as a value, never as module state: there is no global ``Base``, no registry and no
cache, since a shared registry would leak one test's schema into the next and the layering contract
(AAP 0.4.8) permits import-time mutation only in the two registration tables of
:mod:`foundry.data.registry` and :mod:`foundry.reporting.renderers`. SQLAlchemy is imported lazily,
because the plugin has a 150 ms startup budget (implicit requirement I5): every annotation name
comes from ``TYPE_CHECKING`` and every runtime name from inside the function that needs it. It
imports :mod:`foundry.core.exceptions` and nothing else of Foundry (AAP 0.4.8 places ``foundry.db``
on the foundation rung), defines no exception type of its own and raises nothing but
:class:`~foundry.core.exceptions.FoundryError` (AAP 0.14.3). Importing it does no work at all.
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

    An annotation-only alias, which is why it lives under ``TYPE_CHECKING``: naming it at runtime
    would bind a :class:`~sqlalchemy.Table` import into the module's import cost. Code never
    inspects it -- there is no ``isinstance(x, ModelLike)`` anywhere, since a union of a class type
    and a metaclass-built instance has no meaningful runtime check -- and :func:`_as_model` performs
    the two tests separately.
    """

    ModelSpec: TypeAlias = ModelLike | Iterable[ModelLike] | None
    """Everything a caller may pass where a set of models is expected.

    ``None`` for no models, one model-like value for the common single-``Base`` case, or any
    iterable of them; a set is accepted and sorted, which is a legitimate way to pass models that
    carry no order of their own.
    """

__all__: Final[tuple[str, ...]] = (
    "create_all",
    "drop_all",
    "metadata_for",
    "normalize_models",
)
"""The four runtime calls this module publishes, sorted.

``ModelLike`` and ``ModelSpec`` are deliberately absent: both are unions over SQLAlchemy types bound
only under ``TYPE_CHECKING``, so listing them would make ``from foundry.db.schema import *`` raise
``AttributeError`` at runtime, and binding them for real would put a SQLAlchemy import on this
module's import cost (AAP 0.13.1). A consumer imports one under ``if TYPE_CHECKING:``, as
:mod:`foundry.db.session` does.
"""

_NOT_A_MODEL_HINT: Final[str] = (
    "pass declarative model classes (DeclarativeBase subclasses) or Table objects"
)
"""The hint attached to every rejected value.

A caller who reaches this message has almost always passed a mapped *instance* -- the object a query
returned -- rather than the class itself, so the hint names the two forms that are accepted.
"""


# --- Public API --------------------------------------------------------------


def normalize_models(models: ModelSpec) -> tuple[ModelLike, ...]:
    """Return the model-like values ``models`` names, as a tuple.

    The single entry point the rest of the module goes through, so the accepted vocabulary is stated
    once: a declarative model class, a :class:`~sqlalchemy.Table`, an iterable of either, or
    ``None`` for "no models at all"; one value is accepted because the most common argument is one
    class from one ``Base``. A ``set`` or ``frozenset`` is sorted before use, by ``(__module__,
    __qualname__)`` for a class and by ``(metadata.schema or "", fullname)`` for a table: a set's
    iteration order follows string hash randomisation, which ``PYTHONHASHSEED`` controls, so two
    xdist workers could otherwise build two different databases from one declaration. Any other
    iterable keeps the caller's order, because a list is already an order the caller chose.

    Raises:
        FoundryError: If a value is neither a declarative model class nor a ``Table``. The message
            names the offending type and never its value, so a rejected secret cannot reach the
            terminal; a string argument is rejected by its characters' type for the same reason.
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

    Several declarative classes sharing one ``Base`` share one :class:`~sqlalchemy.MetaData`, so
    five models from one ``Base`` name one schema, not five; creating that schema once per class
    would be wasted work at best and an ``OperationalError`` at worst. Metadata is de-duplicated by
    identity, because :class:`~sqlalchemy.MetaData` defines no value comparison and two distinct
    instances are two distinct schemas even when their tables share names. A caller may pass models
    from more than one ``Base`` -- two declarative bases build two independent databases on one
    engine -- which is why the result is a tuple :func:`create_all` loops over.

    Raises:
        FoundryError: If ``models`` holds a value that is not a declarative model class or a
            ``Table``, or if a mapped class is not backed by a ``Table``.
    """
    distinct: list[MetaData] = []
    for model in normalize_models(models):
        metadata = _metadata_of(model)
        if not any(existing is metadata for existing in distinct):
            distinct.append(metadata)
    return tuple(distinct)


def create_all(engine: Engine, models: ModelSpec = None) -> None:
    """Create every table of every model's metadata on ``engine``.

    One :meth:`~sqlalchemy.MetaData.create_all` call runs per distinct metadata, in the order
    :func:`metadata_for` returns them, which is the order the caller listed their models in;
    ``create_all`` already orders the tables inside one metadata by foreign-key dependency, so the
    only ordering pinned here is the one *across* metadata objects. ``checkfirst=False`` is
    deliberate: this helper exists for the ``foundry_db`` fixture, whose engine is a fresh in-memory
    sqlite database (see :mod:`foundry.db.engine`), so its tables cannot already exist and the flag
    would only add a reflection query per table to a suite with a 150 ms startup budget and a
    six-minute wall-clock ceiling. It is safe *because* that database is fresh: a caller who reuses
    this helper against a long-lived engine with pre-existing tables gets an error from the dialect
    rather than a silent no-op, which is the failure mode to want.

    Raises:
        FoundryError: If ``models`` holds a value that is not a declarative model class or a
            ``Table``, or if a mapped class is not backed by a ``Table``.
    """
    for metadata in metadata_for(models):
        metadata.create_all(bind=engine, checkfirst=False)


def drop_all(engine: Engine, models: ModelSpec = None) -> None:
    """Drop every table of every model's metadata from ``engine``.

    The metadata objects are visited in the reverse of the order :func:`metadata_for` returns them,
    so a schema created after another is removed before it and a foreign key that crosses two
    metadata objects cannot block the drop. ``checkfirst=False`` for the reason :func:`create_all`
    documents: the engines this helper is written for hold only the schema Foundry itself created.
    It is the explicit teardown helper for a caller that needs a clean schema inside a live engine
    -- a test class reusing one engine across its methods, for instance. The ``foundry_db`` fixture
    does not call it: disposing the engine destroys an in-memory sqlite database outright, which
    :mod:`foundry.db.session` relies on.

    Raises:
        FoundryError: If ``models`` holds a value that is not a declarative model class or a
            ``Table``, or if a mapped class is not backed by a ``Table``.
    """
    for metadata in reversed(metadata_for(models)):
        metadata.drop_all(bind=engine, checkfirst=False)


# --- Private helpers ---------------------------------------------------------


def _not_a_model(value: object) -> FoundryError:
    """Build the error a rejected value is raised with.

    Returning the error instead of raising it keeps every ``raise`` in the functions that validate,
    and composes the text once for the element-wise rejection in :func:`_as_model` and the
    whole-argument rejection at the end of :func:`normalize_models`.
    """
    return FoundryError(
        f"not a SQLAlchemy model or table: {type(value).__qualname__}",
        hint=_NOT_A_MODEL_HINT,
    )


def _is_model_class(value: object) -> TypeGuard[type[DeclarativeBase]]:
    """Report whether ``value`` is a declaratively mapped class.

    A class qualifies by carrying ``__table__``, which SQLAlchemy attaches to every declaratively
    mapped class and to nothing else a caller is likely to pass. The test is structural on purpose:
    requiring a ``DeclarativeBase`` subclass would reject a class produced by the legacy
    ``declarative_base()`` factory, which is a mapped class with a table like any other. The guard
    form lets a caller narrow ``value`` to the class side of :data:`ModelLike` without a ``cast``.
    """
    return isinstance(value, type) and hasattr(value, "__table__")


def _is_model(value: object) -> bool:
    """Report whether ``value`` is a single model-like value, not a collection of them.

    Kept separate from :func:`_as_model` because :func:`normalize_models` needs the answer before it
    decides whether to iterate: a mapped class is not iterable, but nothing about the annotation
    tells a reader that a ``Table`` is not either.
    """
    from sqlalchemy import Table

    return isinstance(value, Table) or _is_model_class(value)


def _as_model(value: object) -> ModelLike:
    """Narrow one candidate to a model-like value, or raise.

    Raises:
        FoundryError: If the value is neither a declarative model class nor a ``Table``.
    """
    from sqlalchemy import Table

    if isinstance(value, Table):
        return value
    if _is_model_class(value):
        return value
    raise _not_a_model(value)


def _sort_key(model: ModelLike) -> tuple[str, str]:
    """Return the key a set of models is sorted by.

    A class sorts by its defining module and qualified name; a table by its schema (the empty string
    when it has none, so the key is always two strings) and its full name. Both keys come from the
    model's declaration rather than from its argument position, which makes the order identical in
    two processes even though Python's string hashing differs between them.
    """
    from sqlalchemy import Table

    if isinstance(model, Table):
        return (model.metadata.schema or "", model.fullname)
    return (model.__module__, model.__qualname__)


def _metadata_of(model: ModelLike) -> MetaData:
    """Return the metadata that owns ``model``'s table.

    A ``Table`` owns its metadata directly; a declarative class reaches it through the ``__table__``
    SQLAlchemy attaches at mapping time. That attribute is typed as a generic
    :class:`~sqlalchemy.sql.FromClause`, which carries no metadata of its own, so the value is
    narrowed to a ``Table`` before its metadata is read rather than cast into one.

    Raises:
        FoundryError: If a mapped class carries a ``__table__`` that is not a ``Table`` -- a class
            mapped onto a join or a subquery, from which no schema can be derived; naming the class
            and the construct is more useful than the attribute error SQLAlchemy would raise later.
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
