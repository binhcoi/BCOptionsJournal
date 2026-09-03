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
    cc_premium: Decimal            # realized across those chains
    open_calls: tuple[Position, ...]
    share_realized: Decimal        # P/L on the shares disposed from this lot
    disposed: int
    last_disposed_on: date | None
    basis: AdjustedBasis

    @property
    def is_open(self) -> bool:
        return self.remaining > 0

    @property
    def covered(self) -> int:
        """Shares currently spoken for by open calls."""
        return sum(p.shares for p in self.open_calls)

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
    def option_premium(self) -> Decimal:
        return q2(sum((v.acq_premium + v.cc_premium for v in self.lots), ZERO))

    @property
    def total(self) -> Decimal:
        return q2(sum((v.total for v in self.lots), ZERO))


def _is_acquiring_option(position: Position) -> bool:
    """A short put or a long call delivers shares when exercised."""
    return (position.direction is Direction.SHORT) == (position.right is Right.PUT)


def lot_views(index: ChainIndex, positions, lots, disposals,
              rule: MatchingRule = MatchingRule.FIFO) -> list[LotView]:
    """Build a view of every lot. Matching failures surface per ticker in
    ``by_ticker``; here a ticker that cannot be matched simply has no
    allocations, so its lots read as fully held."""
    by_id = {p.id: p for p in positions}

    # Covered-call chains by lot: only chain heads, so a rolled call counts
    # once, with its whole history.
    calls_by_lot: dict[str, list[Chain]] = {}
    open_calls_by_lot: dict[str, list[Position]] = {}
    for p in positions:
        if p.share_lot_id and p.right is Right.CALL and index.is_head(p):
            calls_by_lot.setdefault(p.share_lot_id, []).append(index.chain(p))
            if p.is_open:
                open_calls_by_lot.setdefault(p.share_lot_id, []).append(p)

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

    views: list[LotView] = []
    for lot in lots:
        acquisition = None
        acq_premium = ZERO
        chains = list(calls_by_lot.get(lot.id, ()))
        assigning = by_id.get(lot.assigning_position_id) if lot.assigning_position_id else None
        if assigning is not None:
            chain = index.chain(assigning)
            if _is_acquiring_option(assigning):
                acquisition = chain
                acq_premium = chain.realized
            elif assigning.share_lot_id != lot.id:
                # An imported buy-write records the call as the lot's origin.
                # It is a covered call on this lot, not its acquisition.
                chains.append(chain)

        cc_premium = q2(sum((c.realized for c in chains), ZERO))
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
            open_calls=tuple(open_calls_by_lot.get(lot.id, ())),
            share_realized=realized_by_lot.get(lot.id, ZERO),
            disposed=disposed_by_lot.get(lot.id, 0),
            last_disposed_on=last_by_lot.get(lot.id),
            basis=basis,
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
        out.append(TickerShares(
            underlying=underlying,
            lots=tuple(v for v in views if v.lot.underlying == underlying),
            acquired=acquired,
            disposed=disposed,
            realized=realized,
            held_cost=held_cost,
            error=error,
        ))
    return out


def realized_shares(tickers) -> Decimal:
    return q2(sum((t.realized for t in tickers if not t.error), ZERO))


def suggest_covers(index: ChainIndex, positions, lot_views_) -> dict[str, str]:
    """Propose a covering lot for each unlinked short call.

    A call opened on a ticker while a lot of that ticker was held is almost
    certainly written against it. Where exactly one open-at-the-time lot fits,
    propose it; where several do, propose nothing -- guessing would misplace
    premium between lots. Imported history has no links at all, so this is
    how it gets them, one confirmation at a time.
    """
    by_ticker_lots: dict[str, list[LotView]] = {}
    for v in lot_views_:
        by_ticker_lots.setdefault(v.lot.underlying, []).append(v)

    out: dict[str, str] = {}
    for p in positions:
        if not (p.right is Right.CALL and p.direction is Direction.SHORT):
            continue
        if p.share_lot_id or not index.is_head(p):
            continue
        # Only propose for the chain's first leg; the roll inherits the link.
        candidates = [
            v for v in by_ticker_lots.get(p.underlying, ())
            if v.lot.acquired_on <= p.opened_on
            and (v.last_disposed_on is None or v.last_disposed_on >= p.opened_on)
            and v.lot.assigning_position_id != p.id
        ]
        if len(candidates) == 1:
            out[p.id] = candidates[0].lot.id
    return out
