"""Share lot matching.

Options need no lot matching -- one row is one position. Shares do, and it is
not a detail: where lots were acquired at very different prices, FIFO and LIFO
can differ by tens of thousands on a single disposal, and leave a different lot
behind besides. An unstated convention is therefore a wrong answer waiting to
happen.

FIFO is the default, with a specific-lot override per disposal, and the
allocation rows record what was actually chosen.
"""

from dataclasses import dataclass
from decimal import Decimal

from ..domain.enums import MatchingRule
from ..domain.money import ZERO, q2
from ..domain.types import ShareAllocation, ShareDisposal, ShareLot


class InsufficientSharesError(ValueError):
    """A disposal wants more shares than any lot can supply.

    Means an acquisition is missing from the record -- typically because the
    legacy sheet had nowhere to put an outright share purchase. Refused rather
    than computed through: a figure derived from a negative share count looks
    plausible and is meaningless.
    """


@dataclass
class LotState:
    lot: ShareLot
    remaining: int

    @property
    def fee_per_share(self) -> Decimal:
        if self.lot.quantity == 0:
            return ZERO
        return self.lot.fee / Decimal(self.lot.quantity)


@dataclass
class MatchResult:
    allocations: tuple[ShareAllocation, ...]
    remaining: tuple[LotState, ...]

    @property
    def realized(self) -> Decimal:
        return q2(sum((a.realized_pl for a in self.allocations), ZERO))

    @property
    def open_quantity(self) -> int:
        return sum(state.remaining for state in self.remaining)

    @property
    def open_cost(self) -> Decimal:
        """Cost basis of shares still held, fees included pro-rata."""
        total = ZERO
        for state in self.remaining:
            total += state.lot.cost_per_share * state.remaining
            total += state.fee_per_share * state.remaining
        return q2(total)

    def open_lots(self) -> tuple[LotState, ...]:
        return tuple(s for s in self.remaining if s.remaining > 0)


def match(
    lots, disposals, rule: MatchingRule = MatchingRule.FIFO
) -> MatchResult:
    """Allocate disposals against lots, oldest disposal first.

    Fees are spread pro-rata: a lot's acquisition fee by shares consumed, a
    disposal's fee by shares in that allocation. Rounding differences land on
    the final allocation of each disposal so the totals stay exact.
    """
    states = [LotState(lot=lot, remaining=lot.quantity) for lot in lots]
    allocations: list[ShareAllocation] = []

    for disposal in sorted(disposals, key=lambda d: (d.disposed_on, d.id)):
        allocations.extend(_allocate_one(disposal, states, rule))

    return MatchResult(allocations=tuple(allocations), remaining=tuple(states))


def _allocate_one(
    disposal: ShareDisposal, states: list[LotState], rule: MatchingRule
) -> list[ShareAllocation]:
    candidates = _candidates(disposal, states, rule)

    available = sum(state.remaining for state in candidates)
    if available < disposal.quantity:
        if disposal.specific_lot_ids:
            late = [s.lot for s in states
                    if s.lot.id in disposal.specific_lot_ids
                    and s.lot.acquired_on > disposal.disposed_on]
            if late:
                raise InsufficientSharesError(
                    f"the sale of {disposal.quantity} on {disposal.disposed_on} is "
                    f"pinned to a lot acquired on {late[0].acquired_on}, after the "
                    "sale. Shares cannot be sold before they were bought: date the "
                    "sale later, or correct the lot's date."
                )
            raise InsufficientSharesError(
                f"the sale of {disposal.quantity} on {disposal.disposed_on} is "
                f"pinned to a specific lot that holds only {available}. Either "
                "the lot is wrong or the quantity is; other lots cannot cover a "
                "pinned sale."
            )
        raise InsufficientSharesError(
            f"the sale of {disposal.quantity} on {disposal.disposed_on} needs "
            f"more shares than were held on that date ({available}). "
            "An acquisition is missing, or is dated after the sale."
        )

    outstanding = disposal.quantity
    disposal_fee_per_share = (
        disposal.fee / Decimal(disposal.quantity) if disposal.quantity else ZERO
    )

    allocations: list[ShareAllocation] = []
    for state in candidates:
        if outstanding == 0:
            break
        if state.remaining == 0:
            continue

        take = min(state.remaining, outstanding)
        state.remaining -= take
        outstanding -= take

        cost = state.lot.cost_per_share * take + state.fee_per_share * take
        proceeds = disposal.proceeds_per_share * take - disposal_fee_per_share * take
        allocations.append(
            ShareAllocation(
                disposal_id=disposal.id,
                lot_id=state.lot.id,
                quantity=take,
                realized_pl=q2(proceeds - cost),
                cost_basis=q2(cost),
                proceeds=q2(proceeds),
            )
        )

    _absorb_rounding(allocations, disposal)
    return allocations


def _candidates(
    disposal: ShareDisposal, states: list[LotState], rule: MatchingRule
) -> list[LotState]:
    """Lots eligible for this disposal, in consumption order.

    Only lots acquired on or before the disposal date qualify -- but same-day
    acquisitions do count, which matters when an assignment and the sale of the
    resulting shares fall on the same date.
    """
    eligible = [
        state
        for state in states
        if state.lot.underlying.upper() == disposal.underlying.upper()
        and state.lot.acquired_on <= disposal.disposed_on
    ]

    if disposal.specific_lot_ids:
        order = {lot_id: i for i, lot_id in enumerate(disposal.specific_lot_ids)}
        chosen = [s for s in eligible if s.lot.id in order]
        chosen.sort(key=lambda s: order[s.lot.id])
        return chosen

    eligible.sort(key=lambda s: (s.lot.acquired_on, s.lot.id))
    if rule is MatchingRule.LIFO:
        eligible.reverse()
    return eligible


def _absorb_rounding(allocations: list[ShareAllocation], disposal: ShareDisposal) -> None:
    """Push per-share rounding drift onto the last allocation.

    Guarantees sum(proceeds) equals the disposal's actual proceeds exactly, so
    reports never disagree with the ledger by a cent.
    """
    if not allocations:
        return
    target = q2(disposal.proceeds)
    actual = q2(sum((a.proceeds for a in allocations), ZERO))
    drift = q2(target - actual)
    if drift == 0:
        return
    last = allocations[-1]
    allocations[-1] = ShareAllocation(
        disposal_id=last.disposal_id,
        lot_id=last.lot_id,
        quantity=last.quantity,
        realized_pl=q2(last.realized_pl + drift),
        cost_basis=last.cost_basis,
        proceeds=q2(last.proceeds + drift),
    )


def net_share_count(lots, disposals) -> int:
    """Signed share count. Negative means an acquisition was never recorded."""
    acquired = sum(lot.quantity for lot in lots)
    disposed = sum(d.quantity for d in disposals)
    return acquired - disposed
