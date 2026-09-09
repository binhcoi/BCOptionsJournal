"""Data health: what in the journal does not add up.

Every check here is over records as entered. None of them changes anything;
each names the record, says what is wrong, and points at the page where it
can be fixed. The guards at entry stop most of these from ever happening --
this is for what slipped past them, was imported, or was true once and is
not any more (an option past its expiry with no outcome recorded).
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from ..domain.enums import Status
from ..domain.types import Position

DEFAULT_FEE_RATE = Decimal("0.65")

ERROR = "error"
WARNING = "warning"

# An import flag that has a live check is only shown while the live check
# still fires for that record; once the data is fixed, the flag is cleared.
LIVE_FOR_IMPORT = {
    "chain_date_order": "roll_dates",
    "close_before_open": "date_order",
    "expiry_before_open": "date_order",
    "missing_close_date": "missing_close",
    "negative_share_count": "blocked_shares",
}


@dataclass(frozen=True)
class Issue:
    kind: str
    severity: str
    entity_type: str      # position | share_lot | share_disposal | ticker
    entity_id: str
    detail: str
    href: str             # where to go to fix it


def fee_unusual(p: Position, fee_rate: Decimal = DEFAULT_FEE_RATE) -> bool:
    """A fee present but far off the per-contract rate. Zero is normal
    (expiries, assignments). The same rule the importer and the entry
    validator apply, so the three agree on what is odd."""
    expected = fee_rate * p.quantity
    return any(fee > 0 and (fee < expected / 2 or fee > expected * Decimal("1.5"))
               for fee in (p.open_fee, p.close_fee))


def _import_flag_still_true(flag, by_id, fee_rate) -> bool | None:
    """Re-check an import flag against the record as it now stands. None
    means the kind has no live re-check and the flag stays until dismissed."""
    p = by_id.get(flag["entity_id"])
    if p is None:
        return None
    if flag["kind"] == "fee_unusual":
        return fee_unusual(p, fee_rate)
    if flag["kind"] == "quantity_drop_on_roll":
        parent = by_id.get(p.rolled_from_id) if p.rolled_from_id else None
        return parent is not None and p.quantity < parent.quantity
    return None


def check(positions, lots, disposals, *, blocked: dict | None = None,
          import_flags=(), today: date | None = None,
          fee_rate: Decimal = DEFAULT_FEE_RATE) -> list[Issue]:
    """Every issue found, errors first. ``blocked`` maps a ticker to the
    reason its share matching failed."""
    today = today or date.today()
    by_id = {p.id: p for p in positions}
    lot_ids = {l.id for l in lots}
    issues: list[Issue] = []

    def pos_href(p: Position) -> str:
        return f"/position/{p.id}"

    def raw_href(p: Position) -> str:
        return f"/position/{p.id}#raw"

    def data_href(underlying: str) -> str:
        return f"/shares/{underlying}/data"

    # --- dates that have not happened
    for p in positions:
        if p.opened_on > today:
            issues.append(Issue("future_date", ERROR, "position", p.id,
                                f"opened on {p.opened_on}, which has not come", raw_href(p)))
        if p.closed_on and p.closed_on > today:
            issues.append(Issue("future_date", ERROR, "position", p.id,
                                f"closed on {p.closed_on}, which has not come", raw_href(p)))
    for l in lots:
        if l.acquired_on > today:
            issues.append(Issue("future_date", ERROR, "share_lot", l.id,
                                f"{l.underlying} lot dated {l.acquired_on}, which has not come",
                                data_href(l.underlying)))
    for d in disposals:
        if d.disposed_on > today:
            issues.append(Issue("future_date", ERROR, "share_disposal", d.id,
                                f"{d.underlying} sale dated {d.disposed_on}, which has not come",
                                data_href(d.underlying)))

    # --- a record whose own dates disagree
    for p in positions:
        if p.expiry < p.opened_on:
            issues.append(Issue("date_order", ERROR, "position", p.id,
                                f"expires {p.expiry}, before it opened on {p.opened_on}", raw_href(p)))
        if p.closed_on and p.closed_on < p.opened_on:
            issues.append(Issue("date_order", ERROR, "position", p.id,
                                f"closed {p.closed_on}, before it opened on {p.opened_on}", raw_href(p)))
        if not p.is_open and p.status is not Status.SPLIT and p.closed_on is None:
            issues.append(Issue("missing_close", ERROR, "position", p.id,
                                f"{p.status.value.lower()} but no close date is recorded", raw_href(p)))

    # --- options past expiry with no outcome
    for p in positions:
        if p.is_open and p.expiry < today:
            issues.append(Issue("past_expiry", ERROR, "position", p.id,
                                f"expired {p.expiry} and no outcome is recorded",
                                f"/position/{p.id}?do=expire"))

    # --- share matching blocked
    for name, reason in sorted((blocked or {}).items()):
        issues.append(Issue("blocked_shares", ERROR, "ticker", name, reason, data_href(name)))

    # --- links to records that are not there
    for p in positions:
        for field_name, ref in (("rolled_from_id", p.rolled_from_id),
                                ("split_from_id", p.split_from_id)):
            if ref and ref not in by_id:
                issues.append(Issue("dangling_link", ERROR, "position", p.id,
                                    f"{field_name} points at a position that does not exist",
                                    pos_href(p)))
    for l in lots:
        if l.assigning_position_id and l.assigning_position_id not in by_id:
            issues.append(Issue("dangling_link", WARNING, "share_lot", l.id,
                                f"{l.underlying} lot came from an assignment that is gone",
                                data_href(l.underlying)))
    for d in disposals:
        if d.disposing_position_id and d.disposing_position_id not in by_id:
            issues.append(Issue("dangling_link", WARNING, "share_disposal", d.id,
                                f"{d.underlying} sale came from a call that is gone",
                                data_href(d.underlying)))
        for lot_id in d.specific_lot_ids:
            if lot_id not in lot_ids:
                issues.append(Issue("dangling_link", ERROR, "share_disposal", d.id,
                                    f"{d.underlying} sale is pinned to a lot that does not exist",
                                    data_href(d.underlying)))

    # --- chains that do not line up
    for p in positions:
        if p.rolled_from_id and p.rolled_from_id in by_id:
            prev = by_id[p.rolled_from_id]
            if prev.closed_on and prev.closed_on != p.opened_on:
                issues.append(Issue("roll_dates", WARNING, "position", p.id,
                                    f"opened {p.opened_on} but the leg it rolled from closed "
                                    f"{prev.closed_on}", raw_href(p)))
            if prev.is_open:
                issues.append(Issue("roll_dates", ERROR, "position", p.id,
                                    "rolled from a leg that is still open", pos_href(prev)))
    for parent in positions:
        if parent.status is Status.SPLIT:
            halves = [c for c in positions if c.split_from_id == parent.id]
            total = sum(c.quantity for c in halves)
            if total != parent.quantity:
                issues.append(Issue("split_sum", ERROR, "position", parent.id,
                                    f"split into {total} contract(s) but held {parent.quantity}",
                                    pos_href(parent)))

    # --- the same trade twice
    seen: dict[tuple, Position] = {}
    for p in sorted(positions, key=lambda p: (p.opened_on, p.id)):
        key = (p.underlying, p.expiry, p.strike, p.right, p.direction, p.quantity,
               p.opened_on, p.open_price)
        if key in seen and p.split_from_id is None and seen[key].split_from_id is None:
            issues.append(Issue("duplicate", WARNING, "position", p.id,
                                f"same terms, date and price as {seen[key].id[:8]}", pos_href(p)))
        seen.setdefault(key, p)

    # --- things known to be approximate
    for l in lots:
        if l.estimated:
            issues.append(Issue("estimated", WARNING, "share_lot", l.id,
                                f"{l.underlying} lot of {l.quantity} was reconstructed, not recorded: "
                                "confirm it against a statement",
                                f"{data_href(l.underlying)}#lot-{l.id}"))
    live = {(i.kind, i.entity_id) for i in issues}
    for f in import_flags:
        superseded_by = LIVE_FOR_IMPORT.get(f["kind"])
        if superseded_by and (superseded_by, f["entity_id"]) not in live:
            continue        # the data has been fixed; see cleared_flags()
        if _import_flag_still_true(f, by_id, fee_rate) is False:
            continue        # likewise, re-checked against the record itself
        issues.append(Issue(f"import:{f['kind']}", WARNING, f["entity_type"], f["entity_id"],
                            f["detail"] or f["kind"],
                            f"/position/{f['entity_id']}#raw" if f["entity_type"] == "position"
                            else "/shares"))

    issues.sort(key=lambda i: (0 if i.severity == ERROR else 1, i.kind, i.entity_id))
    return issues


def cleared_flags(import_flags, issues) -> list:
    """Import flags that ``check`` left out: fixed in the data, safe to resolve."""
    shown = {(i.kind, i.entity_id) for i in issues}
    return [f for f in import_flags if ("import:" + f["kind"], f["entity_id"]) not in shown]


def summary(issues) -> dict[str, int]:
    out: dict[str, int] = {}
    for i in issues:
        out[i.kind] = out.get(i.kind, 0) + 1
    return out
