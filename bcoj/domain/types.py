"""Core records. Plain dataclasses so the engine stays free of any I/O."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from .enums import Direction, DisposalKind, Right, ShareSource, Status
from .money import ZERO


@dataclass
class Position:
    """One option position, opened and closed. Mirrors a legacy sheet row.

    Only entered values live here. Cash flows, P/L, chain carry, targets,
    break-even and capital at risk are all computed (see bcoj.engine), so a
    corrected rule fixes every historical row with nothing to migrate.
    """

    id: str
    underlying: str
    expiry: date
    strike: Decimal
    right: Right
    direction: Direction
    quantity: int  # always positive; direction carries the sign
    opened_on: date
    open_price: Decimal
    open_fee: Decimal = ZERO
    closed_on: date | None = None
    close_price: Decimal | None = None
    close_fee: Decimal = ZERO
    status: Status = Status.OPEN
    multiplier: int = 100
    rolled_from_id: str | None = None
    split_from_id: str | None = None
    # The parent's quantity at the moment of a split. A fact about the event,
    # not a derivation, and the only way to divide inherited chain history
    # between the halves rather than duplicating it onto both.
    split_from_quantity: int | None = None
    share_lot_id: str | None = None
    spread_group_id: str | None = None
    target_pct: Decimal | None = None
    notes: str = ""
    tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError(
                f"{self.id}: quantity must be positive; direction carries the sign"
            )
        if self.multiplier <= 0:
            raise ValueError(f"{self.id}: multiplier must be positive")

    @property
    def signed_quantity(self) -> int:
        """The sheet's convention: positive short, negative long."""
        return self.direction.sign * self.quantity

    @property
    def shares(self) -> int:
        """Shares controlled, unsigned."""
        return self.quantity * self.multiplier

    @property
    def is_open(self) -> bool:
        return self.status.is_open

    @property
    def is_superseded(self) -> bool:
        return self.status.is_superseded

    @property
    def split_ratio(self):
        """This half's share of the pre-split position, or None if not a split."""
        if self.split_from_id is None or not self.split_from_quantity:
            return None
        return Decimal(self.quantity) / Decimal(self.split_from_quantity)


@dataclass
class ShareLot:
    """An acquisition of shares. Disposals are separate records (see below),
    because which lot a sale consumes changes the answer materially -- by tens
    of thousands, where lots were acquired at very different prices."""

    id: str
    underlying: str
    quantity: int
    acquired_on: date
    cost_per_share: Decimal
    fee: Decimal = ZERO
    source: ShareSource = ShareSource.OUTRIGHT_BUY
    assigning_position_id: str | None = None
    estimated: bool = False  # reconstructed from memory, not a broker record
    notes: str = ""

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError(f"{self.id}: share lot quantity must be positive")

    @property
    def cost(self) -> Decimal:
        return self.cost_per_share * self.quantity + self.fee


@dataclass
class ShareDisposal:
    """A sale of shares, either outright or by having calls exercised."""

    id: str
    underlying: str
    quantity: int
    disposed_on: date
    proceeds_per_share: Decimal
    fee: Decimal = ZERO
    kind: DisposalKind = DisposalKind.SOLD
    disposing_position_id: str | None = None
    # Explicit lot choice, when not using the account's matching rule.
    specific_lot_ids: tuple[str, ...] = ()
    notes: str = ""

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError(f"{self.id}: disposal quantity must be positive")

    @property
    def proceeds(self) -> Decimal:
        return self.proceeds_per_share * self.quantity - self.fee


@dataclass
class ShareAllocation:
    """Which lot a disposal consumed, and the P/L that fell out of it."""

    disposal_id: str
    lot_id: str
    quantity: int
    realized_pl: Decimal
    cost_basis: Decimal
    proceeds: Decimal
