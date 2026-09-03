"""Wheel tests: what a lot made, options and stock together. Hand-computed."""

import unittest
from datetime import date
from decimal import Decimal

from bcoj.domain.enums import Direction, DisposalKind, Right, ShareSource, Status
from bcoj.domain.types import Position, ShareDisposal, ShareLot
from bcoj.engine.chains import ChainIndex
from bcoj.engine.wheels import by_ticker, lot_views, realized_shares, suggest_covers

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


class TestSuggestCovers(unittest.TestCase):
    def setUp(self):
        self.lot = ShareLot(id="lot", underlying="ACME", quantity=100,
                            acquired_on=date(2026, 3, 20), cost_per_share=D("35"),
                            source=ShareSource.PUT_ASSIGNMENT)

    def _suggest(self, positions, lots):
        index = ChainIndex(positions)
        views = lot_views(index, positions, lots, [])
        return suggest_covers(index, positions, views)

    def test_call_written_while_lot_held_is_proposed(self):
        call = pos(id="c", right=Right.CALL, opened_on=date(2026, 4, 1))
        self.assertEqual(self._suggest([call], [self.lot]), {"c": "lot"})

    def test_call_written_before_the_lot_existed_is_not(self):
        call = pos(id="c", right=Right.CALL, opened_on=date(2026, 3, 1))
        self.assertEqual(self._suggest([call], [self.lot]), {})

    def test_ambiguous_lots_propose_nothing(self):
        other = ShareLot(id="lot2", underlying="ACME", quantity=100,
                         acquired_on=date(2026, 3, 25), cost_per_share=D("36"))
        call = pos(id="c", right=Right.CALL, opened_on=date(2026, 4, 1))
        self.assertEqual(self._suggest([call], [self.lot, other]), {})

    def test_already_linked_puts_and_longs_are_skipped(self):
        linked = pos(id="l", right=Right.CALL, opened_on=date(2026, 4, 1), share_lot_id="x")
        put = pos(id="p", opened_on=date(2026, 4, 1))
        long_call = pos(id="lc", right=Right.CALL, direction=Direction.LONG,
                        opened_on=date(2026, 4, 1))
        self.assertEqual(self._suggest([linked, put, long_call], [self.lot]), {})

    def test_only_the_chain_head_is_proposed(self):
        first = pos(id="c1", right=Right.CALL, opened_on=date(2026, 4, 1),
                    close_price=D("1"), status=Status.ROLLED, closed_on=date(2026, 4, 10))
        head = pos(id="c2", right=Right.CALL, opened_on=date(2026, 4, 10), rolled_from_id="c1")
        self.assertEqual(self._suggest([first, head], [self.lot]), {"c2": "lot"})


if __name__ == "__main__":
    unittest.main()
