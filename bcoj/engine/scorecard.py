"""The scorecard: four figures that say how a set of campaigns is doing.

The same card appears on the positions list and on a position's page, so
the scope is defined once, here: the campaigns (whole by_root) behind the
positions handed in. Summing over by_root rather than over listed rows is
what keeps a figure from being counted twice when a chain is half listed.

    so far     = banked + premium in hand (every open leg)
    banked     = realized on every closed leg; when only listed legs count, an
                 open leg brings its chain carry -- what its chain has already
                 booked -- and a listed ancestor is not counted twice
    at target  = banked + in hand - the buy-backs at target; equally, banked
                 plus the premium kept if every open leg closes at its target
    at risk    = capital at risk of the open legs
    kept       = realized / premium collected, over the closed legs
"""

from dataclasses import dataclass
from decimal import Decimal

from ..domain.enums import Status
from ..domain.money import ZERO, q2
from .chains import ChainIndex
from .pnl import days_held, open_cash, realized_pl
from .risk import break_even, capital_at_risk
from .targets import target


@dataclass(frozen=True)
class Scorecard:
    positions: int          # rows handed in
    campaigns: int          # by_root they belong to
    legs: int               # every leg in those by_root
    open_legs: int
    contracts: int          # open contracts
    banked: Decimal
    in_hand: Decimal
    to_come: Decimal        # premium kept if every open leg closes at target
    at_risk: Decimal
    break_even: Decimal | None   # only when exactly one open chain
    closed_legs: int
    wins: int
    premium_closed: Decimal      # what the closed legs collected
    days_closed: int
    realized_closed: Decimal     # what the closed legs realized (for "kept")

    @property
    def so_far(self) -> Decimal:
        return q2(self.banked + self.in_hand)

    @property
    def at_target(self) -> Decimal:
        """Banked plus what the open legs keep at target. Not so_far plus
        to_come: both of those already hold the premium in hand."""
        return q2(self.banked + self.to_come)

    @property
    def to_close(self) -> Decimal:
        """The obligation: cash to buy back every open leg at its target."""
        return q2(self.in_hand - self.to_come)

    @property
    def return_at_target(self) -> Decimal | None:
        if self.at_risk <= 0:
            return None
        return q2(self.to_come / self.at_risk * 100)

    @property
    def kept(self) -> Decimal | None:
        """Realized as a share of premium collected, closed legs only."""
        if self.premium_closed <= 0:
            return None
        return q2(self.realized_closed / self.premium_closed * 100)

    @property
    def win_rate(self) -> Decimal | None:
        return None if not self.closed_legs else q2(Decimal(self.wins) / self.closed_legs * 100)

    @property
    def avg_days(self) -> int | None:
        return None if not self.closed_legs else round(self.days_closed / self.closed_legs)


def scorecard(index: ChainIndex, positions, whole_campaigns: bool = True) -> Scorecard:
    """``whole_campaigns`` sums over the by_root behind the positions -- the
    position page's view. Off, only the positions handed in count: what a
    filtered list shows is what its scorecard describes."""
    positions = list(positions)
    by_root: dict[str, list] = {}
    for p in positions:
        root = index.root(p)
        if root.id not in by_root:
            by_root[root.id] = [leg for leg, _ in index.campaign(root)]
    if whole_campaigns:
        legs = [leg for camp in by_root.values() for leg in camp]
    else:
        seen = set()
        legs = [p for p in positions if not (p.id in seen or seen.add(p.id))]
    open_legs = [p for p in legs if p.is_open]
    closed = [p for p in legs if not p.is_open and p.status is not Status.SPLIT]

    if whole_campaigns:
        banked = sum((realized_pl(p) for p in closed), ZERO)
    else:
        # An open leg's carry is what its chain already banked. A closed leg
        # that is an ancestor of a listed open leg is inside that carry.
        ancestors = set()
        for p in open_legs:
            ancestors.update(leg.id for leg in index.lineage(p) if leg.id != p.id)
        banked = (sum((index.carry(p) for p in open_legs), ZERO)
                  + sum((realized_pl(p) for p in closed if p.id not in ancestors), ZERO))

    to_come = ZERO
    for p in open_legs:
        tgt = target(p, index.carry(p))
        to_come += open_cash(p) + tgt.expected_closing
    open_heads = [p for p in open_legs]
    be = None
    if len(open_heads) == 1:
        found = break_even(index.chain(open_heads[0]))
        be = found.price if found else None

    return Scorecard(
        positions=len(list(positions)),
        campaigns=len(by_root),
        legs=len(legs),
        open_legs=len(open_legs),
        contracts=sum(p.quantity for p in open_legs),
        banked=q2(banked),
        in_hand=q2(sum((open_cash(p) for p in open_legs), ZERO)),
        to_come=q2(to_come),
        at_risk=q2(sum((capital_at_risk(p) or ZERO for p in open_legs), ZERO)),
        break_even=be,
        closed_legs=len(closed),
        wins=sum(realized_pl(p) > 0 for p in closed),
        premium_closed=q2(sum((open_cash(p) for p in closed), ZERO)),
        days_closed=sum(days_held(p) or 0 for p in closed),
        realized_closed=q2(sum((realized_pl(p) for p in closed), ZERO)),
    )
