"""Profit targets -- the sheet's ``E P/L`` and ``E Closing`` ("E" for Expected).

On an open row the sheet's ``Close_U`` is a *target* price, not a market mark:
nothing in the sheet quotes the market. It is the price at which the chain
would net a given fraction of its available credit. Default 50%.

The target is computed rather than stored, so it stays correct as a chain's
carry changes underneath it.
"""

from dataclasses import dataclass
from decimal import Decimal

from ..domain.money import ZERO, q2
from ..domain.types import Position
from .pnl import close_cash_at, open_cash

DEFAULT_TARGET_PCT = Decimal("0.50")


@dataclass
class Target:
    """A profit target for one open position, in the context of its chain."""

    price: Decimal
    expected_closing: Decimal
    expected_pl: Decimal
    net_credit: Decimal
    pct: Decimal
    applicable: bool

    @property
    def capture(self) -> Decimal | None:
        """Fraction of net credit the target represents. None when moot."""
        if not self.applicable or self.net_credit == 0:
            return None
        return self.expected_pl / self.net_credit


def target_price(
    position: Position, carry: Decimal = ZERO, pct: Decimal | None = None
) -> Decimal:
    """Close price at which the chain nets ``pct`` of its available credit.

    Derived by solving close_cash for the price that lands expected_pl on
    target, then rounding to the cent as the sheet does:

        net      = open_cash + carry
        needed   = net * pct - net
        price    = -(needed + close_fee) / (Q x mult)

    Clamped at zero: a chain already underwater has no profit target, and its
    best case is expiring worthless (price 0), which is what the sheet shows.
    """
    pct = _resolve_pct(position, pct)
    net = q2(open_cash(position) + carry)
    needed_close_cash = q2(net * pct) - net
    denominator = Decimal(position.signed_quantity * position.multiplier)
    if denominator == 0:  # defensive; Position forbids zero quantity
        return ZERO
    raw = -(needed_close_cash + position.close_fee) / denominator
    price = q2(raw)
    return price if price > 0 else ZERO


def target(
    position: Position, carry: Decimal = ZERO, pct: Decimal | None = None
) -> Target:
    """Full target figures for an open position."""
    pct = _resolve_pct(position, pct)
    net = q2(open_cash(position) + carry)
    price = target_price(position, carry, pct)
    expected_closing = close_cash_at(position, price)
    expected_pl = q2(open_cash(position) + expected_closing + carry)
    return Target(
        price=price,
        expected_closing=expected_closing,
        expected_pl=expected_pl,
        net_credit=net,
        pct=pct,
        # A net debit chain has no credit to capture a fraction of.
        applicable=net > 0,
    )


def _resolve_pct(position: Position, pct: Decimal | None) -> Decimal:
    if pct is not None:
        return pct
    if position.target_pct is not None:
        return position.target_pct
    return DEFAULT_TARGET_PCT
