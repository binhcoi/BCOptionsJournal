"""The spreadsheet's own summary formulas, reimplemented for diffing.

These are deliberately *not* in bcoj.engine: they are what the sheet computes,
including its bugs, so the import can prove it read the data correctly before
the corrected figures replace them.

Verified by reproducing a real panel figure for figure before replacing it.
"""

from dataclasses import dataclass
from decimal import Decimal

from ..domain.enums import Right, Status
from ..domain.money import ZERO, q2


def _sum(values) -> Decimal:
    return q2(sum((v for v in values if v is not None), ZERO))


def _open_rows(rows):
    return [
        r for r in rows if r.position is not None and r.position.status.is_open
    ]


@dataclass
class Panel:
    """A summary panel, portfolio-wide or filtered to one symbol."""

    symbol: str | None
    realized_pl: Decimal
    assignment: Decimal
    adjusted_pl: Decimal
    unrealised_pl: Decimal
    current_premium: Decimal
    current_cost_basis: Decimal
    current_put_risk: Decimal
    expected_pl: Decimal
    expected_closing: Decimal
    open_calls: int
    open_puts: int
    shares: int
    adjusted_unit_price: Decimal | None
    adjusted_cover_call: Decimal | None
    # Diagnostics the sheet cannot show.
    cover_call_double_count: Decimal
    adjusted_cover_call_corrected: Decimal | None


def panel(rows, symbol: str | None = None) -> Panel:
    """Reproduce the sheet's summary panel from parsed rows."""
    if symbol is not None:
        wanted = symbol.upper()
        rows = [
            r
            for r in rows
            if r.position is not None and r.position.underlying == wanted
        ]

    realized = _sum(r.sheet.pl for r in rows)
    assignment = _sum(r.sheet.assignment for r in rows)
    open_rows = _open_rows(rows)

    current_premium = _sum(r.sheet.open_cash for r in open_rows)
    expected_pl = _sum(r.sheet.e_pl for r in open_rows)

    shares = share_count(rows)

    call_pl = _sum(
        r.sheet.pl
        for r in rows
        if r.position is not None and r.position.right is Right.CALL
    )
    assigned_pl = _sum(
        r.sheet.pl
        for r in rows
        if r.position is not None and r.position.status is Status.ASSIGNED
    )
    # Rows that are both a call and assigned satisfy both SUMIFs in the
    # sheet's formula and are therefore counted twice.
    overlap = _sum(
        r.sheet.pl
        for r in rows
        if r.position is not None
        and r.position.right is Right.CALL
        and r.position.status is Status.ASSIGNED
    )

    aup = None
    acc = None
    acc_fixed = None
    if shares != 0:
        divisor = Decimal(shares)
        aup = q2(-(realized + assignment) / divisor)
        acc = q2(-(assignment + call_pl + assigned_pl) / divisor)
        acc_fixed = q2(-(assignment + call_pl + assigned_pl - overlap) / divisor)

    return Panel(
        symbol=symbol.upper() if symbol else None,
        realized_pl=realized,
        assignment=assignment,
        adjusted_pl=q2(realized + assignment),
        unrealised_pl=q2(current_premium + expected_pl),
        current_premium=current_premium,
        current_cost_basis=_sum(r.sheet.cost_basis for r in open_rows),
        current_put_risk=_sum(r.sheet.put_risk for r in open_rows),
        expected_pl=expected_pl,
        expected_closing=_sum(r.sheet.e_closing for r in open_rows),
        open_calls=sum(
            r.position.quantity
            for r in open_rows
            if r.position.right is Right.CALL
        ),
        open_puts=sum(
            r.position.quantity
            for r in open_rows
            if r.position.right is Right.PUT
        ),
        shares=shares,
        adjusted_unit_price=aup,
        adjusted_cover_call=acc,
        cover_call_double_count=overlap,
        adjusted_cover_call_corrected=acc_fixed,
    )


def share_count(rows) -> int:
    """Net shares implied by the sheet, using its own conventions.

    Reproduces the panel's ``Shares`` cell -- including an impossible negative
    count, which is the point: a number has to be reproduced before it can be
    diagnosed.
    """
    total = 0
    for row in rows:
        position = row.position
        if position is None:
            continue
        if row.buy_write is not None:
            total += position.shares
        if position.status is Status.ASSIGNED:
            if position.right is Right.PUT:
                total += position.shares
            else:
                total -= position.shares
    return total


def symbols(rows) -> tuple[str, ...]:
    seen = {r.position.underlying for r in rows if r.position is not None}
    return tuple(sorted(seen))
