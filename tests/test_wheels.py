"""Wheel tests: what a ticker's shares made, options and stock together.
Hand-computed."""

import unittest
from datetime import date
from decimal import Decimal

from bcoj.domain.enums import Direction, DisposalKind, Right, ShareSource, Status
from bcoj.domain.types import Position, ShareDisposal, ShareLot
from bcoj.engine.chains import ChainIndex
from bcoj.engine.wheels import by_ticker, lot_views, option_pl_by_ticker, realized_shares

D = Decimal


def pos(**kw) -> Position:
    base = dict(id="p", underlying="ACME", expiry=date(2026, 3, 20), strike=D("35"),
                right=Right.PUT, direction=Direction.SHORT, quantity=1,
                opened_on=date(2026, 1, 5), open_price=D("2.00"), open_fee=D("0.65"),
                status=Status.OPEN)
    base.update(kw)
    return Position(**base)


class TestFullWheel(unittest.TestCase):
    """Put assigned -> two covered calls -> called away."""

    def setUp(self):
        self.put = pos(id="put", close_price=D("0"), close_fee=D("0"),
                       status=Status.ASSIGNED, closed_on=date(2026, 3, 20))
        self.lot = ShareLot(id="lot", underlying="ACME", quantity=100,
                            acquired_on=date(2026, 3, 20), cost_per_share=D("35"),
                            source=ShareSource.PUT_ASSIGNMENT, assigning_position_id="put")
        self.call1 = pos(id="c1", right=Right.CALL, strike=D("38"), opened_on=date(2026, 3, 23),
                         expiry=date(2026, 4, 17), open_price=D("1.00"), close_price=D("0.30"),
                         close_fee=D("0.65"), status=Status.CLOSED, closed_on=date(2026, 4, 10))
        self.call2 = pos(id="c2", right=Right.CALL, strike=D("40"), opened_on=date(2026, 4, 13),
                         expiry=date(2026, 5, 15), open_price=D("0.80"), close_price=D("0"),
                         close_fee=D("0"), status=Status.ASSIGNED, closed_on=date(2026, 5, 15))
        self.disposal = ShareDisposal(id="d", underlying="ACME", quantity=100,
                                      disposed_on=date(2026, 5, 15), proceeds_per_share=D("40"),
                                      kind=DisposalKind.CALLED_AWAY, disposing_position_id="c2")
        self.positions = [self.put, self.call1, self.call2]
        self.index = ChainIndex(self.positions)

    def view(self):
        return lot_views(self.index, self.positions, [self.lot], [self.disposal])[0]

    def test_lot_is_closed_out(self):
        v = self.view()
        self.assertEqual(v.share_realized, D("500.00"))       # (40 - 35) x 100
        self.assertFalse(v.is_open)
        self.assertEqual(v.remaining, 0)
        self.assertEqual(v.disposed, 100)
        self.assertEqual(v.last_disposed_on, date(2026, 5, 15))
        self.assertEqual(v.days, 56)

    def test_option_pl_is_every_closed_leg_on_the_ticker(self):
        # 199.35 put + 68.70 first call + 79.35 second call.
        self.assertEqual(option_pl_by_ticker(self.positions), {"ACME": D("347.40")})

    def test_ticker_rollup(self):
        t = by_ticker(self.index, self.positions, [self.lot], [self.disposal])[0]
        self.assertEqual(t.underlying, "ACME")
        self.assertEqual(t.held, 0)
        self.assertEqual(t.realized, D("500.00"))
        self.assertEqual(t.option_pl, D("347.40"))
        self.assertEqual(t.total, D("847.40"))
        self.assertTrue(t.balanced)
        self.assertIsNone(t.basis)     # nothing held: no per-share basis


class TestOpenLotWithOpenCall(unittest.TestCase):
    def setUp(self):
        self.lot = ShareLot(id="lot", underlying="BETA", quantity=300,
                            acquired_on=date(2026, 1, 5), cost_per_share=D("10"),
                            source=ShareSource.OUTRIGHT_BUY)
        self.call = pos(id="c", underlying="BETA", right=Right.CALL, strike=D("12"),
                        quantity=2, open_price=D("0.50"), open_fee=D("1.30"))
        self.index = ChainIndex([self.call])

    def test_coverage_arithmetic(self):
        v = lot_views(self.index, [self.call], [self.lot], [])[0]
        self.assertTrue(v.is_open)
        self.assertEqual(v.remaining, 300)
        self.assertEqual(v.covered, 200)
        self.assertEqual(v.uncovered, 100)
        self.assertEqual(v.over_covered, 0)
        self.assertEqual(v.open_calls, (self.call,))

    def test_open_premium_does_not_count(self):
        t = by_ticker(self.index, [self.call], [self.lot], [])[0]
        self.assertEqual(t.option_pl, D("0"))
        self.assertEqual(t.total, D("0"))
        self.assertEqual(t.basis.unit_price, D("10.00"))     # 3,000 / 300, nothing paid back yet
        self.assertEqual(t.basis.min_call_strike, D("10.00"))

    def test_over_covered_is_flagged(self):
        big = pos(id="big", underlying="BETA", right=Right.CALL, quantity=4, open_price=D("0.50"))
        v = lot_views(ChainIndex([big]), [big], [self.lot], [])[0]
        self.assertEqual(v.over_covered, 100)

    def test_basis_blends_the_lots_held(self):
        other = ShareLot(id="lot2", underlying="BETA", quantity=100,
                         acquired_on=date(2026, 2, 1), cost_per_share=D("14"))
        t = by_ticker(self.index, [self.call], [self.lot, other], [])[0]
        self.assertEqual(t.held, 400)
        self.assertEqual(t.held_cost, D("4400.00"))
        self.assertEqual(t.basis.unit_price, D("11.00"))   # 4,400 / 400


class TestEverythingPaidBackLowersTheBasis(unittest.TestCase):
    """Shares are held at what was paid; every closed option and every sold
    share on the ticker lowers what the rest still need to fetch."""

    LOT = ShareLot(id="lot", underlying="GAMMA", quantity=200,
                   acquired_on=date(2026, 1, 5), cost_per_share=D("10"))

    def test_a_put_that_never_assigned_still_counts(self):
        put = pos(id="x", underlying="GAMMA", strike=D("9"), close_price=D("0"), close_fee=D("0"),
                  status=Status.EXPIRED, closed_on=date(2026, 3, 20))       # 199.35 kept
        t = by_ticker(ChainIndex([put]), [put], [self.LOT], [])[0]
        self.assertEqual(t.option_pl, D("199.35"))
        self.assertEqual(t.basis.unit_price, D("9.00"))       # (2,000 - 199.35) / 200 = 9.0033
        self.assertEqual(t.basis.min_call_strike, D("9.00"))

    def test_sold_shares_count_through_their_realized_pl(self):
        sale = ShareDisposal(id="d", underlying="GAMMA", quantity=100,
                             disposed_on=date(2026, 2, 1), proceeds_per_share=D("12"))
        t = by_ticker(ChainIndex([]), [], [self.LOT], [sale])[0]
        self.assertEqual(t.held, 100)
        self.assertEqual(t.realized, D("200.00"))            # (12 - 10) x 100
        self.assertEqual(t.held_cost, D("1000.00"))          # the half still held, at cost
        self.assertEqual(t.basis.unit_price, D("8.00"))      # (1,000 - 200) / 100
        self.assertEqual(t.basis.paid_back, D("200.00"))

    def test_shares_already_paid_for_have_no_floor(self):
        big = pos(id="x", underlying="GAMMA", quantity=10, open_price=D("2.50"), open_fee=D("0"),
                  close_price=D("0"), close_fee=D("0"), status=Status.EXPIRED,
                  closed_on=date(2026, 3, 20))                                # 2,500 kept
        t = by_ticker(ChainIndex([big]), [big], [self.LOT], [])[0]
        self.assertEqual(t.basis.unit_price, D("-2.50"))     # (2,000 - 2,500) / 200
        self.assertEqual(t.basis.min_call_strike, D("0.00")) # any strike is profit

    def test_a_losing_option_raises_the_basis(self):
        bad = pos(id="x", underlying="GAMMA", open_price=D("1.00"), open_fee=D("0"),
                  close_price=D("4.00"), close_fee=D("0"), status=Status.CLOSED,
                  closed_on=date(2026, 2, 1))                                 # -300
        t = by_ticker(ChainIndex([bad]), [bad], [self.LOT], [])[0]
        self.assertEqual(t.basis.unit_price, D("11.50"))     # (2,000 + 300) / 200


class TestImportedBuyWriteOrigin(unittest.TestCase):
    """An imported buy-write lot names the call as its origin; the call's
    premium is ticker option P/L like any other."""

    def test_call_origin_counts_once(self):
        call = pos(id="bw", right=Right.CALL, strike=D("12"), open_price=D("0.50"),
                   close_price=D("0"), close_fee=D("0.65"), status=Status.ASSIGNED,
                   closed_on=date(2026, 3, 20))
        lot = ShareLot(id="lot", underlying="ACME", quantity=100,
                       acquired_on=date(2026, 1, 5), cost_per_share=D("11"),
                       source=ShareSource.BUY_WRITE, assigning_position_id="bw")
        t = by_ticker(ChainIndex([call]), [call], [lot], [])[0]
        self.assertEqual(t.option_pl, D("48.70"))            # 50 - 0.65 open - 0.65 close
        self.assertEqual(t.basis.unit_price, D("10.51"))     # (1,100 - 48.70) / 100


class TestDeficit(unittest.TestCase):
    def test_ticker_in_deficit_is_reported_not_summed(self):
        lot = ShareLot(id="lot", underlying="OMEGA", quantity=300,
                       acquired_on=date(2026, 1, 5), cost_per_share=D("27"))
        disposal = ShareDisposal(id="d", underlying="OMEGA", quantity=400,
                                 disposed_on=date(2026, 3, 1), proceeds_per_share=D("30"))
        tickers = by_ticker(ChainIndex([]), [], [lot], [disposal])
        t = tickers[0]
        self.assertFalse(t.balanced)
        self.assertEqual(t.held, -100)
        self.assertIn("missing", t.error)
        self.assertIsNone(t.basis)
        self.assertEqual(realized_shares(tickers), D("0"))


class TestCoverAcrossLots(unittest.TestCase):
    def test_open_calls_cover_the_oldest_lots_first(self):
        a = ShareLot(id="a", underlying="ACME", quantity=200, acquired_on=date(2026, 1, 5),
                     cost_per_share=D("10"))
        b = ShareLot(id="b", underlying="ACME", quantity=100, acquired_on=date(2026, 2, 1),
                     cost_per_share=D("12"))
        call = pos(id="c", right=Right.CALL, strike=D("15"), quantity=3, open_price=D("1.00"),
                   open_fee=D("1.95"), opened_on=date(2026, 2, 2))
        views = {v.lot.id: v for v in lot_views(ChainIndex([call]), [call], [a, b], [])}
        self.assertEqual((views["a"].covered, views["b"].covered), (200, 100))
        self.assertEqual((views["a"].uncovered, views["b"].uncovered), (0, 0))
        self.assertEqual(views["a"].open_calls, (call,))
        # Closed, the call is 298.05 - 30 - 1.95 = 266.10 of ticker option P/L.
        closed = pos(id="c", right=Right.CALL, strike=D("15"), quantity=3, open_price=D("1.00"),
                     open_fee=D("1.95"), opened_on=date(2026, 2, 2), closed_on=date(2026, 3, 1),
                     close_price=D("0.10"), close_fee=D("1.95"), status=Status.CLOSED)
        t = by_ticker(ChainIndex([closed]), [closed], [a, b], [])[0]
        self.assertEqual(t.option_pl, D("266.10"))
        self.assertEqual(t.basis.unit_price, D("9.78"))      # (3,200 - 266.10) / 300


if __name__ == "__main__":
    unittest.main()
