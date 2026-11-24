"""The result-set comparator behind ``foundry report compare``.

Two ``results.json`` documents describe two runs of the same suite, and the question a person
asks of them is which tests started failing, which stopped failing and which were failing in
both. :func:`diff` answers it from the two documents alone -- no live run state, no configuration
and no clock -- by reducing each to the node ids whose outcome counts against its run and taking
the three set differences. :class:`ComparisonDiff` carries the answer as three sorted node-id
tuples: the sets AAP 0.4.4 has the command print, AAP 0.7.3 has its ``--json`` document carry and
AAP 0.7.4 has the terminal render as Newly failing, Newly passing and Still failing in that
order. :func:`diff_files` is the same comparison over two paths, each read through
:func:`foundry.reporting.store.read`.

The comparison rule
    A node id is **failing** in a document when its outcome is in
    :data:`foundry.reporting.record.FAILING_OUTCOMES` -- ``failed`` or ``error`` -- read from
    there rather than restated, because that is the one definition the JUnit writer and the run's
    exit code share. Everything else the outcome vocabulary holds is not a failure here:

    * ``quarantined_failed`` and ``quarantine_passed`` are excluded from all three sets.
      Quarantine exists so that a known-flaky failure does not fail the build, and ``report`` mode
      keeps the run at exit ``0`` (AAP 0.4.7): reporting one as a regression would contradict the
      exit code of the run that produced it. A person still sees them, in the report's own
      Quarantined section.
    * ``skipped``, ``xfailed``, ``xpassed``, ``passed`` and ``not_run`` are not failures.
      ``not_run`` is an item a lost worker never executed (assumption A12): nothing was observed
      about it, so it can neither regress nor recover.
    * ``run_error`` elements are skipped entirely (AAP 0.4.6). A run error carries no node id and
      no outcome, so it says nothing about which test regressed; the JUnit writer reports it and
      the report lists it in its Errors section instead.

    A node id only one document records is one the other run did not have. Failing in the newer
    document and absent from the baseline it is ``newly_failing``; failing in the baseline and
    absent from the newer document it is ``newly_passing``, because the defect is no longer
    observed, which is exactly what the set difference reports. A node id failing in neither
    document appears in none of the three sets.

    A document is per run and unique by node id, so listing one node id twice is not a supported
    input; should it happen the last element wins, which keeps the answer a function of the
    document alone.

Determinism
    The comparison is a pure function of its two arguments: no I/O, no logging, no clock and no
    mutation of what it was given. Internally each document reduces to a set and nothing holding a
    set reaches a caller: :class:`ComparisonDiff` canonicalises every field to a sorted,
    de-duplicated tuple on construction, so equal input yields an equal value, the same JSON bytes
    and the same rendered page, and the answer does not depend on the order either document listed
    its elements in (AAP 0.13.1).

Reading and failure
    The only file this module touches is a document it was asked to compare, and it reads it
    through :func:`foundry.reporting.store.read` rather than opening it. That reader raises
    :class:`foundry.core.exceptions.DataError` naming the file for anything that is not a Foundry
    result document, and :func:`diff_files` deliberately lets it through: the command line turns it
    into exit ``2`` (AAP 0.4.4) and a caller that caught it here would have to invent that mapping
    a second time. This module raises nothing of its own, computes no exit code and renders
    nothing; the three sets and one predicate are the whole of its answer.

Import direction and import-time discipline (AAP 0.4.8)
    :mod:`foundry.reporting` imports ``foundry.core``, ``foundry.obs`` and its own modules and no
    capability package; this module needs only :mod:`foundry.reporting.record` and
    :mod:`foundry.reporting.store`. Importing it defines names and nothing else: no I/O, no
    environment read, no logger configuration and no module-level mutable state.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from foundry.reporting.record import FAILING_OUTCOMES, Outcome, ResultElement, ResultRecord
from foundry.reporting.store import read

__all__: Final[tuple[str, ...]] = ("ComparisonDiff", "diff", "diff_files")


# --- The comparison result -------------------------------------------------


@dataclass(frozen=True)
class ComparisonDiff:
    """How a run's failures changed between a baseline document and a newer one.

    Every field is a sorted, de-duplicated tuple of node ids, canonicalised on construction, so a
    value assembled by hand and a value :func:`diff` returned compare equal for equal content and
    no set ever reaches a document or a rendered page.

    Attributes:
        newly_failing: Node ids failing in the newer document that were not failing in the
            baseline -- the regressions, and the only field that changes the command's exit code.
        newly_passing: Node ids failing in the baseline that the newer document no longer reports
            as failing, an id the newer run did not collect included.
        still_failing: Node ids failing in both documents.
    """

    newly_failing: tuple[str, ...] = ()
    newly_passing: tuple[str, ...] = ()
    still_failing: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Canonicalise every field to a sorted, de-duplicated tuple.

        Imposing the invariant here rather than trusting each constructor keeps the determinism
        rule (AAP 0.13.1) a property of the value instead of a property of one function, so a
        diff a caller, a deserialiser or a test assembles sorts the same way. ``dict.fromkeys``
        de-duplicates without a set being iterated on the way to an output.
        """
        object.__setattr__(self, "newly_failing", _canonical(self.newly_failing))
        object.__setattr__(self, "newly_passing", _canonical(self.newly_passing))
        object.__setattr__(self, "still_failing", _canonical(self.still_failing))

    def as_dict(self) -> dict[str, list[str]]:
        """Return the diff as the document ``foundry report compare --json`` prints.

        Returns:
            A mapping with exactly the three keys AAP 0.7.3 fixes, holding lists rather than
            tuples because ``json.dumps`` serialises a list and rejects nothing else; each list's
            node ids are in ascending order.
        """
        return {
            "newly_failing": list(self.newly_failing),
            "newly_passing": list(self.newly_passing),
            "still_failing": list(self.still_failing),
        }

    @property
    def has_new_failures(self) -> bool:
        """Return whether the newer run regressed, the predicate the command maps to exit ``1``.

        A property, like :attr:`foundry.reporting.record.ResultRecord.is_failing`, so a predicate
        over the value reads as the state it reports rather than as an action on it.

        Returns:
            ``True`` when :attr:`newly_failing` holds at least one node id.
        """
        return bool(self.newly_failing)

    @property
    def is_empty(self) -> bool:
        """Return whether neither document reports a failing test.

        The command prints one "no differences" line for this state rather than three empty
        groups, and two green runs are the ordinary case. Two documents that share a failure are
        not this state, because :attr:`still_failing` then names it.

        Returns:
            ``True`` when all three tuples are empty.
        """
        return not (self.newly_failing or self.newly_passing or self.still_failing)

    def counts(self) -> dict[str, int]:
        """Return each set's size, keyed as :meth:`as_dict` keys them.

        The terminal renderer labels its three groups with these numbers, so a group's size is
        computed in one place rather than three.

        Returns:
            A mapping of the three document keys to the length of the set each one names.
        """
        return {
            "newly_failing": len(self.newly_failing),
            "newly_passing": len(self.newly_passing),
            "still_failing": len(self.still_failing),
        }


# --- The two entry points --------------------------------------------------


def diff(base: Sequence[ResultElement], new: Sequence[ResultElement]) -> ComparisonDiff:
    """Compare two result documents and return the three node-id sets.

    Pure and total: it reads nothing, writes nothing, logs nothing and leaves both arguments
    untouched, so it can be called as often as a caller likes -- twice on the same pair of
    documents returns an equal value, and shifting the order of either sequence changes nothing,
    because each side is reduced to a set of node ids before the difference is taken.

    Args:
        base: The baseline run's elements: result records, run errors, or both in any order,
            normally as :func:`foundry.reporting.store.read` returned them.
        new: The newer run's elements, in the same shape.

    Returns:
        The regressions, the recoveries and the standing failures, each as a sorted tuple of node
        ids and de-duplicated.
    """
    base_failing = _failing_nodeids(base)
    new_failing = _failing_nodeids(new)
    return ComparisonDiff(
        newly_failing=_canonical(new_failing - base_failing),
        newly_passing=_canonical(base_failing - new_failing),
        still_failing=_canonical(base_failing & new_failing),
    )


def diff_files(base: str | Path, new: str | Path) -> ComparisonDiff:
    """Compare two ``results.json`` documents on disk.

    A thin reading edge over :func:`diff`: both documents go through
    :func:`foundry.reporting.store.read`, so they are validated by the same reader every other
    consumer of a result document uses and a document this release cannot parse is refused in the
    same words wherever it is opened.

    Args:
        base: Path of the baseline run's ``results.json``.
        new: Path of the newer run's ``results.json``.

    Returns:
        The same three sets :func:`diff` returns for the two decoded documents.

    Raises:
        foundry.core.exceptions.DataError: A path is missing, is a directory, cannot be decoded as
            UTF-8, or does not hold a Foundry result document. Both the message and the ``file``
            attribute name the offending path, which is what the command line reports before
            exiting ``2`` (AAP 0.4.4). It is deliberately not caught here.
    """
    return diff(read(base), read(new))


# --- Reducing one document to its failing node ids -------------------------


def _failing_nodeids(elements: Sequence[ResultElement]) -> frozenset[str]:
    """Return the node ids of the elements whose outcome counts against their run.

    The set is what a caller must not see; :class:`ComparisonDiff` turns the differences of two
    of them into sorted tuples, and this module never hands one out.

    Args:
        elements: One document's elements, in the order the document listed them.

    Returns:
        The node ids failing in that document, as a set, with the last element winning where a
        node id repeats.
    """
    index = _index_by_nodeid(elements)
    return frozenset(nodeid for nodeid, record in index.items() if _is_failing(record.outcome))


def _index_by_nodeid(elements: Sequence[ResultElement]) -> dict[str, ResultRecord]:
    """Map every result record's node id to the record, skipping the run-error elements.

    A run error describes the run rather than a test, so it carries neither a node id nor an
    outcome and can say nothing about which test regressed (AAP 0.4.6). A repeated node id is
    resolved last-wins: a document is per run and unique by node id, so this is a defensive rule
    rather than a supported input, and reading it that way keeps the answer a function of the
    document's own order and nothing else.

    Args:
        elements: One document's elements, in the order the document listed them.

    Returns:
        The document's result records, keyed by node id.
    """
    index: dict[str, ResultRecord] = {}
    for element in elements:
        if isinstance(element, ResultRecord):
            index[element.nodeid] = element
    return index


def _is_failing(outcome: Outcome) -> bool:
    """Return whether ``outcome`` counts against the run in a comparison.

    The rule is :data:`foundry.reporting.record.FAILING_OUTCOMES` -- ``failed`` and ``error`` --
    read from the record module rather than restated, so the comparator and the run's exit code
    cannot come to different conclusions about which run failed. The quarantine outcomes stay
    outside it by that definition: ``report`` mode keeps a quarantined failure from failing the
    run (AAP 0.4.7), so counting one here would report a regression the run itself did not see.

    Args:
        outcome: The outcome a result record carries.

    Returns:
        ``True`` for ``failed`` and ``error``, ``False`` for the other seven values.
    """
    return outcome in FAILING_OUTCOMES


def _canonical(nodeids: Iterable[str]) -> tuple[str, ...]:
    """Return ``nodeids`` as a sorted, de-duplicated tuple of strings.

    Args:
        nodeids: The node ids to canonicalise, as any iterable -- a set difference, a frozenset or
            a sequence a caller supplied.

    Returns:
        The node ids in ascending order with repeats removed.
    """
    return tuple(sorted(dict.fromkeys(nodeids)))
