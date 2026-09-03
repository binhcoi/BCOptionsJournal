"""Flag suspicious rows. Never correct them.

The sheet's arithmetic is sound but its semantics are not, because only option
trades got a row and everything else had to be disguised as one
(docs/legacy-format.md).
These checks surface what needs a human, and the app's data-health screen is
where they get resolved.
"""

from dataclasses import dataclass
from decimal import Decimal

from ..domain.money import q2
from .legacy_csv import ParsedRow

# Strikes are listed on a half-dollar grid or coarser. An off-grid value is
# not a strike at all: it is an average share price standing in for a share
# transaction the sheet had no row for.
STRIKE_GRID = Decimal("0.50")
DEFAULT_FEE_RATE = Decimal("0.65")

# Kinds, roughly by how much they matter.
DISGUISED_SHARE_ROW = "disguised_share_row"
PLACEHOLDER_ROW = "placeholder_row"
NEGATIVE_SHARE_COUNT = "negative_share_count"
QUANTITY_DROP_ON_ROLL = "quantity_drop_on_roll"
EXPIRY_BEFORE_OPEN = "expiry_before_open"
CLOSE_BEFORE_OPEN = "close_before_open"
MISSING_CLOSE_DATE = "missing_close_date"
CLOSE_QUANTITY_MISMATCH = "close_quantity_mismatch"
FEE_UNUSUAL = "fee_unusual"
CHAIN_PARENT_MISSING = "chain_parent_missing"
CHAIN_DATE_ORDER = "chain_date_order"


@dataclass
class Flag:
    entity_id: str
    kind: str
    detail: str
    line_no: int = 0

    def __str__(self) -> str:
        return f"{self.kind}: {self.entity_id} - {self.detail}"


def triage(parsed_rows) -> list[Flag]:
    """Run every check over the parsed rows."""
    rows = [r for r in parsed_rows if r.position is not None]
    flags: list[Flag] = []

    for row in rows:
        flags.extend(_row_flags(row))

    flags.extend(_chain_flags(rows))
    flags.extend(_share_balance_flags(rows))
    return flags


def _row_flags(row: ParsedRow) -> list[Flag]:
    position = row.position
    out: list[Flag] = []

    def flag(kind: str, detail: str) -> None:
        out.append(Flag(position.id, kind, detail, row.line_no))

    zero_strike = position.strike == 0
    zero_premium = position.open_price == 0
    off_grid = not zero_strike and (position.strike % STRIKE_GRID) != 0
    expiry_not_after_open = position.expiry <= position.opened_on
    fee_only_pl = q2(-(position.open_fee + position.close_fee))
    pl_is_fees_only = (
        row.sheet.pl is not None and row.sheet.pl == fee_only_pl and fee_only_pl != 0
    )

    if zero_strike and zero_premium:
        flag(
            PLACEHOLDER_ROW,
            f"strike 0, no premium, expiring {position.expiry} - a parking row",
        )
    else:
        # Each signal is weak alone; two or more means this row is standing in
        # for a share transaction (docs/legacy-format.md).
        signals = []
        if off_grid:
            signals.append(f"strike {position.strike} is not on a listed grid")
        if zero_premium:
            signals.append("no premium collected")
        if expiry_not_after_open:
            signals.append(
                f"expiry {position.expiry} not after open {position.opened_on}"
            )
        if pl_is_fees_only:
            signals.append("P/L is exactly the negated fees")

        if len(signals) >= 2:
            flag(
                DISGUISED_SHARE_ROW,
                "probably a share transaction, not an option: " + "; ".join(signals),
            )
        else:
            if off_grid:
                flag(DISGUISED_SHARE_ROW, signals[0])
            if position.expiry < position.opened_on:
                flag(
                    EXPIRY_BEFORE_OPEN,
                    f"expiry {position.expiry} precedes open {position.opened_on}",
                )

    if position.closed_on is not None and position.closed_on < position.opened_on:
        flag(
            CLOSE_BEFORE_OPEN,
            f"closed {position.closed_on} before opening {position.opened_on}",
        )

    if not position.status.is_open and position.closed_on is None:
        flag(MISSING_CLOSE_DATE, f"status {position.status.value} with no close date")

    if row.close_quantity is not None and not position.status.is_open:
        if row.close_quantity != -position.signed_quantity:
            flag(
                CLOSE_QUANTITY_MISMATCH,
                f"opened {position.signed_quantity}, closed {row.close_quantity}"
                " - a partial event may have been flattened",
            )

    out.extend(_fee_flags(row))
    return out


def _fee_flags(row: ParsedRow) -> list[Flag]:
    """Fees inconsistent with the contract count.

    A zero fee is normal (expiries, assignments, cheap closes). A fee that is
    present but far off the per-contract rate is the fingerprint of a reshaped
    row -- an opening fee matching the contract count beside a closing fee
    matching some smaller number of them.
    """
    position = row.position
    expected = DEFAULT_FEE_RATE * position.quantity
    out: list[Flag] = []

    for label, fee in (("opening", position.open_fee), ("closing", position.close_fee)):
        if fee <= 0:
            continue
        if fee < expected / 2 or fee > expected * Decimal("1.5"):
            implied = q2(fee / Decimal(position.quantity))
            out.append(
                Flag(
                    position.id,
                    FEE_UNUSUAL,
                    f"{label} fee {fee} on {position.quantity} contracts "
                    f"({implied}/contract, expected ~{DEFAULT_FEE_RATE})",
                    row.line_no,
                )
            )
    return out


def _chain_flags(rows) -> list[Flag]:
    """Checks that need the whole set: broken links, shrinking rolls."""
    by_id = {r.position.id: r for r in rows}
    out: list[Flag] = []

    for row in rows:
        parent_id = row.position.rolled_from_id
        if not parent_id:
            continue

        parent_row = by_id.get(parent_id)
        if parent_row is None:
            out.append(
                Flag(
                    row.position.id,
                    CHAIN_PARENT_MISSING,
                    f"rolled from {parent_id}, which is not in the file",
                    row.line_no,
                )
            )
            continue

        parent = parent_row.position
        child = row.position

        if child.quantity < parent.quantity:
            out.append(
                Flag(
                    child.id,
                    QUANTITY_DROP_ON_ROLL,
                    f"rolled from {parent.id} at {parent.quantity} contracts into "
                    f"{child.quantity} - part of the lot may have been assigned",
                    row.line_no,
                )
            )

        if child.opened_on < parent.opened_on:
            out.append(
                Flag(
                    child.id,
                    CHAIN_DATE_ORDER,
                    f"opens {child.opened_on}, before its predecessor "
                    f"{parent.id} on {parent.opened_on}",
                    row.line_no,
                )
            )

    return out


def _share_balance_flags(rows) -> list[Flag]:
    """A ticker whose share count goes negative is missing an acquisition.

    The sheet divided by such a count and produced plausible-looking numbers;
    here it is a blocking flag.
    """
    from .legacy_formulas import share_count, symbols

    out: list[Flag] = []
    for symbol in symbols(rows):
        subset = [r for r in rows if r.position.underlying == symbol]
        count = share_count(subset)
        if count < 0:
            out.append(
                Flag(
                    symbol,
                    NEGATIVE_SHARE_COUNT,
                    f"net share count is {count}; {-count} shares were acquired "
                    "without being recorded",
                )
            )
    return out


def summarize(flags) -> dict[str, int]:
    tally: dict[str, int] = {}
    for flag in flags:
        tally[flag.kind] = tally.get(flag.kind, 0) + 1
    return dict(sorted(tally.items(), key=lambda kv: (-kv[1], kv[0])))
