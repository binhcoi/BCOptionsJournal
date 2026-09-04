"""Adjusted share basis -- "what do I really own these at".

One figure per ticker, never per lot:

    adjusted basis  = (cost of shares held - option P/L - share P/L) / shares held
    min call strike = adjusted basis, rounded up to a real strike

Shares are bought at what was paid: the strike for an assigned put, the fill
for an outright buy, fees in. Everything the ticker has already paid back --
every closed option leg, put or call, whether or not it ever touched a share,
and the P/L on every share sold -- lowers what the remaining shares still need
to fetch. Lifetime, realized only: open premium is not yet money.

``min_call_strike`` is the actionable form: writing a call below the adjusted
basis locks in a loss, so it is the floor worth knowing before selling one.
"""

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING

from ..domain.money import ZERO, q2

# Strike grids vary by underlying and price, so this is deliberately coarse:
# a floor rounded up to the next half-dollar is always a safe floor, even if
# that exact strike is not listed.
DEFAULT_STRIKE_INCREMENT = Decimal("0.50")


@dataclass(frozen=True)
class AdjustedBasis:
    """A ticker's effective cost per share held. ``available`` is false when
    there is nothing to divide by -- reported as unavailable, never as an
    error code."""

    underlying: str
    held: int
    cost_held: Decimal
    option_pl: Decimal
    share_pl: Decimal
    available: bool
    unit_price: Decimal | None
    min_call_strike: Decimal | None
    reason: str = ""

    @property
    def paid_back(self) -> Decimal:
        """What the ticker has already returned, options and shares together."""
        return q2(self.option_pl + self.share_pl)


def adjusted_basis(
    underlying: str,
    held: int,
    cost_held: Decimal,
    option_pl: Decimal = ZERO,
    share_pl: Decimal = ZERO,
    increment: Decimal = DEFAULT_STRIKE_INCREMENT,
) -> AdjustedBasis:
    """The ticker's adjusted basis. A negative figure means the shares are
    already paid for: any sale is profit."""
    if held <= 0:
        return AdjustedBasis(
            underlying=underlying, held=held, cost_held=ZERO,
            option_pl=q2(option_pl), share_pl=q2(share_pl), available=False,
            unit_price=None, min_call_strike=None, reason="no shares held",
        )
    unit_price = q2((cost_held - option_pl - share_pl) / Decimal(held))
    return AdjustedBasis(
        underlying=underlying, held=held, cost_held=q2(cost_held),
        option_pl=q2(option_pl), share_pl=q2(share_pl), available=True,
        unit_price=unit_price,
        min_call_strike=round_up_to_strike(max(unit_price, ZERO), increment),
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
