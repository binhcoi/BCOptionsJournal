"""Roll chains.

A chain is the lineage of a position through rolls and splits. The legacy sheet
carried this by hand in its ``Cost basis`` column; here it is derived.

    carry(p) = realized_pl(predecessor) + carry(predecessor)

Chains matter because per-leg P/L alone misleads on a roll: the leg being
closed is usually a loss even when the position is net ahead. A chain of a
dozen or more legs spanning years is normal, and only the chain total answers
"am I still up on this, and how long have I been in it".
"""

from dataclasses import dataclass
from decimal import Decimal

from ..domain.money import ZERO, q2
from ..domain.types import Position
from .pnl import open_cash, realized_pl

# A malformed sheet could contain a cycle; refuse to loop forever.
MAX_CHAIN_DEPTH = 500


class ChainCycleError(ValueError):
    """A position is its own ancestor."""


def inherit(inherited: Decimal, leg: Position) -> Decimal:
    """One step of carry: what ``leg`` takes from all the history above it.

    A roll inherits the whole thing -- the position continues, so it owes
    everything its predecessors accumulated. A split inherits a *share*,
    pro-rata by quantity. Duplicating the full history onto both halves of a
    split would double-count it and, worse, make break-even nonsense by
    spreading a whole chain's loss across a fraction of the contracts.

    This is the one place that rule is expressed. Both ``Chain`` and
    ``ChainIndex`` go through it, so they cannot drift apart.
    """
    ratio = leg.split_ratio
    return q2(inherited * ratio) if ratio is not None else q2(inherited)


def carry_along(legs) -> Decimal:
    """Chain P/L carried into the last of an ordered lineage.

        carry(first)  = 0
        carry(leg[i]) = inherit(realized(leg[i-1]) + carry(leg[i-1]), leg[i])
    """
    carry = ZERO
    for previous, leg in zip(legs, legs[1:]):
        carry = inherit(realized_pl(previous) + carry, leg)
    return carry


@dataclass
class Chain:
    """A lineage, oldest leg first."""

    legs: tuple[Position, ...]

    @property
    def head(self) -> Position:
        """The current (most recent) leg."""
        return self.legs[-1]

    @property
    def root(self) -> Position:
        return self.legs[0]

    @property
    def underlying(self) -> str:
        return self.head.underlying

    @property
    def leg_count(self) -> int:
        return len(self.legs)

    @property
    def is_open(self) -> bool:
        return self.head.is_open

    @property
    def realized(self) -> Decimal:
        """Total realized by this chain: what it carried, plus the head's own.

        Not a plain sum of legs, because a split half owns only its share of
        the history above it.
        """
        return q2(self.carry + realized_pl(self.head))

    @property
    def carry(self) -> Decimal:
        """Realized total carried into the head. The sheet's Cost basis."""
        return carry_along(self.legs)

    @property
    def net_credit(self) -> Decimal:
        """Credit the chain has to show for itself. Base for target and break-even.

        While the head is open, no closing fee has been paid, so the figure is
        the head's opening cash plus carry.

        Once the head has closed the actual outcome is known and includes its
        closing fee, so the realized total is the honest number. Using opening
        cash there overstates the credit by that fee, which shows up as a
        break-even a cent too favourable.
        """
        if self.head.is_open:
            return q2(open_cash(self.head) + self.carry)
        return self.realized

    @property
    def opened_on(self):
        return self.root.opened_on

    @property
    def closed_on(self):
        return self.head.closed_on

    @property
    def days(self) -> int | None:
        """Duration of the whole chain, not of one leg."""
        end = self.head.closed_on
        if end is None:
            return None
        return (end - self.root.opened_on).days


class ChainIndex:
    """Lineage lookups over a set of positions."""

    def __init__(self, positions):
        self._by_id: dict[str, Position] = {}
        for position in positions:
            if position.id in self._by_id:
                raise ValueError(f"duplicate position id {position.id!r}")
            self._by_id[position.id] = position

        self._successors: dict[str, list[str]] = {}
        for position in self._by_id.values():
            parent = self.parent_id(position)
            if parent is not None:
                self._successors.setdefault(parent, []).append(position.id)

        self._carry_cache: dict[str, Decimal] = {}

    @staticmethod
    def parent_id(position: Position) -> str | None:
        """A position descends from either a roll or a split, never both."""
        return position.rolled_from_id or position.split_from_id

    def get(self, position_id: str) -> Position | None:
        return self._by_id.get(position_id)

    def lineage(self, position: Position) -> tuple[Position, ...]:
        """Ancestors then the position itself, oldest first.

        A parent id that isn't present (a chain whose earlier legs live outside
        the imported range) simply truncates the lineage.
        """
        legs: list[Position] = []
        seen: set[str] = set()
        current: Position | None = position
        while current is not None:
            if current.id in seen:
                raise ChainCycleError(
                    f"{position.id}: roll chain cycles at {current.id}"
                )
            seen.add(current.id)
            legs.append(current)
            if len(legs) > MAX_CHAIN_DEPTH:
                raise ChainCycleError(f"{position.id}: chain exceeds depth limit")
            parent = self.parent_id(current)
            current = self._by_id.get(parent) if parent else None
        legs.reverse()
        return tuple(legs)

    def chain(self, position: Position) -> Chain:
        return Chain(legs=self.lineage(position))

    def carry(self, position: Position) -> Decimal:
        """Chain P/L brought forward into this position. See ``carry_along``.

        Memoized, because reconciliation asks for it once per row.
        """
        cached = self._carry_cache.get(position.id)
        if cached is not None:
            return cached

        parent_id = self.parent_id(position)
        parent = self._by_id.get(parent_id) if parent_id else None
        if parent is None:
            value = ZERO
        else:
            value = inherit(realized_pl(parent) + self.carry(parent), position)
        self._carry_cache[position.id] = value
        return value

    def successors(self, position: Position) -> tuple[Position, ...]:
        ids = self._successors.get(position.id, ())
        return tuple(self._by_id[i] for i in ids)

    def is_head(self, position: Position) -> bool:
        """True when nothing rolls out of this position."""
        return not self._successors.get(position.id)

    def heads(self) -> tuple[Position, ...]:
        """The final leg of every chain."""
        return tuple(p for p in self._by_id.values() if self.is_head(p))

    def chains(self) -> tuple[Chain, ...]:
        return tuple(self.chain(head) for head in self.heads())
