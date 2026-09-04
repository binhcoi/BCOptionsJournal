"""Wheels: what a share lot actually made, options and stock together.

A lot arrives by assignment, by buy-write or by outright purchase. Calls get
written against it. Eventually it is called away or sold. The sheet could
record each of those as a row and could total none of them, which is why the
share side of a wheel was invisible (plan.md §3.1).

Everything here is derived from positions, lots and disposals. Nothing is
stored, so a corrected rule fixes every historical wheel at once.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from ..domain.enums import Direction, MatchingRule, Right
from ..domain.money import ZERO, q2
from ..domain.types import Position, ShareLot
from .basis import AdjustedBasis, adjusted_basis, blended
from .chains import Chain, ChainIndex
from .shares import InsufficientSharesError, match


@dataclass
class LotView:
    """One share lot with everything attached to it."""

    lot: ShareLot
    remaining: int
    acquisition: Chain | None      # the option chain that delivered the shares
    acq_premium: Decimal           # that chain's realized total
    call_chains: tuple[Chain, ...]  # covered calls written against the lot
    cc_premium: Decimal            # realized across those chains, this lot's share
    open_calls: tuple[Position, ...]
    share_realized: Decimal        # P/L on the shares disposed from this lot
    disposed: int
    last_disposed_on: date | None
    basis: AdjustedBasis
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
    def total(self) -> Decimal:
        """The wheel's answer: acquisition premium + call premium + share P/L.

        Realized only. Open calls contribute what their chains have realized
        so far; the shares still held contribute nothing until sold.
        """
        return q2(self.acq_premium + self.cc_premium + self.share_realized)

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
    held_cost: Decimal         # cost basis of what is still held
    error: str = ""            # matching refused: an acquisition is missing
    call_premium: Decimal = ZERO   # realized by the ticker's short-call chains
    open_call_shares: int = 0      # shares the open short calls control

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
    def blended(self) -> AdjustedBasis | None:
        return blended([v.basis for v in self.open_lots])

    @property
    def covered_shares(self) -> int:
        return min(max(self.held, 0), self.open_call_shares)

    @property
    def uncovered_shares(self) -> int:
        """Shares the open calls control beyond what is held: naked."""
        return max(self.open_call_shares - max(self.held, 0), 0)

    @property
    def option_premium(self) -> Decimal:
        return q2(sum((v.acq_premium for v in self.lots), ZERO) + self.call_premium)

    @property
    def total(self) -> Decimal:
        return q2(self.option_premium + self.realized)


def _is_acquiring_option(position: Position) -> bool:
    """A short put or a long call delivers shares when exercised."""
    return (position.direction is Direction.SHORT) == (position.right is Right.PUT)


def lot_views(index: ChainIndex, positions, lots, disposals,
              rule: MatchingRule = MatchingRule.FIFO) -> list[LotView]:
    """Build a view of every lot. Matching failures surface per ticker in
    ``by_ticker``; here a ticker that cannot be matched simply has no
    allocations, so its lots read as fully held."""
    by_id = {p.id: p for p in positions}

    # Covered calls belong to the ticker, not to a lot: when one is assigned
    # the broker delivers shares by the account's matching rule, so a stored
    # link between a call and a lot describes nothing real. Premium from the
    # ticker's short-call chains (heads only, so a rolled call counts once) is
    # spread over the ticker's lots by size; coverage is what the open calls
    # control against what the ticker holds, oldest lots first.
    chains_by_ticker: dict[str, list[Chain]] = {}
    open_calls_by_ticker: dict[str, list[Position]] = {}
    for p in positions:
        if p.right is Right.CALL and p.direction is Direction.SHORT and index.is_head(p):
            chains_by_ticker.setdefault(p.underlying, []).append(index.chain(p))
            if p.is_open:
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

    size_by_ticker: dict[str, int] = {}
    for lot in lots:
        size_by_ticker[lot.underlying] = size_by_ticker.get(lot.underlying, 0) + lot.quantity
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
        acquisition = None
        acq_premium = ZERO
        assigning = by_id.get(lot.assigning_position_id) if lot.assigning_position_id else None
        if assigning is not None and _is_acquiring_option(assigning):
            acquisition = index.chain(assigning)
            acq_premium = acquisition.realized

        chains = list(chains_by_ticker.get(lot.underlying, ()))
        size = size_by_ticker.get(lot.underlying, 0)
        call_premium = q2(sum((c.realized for c in chains), ZERO))
        cc_premium = (q2(call_premium * Decimal(lot.quantity) / Decimal(size)) if size else ZERO)
        remaining = remaining_by_lot.get(lot.id, lot.quantity)
        basis = adjusted_basis(
            lot, acq_premium=acq_premium, cc_premium=cc_premium,
            quantity=remaining if remaining > 0 else lot.quantity,
        )
        views.append(LotView(
            lot=lot,
            remaining=remaining,
            acquisition=acquisition,
            acq_premium=acq_premium,
            call_chains=tuple(chains),
            cc_premium=cc_premium,
            open_calls=tuple(open_calls_by_ticker.get(lot.underlying, ())),
            share_realized=realized_by_lot.get(lot.id, ZERO),
            disposed=disposed_by_lot.get(lot.id, 0),
            last_disposed_on=last_by_lot.get(lot.id),
            basis=basis,
            covered_shares=covered_by_lot.get(lot.id, 0),
        ))
    return views


def by_ticker(index: ChainIndex, positions, lots, disposals,
              rule: MatchingRule = MatchingRule.FIFO) -> list[TickerShares]:
    views = lot_views(index, positions, lots, disposals, rule)
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
            call_premium=q2(sum((c.realized for c in (mine[0].call_chains if mine else ())), ZERO)),
            open_call_shares=sum(p.shares for p in open_calls),
        ))
    return out


def realized_shares(tickers) -> Decimal:
    return q2(sum((t.realized for t in tickers if not t.error), ZERO))
