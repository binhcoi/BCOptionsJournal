"""Cash flows and realized P/L for a single option position.

One formula covers long and short, calls and puts:

    open_cash  =  Q x mult x open_price  - open_fee
    close_cash = -Q x mult x close_price - close_fee
    P/L        = open_cash + close_cash

where Q is signed: positive short, negative long.
"""

from decimal import Decimal

from ..domain.money import ZERO, q2
from ..domain.types import Position


def open_cash(position: Position) -> Decimal:
    """Cash effect of opening. Positive for a credit, negative for a debit.

    Matches the sheet's ``Open`` column, including on open rows.
    """
    notional = Decimal(position.signed_quantity * position.multiplier) * position.open_price
    return q2(notional - position.open_fee)


def close_cash_at(position: Position, price: Decimal) -> Decimal:
    """Cash effect of closing at a hypothetical price.

    Used both for the actual close and for the profit target (E Closing).
    """
    notional = Decimal(-position.signed_quantity * position.multiplier) * price
    return q2(notional - position.close_fee)


def close_cash(position: Position) -> Decimal:
    """Cash effect of the actual close.

    Zero while the position is open, matching the sheet's ``Close`` column --
    on an open row the sheet's ``Close_U`` holds a *target*, not a close, so it
    must not leak into realized figures.
    """
    if position.is_open or position.close_price is None:
        return ZERO
    return close_cash_at(position, position.close_price)


def realized_pl(position: Position) -> Decimal:
    """Realized P/L for this leg alone.

    Zero while open, and zero once superseded by a split -- the children
    realize what this position would have.
    """
    if position.is_open or position.is_superseded:
        return ZERO
    return q2(open_cash(position) + close_cash(position))


def premium_collected(position: Position) -> Decimal:
    """Gross credit received on opening, before fees. Zero for a long."""
    if position.direction.sign < 0:
        return ZERO
    return q2(Decimal(position.shares) * position.open_price)


def fees(position: Position) -> Decimal:
    total = position.open_fee
    if not position.is_open:
        total = total + position.close_fee
    return q2(total)


def days_held(position: Position) -> int | None:
    """Calendar days from open to close. None while open."""
    if position.closed_on is None:
        return None
    return (position.closed_on - position.opened_on).days
