"""Reporting: the journal grouped by period, by ticker, and by outcome.

Every figure here is a sum or a ratio of figures the engine already computes
per leg. Scopes are never mixed: option P/L is realized by the leg's close
date, share P/L by the disposal's date, and the two are shown side by side
before they are added.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from ..domain.enums import Direction, MatchingRule, Status
from ..domain.money import ZERO, q2
from ..domain.types import Position
from .chains import ChainIndex
from .pnl import days_held, open_cash, realized_pl
from .risk import capital_at_risk
from .shares import InsufficientSharesError, match
from .targets import target

GRANULARITIES = ("month", "quarter", "year")


def period_key(day: date, granularity: str) -> str:
    if granularity == "year":
        return f"{day.year}"
    if granularity == "quarter":
        return f"{day.year}-Q{(day.month - 1) // 3 + 1}"
    return f"{day.year}-{day.month:02d}"


# ---------------------------------------------------------------------------
# share P/L as dated events


@dataclass(frozen=True)
class ShareEvent:
    """One allocation's realized P/L, dated by the sale that produced it."""

    on: date
    underlying: str
    realized: Decimal


def share_events(lots, disposals, rule: MatchingRule = MatchingRule.FIFO) -> list[ShareEvent]:
    """Realized share P/L per allocation. A ticker whose lots cannot be
    matched contributes nothing rather than something wrong."""
    events: list[ShareEvent] = []
    by_id = {d.id: d for d in disposals}
    for name in sorted({l.underlying for l in lots} | {d.underlying for d in disposals}):
        mine_l = [l for l in lots if l.underlying == name]
        mine_d = [d for d in disposals if d.underlying == name]
        try:
            result = match(mine_l, mine_d, rule)
        except InsufficientSharesError:
            continue
        for a in result.allocations:
            events.append(ShareEvent(on=by_id[a.disposal_id].disposed_on,
                                     underlying=name, realized=a.realized_pl))
    return events


# ---------------------------------------------------------------------------
# by period


@dataclass(frozen=True)
class PeriodRow:
    key: str
    options: Decimal
    shares: Decimal
    legs: int          # option legs closed in the period
    running: Decimal   # cumulative total through this period

    @property
    def total(self) -> Decimal:
        return q2(self.options + self.shares)


def _realizing(positions):
    """Legs that booked something: closed by any route, dated by close."""
    return [p for p in positions
            if not p.is_open and p.status is not Status.SPLIT and p.closed_on is not None]


def by_period(positions, events, granularity: str = "month") -> list[PeriodRow]:
    options: dict[str, Decimal] = {}
    legs: dict[str, int] = {}
    shares: dict[str, Decimal] = {}
    for p in _realizing(positions):
        key = period_key(p.closed_on, granularity)
        options[key] = options.get(key, ZERO) + realized_pl(p)
        legs[key] = legs.get(key, 0) + 1
    for e in events:
        key = period_key(e.on, granularity)
        shares[key] = shares.get(key, ZERO) + e.realized

    rows = []
    running = ZERO
    for key in sorted(set(options) | set(shares)):
        o = q2(options.get(key, ZERO))
        s = q2(shares.get(key, ZERO))
        running = q2(running + o + s)
        rows.append(PeriodRow(key=key, options=o, shares=s, legs=legs.get(key, 0), running=running))
    return rows


# ---------------------------------------------------------------------------
# by ticker


@dataclass(frozen=True)
class TickerRow:
    underlying: str
    options: Decimal
    shares: Decimal
    open_premium: Decimal
    at_risk: Decimal
    open_count: int
    closed_legs: int

    @property
    def total(self) -> Decimal:
        return q2(self.options + self.shares)


def by_ticker(positions, events) -> list[TickerRow]:
    names = sorted({p.underlying for p in positions} | {e.underlying for e in events})
    rows = []
    for name in names:
        mine = [p for p in positions if p.underlying == name]
        open_ones = [p for p in mine if p.is_open]
        rows.append(TickerRow(
            underlying=name,
            options=q2(sum((realized_pl(p) for p in mine), ZERO)),
            shares=q2(sum((e.realized for e in events if e.underlying == name), ZERO)),
            open_premium=q2(sum((open_cash(p) for p in open_ones), ZERO)),
            at_risk=q2(sum((capital_at_risk(p) or ZERO for p in open_ones), ZERO)),
            open_count=len(open_ones),
            closed_legs=len(_realizing(mine)),
        ))
    rows.sort(key=lambda t: (-t.total, t.underlying))
    return rows


# ---------------------------------------------------------------------------
# how legs ended, against the target


@dataclass(frozen=True)
class Outcomes:
    """Closed short legs of one group and how they ended."""

    label: str
    legs: int
    closed: int
    rolled: int
    expired: int
    assigned: int
    hit: int              # closed at or past the target, or expired worthless
    wins: int             # realized above zero
    realized: Decimal
    premium: Decimal      # opening cash of those legs
    days: int             # total days held, for the average

    @property
    def hit_rate(self) -> Decimal | None:
        return None if not self.legs else q2(Decimal(self.hit) / self.legs * 100)

    @property
    def win_rate(self) -> Decimal | None:
        return None if not self.legs else q2(Decimal(self.wins) / self.legs * 100)

    @property
    def capture(self) -> Decimal | None:
        """Realized as a share of premium taken in. Over 100 cannot happen;
        negative means the group gave back more than it collected."""
        return None if self.premium <= 0 else q2(self.realized / self.premium * 100)

    @property
    def avg_days(self) -> Decimal | None:
        return None if not self.legs else q2(Decimal(self.days) / self.legs)

    @property
    def avg_credit(self) -> Decimal | None:
        return None if not self.legs else q2(self.premium / self.legs)


def dte_bucket(p: Position) -> str:
    dte = (p.expiry - p.opened_on).days
    if dte <= 7:
        return "0-7 DTE"
    if dte <= 30:
        return "8-30 DTE"
    if dte <= 60:
        return "31-60 DTE"
    return "60+ DTE"


def hit_target(p: Position, index: ChainIndex) -> bool:
    """Did the leg end at or past its target? Expiring worthless is the whole
    premium, so yes. Rolling or assignment is not a close at target."""
    if p.status is Status.EXPIRED:
        return True
    if p.status is Status.CLOSED and p.close_price is not None:
        tgt = target(p, index.carry(p))
        return tgt.applicable and p.close_price <= tgt.price
    return False


def target_performance(positions, key=lambda p: p.underlying, index: ChainIndex | None = None) -> list[Outcomes]:
    index = index or ChainIndex(positions)
    groups: dict[str, list[Position]] = {}
    for p in _realizing(positions):
        if p.direction is not Direction.SHORT:
            continue
        groups.setdefault(key(p), []).append(p)

    out = []
    for label in sorted(groups):
        legs = groups[label]
        out.append(Outcomes(
            label=label,
            legs=len(legs),
            closed=sum(p.status is Status.CLOSED for p in legs),
            rolled=sum(p.status is Status.ROLLED for p in legs),
            expired=sum(p.status is Status.EXPIRED for p in legs),
            assigned=sum(p.status is Status.ASSIGNED for p in legs),
            hit=sum(hit_target(p, index) for p in legs),
            wins=sum(realized_pl(p) > 0 for p in legs),
            realized=q2(sum((realized_pl(p) for p in legs), ZERO)),
            premium=q2(sum((open_cash(p) for p in legs), ZERO)),
            days=sum(days_held(p) or 0 for p in legs),
        ))
    return out


def overall(outcomes) -> Outcomes | None:
    if not outcomes:
        return None
    return Outcomes(
        label="All",
        legs=sum(o.legs for o in outcomes),
        closed=sum(o.closed for o in outcomes),
        rolled=sum(o.rolled for o in outcomes),
        expired=sum(o.expired for o in outcomes),
        assigned=sum(o.assigned for o in outcomes),
        hit=sum(o.hit for o in outcomes),
        wins=sum(o.wins for o in outcomes),
        realized=q2(sum((o.realized for o in outcomes), ZERO)),
        premium=q2(sum((o.premium for o in outcomes), ZERO)),
        days=sum(o.days for o in outcomes),
    )
