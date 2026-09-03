"""Capital at risk and break-even.

Break-even is the figure the legacy sheet never had, and the one a rolled
position most needs: the underlying price at which the whole chain, however
many years of rolling it represents, nets zero.

    per_share  = net_chain_credit / (contracts × multiplier)
    break_even = strike − per_share      (short put; mirrored for a call)

Below that price at expiry, every leg of the chain together is a net loss.
"""

from dataclasses import dataclass
from decimal import Decimal

from ..domain.enums import Direction, Right
from ..domain.money import ZERO, q2
from ..domain.types import Position
from .chains import Chain
from .pnl import open_cash


def put_risk(position: Position) -> Decimal | None:
    """The sheet's ``Put Risk`` column, reproduced.

    Signed cash obligation on a put; None for a call (the sheet writes "-").
    Long puts come out negative, since no cash is owed on one.
    """
    if position.right is not Right.PUT:
        return None
    return q2(Decimal(position.signed_quantity * position.multiplier) * position.strike)


def capital_at_risk(
    position: Position, share_basis: Decimal | None = None
) -> Decimal | None:
    """Capital genuinely committed.

    Short put   -- cash to take assignment: strike x mult x qty
    Covered call-- the cost basis of the shares backing it
    Long option -- the debit paid
    Naked call  -- unbounded; None, and excluded from return averages

    ``share_basis`` supplies the covering lot's cost when known.
    """
    if position.direction is Direction.LONG:
        return q2(abs(open_cash(position)))

    if position.right is Right.PUT:
        return q2(Decimal(position.shares) * position.strike)

    # Short call.
    if share_basis is not None:
        return q2(share_basis)
    return None  # naked


@dataclass
class BreakEven:
    """Underlying price at which a chain nets zero if taken to assignment."""

    price: Decimal
    net_credit: Decimal
    per_share: Decimal
    strike: Decimal


def break_even(chain: Chain) -> BreakEven | None:
    """Break-even for the chain's current leg.

    For a short put the credit lowers the effective purchase price; for a short
    call it raises the effective sale price. Undefined for long options, which
    break even on premium rather than on assignment.
    """
    head = chain.head
    if head.direction is Direction.LONG:
        return None

    net = chain.net_credit
    per_share = net / Decimal(head.shares)
    if head.right is Right.PUT:
        price = head.strike - per_share
    else:
        price = head.strike + per_share

    return BreakEven(
        price=q2(price),
        net_credit=net,
        per_share=q2(per_share),
        strike=head.strike,
    )


def annualized_return(
    realized: Decimal, capital: Decimal | None, days: int | None
) -> Decimal | None:
    """Return on capital at risk, annualized. None when either input is moot."""
    if capital is None or capital <= 0 or not days or days <= 0:
        return None
    return q2((realized / capital) * (Decimal(365) / Decimal(days)) * 100)


def credit_to_recover(chain: Chain) -> Decimal:
    """Credit still needed to bring a chain back to break-even.

    Zero once the chain is net positive. On an underwater chain this is the
    figure that says how much the rolling still has to claw back -- the number
    worth seeing before agreeing to roll again.
    """
    net = chain.net_credit
    return ZERO if net >= 0 else q2(-net)
