"""Wheels: what a ticker's shares actually made, options and stock together.

Shares arrive by assignment, by buy-write or by outright purchase. Options get
written around them. Eventually the shares are called away or sold. The sheet
could record each of those as a row and could total none of them, which is why
the share side of a wheel was invisible (plan.md §3.1).

The unit is the ticker, not the lot. Covered calls are covered by whatever the
ticker holds, assignment delivers shares by the account's matching rule, and
a put that expired worthless paid for the shares just as surely as one that
assigned -- so option P/L is one lifetime figure per ticker and is never
apportioned to lots. Everything here is derived from positions, lots and
disposals. Nothing is stored, so a corrected rule fixes every history at once.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from ..domain.enums import Direction, MatchingRule, Right, Status
from ..domain.money import ZERO, q2
from ..domain.types import Position, ShareLot
from .basis import AdjustedBasis, adjusted_basis
from .chains import ChainIndex
from .pnl import realized_pl
from .shares import InsufficientSharesError, match


@dataclass
class LotView:
    """One share lot: what is left of it, what its sold shares made, and how
    much of it the open calls speak for."""

    lot: ShareLot
    remaining: int
    open_calls: tuple[Position, ...]
    share_realized: Decimal        # P/L on the shares disposed from this lot
    disposed: int
    last_disposed_on: date | None
    covered_shares: int = 0        # shares of this lot spoken for by open calls

    @property
    def is_open(self) -> bool:
        return self.remaining > 0

    @property
    def covered(self) -> int:
        """Shares currently spoken for by open calls."""
        return self.covered_shares

    @property
    def uncovered(self) -> int:
        return max(self.remaining - self.covered, 0)

    @property
    def over_covered(self) -> int:
        """Calls written against more shares than the lot still holds."""
        return max(self.covered - self.remaining, 0)

    @property
    def days(self) -> int:
        end = self.last_disposed_on if not self.is_open else date.today()
        return ((end or date.today()) - self.lot.acquired_on).days


@dataclass
class TickerShares:
    """Everything about one underlying's stock."""

    underlying: str
    lots: tuple[LotView, ...]
    acquired: int
    disposed: int
    realized: Decimal          # share P/L across all lots
    held_cost: Decimal         # what is still held, at what was paid
    error: str = ""            # matching refused: an acquisition is missing
    option_pl: Decimal = ZERO  # every closed option leg on the ticker, lifetime
    open_call_shares: int = 0  # shares the open short calls control

    @property
    def held(self) -> int:
        return self.acquired - self.disposed

    @property
    def balanced(self) -> bool:
        return self.held >= 0 and not self.error

    @property
    def open_lots(self) -> tuple[LotView, ...]:
        return tuple(v for v in self.lots if v.is_open)

    @property
    def covered_shares(self) -> int:
        return min(max(self.held, 0), self.open_call_shares)

    @property
    def uncovered_shares(self) -> int:
        """Shares the open calls control beyond what is held: naked."""
        return max(self.open_call_shares - max(self.held, 0), 0)

    @property
    def basis(self) -> AdjustedBasis | None:
        """What the shares held really cost after everything the ticker has
        paid back. None when nothing is held or the sales cannot be matched."""
        if self.error or self.held <= 0:
            return None
        return adjusted_basis(self.underlying, self.held, self.held_cost,
                              self.option_pl, self.realized)

    @property
    def total(self) -> Decimal:
        """The wheel's answer: option P/L plus share P/L, realized only."""
        return q2(self.option_pl + self.realized)


def option_pl_by_ticker(positions) -> dict[str, Decimal]:
    """Realized option P/L per underlying, every closed leg, lifetime."""
    out: dict[str, Decimal] = {}
    for p in positions:
        if p.is_open or p.status is Status.SPLIT:
            continue
        out[p.underlying] = out.get(p.underlying, ZERO) + realized_pl(p)
    return {k: q2(v) for k, v in out.items()}


def lot_views(index: ChainIndex, positions, lots, disposals,
              rule: MatchingRule = MatchingRule.FIFO) -> list[LotView]:
    """Build a view of every lot. Matching failures surface per ticker in
    ``by_ticker``; here a ticker that cannot be matched simply has no
    allocations, so its lots read as fully held.

    Covered calls belong to the ticker, not to a lot: when one is assigned the
    broker delivers shares by the account's matching rule. Coverage is what
    the open calls control laid over what the ticker holds, oldest lots first.
    """
    open_calls_by_ticker: dict[str, list[Position]] = {}
    for p in positions:
        if p.is_open and p.right is Right.CALL and p.direction is Direction.SHORT:
            open_calls_by_ticker.setdefault(p.underlying, []).append(p)

    # Share P/L per lot, from matching.
    realized_by_lot: dict[str, Decimal] = {}
    disposed_by_lot: dict[str, int] = {}
    last_by_lot: dict[str, date] = {}
    remaining_by_lot: dict[str, int] = {lot.id: lot.quantity for lot in lots}
    for underlying in sorted({lot.underlying for lot in lots}):
        subset_lots = [l for l in lots if l.underlying == underlying]
        subset_disp = [d for d in disposals if d.underlying == underlying]
        try:
            result = match(subset_lots, subset_disp, rule)
        except InsufficientSharesError:
            continue
        for a in result.allocations:
            realized_by_lot[a.lot_id] = q2(realized_by_lot.get(a.lot_id, ZERO) + a.realized_pl)
            disposed_by_lot[a.lot_id] = disposed_by_lot.get(a.lot_id, 0) + a.quantity
        for state in result.remaining:
            remaining_by_lot[state.lot.id] = state.remaining
        disposal_dates = {d.id: d.disposed_on for d in subset_disp}
        for a in result.allocations:
            when = disposal_dates[a.disposal_id]
            if a.lot_id not in last_by_lot or when > last_by_lot[a.lot_id]:
                last_by_lot[a.lot_id] = when

    # Coverage: the open calls' shares laid over the open lots, oldest first.
    covered_by_lot: dict[str, int] = {}
    for underlying, calls in open_calls_by_ticker.items():
        left = sum(p.shares for p in calls)
        for lot in sorted((l for l in lots if l.underlying == underlying),
                          key=lambda l: (l.acquired_on, l.id)):
            take = min(remaining_by_lot.get(lot.id, lot.quantity), left)
            if take > 0:
                covered_by_lot[lot.id] = take
                left -= take
        if left > 0 and any(l.underlying == underlying for l in lots):
            # More called than held: the excess sits on the last lot, where
            # the ticker page shows it as over-covered.
            last = max((l for l in lots if l.underlying == underlying),
                       key=lambda l: (l.acquired_on, l.id))
            covered_by_lot[last.id] = covered_by_lot.get(last.id, 0) + left

    views: list[LotView] = []
    for lot in lots:
        views.append(LotView(
            lot=lot,
            remaining=remaining_by_lot.get(lot.id, lot.quantity),
            open_calls=tuple(open_calls_by_ticker.get(lot.underlying, ())),
            share_realized=realized_by_lot.get(lot.id, ZERO),
            disposed=disposed_by_lot.get(lot.id, 0),
            last_disposed_on=last_by_lot.get(lot.id),
            covered_shares=covered_by_lot.get(lot.id, 0),
        ))
    return views


def by_ticker(index: ChainIndex, positions, lots, disposals,
              rule: MatchingRule = MatchingRule.FIFO) -> list[TickerShares]:
    views = lot_views(index, positions, lots, disposals, rule)
    option_pl = option_pl_by_ticker(positions)
    out: list[TickerShares] = []
    for underlying in sorted({l.underlying for l in lots} | {d.underlying for d in disposals}):
        subset_lots = [l for l in lots if l.underlying == underlying]
        subset_disp = [d for d in disposals if d.underlying == underlying]
        acquired = sum(l.quantity for l in subset_lots)
        disposed = sum(d.quantity for d in subset_disp)
        error = ""
        realized = ZERO
        held_cost = ZERO
        try:
            result = match(subset_lots, subset_disp, rule)
            realized = result.realized
            held_cost = result.open_cost
        except InsufficientSharesError as exc:
            error = str(exc)
        mine = tuple(v for v in views if v.lot.underlying == underlying)
        open_calls = mine[0].open_calls if mine else tuple(
            p for p in positions if p.underlying == underlying and p.is_open
            and p.right is Right.CALL and p.direction is Direction.SHORT)
        out.append(TickerShares(
            underlying=underlying,
            lots=mine,
            acquired=acquired,
            disposed=disposed,
            realized=realized,
            held_cost=held_cost,
            error=error,
            option_pl=option_pl.get(underlying, ZERO),
            open_call_shares=sum(p.shares for p in open_calls),
        ))
    return out


def realized_shares(tickers) -> Decimal:
    return q2(sum((t.realized for t in tickers if not t.error), ZERO))
