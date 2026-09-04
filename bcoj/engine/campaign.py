"""A campaign: everything that grew from one opening trade.

A chain is one lineage. Once a position is split, the same opening trade has
several branches, and the question "how is this going?" is about all of them
together: cash in and out so far, what it would take to close, how the size
and the capital at risk moved, what got assigned. These are sums over the
family, so nothing is counted twice: a leg realizes once, an open leg carries
its premium once.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from ..domain.enums import Status
from ..domain.money import ZERO, q2
from ..domain.types import Position
from .chains import ChainIndex
from .pnl import open_cash, realized_pl
from .risk import capital_at_risk
from .targets import target


@dataclass(frozen=True)
class Campaign:
    root: Position
    legs: tuple[Position, ...]
    open_legs: tuple[Position, ...]
    realized: Decimal              # every leg that booked something
    open_premium: Decimal          # credit still held against open legs
    expected_at_target: Decimal    # family P/L if every open leg closes at target
    cost_to_close_at_target: Decimal
    at_risk_start: Decimal
    at_risk_now: Decimal
    assigned_legs: int
    assigned_shares: int
    assigned_cash: Decimal         # shares x strike, what assignment cost or paid
    rolls: int
    splits: int

    @property
    def net_so_far(self) -> Decimal:
        """Cash the campaign is ahead by today: realized plus premium in hand."""
        return q2(self.realized + self.open_premium)

    @property
    def contracts_start(self) -> int:
        return self.root.quantity

    @property
    def contracts_now(self) -> int:
        return sum(p.quantity for p in self.open_legs)

    @property
    def strikes_now(self) -> tuple[Decimal, ...]:
        return tuple(sorted({p.strike for p in self.open_legs}))

    @property
    def started(self) -> date:
        return self.root.opened_on

    @property
    def days(self) -> int:
        end = date.today() if self.open_legs else max(
            (p.closed_on for p in self.legs if p.closed_on), default=self.started)
        return (end - self.started).days

    @property
    def is_open(self) -> bool:
        return bool(self.open_legs)


def campaign(index: ChainIndex, position: Position) -> Campaign:
    legs = tuple(leg for leg, _ in index.family(position))
    root = legs[0]
    open_legs = tuple(p for p in legs if p.is_open)
    expected = ZERO
    closing = ZERO
    for p in open_legs:
        tgt = target(p, index.carry(p))
        expected += tgt.expected_pl
        closing += tgt.expected_closing
    assigned = [p for p in legs if p.status is Status.ASSIGNED]
    return Campaign(
        root=root,
        legs=legs,
        open_legs=open_legs,
        realized=q2(sum((realized_pl(p) for p in legs if not p.is_open), ZERO)),
        open_premium=q2(sum((open_cash(p) for p in open_legs), ZERO)),
        expected_at_target=q2(expected),
        cost_to_close_at_target=q2(-closing),
        at_risk_start=capital_at_risk(root) or ZERO,
        at_risk_now=q2(sum((capital_at_risk(p) or ZERO for p in open_legs), ZERO)),
        assigned_legs=len(assigned),
        assigned_shares=sum(p.shares for p in assigned),
        assigned_cash=q2(sum((Decimal(p.shares) * p.strike for p in assigned), ZERO)),
        rolls=sum(p.status is Status.ROLLED for p in legs),
        splits=sum(p.status is Status.SPLIT for p in legs),
    )
