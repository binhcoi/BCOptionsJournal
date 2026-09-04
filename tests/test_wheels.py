"""Wheel tests: what a lot made, options and stock together. Hand-computed."""

import unittest
from datetime import date
from decimal import Decimal

from bcoj.domain.enums import Direction, DisposalKind, Right, ShareSource, Status
from bcoj.domain.types import Position, ShareDisposal, ShareLot
from bcoj.engine.chains import ChainIndex
from bcoj.engine.wheels import by_ticker, lot_views, realized_shares

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
                         close_fee=D("0.65"), status=Status.CLOSED, closed_on=date(2026, 4, 10),
                         share_lot_id="lot")
        self.call2 = pos(id="c2", right=Right.CALL, strike=D("40"), opened_on=date(2026, 4, 13),
                         expiry=date(2026, 5, 15), open_price=D("0.80"), close_price=D("0"),
                         close_fee=D("0"), status=Status.ASSIGNED, closed_on=date(2026, 5, 15),
                         share_lot_id="lot")
        self.disposal = ShareDisposal(id="d", underlying="ACME", quantity=100,
                                      disposed_on=date(2026, 5, 15), proceeds_per_share=D("40"),
                                      kind=DisposalKind.CALLED_AWAY, disposing_position_id="c2",
                                      specific_lot_ids=("lot",))
        self.positions = [self.put, self.call1, self.call2]
        self.index = ChainIndex(self.positions)

    def view(self):
        return lot_views(self.index, self.positions, [self.lot], [self.disposal])[0]

    def test_components(self):
        v = self.view()
        self.assertEqual(v.acq_premium, D("199.35"))          # 200 - 0.65
        self.assertEqual(v.cc_premium, D("148.05"))           # 68.70 + 79.35
        self.assertEqual(v.share_realized, D("500.00"))       # (40 - 35) x 100
        self.assertEqual(len(v.call_chains), 2)
        self.assertIsNotNone(v.acquisition)

    def test_total_is_the_sum_of_all_three(self):
        self.assertEqual(self.view().total, D("847.40"))

    def test_lot_is_closed_out(self):
        v = self.view()
        self.assertFalse(v.is_open)
        self.assertEqual(v.remaining, 0)
        self.assertEqual(v.disposed, 100)
        self.assertEqual(v.last_disposed_on, date(2026, 5, 15))
        self.assertEqual(v.days, 56)

    def test_adjusted_basis_on_the_original_lot(self):
        b = self.view().basis
        self.assertEqual(b.unit_price, D("33.01"))    # (3,500 - 199.35) / 100
        self.assertEqual(b.after_calls, D("31.53"))   # less 148.05 of calls
        self.assertEqual(b.min_call_strike, D("32.00"))

    def test_ticker_rollup(self):
        t = by_ticker(self.index, self.positions, [self.lot], [self.disposal])[0]
        self.assertEqual(t.underlying, "ACME")
        self.assertEqual(t.held, 0)
        self.assertEqual(t.realized, D("500.00"))
        self.assertEqual(t.option_premium, D("347.40"))
        self.assertEqual(t.total, D("847.40"))
        self.assertTrue(t.balanced)
        self.assertIsNone(t.blended)     # nothing held


class TestOpenLotWithOpenCall(unittest.TestCase):
    def setUp(self):
        self.lot = ShareLot(id="lot", underlying="BETA", quantity=300,
                            acquired_on=date(2026, 1, 5), cost_per_share=D("10"),
                            source=ShareSource.OUTRIGHT_BUY)
        self.call = pos(id="c", underlying="BETA", right=Right.CALL, strike=D("12"),
                        quantity=2, open_price=D("0.50"), open_fee=D("1.30"),
                        share_lot_id="lot")
        self.index = ChainIndex([self.call])

    def test_coverage_arithmetic(self):
        v = lot_views(self.index, [self.call], [self.lot], [])[0]
        self.assertTrue(v.is_open)
        self.assertEqual(v.remaining, 300)
        self.assertEqual(v.covered, 200)
        self.assertEqual(v.uncovered, 100)
        self.assertEqual(v.over_covered, 0)
        self.assertEqual(v.open_calls, (self.call,))

    def test_open_call_has_realized_nothing_yet(self):
        v = lot_views(self.index, [self.call], [self.lot], [])[0]
        self.assertEqual(v.cc_premium, D("0"))
        self.assertEqual(v.total, D("0"))
        self.assertEqual(v.basis.unit_price, D("10.00"))
        self.assertEqual(v.basis.after_calls, D("10.00"))

    def test_over_covered_is_flagged(self):
        big = pos(id="big", underlying="BETA", right=Right.CALL, quantity=4,
                  open_price=D("0.50"), share_lot_id="lot")
        v = lot_views(ChainIndex([big]), [big], [self.lot], [])[0]
        self.assertEqual(v.over_covered, 100)

    def test_blended_over_open_lots(self):
        other = ShareLot(id="lot2", underlying="BETA", quantity=100,
                         acquired_on=date(2026, 2, 1), cost_per_share=D("14"))
        t = by_ticker(self.index, [self.call], [self.lot, other], [])[0]
        self.assertEqual(t.held, 400)
        self.assertEqual(t.blended.unit_price, D("11.00"))   # 4,400 / 400
        self.assertEqual(t.held_cost, D("4400.00"))


class TestImportedBuyWriteOrigin(unittest.TestCase):
    """An imported buy-write lot names the call as its origin.

    That call is a covered call on the lot, not its acquisition."""

    def test_call_origin_is_a_covered_call(self):
        call = pos(id="bw", right=Right.CALL, strike=D("12"), open_price=D("0.50"),
                   close_price=D("0"), close_fee=D("0.65"), status=Status.ASSIGNED,
                   closed_on=date(2026, 3, 20))
        lot = ShareLot(id="lot", underlying="ACME", quantity=100,
                       acquired_on=date(2026, 1, 5), cost_per_share=D("11"),
                       source=ShareSource.BUY_WRITE, assigning_position_id="bw")
        v = lot_views(ChainIndex([call]), [call], [lot], [])[0]
        self.assertIsNone(v.acquisition)
        self.assertEqual(v.acq_premium, D("0"))
        self.assertEqual(len(v.call_chains), 1)
        self.assertEqual(v.cc_premium, D("48.70"))   # 50 - 0.65 open - 0.65 close


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
        self.assertEqual(realized_shares(tickers), D("0"))



if __name__ == "__main__":
    unittest.main()


class TestCoverAcrossLots(unittest.TestCase):
    def test_premium_is_split_by_shares_and_each_lot_counts_its_cover(self):
        from datetime import date
        from decimal import Decimal as D
        from bcoj.domain.enums import Direction, Right
        from bcoj.domain.types import Position, ShareLot
        from bcoj.engine.chains import ChainIndex
        from bcoj.engine.wheels import lot_views
        a = ShareLot(id="a", underlying="ACME", quantity=200, acquired_on=date(2026, 1, 5),
                     cost_per_share=D("10"))
        b = ShareLot(id="b", underlying="ACME", quantity=100, acquired_on=date(2026, 2, 1),
                     cost_per_share=D("12"))
        call = Position(id="c", underlying="ACME", expiry=date(2026, 3, 20), strike=D("15"),
                        right=Right.CALL, direction=Direction.SHORT, quantity=3,
                        opened_on=date(2026, 2, 2), open_price=D("1.00"), open_fee=D("1.95"),
                        covers=(("a", 200), ("b", 100)))
        views = {v.lot.id: v for v in lot_views(ChainIndex([call]), [call], [a, b], [])}
        # Open premium 298.05: two thirds to the 200-share lot, one third to the 100.
        self.assertEqual(views["a"].covered, 200)
        self.assertEqual(views["b"].covered, 100)
        self.assertEqual((views["a"].uncovered, views["b"].uncovered), (0, 0))
        self.assertEqual(views["a"].open_calls, (call,))
        self.assertEqual(views["a"].cc_premium + views["b"].cc_premium, D("0.00"))  # nothing realized yet
        closed = Position(id="c", underlying="ACME", expiry=date(2026, 3, 20), strike=D("15"),
                          right=Right.CALL, direction=Direction.SHORT, quantity=3,
                          opened_on=date(2026, 2, 2), open_price=D("1.00"), open_fee=D("1.95"),
                          closed_on=date(2026, 3, 1), close_price=D("0.10"), close_fee=D("1.95"),
                          status=Status.CLOSED, covers=(("a", 200), ("b", 100)))
        views = {v.lot.id: v for v in lot_views(ChainIndex([closed]), [closed], [a, b], [])}
        # Realized 298.05 - 31.95 = 266.10: 177.40 to the first lot, 88.70 to the second.
        self.assertEqual(views["a"].cc_premium, D("177.40"))
        self.assertEqual(views["b"].cc_premium, D("88.70"))
        self.assertEqual(call.share_lot_id, "a")
