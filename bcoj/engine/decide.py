"""Decision support: what a roll would do, what each expiry could cost, and
where the risk is concentrated.

Nothing here has a rule of its own. Every figure is the engine's existing
arithmetic applied to a hypothetical (a roll not yet recorded) or grouped
differently (by expiry, by ticker). The roll preview in particular is built by
running the real roll action on a copy and reading the resulting chain, so the
panel cannot disagree with what the journal records a moment later.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from ..domain.enums import Direction, Right
from ..domain.money import ZERO, q2
from ..domain.types import Position
from . import actions
from .chains import ChainIndex
from .pnl import close_cash, open_cash, realized_pl
from .risk import BreakEven, break_even, capital_at_risk, credit_to_recover
from .targets import Target, target


# ---------------------------------------------------------------------------
# roll preview


@dataclass(frozen=True)
class RollPreview:
    """Before-and-after of a roll that has not happened."""

    closing_leg: Position
    new_leg: Position
    closing_realized: Decimal      # what closing the current leg books
    this_roll: Decimal             # buy-back plus new premium, fees in; credit > 0
    carry_before: Decimal
    carry_after: Decimal           # carried into the new leg
    net_credit_before: Decimal
    net_credit_after: Decimal
    break_even_before: BreakEven | None
    break_even_after: BreakEven | None
    at_risk_before: Decimal | None
    at_risk_after: Decimal | None
    target_after: Target
    to_recover_after: Decimal
    dte_after: int

    @property
    def size_change(self) -> int:
        return self.new_leg.quantity - self.closing_leg.quantity

    @property
    def grows(self) -> bool:
        """The flag worth a warning: this is how a position quietly grows
        several-fold, one roll at a time."""
        return self.size_change > 0

    @property
    def strike_change(self) -> Decimal:
        return q2(self.new_leg.strike - self.closing_leg.strike)

    @property
    def is_credit(self) -> bool:
        return self.this_roll >= 0

    @property
    def underwater_after(self) -> bool:
        return self.to_recover_after > 0


def roll_preview(
    positions,
    position: Position,
    *,
    close_price: Decimal,
    new_expiry: date,
    new_strike: Decimal,
    new_price: Decimal,
    on: date | None = None,
    close_fee: Decimal = ZERO,
    new_fee: Decimal = ZERO,
    new_quantity: int | None = None,
) -> RollPreview:
    """Everything the roll form should show before the button is pressed.

    ``positions`` is the whole journal, so the chain the leg belongs to is
    found and carried correctly, splits included.
    """
    before = ChainIndex(positions).chain(position)
    result = actions.roll(
        position,
        close_price=close_price,
        new_expiry=new_expiry,
        new_strike=new_strike,
        new_price=new_price,
        on=on,
        close_fee=close_fee,
        new_fee=new_fee,
        new_quantity=new_quantity,
    )
    closing, new_leg = result.updated[0], result.created[0]

    others = [p for p in positions if p.id != position.id]
    after = ChainIndex(others + [closing, new_leg]).chain(new_leg)

    when = on or date.today()
    return RollPreview(
        closing_leg=closing,
        new_leg=new_leg,
        closing_realized=realized_pl(closing),
        this_roll=q2(close_cash(closing) + open_cash(new_leg)),
        carry_before=before.carry,
        carry_after=after.carry,
        net_credit_before=before.net_credit,
        net_credit_after=after.net_credit,
        break_even_before=break_even(before),
        break_even_after=break_even(after),
        at_risk_before=capital_at_risk(position),
        at_risk_after=capital_at_risk(new_leg),
        target_after=target(new_leg, after.carry),
        to_recover_after=credit_to_recover(after),
        dte_after=(new_expiry - when).days,
    )


# ---------------------------------------------------------------------------
# obligation calendar


@dataclass(frozen=True)
class Obligation:
    """One open short position and what assignment would demand of it."""

    position: Position
    cash: Decimal | None       # a put: cash to take the shares
    shares: int | None         # a call: shares to deliver
    break_even: BreakEven | None
    carry: Decimal


@dataclass(frozen=True)
class ExpiryDay:
    expiry: date
    dte: int
    items: tuple[Obligation, ...]

    @property
    def cash_if_assigned(self) -> Decimal:
        return q2(sum((o.cash for o in self.items if o.cash is not None), ZERO))

    @property
    def shares_to_deliver(self) -> int:
        return sum(o.shares for o in self.items if o.shares is not None)

    @property
    def tickers(self) -> tuple[str, ...]:
        return tuple(sorted({o.position.underlying for o in self.items}))


def obligations(positions, today: date | None = None) -> list[ExpiryDay]:
    """Open short positions grouped by expiry, soonest first.

    Long options are left out: the most they can cost has already been paid.
    A short put owes cash for the shares; a short call owes the shares.
    """
    today = today or date.today()
    index = ChainIndex(positions)
    by_day: dict[date, list[Obligation]] = {}
    for p in positions:
        if not p.is_open or p.direction is not Direction.SHORT:
            continue
        chain = index.chain(p)
        item = Obligation(
            position=p,
            cash=q2(Decimal(p.shares) * p.strike) if p.right is Right.PUT else None,
            shares=p.shares if p.right is Right.CALL else None,
            break_even=break_even(chain),
            carry=index.carry(p),
        )
        by_day.setdefault(p.expiry, []).append(item)

    days = []
    for expiry in sorted(by_day):
        items = sorted(by_day[expiry], key=lambda o: (o.position.underlying, o.position.strike))
        days.append(ExpiryDay(expiry=expiry, dte=(expiry - today).days, items=tuple(items)))
    return days


# ---------------------------------------------------------------------------
# concentration


@dataclass(frozen=True)
class TickerRisk:
    """Where one ticker's capital sits, at every scope the app labels."""

    underlying: str
    at_risk: Decimal          # options: cash to take assignment / debit paid
    shares_at_cost: Decimal   # shares held, at cost
    naked_calls: int          # short calls with no lot behind them: unbounded
    open_premium: Decimal
    carry: Decimal            # chain carry into the open legs
    realized: Decimal         # ticker scope: every leg ever, rolled ones included
    open_positions: int

    @property
    def exposure(self) -> Decimal:
        return q2(self.at_risk + self.shares_at_cost)


def concentration(positions, shares_at_cost: dict[str, Decimal] | None = None) -> list[TickerRisk]:
    """Capital committed per ticker, largest first.

    A short call written against a lot is not counted again as option risk:
    the shares behind it already are, at cost. A short call with no lot is
    counted as naked, and its risk left out of the sum because it has no
    ceiling -- a sum that included it would be a lie either way.
    """
    shares_at_cost = shares_at_cost or {}
    index = ChainIndex(positions)
    names = sorted({p.underlying for p in positions} | set(shares_at_cost))
    out = []
    for name in names:
        mine = [p for p in positions if p.underlying == name]
        open_ones = [p for p in mine if p.is_open]
        at_risk = ZERO
        naked = 0
        for p in open_ones:
            if p.direction is Direction.SHORT and p.right is Right.CALL:
                if p.share_lot_id is None:
                    naked += 1
                continue
            at_risk += capital_at_risk(p) or ZERO
        out.append(TickerRisk(
            underlying=name,
            at_risk=q2(at_risk),
            shares_at_cost=q2(shares_at_cost.get(name, ZERO)),
            naked_calls=naked,
            open_premium=q2(sum((open_cash(p) for p in open_ones), ZERO)),
            carry=q2(sum((index.carry(p) for p in open_ones), ZERO)),
            realized=q2(sum((realized_pl(p) for p in mine), ZERO)),
            open_positions=len(open_ones),
        ))
    out.sort(key=lambda t: (-t.exposure, t.underlying))
    return out


def total_exposure(risks) -> Decimal:
    return q2(sum((t.exposure for t in risks), ZERO))
