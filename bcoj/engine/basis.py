"""Adjusted share basis -- "what do I really own these at".

    adjusted_unit_price  = (lot_cost - acq_premium) / quantity
    adjusted_after_calls = (lot_cost - acq_premium - cc_premium) / quantity
    min_call_strike      = adjusted_after_calls, rounded up to a real strike

Two figures rather than one, so the covered-call contribution is visible
instead of blended in: the first is the basis after the premium that acquired
the shares, the second after the calls written against them since.

``min_call_strike`` is the actionable form -- writing a call below the adjusted
basis locks in a loss, so it is the floor worth knowing before selling one.

Premium enters as a chain's cumulative net, so a losing call chain correctly
*raises* the basis. Only realized premium counts; open premium is projected
separately.
"""

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING

from ..domain.money import ZERO, q2
from ..domain.types import ShareLot

# Strike grids vary by underlying and price, so this is deliberately coarse:
# a floor rounded up to the next half-dollar is always a safe floor, even if
# that exact strike is not listed.
DEFAULT_STRIKE_INCREMENT = Decimal("0.50")


@dataclass
class AdjustedBasis:
    """Per-lot effective cost. ``available`` is false when there is nothing to
    divide by -- reported as unavailable, never as an error code."""

    lot_id: str | None
    underlying: str
    quantity: int
    lot_cost: Decimal
    acq_premium: Decimal
    cc_premium: Decimal
    available: bool
    unit_price: Decimal | None
    after_calls: Decimal | None
    min_call_strike: Decimal | None
    reason: str = ""

    @property
    def calls_contributed(self) -> Decimal | None:
        """Per-share reduction the covered calls actually bought."""
        if not self.available:
            return None
        return q2(self.unit_price - self.after_calls)


def adjusted_basis(
    lot: ShareLot,
    acq_premium: Decimal = ZERO,
    cc_premium: Decimal = ZERO,
    quantity: int | None = None,
    increment: Decimal = DEFAULT_STRIKE_INCREMENT,
) -> AdjustedBasis:
    """Adjusted basis for one lot.

    ``quantity`` overrides the lot size when only part of it is still held, so
    the figure follows the shares that remain.
    """
    held = lot.quantity if quantity is None else quantity

    if held <= 0:
        return AdjustedBasis(
            lot_id=lot.id,
            underlying=lot.underlying,
            quantity=held,
            lot_cost=ZERO,
            acq_premium=q2(acq_premium),
            cc_premium=q2(cc_premium),
            available=False,
            unit_price=None,
            after_calls=None,
            min_call_strike=None,
            reason="no shares held",
        )

    per_share_cost = lot.cost_per_share + (
        lot.fee / Decimal(lot.quantity) if lot.quantity else ZERO
    )
    lot_cost = q2(per_share_cost * held)

    unit_price = q2((lot_cost - acq_premium) / Decimal(held))
    after_calls = q2((lot_cost - acq_premium - cc_premium) / Decimal(held))

    return AdjustedBasis(
        lot_id=lot.id,
        underlying=lot.underlying,
        quantity=held,
        lot_cost=lot_cost,
        acq_premium=q2(acq_premium),
        cc_premium=q2(cc_premium),
        available=True,
        unit_price=unit_price,
        after_calls=after_calls,
        min_call_strike=round_up_to_strike(after_calls, increment),
    )


def round_up_to_strike(
    price: Decimal, increment: Decimal = DEFAULT_STRIKE_INCREMENT
) -> Decimal:
    """Round a break-even price up to the next strike increment.

    Up, never down: the floor has to err high, because rounding it down would
    suggest a strike that locks in a loss.
    """
    if increment <= 0:
        return q2(price)
    steps = (price / increment).to_integral_value(rounding=ROUND_CEILING)
    return q2(steps * increment)


def blended(bases, increment: Decimal = DEFAULT_STRIKE_INCREMENT) -> AdjustedBasis | None:
    """Blend several lots of one underlying into a single adjusted basis.

    The legacy sheet only ever showed a blended figure. Per-lot detail matters
    whenever lots were acquired at sharply different prices, where the blend
    describes none of them well.
    """
    usable = [b for b in bases if b.available]
    if not usable:
        return None

    underlying = usable[0].underlying
    held = sum(b.quantity for b in usable)
    lot_cost = q2(sum((b.lot_cost for b in usable), ZERO))
    acq = q2(sum((b.acq_premium for b in usable), ZERO))
    cc = q2(sum((b.cc_premium for b in usable), ZERO))

    unit_price = q2((lot_cost - acq) / Decimal(held))
    after_calls = q2((lot_cost - acq - cc) / Decimal(held))

    return AdjustedBasis(
        lot_id=None,
        underlying=underlying,
        quantity=held,
        lot_cost=lot_cost,
        acq_premium=acq,
        cc_premium=cc,
        available=True,
        unit_price=unit_price,
        after_calls=after_calls,
        min_call_strike=round_up_to_strike(after_calls, increment),
    )
