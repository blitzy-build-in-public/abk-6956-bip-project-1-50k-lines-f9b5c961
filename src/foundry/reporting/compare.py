"""The result-set comparator behind ``foundry report compare``.

:func:`diff` reduces two result documents to the sorted newly-failing, newly-passing and
still-failing node-id sets; a node id is failing exactly when its outcome is in
``FAILING_OUTCOMES``. AAP 0.4.4, 0.4.6, 0.4.7, 0.7.3, 0.7.4, 0.13.1.
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

    Every field is a sorted, de-duplicated tuple of node ids, canonicalised on construction, so no
    set reaches a document or a rendered page.

    Attributes:
        newly_failing: Node ids failing in the newer document only -- the regressions, and the only
            field that changes the command's exit code.
        newly_passing: Node ids failing in the baseline that the newer document no longer fails.
        still_failing: Node ids failing in both documents.
    """

    newly_failing: tuple[str, ...] = ()
    newly_passing: tuple[str, ...] = ()
    still_failing: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Canonicalise every field to a sorted, de-duplicated tuple (AAP 0.13.1).

        ``dict.fromkeys`` de-duplicates without a set being iterated on the way to an output.
        """
        object.__setattr__(self, "newly_failing", _canonical(self.newly_failing))
        object.__setattr__(self, "newly_passing", _canonical(self.newly_passing))
        object.__setattr__(self, "still_failing", _canonical(self.still_failing))

    def as_dict(self) -> dict[str, list[str]]:
        """Return the diff as the document ``foundry report compare --json`` prints.

        Returns:
            The three keys AAP 0.7.3 fixes, as ascending node-id lists because ``json.dumps``
            serialises a list and rejects nothing else.
        """
        return {
            "newly_failing": list(self.newly_failing),
            "newly_passing": list(self.newly_passing),
            "still_failing": list(self.still_failing),
        }

    @property
    def has_new_failures(self) -> bool:
        """Return whether the newer run regressed: the predicate the command maps to exit ``1``.

        A property, like :attr:`foundry.reporting.record.ResultRecord.is_failing`, so it reads as
        the state it reports. ``True`` when :attr:`newly_failing` is non-empty.
        """
        return bool(self.newly_failing)

    @property
    def is_empty(self) -> bool:
        """Return whether neither document reports a failing test.

        The command prints one "no differences" line for this state rather than three empty
        groups. ``True`` when all three tuples are empty.
        """
        return not (self.newly_failing or self.newly_passing or self.still_failing)

    def counts(self) -> dict[str, int]:
        """Return each set's size, keyed as :meth:`as_dict` keys them.

        The terminal renderer labels its three groups with these numbers.
        """
        return {
            "newly_failing": len(self.newly_failing),
            "newly_passing": len(self.newly_passing),
            "still_failing": len(self.still_failing),
        }


# --- The two entry points --------------------------------------------------


def diff(base: Sequence[ResultElement], new: Sequence[ResultElement]) -> ComparisonDiff:
    """Compare two result documents and return the three node-id sets.

    Pure and total: it reads, writes and logs nothing and leaves both arguments untouched, and
    shifting the order of either sequence changes nothing, because each side is reduced to a set
    of node ids before the difference is taken.

    Args:
        base: The baseline run's elements: result records, run errors, or both in any order.
        new: The newer run's elements, in the same shape.

    Returns:
        The regressions, the recoveries and the standing failures, each a sorted, de-duplicated
        tuple of node ids.
    """
    base_failing = _failing_nodeids(base)
    new_failing = _failing_nodeids(new)
    return ComparisonDiff(
        newly_failing=_canonical(new_failing - base_failing),
        newly_passing=_canonical(base_failing - new_failing),
        still_failing=_canonical(base_failing & new_failing),
    )


def diff_files(base: str | Path, new: str | Path) -> ComparisonDiff:
    """Compare two ``results.json`` documents on disk, each through :func:`diff`.

    Returns:
        The same three sets :func:`diff` returns for the two decoded documents.

    Raises:
        foundry.core.exceptions.DataError: A path is missing, is a directory, cannot be decoded as
            UTF-8, or does not hold a Foundry result document. The message and the ``file``
            attribute name the offending path, which the command line reports before exiting ``2``
            (AAP 0.4.4). It is deliberately not caught here.
    """
    return diff(read(base), read(new))


# --- Reducing one document to its failing node ids -------------------------


def _failing_nodeids(elements: Sequence[ResultElement]) -> frozenset[str]:
    """Return the node ids of the elements whose outcome counts against their run.

    The set never leaves this module: :class:`ComparisonDiff` turns differences of two into sorted
    tuples. A repeated node id resolves last-wins.
    """
    index = _index_by_nodeid(elements)
    return frozenset(nodeid for nodeid, record in index.items() if _is_failing(record.outcome))


def _index_by_nodeid(elements: Sequence[ResultElement]) -> dict[str, ResultRecord]:
    """Map every result record's node id to the record, skipping the run-error elements.

    A run error describes the run rather than a test, so it carries neither a node id nor an
    outcome and can say nothing about which test regressed (AAP 0.4.6). A repeated node id is a
    defensive case rather than a supported input, resolved last-wins so the answer stays a function
    of the document's own order.
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
    cannot come to different conclusions. The quarantine outcomes stay outside it, so counting one
    here would report a regression the run itself did not see (AAP 0.4.7).
    """
    return outcome in FAILING_OUTCOMES


def _canonical(nodeids: Iterable[str]) -> tuple[str, ...]:
    """Return ``nodeids`` as a sorted, de-duplicated tuple of strings.

    Returns:
        The node ids in ascending order with repeats removed.
    """
    return tuple(sorted(dict.fromkeys(nodeids)))
