"""The operations a journal entry performs, as pure functions.

Each returns what should be written rather than writing it, so the arithmetic
is testable without a database and the storage layer stays a thin shim.

The share side is not optional. Assigning an option moves stock, and an
assignment that records the option but forgets the shares is exactly how a
spreadsheet loses track of share P/L -- so ``assign`` always produces the lot
or the disposal alongside the closed option.
"""

import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from ..domain.enums import (
    Direction,
    DisposalKind,
    Right,
    ShareSource,
    Status,
)
from ..domain.money import ZERO, q2
from ..domain.types import Position, ShareDisposal, ShareLot


def new_id() -> str:
    return uuid.uuid4().hex


@dataclass
class ActionResult:
    """What an action wants written. Nothing here has touched storage yet."""

    updated: list[Position] = field(default_factory=list)
    created: list[Position] = field(default_factory=list)
    lots: list[ShareLot] = field(default_factory=list)
    disposals: list[ShareDisposal] = field(default_factory=list)
    summary: str = ""

    @property
    def positions(self) -> list[Position]:
        return self.updated + self.created


class ActionError(ValueError):
    """The action cannot be applied to this position."""


def _require_open(position: Position, what: str) -> None:
    if not position.is_open:
        raise ActionError(
            f"cannot {what} {position.underlying} {position.strike}"
            f" {position.right.value}: it is already {position.status.value}"
        )


def _replace(position: Position, **changes) -> Position:
    """A copy with fields changed. Dataclass replace, minus the import dance."""
    from dataclasses import replace

    return replace(position, **changes)


# ---------------------------------------------------------------------------
# closing out


def close(
    position: Position,
    closed_on: date,
    close_price: Decimal,
    close_fee: Decimal = ZERO,
) -> ActionResult:
    """Buy or sell to close at a price."""
    _require_open(position, "close")
    if close_price < 0:
        raise ActionError("close price cannot be negative")

    updated = _replace(
        position,
        closed_on=closed_on,
        close_price=q2(close_price),
        close_fee=q2(close_fee),
        status=Status.CLOSED,
    )
    return ActionResult(updated=[updated], summary="closed")


def expire(
    position: Position, on: date | None = None, close_fee: Decimal = ZERO
) -> ActionResult:
    """Let it expire worthless: the whole credit realizes, usually fee-free."""
    _require_open(position, "expire")
    updated = _replace(
        position,
        closed_on=on or position.expiry,
        close_price=ZERO,
        close_fee=q2(close_fee),
        status=Status.EXPIRED,
    )
    return ActionResult(updated=[updated], summary="expired worthless")


def assign(
    position: Position,
    on: date | None = None,
    close_fee: Decimal = ZERO,
    share_fee: Decimal = ZERO,
    covering_lot_id: str | None = None,
) -> ActionResult:
    """Assignment or exercise: the option closes and stock moves.

    Which way the stock moves depends on both side and right:

        short put   -> shares acquired at the strike
        short call  -> shares delivered at the strike
        long put    -> shares delivered (you exercised)
        long call   -> shares acquired (you exercised)

    ``covering_lot_id`` earmarks the lot a short call delivers from. Without
    it, matching falls back to the account rule, which for a buy-write would
    reach past the shares actually bought for that call.
    """
    _require_open(position, "assign")

    when = on or position.expiry
    updated = _replace(
        position,
        closed_on=when,
        close_price=ZERO,
        close_fee=q2(close_fee),
        status=Status.ASSIGNED,
    )
    result = ActionResult(updated=[updated])

    shares = position.shares
    acquiring = (position.direction is Direction.SHORT) == (
        position.right is Right.PUT
    )

    if acquiring:
        source = (
            ShareSource.PUT_ASSIGNMENT
            if position.right is Right.PUT
            else ShareSource.CALL_EXERCISE
        )
        result.lots.append(
            ShareLot(
                id=new_id(),
                underlying=position.underlying,
                quantity=shares,
                acquired_on=when,
                cost_per_share=position.strike,
                fee=q2(share_fee),
                source=source,
                assigning_position_id=position.id,
                notes=f"{position.direction.value.lower()} "
                f"{position.right.value.lower()} at {position.strike}",
            )
        )
        result.summary = f"assigned - acquired {shares} shares at {position.strike}"
    else:
        earmark = covering_lot_id or position.share_lot_id
        result.disposals.append(
            ShareDisposal(
                id=new_id(),
                underlying=position.underlying,
                quantity=shares,
                disposed_on=when,
                proceeds_per_share=position.strike,
                fee=q2(share_fee),
                kind=(
                    DisposalKind.CALLED_AWAY
                    if position.direction is Direction.SHORT
                    else DisposalKind.SOLD
                ),
                disposing_position_id=position.id,
                specific_lot_ids=(earmark,) if earmark else (),
            )
        )
        result.summary = f"assigned - delivered {shares} shares at {position.strike}"

    return result


# ---------------------------------------------------------------------------
# rolling


def roll(
    position: Position,
    close_price: Decimal,
    new_expiry: date,
    new_strike: Decimal,
    new_price: Decimal,
    on: date | None = None,
    close_fee: Decimal = ZERO,
    new_fee: Decimal = ZERO,
    new_quantity: int | None = None,
    new_id_: str | None = None,
) -> ActionResult:
    """Close this leg and open its successor in one commit.

    Quantity may change -- rolls often resize -- so the caller states it
    explicitly rather than inheriting silently.
    """
    _require_open(position, "roll")
    if close_price < 0 or new_price < 0:
        raise ActionError("prices cannot be negative")

    quantity = position.quantity if new_quantity is None else new_quantity
    if quantity <= 0:
        raise ActionError("rolled quantity must be positive")

    when = on or date.today()
    closed = _replace(
        position,
        closed_on=when,
        close_price=q2(close_price),
        close_fee=q2(close_fee),
        status=Status.ROLLED,
    )
    successor = Position(
        id=new_id_ or new_id(),
        underlying=position.underlying,
        expiry=new_expiry,
        strike=q2(new_strike),
        right=position.right,
        direction=position.direction,
        quantity=quantity,
        multiplier=position.multiplier,
        opened_on=when,
        open_price=q2(new_price),
        open_fee=q2(new_fee),
        status=Status.OPEN,
        rolled_from_id=position.id,
        share_lot_id=position.share_lot_id,
        spread_group_id=position.spread_group_id,
        target_pct=position.target_pct,
    )

    direction = "out" if new_expiry > position.expiry else "in"
    resize = "" if quantity == position.quantity else f", {position.quantity}->{quantity}"
    return ActionResult(
        updated=[closed],
        created=[successor],
        summary=f"rolled {direction} to {new_strike} {new_expiry}{resize}",
    )


# ---------------------------------------------------------------------------
# splitting


def split(
    position: Position,
    quantity: int,
    on: date | None = None,
    ids: tuple[str, str] | None = None,
) -> ActionResult:
    """Divide a position into two, so the halves can take different paths.

    The case this exists for: part of a lot is assigned and the rest rolls on.

    Both halves keep the original open date and price, and share the opening
    fee pro-rata with the remainder landing on the first so the two sum exactly
    to what was paid. Each records the pre-split quantity, which is what lets
    chain history be divided between them instead of duplicated onto both.

    The parent is kept as a ``SPLIT`` record -- a journal should not delete
    something that happened -- and contributes nothing to any total thereafter.
    """
    _require_open(position, "split")
    if not 0 < quantity < position.quantity:
        raise ActionError(
            f"split quantity must be between 1 and {position.quantity - 1};"
            f" got {quantity}"
        )

    when = on or date.today()
    total = position.quantity
    first_qty, second_qty = quantity, total - quantity

    first_fee = q2(position.open_fee * Decimal(first_qty) / Decimal(total))
    second_fee = q2(position.open_fee - first_fee)

    first_id, second_id = ids if ids else (new_id(), new_id())

    def child(child_id: str, qty: int, fee: Decimal) -> Position:
        return Position(
            id=child_id,
            underlying=position.underlying,
            expiry=position.expiry,
            strike=position.strike,
            right=position.right,
            direction=position.direction,
            quantity=qty,
            multiplier=position.multiplier,
            opened_on=position.opened_on,
            open_price=position.open_price,
            open_fee=fee,
            close_fee=q2(
                position.close_fee * Decimal(qty) / Decimal(total)
            ),
            status=Status.OPEN,
            rolled_from_id=position.rolled_from_id,
            split_from_id=position.id,
            split_from_quantity=total,
            share_lot_id=position.share_lot_id,
            spread_group_id=position.spread_group_id,
            target_pct=position.target_pct,
            notes=position.notes,
        )

    superseded = _replace(
        position,
        status=Status.SPLIT,
        closed_on=when,
        close_price=ZERO,
        close_fee=ZERO,
    )

    return ActionResult(
        updated=[superseded],
        created=[
            child(first_id, first_qty, first_fee),
            child(second_id, second_qty, second_fee),
        ],
        summary=f"split {total} into {first_qty} and {second_qty}",
    )


# ---------------------------------------------------------------------------
# shares without an option


def buy_shares(
    underlying: str,
    quantity: int,
    price: Decimal,
    on: date,
    fee: Decimal = ZERO,
    lot_id: str | None = None,
) -> ActionResult:
    """Buy stock outright, with no option involved.

    The case a sheet of option rows has no room for, and the reason its share
    counts drift. A lot from here behaves like any other.
    """
    if quantity <= 0:
        raise ActionError("share quantity must be positive")
    if price < 0:
        raise ActionError("share price cannot be negative")

    return ActionResult(
        lots=[
            ShareLot(
                id=lot_id or new_id(),
                underlying=underlying.upper(),
                quantity=quantity,
                acquired_on=on,
                cost_per_share=q2(price),
                fee=q2(fee),
                source=ShareSource.OUTRIGHT_BUY,
            )
        ],
        summary=f"bought {quantity} {underlying.upper()} at {price}",
    )


def sell_shares(
    underlying: str,
    quantity: int,
    price: Decimal,
    on: date,
    fee: Decimal = ZERO,
    specific_lot_ids: tuple[str, ...] = (),
    disposal_id: str | None = None,
) -> ActionResult:
    if quantity <= 0:
        raise ActionError("share quantity must be positive")
    if price < 0:
        raise ActionError("share price cannot be negative")

    return ActionResult(
        disposals=[
            ShareDisposal(
                id=disposal_id or new_id(),
                underlying=underlying.upper(),
                quantity=quantity,
                disposed_on=on,
                proceeds_per_share=q2(price),
                fee=q2(fee),
                kind=DisposalKind.SOLD,
                specific_lot_ids=specific_lot_ids,
            )
        ],
        summary=f"sold {quantity} {underlying.upper()} at {price}",
    )


def buy_write(
    underlying: str,
    shares: int,
    share_price: Decimal,
    expiry: date,
    strike: Decimal,
    call_price: Decimal,
    on: date,
    contracts: int | None = None,
    multiplier: int = 100,
    share_fee: Decimal = ZERO,
    option_fee: Decimal = ZERO,
    lot_id: str | None = None,
    position_id: str | None = None,
) -> ActionResult:
    """Buy stock and write a call against it, in one commit.

    The call is linked to the lot, so it counts as covered and its shares are
    earmarked for delivery if it is exercised.
    """
    if shares <= 0:
        raise ActionError("share quantity must be positive")

    quantity = contracts if contracts is not None else shares // multiplier
    if quantity <= 0:
        raise ActionError(
            f"{shares} shares is not enough to cover a contract of {multiplier}"
        )
    if quantity * multiplier > shares:
        raise ActionError(
            f"{quantity} contracts need {quantity * multiplier} shares,"
            f" but only {shares} are being bought"
        )

    lot = ShareLot(
        id=lot_id or new_id(),
        underlying=underlying.upper(),
        quantity=shares,
        acquired_on=on,
        cost_per_share=q2(share_price),
        fee=q2(share_fee),
        source=ShareSource.BUY_WRITE,
    )
    call = Position(
        id=position_id or new_id(),
        underlying=underlying.upper(),
        expiry=expiry,
        strike=q2(strike),
        right=Right.CALL,
        direction=Direction.SHORT,
        quantity=quantity,
        multiplier=multiplier,
        opened_on=on,
        open_price=q2(call_price),
        open_fee=q2(option_fee),
        status=Status.OPEN,
        share_lot_id=lot.id,
    )
    return ActionResult(
        created=[call],
        lots=[lot],
        summary=f"buy-write: {shares} {underlying.upper()} at {share_price}"
        f" against {quantity} x {strike} calls",
    )
