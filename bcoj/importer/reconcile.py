"""Reconciliation: recompute every figure and diff it against the sheet.

This is the engine's acceptance test. Five years of hand-maintained P/L is a better
oracle than any fixture, so the engine is not trusted until it agrees with it
row by row -- or until each disagreement is explained.
"""

from dataclasses import dataclass, field
from decimal import Decimal

from ..domain.money import ZERO, fmt, q2
from ..engine.chains import ChainIndex
from ..engine.pnl import close_cash, open_cash, realized_pl
from ..engine.risk import put_risk
from ..engine.targets import target
from .legacy_csv import ParsedRow, derived_assignment

# Cent-level drift is the sheet rounding a derived cell; anything larger is a
# real disagreement that needs a human.
ROUNDING_TOLERANCE = Decimal("0.02")

EXACT = "exact"
ROUNDING = "rounding"
MISMATCH = "mismatch"
ABSENT = "absent"


@dataclass
class FieldDiff:
    field: str
    computed: Decimal | None
    sheet: Decimal | None

    @property
    def delta(self) -> Decimal | None:
        if self.computed is None or self.sheet is None:
            return None
        return q2(self.computed - self.sheet)

    @property
    def verdict(self) -> str:
        if self.computed is None or self.sheet is None:
            # Only a problem if exactly one side has a value.
            if self.computed is None and self.sheet is None:
                return EXACT
            if self.sheet is None:
                return ABSENT
            return MISMATCH
        delta = abs(self.delta)
        if delta == 0:
            return EXACT
        if delta <= ROUNDING_TOLERANCE:
            return ROUNDING
        return MISMATCH


@dataclass
class RowDiff:
    row_id: str
    line_no: int
    underlying: str
    diffs: list[FieldDiff] = field(default_factory=list)
    errors: tuple[str, ...] = ()

    @property
    def verdict(self) -> str:
        if self.errors:
            return MISMATCH
        verdicts = {d.verdict for d in self.diffs}
        if MISMATCH in verdicts:
            return MISMATCH
        if ABSENT in verdicts:
            return ABSENT
        if ROUNDING in verdicts:
            return ROUNDING
        return EXACT

    def problems(self) -> list[FieldDiff]:
        return [d for d in self.diffs if d.verdict in (MISMATCH, ROUNDING, ABSENT)]


@dataclass
class Reconciliation:
    rows: list[RowDiff] = field(default_factory=list)
    unparsed: list[ParsedRow] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        tally = {EXACT: 0, ROUNDING: 0, MISMATCH: 0, ABSENT: 0}
        for row in self.rows:
            tally[row.verdict] += 1
        return tally

    @property
    def clean(self) -> bool:
        tally = self.counts()
        return not self.unparsed and tally[MISMATCH] == 0 and tally[ABSENT] == 0

    def failures(self) -> list[RowDiff]:
        return [r for r in self.rows if r.verdict in (MISMATCH, ABSENT)]

    def rounding(self) -> list[RowDiff]:
        return [r for r in self.rows if r.verdict == ROUNDING]


def reconcile(parsed_rows) -> Reconciliation:
    """Diff computed figures against the sheet's own columns, per row."""
    result = Reconciliation()
    usable = [r for r in parsed_rows if r.position is not None]
    result.unparsed = [r for r in parsed_rows if r.position is None]

    index = ChainIndex(r.position for r in usable)

    for row in usable:
        position = row.position
        carry = index.carry(position)

        diffs = [
            FieldDiff("Open", open_cash(position), row.sheet.open_cash),
            FieldDiff("Close", close_cash(position), row.sheet.close_cash),
            FieldDiff("P/L", realized_pl(position), row.sheet.pl),
            FieldDiff("Cost basis", carry, row.sheet.cost_basis),
            FieldDiff("Assignment", derived_assignment(row), row.sheet.assignment),
            FieldDiff("Put Risk", put_risk(position), row.sheet.put_risk),
        ]

        if position.status.is_open:
            projection = target(position, carry)
            diffs.append(
                FieldDiff("E Closing", projection.expected_closing, row.sheet.e_closing)
            )
            diffs.append(FieldDiff("E P/L", projection.expected_pl, row.sheet.e_pl))
            diffs.append(
                FieldDiff("target price", projection.price, row.sheet.close_u)
            )
        else:
            # The sheet leaves the E columns at zero once a row is closed.
            diffs.append(FieldDiff("E Closing", ZERO, row.sheet.e_closing))
            diffs.append(FieldDiff("E P/L", ZERO, row.sheet.e_pl))

        result.rows.append(
            RowDiff(
                row_id=position.id,
                line_no=row.line_no,
                underlying=position.underlying,
                diffs=diffs,
                errors=row.errors,
            )
        )

    return result


def render(reconciliation: Reconciliation, limit: int = 40) -> str:
    """A human-readable report. The thing to actually read after an import."""
    lines: list[str] = []
    tally = reconciliation.counts()
    total = len(reconciliation.rows)

    lines.append("=" * 72)
    lines.append("RECONCILIATION")
    lines.append("=" * 72)
    lines.append(f"  rows parsed      {total}")
    lines.append(f"  exact            {tally[EXACT]}")
    lines.append(f"  rounding (<=2c)  {tally[ROUNDING]}")
    lines.append(f"  mismatch         {tally[MISMATCH]}")
    lines.append(f"  missing a cell   {tally[ABSENT]}")
    lines.append(f"  unparsed         {len(reconciliation.unparsed)}")
    lines.append("")

    if reconciliation.unparsed:
        lines.append("UNPARSED ROWS")
        for row in reconciliation.unparsed[:limit]:
            reasons = "; ".join(row.errors) or "unknown"
            lines.append(f"  line {row.line_no:>4}  {row.row_id:<40} {reasons}")
        lines.append("")

    failures = reconciliation.failures()
    if failures:
        lines.append(f"DISAGREEMENTS ({len(failures)})")
        for row in failures[:limit]:
            lines.append(f"  {row.row_id}  ({row.underlying}, line {row.line_no})")
            for diff in row.problems():
                lines.append(
                    f"      {diff.field:<14} computed {fmt(diff.computed):>15}"
                    f"   sheet {fmt(diff.sheet):>15}"
                    f"   delta {fmt(diff.delta):>12}"
                )
            for err in row.errors:
                lines.append(f"      ! {err}")
        if len(failures) > limit:
            lines.append(f"  ... and {len(failures) - limit} more")
        lines.append("")

    rounding = reconciliation.rounding()
    if rounding:
        lines.append(f"ROUNDING ONLY ({len(rounding)}) - accepted")
        for row in rounding[:limit]:
            fields = ", ".join(d.field for d in row.problems())
            lines.append(f"  {row.row_id}  ({row.underlying})  {fields}")
        if len(rounding) > limit:
            lines.append(f"  ... and {len(rounding) - limit} more")
        lines.append("")

    verdict = "CLEAN" if reconciliation.clean else "NEEDS REVIEW"
    lines.append(f"verdict: {verdict}")
    return "\n".join(lines)
