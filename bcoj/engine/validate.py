"""Entry validation.

Every date error in the legacy data would have been caught here: an expiry
before the open, a close before the open, a leg opening before its own
predecessor. A journal kept by hand for years accumulates these silently, and
they are far cheaper to refuse at entry than to find later.

Problems come in two strengths. An **error** is arithmetically impossible or
would corrupt a total, and the app refuses it. A **warning** is merely unusual
-- a fee off the going rate, a strike off the listed grid -- and the app
accepts it with a note, because unusual things genuinely happen.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from ..domain.enums import Direction, Right, Status
from ..domain.money import q2
from ..domain.types import Position

ERROR = "error"
WARNING = "warning"

DEFAULT_FEE_RATE = Decimal("0.65")
STRIKE_GRID = Decimal("0.50")
# Beyond this a "fee" is more likely a mistyped price.
FEE_SANITY_MULTIPLE = Decimal("20")


@dataclass(frozen=True)
class Problem:
    level: str
    field: str
    message: str

    @property
    def blocking(self) -> bool:
        return self.level == ERROR


def validate_position(
    position: Position,
    existing=None,
    fee_rate: Decimal = DEFAULT_FEE_RATE,
) -> list[Problem]:
    """Check one position, optionally against the positions already recorded.

    ``existing`` enables the checks that need context: a duplicate, or a leg
    that opens before the predecessor it rolled from.
    """
    problems: list[Problem] = []

    def error(field: str, message: str) -> None:
        problems.append(Problem(ERROR, field, message))

    def warn(field: str, message: str) -> None:
        problems.append(Problem(WARNING, field, message))

    # --- identity and size
    if not position.underlying.strip():
        error("underlying", "a ticker is required")
    if position.quantity <= 0:
        error("quantity", "quantity must be at least 1")
    if position.multiplier <= 0:
        error("multiplier", "multiplier must be positive")

    # --- prices and fees
    if position.open_price < 0:
        error("open_price", "premium cannot be negative")
    if position.close_price is not None and position.close_price < 0:
        error("close_price", "close price cannot be negative")
    if position.open_fee < 0 or position.close_fee < 0:
        error("open_fee", "fees cannot be negative")
    if position.strike < 0:
        error("strike", "strike cannot be negative")

    # --- dates
    if position.expiry < position.opened_on:
        error(
            "expiry",
            f"expiry {position.expiry} is before the open date"
            f" {position.opened_on}",
        )
    elif position.expiry == position.opened_on:
        warn("expiry", "opens and expires the same day")

    if position.closed_on is not None:
        if position.closed_on < position.opened_on:
            error(
                "closed_on",
                f"closed {position.closed_on} before opening"
                f" {position.opened_on}",
            )
        if position.closed_on > position.expiry:
            warn(
                "closed_on",
                f"closed {position.closed_on}, after the {position.expiry} expiry",
            )

    # --- status coherence
    if position.status.is_open:
        if position.closed_on is not None or position.close_price is not None:
            error("status", "an open position cannot have a close")
    elif not position.status.is_superseded:
        if position.closed_on is None:
            error("closed_on", f"{position.status.value} needs a close date")
        if position.close_price is None:
            error("close_price", f"{position.status.value} needs a close price")

    if position.status is Status.ASSIGNED and position.close_price not in (
        None,
        Decimal("0"),
    ):
        warn(
            "close_price",
            "an assignment normally closes at zero, not"
            f" {position.close_price}",
        )

    # --- plausibility
    if position.strike > 0 and (position.strike % STRIKE_GRID) != 0:
        warn(
            "strike",
            f"{position.strike} is not on a listed strike increment"
            " - is this really an option?",
        )

    expected_fee = fee_rate * position.quantity
    for label, fee in (
        ("open_fee", position.open_fee),
        ("close_fee", position.close_fee),
    ):
        if fee > 0 and expected_fee > 0:
            if fee > expected_fee * FEE_SANITY_MULTIPLE:
                error(label, f"a fee of {fee} on {position.quantity} contracts"
                             " looks like a price in the wrong field")
            elif fee > expected_fee * Decimal("1.5") or fee < expected_fee / 2:
                warn(
                    label,
                    f"{fee} on {position.quantity} contracts is"
                    f" {q2(fee / position.quantity)}/contract,"
                    f" not the usual {fee_rate}",
                )

    if position.direction is Direction.SHORT and position.right is Right.CALL:
        pass  # whether the call is covered is a ticker-level fact: shares held

    problems.extend(_contextual(position, existing or ()))
    return problems


def _contextual(position: Position, existing) -> list[Problem]:
    """Checks that need to see the rest of the book."""
    problems: list[Problem] = []
    by_id = {p.id: p for p in existing}

    parent_id = position.rolled_from_id or position.split_from_id
    parent = by_id.get(parent_id) if parent_id else None
    if parent_id and parent is None:
        problems.append(
            Problem(WARNING, "rolled_from_id",
                    f"predecessor {parent_id} is not on file")
        )
    elif parent is not None:
        if position.opened_on < parent.opened_on:
            problems.append(
                Problem(
                    ERROR, "opened_on",
                    f"opens {position.opened_on}, before its predecessor"
                    f" on {parent.opened_on}",
                )
            )
        if parent.underlying != position.underlying:
            problems.append(
                Problem(ERROR, "underlying",
                        f"predecessor is {parent.underlying},"
                        f" not {position.underlying}")
            )
        if parent.right is not position.right:
            problems.append(
                Problem(WARNING, "right",
                        f"rolling a {parent.right.value} into a"
                        f" {position.right.value}")
            )

    duplicate = _find_duplicate(position, existing)
    if duplicate is not None:
        problems.append(
            Problem(
                WARNING, "duplicate",
                f"an identical open position was already entered"
                f" ({duplicate.id[:8]}) - entered twice?",
            )
        )
    return problems


def _find_duplicate(position: Position, existing):
    """An open position identical in every field a human would type."""
    for other in existing:
        if other.id == position.id or not other.is_open:
            continue
        if (
            other.underlying == position.underlying
            and other.expiry == position.expiry
            and other.strike == position.strike
            and other.right is position.right
            and other.direction is position.direction
            and other.quantity == position.quantity
            and other.opened_on == position.opened_on
            and other.open_price == position.open_price
        ):
            return other
    return None


def errors(problems) -> list[Problem]:
    return [p for p in problems if p.blocking]


def warnings(problems) -> list[Problem]:
    return [p for p in problems if not p.blocking]


def expiring(positions, today: date | None = None, within_days: int = 0):
    """Open positions at or past expiry: the action-needed queue.

    With no market data the app cannot know whether an expired short put
    expired worthless or was assigned, so it has to ask. ``within_days``
    widens the window to show what is about to need attention.
    """
    today = today or date.today()
    out = []
    for position in positions:
        if not position.is_open:
            continue
        days = (position.expiry - today).days
        if days <= within_days:
            out.append((position, days))
    out.sort(key=lambda pair: (pair[1], pair[0].underlying))
    return out
