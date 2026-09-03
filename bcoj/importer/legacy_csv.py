"""Parse the legacy spreadsheet export.

Faithful by design: this reads what the sheet says and changes nothing.
Corrections happen later, in the app -- see docs/legacy-format.md.

Columns are read positionally, because the header repeats the name "Quantity"
for both the opening and closing legs.
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from ..domain.enums import Direction, Right, Status
from ..domain.money import ZERO, parse_int, parse_money, q2
from ..domain.types import Position

# Positional layout of the export.
COL_ID = 0
COL_DATE = 1
COL_OPTION = 2  # composite label, redundant with symbol/expiry/strike/type
COL_GUID = 3
COL_ROLLED_GUID = 4
COL_SYMBOL = 5
COL_EXPIRATION = 6
COL_STRIKE = 7
COL_TYPE = 8
COL_QUANTITY = 9
COL_OPEN_U = 10
COL_BUY_WRITE = 11
COL_FEE = 12
COL_CLOSE_DATE = 13
COL_CLOSE_U = 14
COL_CLOSE_QUANTITY = 15
COL_C_FEE = 16
COL_STATUS = 17
COL_PL = 18
COL_OPEN = 19
COL_CLOSE = 20
COL_ASSIGNMENT = 21
COL_E_PL = 22
COL_E_CLOSING = 23
COL_COST_BASIS = 24
COL_PUT_RISK = 25
COL_ROLLED_ID = 26

EXPECTED_COLUMNS = 27

EXPECTED_HEADER = (
    "ID,Date,Option,GUID,Rolled GUID,Symbol,Expiration,Strike,Type,Quantity,"
    "Open_U,Buy Write,Fee,Close Date,Close_U,Quantity,C_Fee,Status,P/L,Open,"
    "Close,Assignment,E P/L,E Closing,Cost basis,Put Risk,Rolled ID"
)


@dataclass
class SheetValues:
    """The sheet's own derived columns, kept for the reconciliation diff.

    None means the cell was blank or "-".
    """

    pl: Decimal | None = None
    open_cash: Decimal | None = None
    close_cash: Decimal | None = None
    assignment: Decimal | None = None
    e_pl: Decimal | None = None
    e_closing: Decimal | None = None
    cost_basis: Decimal | None = None
    put_risk: Decimal | None = None
    close_u: Decimal | None = None


@dataclass
class ParsedRow:
    """One export row: the position we derived, plus what the sheet claimed."""

    line_no: int
    raw: tuple[str, ...]
    position: Position | None
    sheet: SheetValues
    buy_write: Decimal | None = None
    close_quantity: int | None = None
    errors: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.position is not None

    @property
    def row_id(self) -> str:
        if self.position is not None:
            return self.position.id
        if len(self.raw) > COL_GUID and self.raw[COL_GUID].strip():
            return self.raw[COL_GUID].strip()
        if self.raw:
            return self.raw[COL_ID].strip() or f"line {self.line_no}"
        return f"line {self.line_no}"


@dataclass
class ParseReport:
    rows: list[ParsedRow] = field(default_factory=list)
    header: tuple[str, ...] = ()
    header_matched: bool = True

    @property
    def parsed(self) -> list[ParsedRow]:
        return [r for r in self.rows if r.ok]

    @property
    def failed(self) -> list[ParsedRow]:
        return [r for r in self.rows if not r.ok]

    @property
    def positions(self) -> list[Position]:
        return [r.position for r in self.rows if r.position is not None]


def parse_rows(reader) -> ParseReport:
    """Parse an iterable of CSV row lists (as from ``csv.reader``)."""
    report = ParseReport()
    rows = iter(reader)

    try:
        header = next(rows)
    except StopIteration:
        return report

    report.header = tuple(cell.strip() for cell in header)
    report.header_matched = ",".join(report.header) == EXPECTED_HEADER

    for line_no, raw in enumerate(rows, start=2):
        if not any(cell.strip() for cell in raw):
            continue  # blank spacer line
        report.rows.append(_parse_row(line_no, raw))

    return report


def parse_file(path) -> ParseReport:
    import csv

    with open(path, newline="", encoding="utf-8-sig") as handle:
        return parse_rows(csv.reader(handle))


def _parse_row(line_no: int, raw_row) -> ParsedRow:
    raw = tuple(raw_row)
    errors: list[str] = []

    def cell(index: int) -> str:
        return raw[index].strip() if index < len(raw) else ""

    if len(raw) < EXPECTED_COLUMNS:
        errors.append(f"expected {EXPECTED_COLUMNS} columns, found {len(raw)}")

    sheet = SheetValues()
    for attr, index in (
        ("pl", COL_PL),
        ("open_cash", COL_OPEN),
        ("close_cash", COL_CLOSE),
        ("assignment", COL_ASSIGNMENT),
        ("e_pl", COL_E_PL),
        ("e_closing", COL_E_CLOSING),
        ("cost_basis", COL_COST_BASIS),
        ("put_risk", COL_PUT_RISK),
        ("close_u", COL_CLOSE_U),
    ):
        try:
            setattr(sheet, attr, parse_money(cell(index)))
        except ValueError as exc:
            errors.append(str(exc))

    position: Position | None = None
    buy_write: Decimal | None = None
    close_quantity: int | None = None

    try:
        buy_write = parse_money(cell(COL_BUY_WRITE))
        if buy_write is not None and buy_write == 0:
            buy_write = None  # the sheet writes 0 for "no share purchase"

        close_quantity = parse_int(cell(COL_CLOSE_QUANTITY))

        identifier = cell(COL_GUID) or cell(COL_ID)
        if not identifier:
            raise ValueError("row has no GUID or ID")

        signed_quantity = parse_int(cell(COL_QUANTITY))
        if signed_quantity is None:
            raise ValueError("missing quantity")
        if signed_quantity == 0:
            raise ValueError("quantity of zero")

        status = Status.parse(cell(COL_STATUS))
        strike = parse_money(cell(COL_STRIKE))
        if strike is None:
            raise ValueError("missing strike")

        open_price = parse_money(cell(COL_OPEN_U))
        if open_price is None:
            raise ValueError("missing Open_U")

        close_price = sheet.close_u
        closed_on = _parse_date(cell(COL_CLOSE_DATE))
        if status.is_open:
            # On an open row Close_U is a profit target, not a close price.
            close_price = None
            closed_on = None

        rolled_from = cell(COL_ROLLED_GUID) or None

        position = Position(
            id=identifier,
            underlying=cell(COL_SYMBOL).upper(),
            expiry=_require_date(cell(COL_EXPIRATION), "Expiration"),
            strike=strike,
            right=Right.parse(cell(COL_TYPE)),
            direction=Direction.from_quantity(signed_quantity),
            quantity=abs(signed_quantity),
            opened_on=_require_date(cell(COL_DATE), "Date"),
            open_price=open_price,
            open_fee=parse_money(cell(COL_FEE)) or ZERO,
            closed_on=closed_on,
            close_price=close_price,
            close_fee=parse_money(cell(COL_C_FEE)) or ZERO,
            status=status,
            rolled_from_id=rolled_from,
        )
    except ValueError as exc:
        errors.append(str(exc))
        position = None

    return ParsedRow(
        line_no=line_no,
        raw=raw,
        position=position,
        sheet=sheet,
        buy_write=buy_write,
        close_quantity=close_quantity,
        errors=tuple(errors),
    )


def _parse_date(text: str) -> date | None:
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"cannot parse date {text!r}")


def _require_date(text: str, label: str) -> date:
    value = _parse_date(text)
    if value is None:
        raise ValueError(f"missing {label}")
    return value


def derived_assignment(row: ParsedRow) -> Decimal:
    """Reconstruct the sheet's ``Assignment`` column from first principles.

    Share cash flow attributable to the row:
      * a ``Buy Write`` price buys quantity x multiplier shares (cash out)
      * an assigned put acquires them at the strike (cash out)
      * an assigned call disposes of them at the strike (cash in)

    A single row can do two of these at once -- a buy-write whose call is then
    exercised both buys and sells the shares -- which is why this returns a net
    rather than a single flow.

    Deriving it makes ``Assignment`` a seventh column to diff rather than a
    figure to trust.
    """
    position = row.position
    if position is None:
        return ZERO

    total = ZERO
    shares = Decimal(position.shares)

    if row.buy_write is not None:
        total -= row.buy_write * shares

    if position.status is Status.ASSIGNED:
        if position.right is Right.PUT:
            total -= position.strike * shares
        else:
            total += position.strike * shares

    return q2(total)
