"""Derive share lots and disposals from the option rows.

The legacy sheet has no row type for shares, so share activity is implied by
two columns (see docs/legacy-format.md):

  * ``Buy Write`` -- a per-share price paid alongside the option
  * an ``Assigned`` status -- puts acquire at the strike, calls dispose at it

A single row can do both, buying shares for a covered call and having them
called away, in which case ``Assignment`` holds the net of the two flows.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from ..domain.enums import DisposalKind, MatchingRule, Right, ShareSource, Status
from ..domain.money import ZERO, q2
from ..domain.types import ShareDisposal, ShareLot
from ..engine.shares import InsufficientSharesError, MatchResult, match
from .estimates import load as load_estimates


@dataclass
class DerivedShares:
    lots: list[ShareLot]
    disposals: list[ShareDisposal]

    def underlyings(self) -> tuple[str, ...]:
        names = {l.underlying for l in self.lots} | {
            d.underlying for d in self.disposals
        }
        return tuple(sorted(names))

    def for_underlying(self, underlying: str) -> "DerivedShares":
        u = underlying.upper()
        return DerivedShares(
            lots=[l for l in self.lots if l.underlying == u],
            disposals=[d for d in self.disposals if d.underlying == u],
        )


def derive(
    rows, include_estimates: bool = False, estimates=None
) -> DerivedShares:
    """Build share lots and disposals from parsed option rows.

    ``estimates`` overrides the configured reconstructed lots; when omitted and
    ``include_estimates`` is set, they are read from config (see
    ``bcoj.importer.estimates``).
    """
    lots: list[ShareLot] = []
    disposals: list[ShareDisposal] = []

    for row in rows:
        position = row.position
        if position is None:
            continue

        shares = position.shares

        if row.buy_write is not None:
            lots.append(
                ShareLot(
                    id=f"{position.id}:bw",
                    underlying=position.underlying,
                    quantity=shares,
                    acquired_on=position.opened_on,
                    cost_per_share=row.buy_write,
                    source=ShareSource.BUY_WRITE,
                    assigning_position_id=position.id,
                    notes="Buy-write recorded on the option row",
                )
            )

        if position.status is Status.ASSIGNED:
            when = position.closed_on or position.expiry
            if position.right is Right.PUT:
                lots.append(
                    ShareLot(
                        id=f"{position.id}:assign",
                        underlying=position.underlying,
                        quantity=shares,
                        acquired_on=when,
                        cost_per_share=position.strike,
                        source=ShareSource.PUT_ASSIGNMENT,
                        assigning_position_id=position.id,
                        notes="Short put assigned",
                    )
                )
            else:
                # A buy-write earmarks its own shares: the ones bought for that
                # call are the ones delivered against it. Plain FIFO would
                # reach past them to an older, cheaper lot and report a gain
                # that never happened.
                earmarked = ()
                if row.buy_write is not None:
                    earmarked = (f"{position.id}:bw",)

                disposals.append(
                    ShareDisposal(
                        id=f"{position.id}:called",
                        underlying=position.underlying,
                        quantity=shares,
                        disposed_on=when,
                        proceeds_per_share=position.strike,
                        kind=DisposalKind.CALLED_AWAY,
                        disposing_position_id=position.id,
                        specific_lot_ids=earmarked,
                        notes="Short call assigned"
                        + (" (buy-write shares)" if earmarked else ""),
                    )
                )

    if include_estimates:
        configured = load_estimates() if estimates is None else estimates
        lots.extend(_estimated_lots(lots, disposals, configured))

    return DerivedShares(lots=lots, disposals=disposals)


def _estimated_lots(lots, disposals, estimates) -> list[ShareLot]:
    """Seed reconstructed lots, but only where the record is short.

    Guarded so re-running never stacks duplicates: an estimate is added only
    when that ticker is actually in deficit, and never for more than the
    shortfall.
    """
    out: list[ShareLot] = []
    for estimate in estimates:
        acquired = sum(
            l.quantity for l in lots if l.underlying == estimate.underlying
        )
        disposed = sum(
            d.quantity for d in disposals if d.underlying == estimate.underlying
        )
        deficit = disposed - acquired
        if deficit <= 0:
            continue

        earliest = min(
            (
                d.disposed_on
                for d in disposals
                if d.underlying == estimate.underlying
            ),
            default=date(1970, 1, 1),
        )
        out.append(
            ShareLot(
                id=f"estimated:{estimate.underlying}",
                underlying=estimate.underlying,
                quantity=min(deficit, estimate.quantity),
                # Dated just before the first disposal that needs it.
                acquired_on=earliest,
                cost_per_share=estimate.cost_per_share,
                source=ShareSource.OUTRIGHT_BUY,
                estimated=True,
                notes=estimate.note,
            )
        )
    return out


@dataclass
class ShareSummary:
    underlying: str
    acquired: int
    disposed: int
    net: int
    realized: Decimal
    open_quantity: int
    open_cost: Decimal
    error: str = ""

    @property
    def balanced(self) -> bool:
        return self.net >= 0 and not self.error


def summarize(
    derived: DerivedShares, rule: MatchingRule = MatchingRule.FIFO
) -> list[ShareSummary]:
    """Per-ticker share P/L under the chosen matching rule."""
    out: list[ShareSummary] = []

    for underlying in derived.underlyings():
        subset = derived.for_underlying(underlying)
        acquired = sum(l.quantity for l in subset.lots)
        disposed = sum(d.quantity for d in subset.disposals)

        realized = ZERO
        open_quantity = acquired - disposed
        open_cost = ZERO
        error = ""

        try:
            result: MatchResult = match(subset.lots, subset.disposals, rule)
            realized = result.realized
            open_quantity = result.open_quantity
            open_cost = result.open_cost
        except InsufficientSharesError as exc:
            error = str(exc)

        out.append(
            ShareSummary(
                underlying=underlying,
                acquired=acquired,
                disposed=disposed,
                net=acquired - disposed,
                realized=q2(realized),
                open_quantity=open_quantity,
                open_cost=open_cost,
                error=error,
            )
        )

    return out
