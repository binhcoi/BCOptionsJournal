"""Engine tests.

Every scenario is synthetic and every expected figure is hand-computed, so a
failure means the engine disagrees with arithmetic done independently of it.

Real trade data is never used here. The authoritative check -- a full
reconciliation against a real five-year export -- lives outside this
repository; see docs/legacy-format.md for what it does.
"""

import unittest
from datetime import date
from decimal import Decimal

from bcoj.domain.enums import (
    Direction,
    DisposalKind,
    MatchingRule,
    Right,
    ShareSource,
    Status,
)
from bcoj.domain.money import fmt, parse_money, q2
from bcoj.domain.types import Position, ShareDisposal, ShareLot
from bcoj.engine.basis import adjusted_basis, blended, round_up_to_strike
from bcoj.engine.chains import ChainCycleError, ChainIndex
from bcoj.engine.pnl import close_cash, days_held, open_cash, realized_pl
from bcoj.engine.risk import break_even, capital_at_risk, credit_to_recover, put_risk
from bcoj.engine.shares import InsufficientSharesError, match, net_share_count
from bcoj.engine.targets import target, target_price

D = Decimal


def position(**kw) -> Position:
    """A short put with sensible defaults; override what a test cares about."""
    base = dict(
        id="p1",
        underlying="ACME",
        expiry=date(2026, 12, 18),
        strike=D("35"),
        right=Right.PUT,
        direction=Direction.SHORT,
        quantity=1,
        opened_on=date(2026, 1, 5),
        open_price=D("1.00"),
        open_fee=D("0.65"),
        status=Status.OPEN,
    )
    base.update(kw)
    return Position(**base)


def closed(**kw) -> Position:
    kw.setdefault("status", Status.CLOSED)
    kw.setdefault("closed_on", date(2026, 2, 6))
    return position(**kw)


class TestMoneyParsing(unittest.TestCase):
    def test_accounting_negatives_and_currency(self):
        cases = {
            "(1,462.00)": D("-1462.00"),
            "$16,500.00": D("16500.00"),
            "-$2,000.00": D("-2000.00"),
            "($350.25)": D("-350.25"),
            "0.00": D("0.00"),
            "0": D("0"),
            "19.1": D("19.1"),
            "$0.00": D("0.00"),
            ".50": D("0.50"),
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(parse_money(raw), expected)

    def test_nulls(self):
        for raw in ("", "-", "   ", None):
            self.assertIsNone(parse_money(raw))

    def test_zero_is_not_null(self):
        """"No premium" and "no cell" mean different things in the sheet."""
        self.assertEqual(parse_money("0.00"), D("0"))
        self.assertIsNone(parse_money("-"))

    def test_bad_value_raises(self):
        with self.assertRaises(ValueError):
            parse_money("twelve dollars")

    def test_fmt_uses_accounting_negatives(self):
        self.assertEqual(fmt(D("-123.45")), "(123.45)")
        self.assertEqual(fmt(D("54321.00")), "54,321.00")
        self.assertEqual(fmt(None), "-")

    def test_fmt_never_shows_a_signed_zero(self):
        """An assignment closes at 0 with a 0 fee: -(0) - 0 is Decimal -0.00."""
        self.assertEqual(fmt(D("-0.00")), "0.00")
        self.assertEqual(fmt(D("0")), "0.00")

    def test_price_is_two_decimals(self):
        from bcoj.domain.money import price

        self.assertEqual(price(D("0")), "0.00")
        self.assertEqual(price(D("2.5")), "2.50")
        self.assertEqual(price(D("-0.00")), "0.00")
        self.assertEqual(price(D("1234.5")), "1234.50")   # no thousands separator
        self.assertEqual(price(None), "-")


class TestPnl(unittest.TestCase):
    """One formula, four shapes of trade."""

    def test_short_call_closed_for_a_profit(self):
        p = closed(
            right=Right.CALL, strike=D("50"),
            open_price=D("2.00"), open_fee=D("0.65"),
            close_price=D("0.50"), close_fee=D("0.65"),
        )
        self.assertEqual(open_cash(p), D("199.35"))     # 200.00 - 0.65
        self.assertEqual(close_cash(p), D("-50.65"))    # -50.00 - 0.65
        self.assertEqual(realized_pl(p), D("148.70"))

    def test_short_put_expired_worthless(self):
        p = closed(
            quantity=2, strike=D("40"),
            open_price=D("1.00"), open_fee=D("1.30"),
            close_price=D("0.00"), close_fee=D("0.00"),
            status=Status.EXPIRED,
        )
        self.assertEqual(realized_pl(p), D("198.70"))   # the whole credit

    def test_long_put_closed_for_a_profit(self):
        """A bought put: the sheet writes this as a negative quantity."""
        p = closed(
            direction=Direction.LONG, strike=D("30"),
            open_price=D("1.50"), open_fee=D("0.65"),
            close_price=D("3.00"), close_fee=D("0.65"),
        )
        self.assertEqual(open_cash(p), D("-150.65"))    # a debit
        self.assertEqual(close_cash(p), D("299.35"))
        self.assertEqual(realized_pl(p), D("148.70"))

    def test_long_leap_call_closed_at_a_loss(self):
        p = closed(
            direction=Direction.LONG, right=Right.CALL, strike=D("100"),
            open_price=D("20.00"), open_fee=D("0.65"),
            close_price=D("12.00"), close_fee=D("0.65"),
        )
        self.assertEqual(open_cash(p), D("-2000.65"))
        self.assertEqual(close_cash(p), D("1199.35"))
        self.assertEqual(realized_pl(p), D("-801.30"))

    def test_long_multi_contract(self):
        p = closed(
            direction=Direction.LONG, quantity=5, strike=D("30"),
            open_price=D("9.00"), open_fee=D("3.25"),
            close_price=D("11.00"), close_fee=D("3.25"),
        )
        self.assertEqual(realized_pl(p), D("993.50"))

    def test_open_position_has_no_realized_pl(self):
        """On an open row Close_U is a target, so it must not leak into P/L."""
        p = position(quantity=10, open_price=D("3.00"), open_fee=D("6.50"))
        self.assertEqual(open_cash(p), D("2993.50"))
        self.assertEqual(close_cash(p), D("0"))
        self.assertEqual(realized_pl(p), D("0"))

    def test_days_held(self):
        self.assertIsNone(days_held(position()))
        self.assertEqual(days_held(closed()), 32)

    def test_quantity_must_be_positive(self):
        """Direction carries the sign, so no arithmetic depends on it."""
        with self.assertRaises(ValueError):
            position(quantity=-1)
        with self.assertRaises(ValueError):
            position(quantity=0)


class TestChains(unittest.TestCase):
    """A four-leg roll chain that grows in size as it goes."""

    LEGS = (
        # id, parent, qty, open, fee, close, cfee, expected leg P/L
        ("l1", None, 1, "1.00", "0.65", "1.50", "0.65", "-51.30"),
        ("l2", "l1", 2, "1.20", "1.30", "2.00", "1.30", "-162.60"),
        ("l3", "l2", 4, "1.50", "2.60", "1.00", "2.60", "194.80"),
        ("l4", "l3", 4, "2.00", "2.60", "0.00", "0.00", "797.40"),
    )

    def _positions(self):
        return [
            closed(
                id=pid, underlying="DELTA", quantity=qty,
                open_price=D(op), open_fee=D(of),
                close_price=D(cp), close_fee=D(cf),
                rolled_from_id=parent,
            )
            for pid, parent, qty, op, of, cp, cf, _ in self.LEGS
        ]

    def test_leg_pl(self):
        for p, (*_, expected) in zip(self._positions(), self.LEGS):
            with self.subTest(leg=p.id):
                self.assertEqual(realized_pl(p), D(expected))

    def test_carry_accumulates(self):
        """The sheet's Cost basis column, derived instead of carried by hand."""
        index = ChainIndex(self._positions())
        expected = {
            "l1": D("0.00"),
            "l2": D("-51.30"),
            "l3": D("-213.90"),   # -51.30 - 162.60
            "l4": D("-19.10"),    # -213.90 + 194.80
        }
        for p in self._positions():
            with self.subTest(leg=p.id):
                self.assertEqual(index.carry(p), expected[p.id])

    def test_chain_shape_and_total(self):
        index = ChainIndex(self._positions())
        self.assertEqual([h.id for h in index.heads()], ["l4"])

        chain = index.chain(index.heads()[0])
        self.assertEqual(chain.leg_count, 4)
        self.assertEqual([l.id for l in chain.legs], ["l1", "l2", "l3", "l4"])
        self.assertEqual(chain.realized, D("778.30"))
        self.assertEqual(chain.carry, D("-19.10"))
        self.assertEqual(chain.underlying, "DELTA")

    def test_split_links_are_followed_too(self):
        parent = closed(id="whole", quantity=4, close_price=D("0.50"))
        child = closed(
            id="half", quantity=2, close_price=D("0.50"), split_from_id="whole"
        )
        index = ChainIndex([parent, child])
        self.assertEqual(index.chain(child).leg_count, 2)

    def test_missing_parent_truncates_rather_than_failing(self):
        """A chain whose earlier legs are outside the imported range."""
        orphan = closed(id="child", rolled_from_id="absent")
        index = ChainIndex([orphan])
        self.assertEqual(index.carry(orphan), D("0"))
        self.assertEqual(index.chain(orphan).leg_count, 1)

    def test_cycle_is_refused(self):
        a = position(id="a", rolled_from_id="b")
        b = position(id="b", rolled_from_id="a")
        with self.assertRaises(ChainCycleError):
            ChainIndex([a, b]).lineage(a)

    def test_duplicate_ids_refused(self):
        with self.assertRaises(ValueError):
            ChainIndex([position(id="dup"), position(id="dup")])

    def test_a_fork_yields_two_heads(self):
        root = closed(id="root", close_price=D("1.00"))
        left = closed(id="left", rolled_from_id="root", close_price=D("1.00"))
        right = closed(id="right", rolled_from_id="root", close_price=D("1.00"))
        index = ChainIndex([root, left, right])
        self.assertEqual({h.id for h in index.heads()}, {"left", "right"})
        self.assertFalse(index.is_head(root))


class TestTargets(unittest.TestCase):
    """The sheet's E columns: Expected, at a profit target. Not a market mark."""

    def test_fifty_percent_target_on_a_fresh_position(self):
        p = position(quantity=10, open_price=D("3.00"),
                     open_fee=D("6.50"), close_fee=D("6.50"))
        t = target(p)
        self.assertEqual(t.net_credit, D("2993.50"))
        self.assertEqual(t.price, D("1.49"))
        self.assertEqual(t.expected_closing, D("-1496.50"))
        self.assertEqual(t.expected_pl, D("1497.00"))
        self.assertTrue(t.applicable)

    def test_carry_moves_the_target(self):
        """A chain that has already lost money needs a cheaper close."""
        p = position(quantity=10, open_price=D("3.00"),
                     open_fee=D("6.50"), close_fee=D("6.50"))
        t = target(p, carry=D("-1000.00"))
        self.assertEqual(t.net_credit, D("1993.50"))
        self.assertEqual(t.price, D("0.99"))
        self.assertEqual(t.expected_pl, D("997.00"))

    def test_underwater_chain_has_no_target(self):
        """A net-debit chain has no credit to capture half of.

        Clamps to zero -- expiring worthless is the best case left.
        """
        p = position(strike=D("0"), open_price=D("0"),
                     open_fee=D("0.65"), close_fee=D("0.65"))
        t = target(p)
        self.assertEqual(t.price, D("0"))
        self.assertEqual(t.expected_closing, D("-0.65"))
        self.assertEqual(t.expected_pl, D("-1.30"))
        self.assertFalse(t.applicable)
        self.assertIsNone(t.capture)

    def test_target_price_never_negative(self):
        p = position(open_price=D("0.05"), open_fee=D("5.00"), close_fee=D("5.00"))
        self.assertGreaterEqual(target_price(p, D("-100")), D("0"))

    def test_per_position_override_beats_the_default(self):
        p = position(quantity=10, open_price=D("3.00"),
                     open_fee=D("6.50"), close_fee=D("6.50"),
                     target_pct=D("0.25"))
        self.assertEqual(target(p).pct, D("0.25"))
        # Settling for a quarter of the credit rather than half means taking
        # profit earlier, which is a *higher* buy-back price.
        self.assertEqual(target(p).price, D("2.24"))
        self.assertGreater(target(p).price, target_price(p, pct=D("0.50")))

    def test_capture_is_the_realised_fraction(self):
        p = position(quantity=10, open_price=D("3.00"),
                     open_fee=D("6.50"), close_fee=D("6.50"))
        self.assertAlmostEqual(float(target(p).capture), 0.5, places=3)


class TestRisk(unittest.TestCase):
    def test_put_risk_matches_sheet_sign_convention(self):
        self.assertEqual(put_risk(position(quantity=22, strike=D("45"))),
                         D("99000.00"))
        # A long put owes nothing, so the sheet shows it negative.
        self.assertEqual(
            put_risk(position(direction=Direction.LONG, strike=D("20"))),
            D("-2000.00"),
        )
        self.assertIsNone(put_risk(position(right=Right.CALL)))

    def test_break_even_on_an_open_chain(self):
        """Break-even uses opening cash while no closing fee has been paid."""
        head = position(quantity=10, strike=D("35"),
                        open_price=D("3.00"), open_fee=D("6.50"))
        parent = closed(id="prior", quantity=10, open_price=D("0"),
                        open_fee=D("2000.00"), close_price=D("0"),
                        close_fee=D("0"), status=Status.ROLLED)
        head.rolled_from_id = "prior"
        chain = ChainIndex([parent, head]).chain(head)

        self.assertEqual(chain.carry, D("-2000.00"))
        self.assertEqual(chain.net_credit, D("993.50"))
        be = break_even(chain)
        self.assertEqual(be.per_share, D("0.99"))
        self.assertEqual(be.price, D("34.01"))

    def test_break_even_on_a_closed_chain_includes_its_closing_fee(self):
        """Once closed, the realized total is the honest credit.

        Using opening cash instead would overstate it by the closing fee and
        report a break-even a cent too favourable: 15.05 rather than 15.06.
        """
        legs = [
            closed(id="l1", quantity=1, open_price=D("1.00"), open_fee=D("0.65"),
                   close_price=D("1.50"), close_fee=D("0.65"),
                   status=Status.ROLLED),
            closed(id="l2", quantity=2, open_price=D("1.20"), open_fee=D("1.30"),
                   close_price=D("2.00"), close_fee=D("1.30"),
                   status=Status.ROLLED, rolled_from_id="l1"),
            closed(id="l3", quantity=4, open_price=D("1.50"), open_fee=D("2.60"),
                   close_price=D("1.00"), close_fee=D("2.60"),
                   status=Status.ROLLED, rolled_from_id="l2"),
        ]
        head = closed(
            id="l4", quantity=4, strike=D("17"),
            open_price=D("2.00"), open_fee=D("2.60"),
            close_price=D("0.00"), close_fee=D("4.00"),
            status=Status.ASSIGNED, rolled_from_id="l3",
        )
        chain = ChainIndex(legs + [head]).chain(head)

        self.assertEqual(chain.carry, D("-19.10"))
        self.assertEqual(realized_pl(head), D("793.40"))
        self.assertEqual(chain.net_credit, D("774.30"))
        self.assertEqual(break_even(chain).price, D("15.06"))

    def test_break_even_is_undefined_for_a_long_option(self):
        head = position(direction=Direction.LONG)
        self.assertIsNone(break_even(ChainIndex([head]).chain(head)))

    def test_short_call_break_even_rises_instead_of_falling(self):
        head = position(right=Right.CALL, quantity=1, strike=D("50"),
                        open_price=D("2.00"), open_fee=D("0"))
        be = break_even(ChainIndex([head]).chain(head))
        self.assertEqual(be.price, D("52.00"))

    def test_credit_to_recover(self):
        head = position(open_price=D("1.00"), open_fee=D("0"))
        parent = closed(id="p", open_price=D("0"), open_fee=D("0"),
                        close_price=D("5.00"), close_fee=D("0"),
                        status=Status.ROLLED)
        head.rolled_from_id = "p"
        chain = ChainIndex([parent, head]).chain(head)
        self.assertEqual(chain.net_credit, D("-400.00"))
        self.assertEqual(credit_to_recover(chain), D("400.00"))

    def test_no_credit_to_recover_when_ahead(self):
        head = position(open_price=D("1.00"), open_fee=D("0"))
        chain = ChainIndex([head]).chain(head)
        self.assertEqual(credit_to_recover(chain), D("0"))

    def test_naked_call_has_no_capital_at_risk(self):
        call = position(right=Right.CALL)
        self.assertIsNone(capital_at_risk(call))
        self.assertEqual(capital_at_risk(call, share_basis=D("9700")), D("9700.00"))

    def test_cash_secured_put_risks_the_strike(self):
        self.assertEqual(
            capital_at_risk(position(quantity=2, strike=D("40"))), D("8000.00")
        )

    def test_long_option_risks_the_debit(self):
        p = position(direction=Direction.LONG,
                     open_price=D("2.00"), open_fee=D("0.65"))
        self.assertEqual(capital_at_risk(p), D("200.65"))


class TestShareMatching(unittest.TestCase):
    """Lots acquired at very different prices, so the rule matters."""

    def _lots(self):
        return [
            ShareLot(id="cheap", underlying="ZETA", quantity=1000,
                     acquired_on=date(2023, 6, 30), cost_per_share=D("9.00"),
                     source=ShareSource.BUY_WRITE),
            ShareLot(id="mid", underlying="ZETA", quantity=300,
                     acquired_on=date(2025, 8, 1), cost_per_share=D("24.00"),
                     source=ShareSource.PUT_ASSIGNMENT),
            ShareLot(id="dear", underlying="ZETA", quantity=1500,
                     acquired_on=date(2026, 2, 13), cost_per_share=D("29.00"),
                     source=ShareSource.PUT_ASSIGNMENT),
        ]

    def _disposal(self):
        return ShareDisposal(
            id="called", underlying="ZETA", quantity=1800,
            disposed_on=date(2026, 2, 13), proceeds_per_share=D("19.50"),
            kind=DisposalKind.CALLED_AWAY,
        )

    def test_fifo_realizes_a_gain(self):
        result = match(self._lots(), [self._disposal()], MatchingRule.FIFO)
        self.assertEqual(result.realized, D("4400.00"))
        self.assertEqual(result.open_quantity, 1000)
        self.assertEqual([s.lot.id for s in result.open_lots()], ["dear"])
        self.assertEqual(result.open_cost, D("29000.00"))

    def test_lifo_realizes_a_loss_of_the_same_shares(self):
        """Twenty thousand dollars apart, on the same disposal."""
        result = match(self._lots(), [self._disposal()], MatchingRule.LIFO)
        self.assertEqual(result.realized, D("-15600.00"))
        self.assertEqual(result.open_quantity, 1000)
        self.assertEqual([s.lot.id for s in result.open_lots()], ["cheap"])
        self.assertEqual(result.open_cost, D("9000.00"))

    def test_the_two_rules_differ_by_exactly_the_lot_spread(self):
        fifo = match(self._lots(), [self._disposal()], MatchingRule.FIFO)
        lifo = match(self._lots(), [self._disposal()], MatchingRule.LIFO)
        self.assertEqual(fifo.realized - lifo.realized, D("20000.00"))

    def test_same_day_acquisition_is_eligible(self):
        """An assignment and the sale of those shares can share a date."""
        lots = [l for l in self._lots() if l.id == "dear"]
        disposal = ShareDisposal(
            id="d", underlying="ZETA", quantity=1500,
            disposed_on=date(2026, 2, 13), proceeds_per_share=D("29.00"),
        )
        result = match(lots, [disposal])
        self.assertEqual(result.realized, D("0.00"))
        self.assertEqual(result.open_quantity, 0)

    def test_a_later_acquisition_is_not_eligible(self):
        lots = [
            ShareLot(id="later", underlying="ZETA", quantity=100,
                     acquired_on=date(2026, 3, 1), cost_per_share=D("10")),
        ]
        disposal = ShareDisposal(
            id="d", underlying="ZETA", quantity=100,
            disposed_on=date(2026, 1, 1), proceeds_per_share=D("12"),
        )
        with self.assertRaises(InsufficientSharesError):
            match(lots, [disposal])

    def test_specific_lots_override_the_rule(self):
        """Buy-write shares are earmarked for their own call.

        Plain FIFO would reach past them to an older, cheaper lot and report a
        gain that never happened.
        """
        lots = [
            ShareLot(id="old", underlying="ZETA", quantity=1000,
                     acquired_on=date(2023, 6, 30), cost_per_share=D("9.00")),
            ShareLot(id="bw", underlying="ZETA", quantity=300,
                     acquired_on=date(2025, 7, 8), cost_per_share=D("19.00"),
                     source=ShareSource.BUY_WRITE),
        ]
        earmarked = ShareDisposal(
            id="called", underlying="ZETA", quantity=300,
            disposed_on=date(2025, 7, 11), proceeds_per_share=D("20.00"),
            kind=DisposalKind.CALLED_AWAY, specific_lot_ids=("bw",),
        )
        self.assertEqual(match(lots, [earmarked]).realized, D("300.00"))

        plain = ShareDisposal(
            id="called2", underlying="ZETA", quantity=300,
            disposed_on=date(2025, 7, 11), proceeds_per_share=D("20.00"),
        )
        self.assertEqual(match(lots, [plain]).realized, D("3300.00"))

    def test_a_deficit_is_refused(self):
        """A negative share count is impossible; refuse rather than guess."""
        lots = [
            ShareLot(id="a", underlying="OMEGA", quantity=400,
                     acquired_on=date(2022, 7, 5), cost_per_share=D("50")),
        ]
        disposal = ShareDisposal(
            id="d", underlying="OMEGA", quantity=500,
            disposed_on=date(2024, 10, 15), proceeds_per_share=D("82"),
        )
        self.assertEqual(net_share_count(lots, [disposal]), -100)
        with self.assertRaises(InsufficientSharesError) as ctx:
            match(lots, [disposal])
        self.assertIn("missing", str(ctx.exception))

    def test_a_reconstructed_lot_closes_the_gap(self):
        lots = [
            ShareLot(id="a", underlying="OMEGA", quantity=100,
                     acquired_on=date(2022, 7, 5), cost_per_share=D("150")),
            ShareLot(id="b", underlying="OMEGA", quantity=100,
                     acquired_on=date(2023, 8, 11), cost_per_share=D("65")),
            ShareLot(id="c", underlying="OMEGA", quantity=200,
                     acquired_on=date(2024, 6, 5), cost_per_share=D("58")),
            ShareLot(id="est", underlying="OMEGA", quantity=100,
                     acquired_on=date(2022, 1, 1), cost_per_share=D("35"),
                     estimated=True),
        ]
        disposal = ShareDisposal(
            id="d", underlying="OMEGA", quantity=500,
            disposed_on=date(2024, 10, 15), proceeds_per_share=D("82"),
            kind=DisposalKind.CALLED_AWAY,
        )
        result = match(lots, [disposal])
        # 41,000 proceeds against 36,600 of cost.
        self.assertEqual(result.realized, D("4400.00"))
        self.assertEqual(result.open_quantity, 0)

    def test_partial_disposal_leaves_the_remainder_open(self):
        lots = [
            ShareLot(id="a", underlying="ZETA", quantity=500,
                     acquired_on=date(2025, 1, 1), cost_per_share=D("10")),
        ]
        disposal = ShareDisposal(
            id="d", underlying="ZETA", quantity=200,
            disposed_on=date(2025, 6, 1), proceeds_per_share=D("12"),
        )
        result = match(lots, [disposal])
        self.assertEqual(result.realized, D("400.00"))
        self.assertEqual(result.open_quantity, 300)
        self.assertEqual(result.open_cost, D("3000.00"))

    def test_fees_are_spread_and_proceeds_sum_exactly(self):
        """Per-share rounding must never leave the ledger a cent adrift."""
        lots = [
            ShareLot(id="a", underlying="X", quantity=7,
                     acquired_on=date(2025, 1, 1), cost_per_share=D("10"),
                     fee=D("1.00")),
            ShareLot(id="b", underlying="X", quantity=6,
                     acquired_on=date(2025, 1, 2), cost_per_share=D("11"),
                     fee=D("1.00")),
        ]
        disposal = ShareDisposal(
            id="d", underlying="X", quantity=13, disposed_on=date(2025, 2, 1),
            proceeds_per_share=D("12.34"), fee=D("1.00"),
        )
        result = match(lots, [disposal])
        self.assertEqual(
            q2(sum(a.proceeds for a in result.allocations)), q2(disposal.proceeds)
        )
        self.assertEqual(sum(a.quantity for a in result.allocations), 13)

    def test_disposals_apply_in_date_order(self):
        lots = [
            ShareLot(id="a", underlying="X", quantity=100,
                     acquired_on=date(2025, 1, 1), cost_per_share=D("10")),
            ShareLot(id="b", underlying="X", quantity=100,
                     acquired_on=date(2025, 2, 1), cost_per_share=D("20")),
        ]
        late = ShareDisposal(id="late", underlying="X", quantity=100,
                             disposed_on=date(2025, 6, 1),
                             proceeds_per_share=D("30"))
        early = ShareDisposal(id="early", underlying="X", quantity=100,
                              disposed_on=date(2025, 3, 1),
                              proceeds_per_share=D("15"))
        result = match(lots, [late, early])
        first = [a for a in result.allocations if a.disposal_id == "early"]
        self.assertEqual(first[0].lot_id, "a")   # the earlier disposal took the
        self.assertEqual(result.realized, D("1500.00"))  # earlier lot


class TestAdjustedBasis(unittest.TestCase):
    """What the shares really cost, before and after covered calls."""

    LOT = ShareLot(
        id="lot", underlying="DELTA", quantity=100,
        acquired_on=date(2022, 2, 4), cost_per_share=D("130"),
        source=ShareSource.PUT_ASSIGNMENT,
    )

    def test_basis_before_and_after_calls(self):
        # 13,000 cost, less 600 of acquisition premium, less 2,400 of calls.
        basis = adjusted_basis(
            self.LOT, acq_premium=D("600.00"), cc_premium=D("2400.00")
        )
        self.assertTrue(basis.available)
        self.assertEqual(basis.unit_price, D("124.00"))
        self.assertEqual(basis.after_calls, D("100.00"))
        self.assertEqual(basis.min_call_strike, D("100.00"))
        self.assertEqual(basis.calls_contributed, D("24.00"))

    def test_losing_call_chain_raises_the_basis(self):
        """Premium enters as a net, so a loss pushes the basis up."""
        basis = adjusted_basis(
            self.LOT, acq_premium=D("600.00"), cc_premium=D("-1000.00")
        )
        self.assertGreater(basis.after_calls, basis.unit_price)
        self.assertEqual(basis.after_calls, D("134.00"))

    def test_acquisition_fee_is_included(self):
        lot = ShareLot(id="l", underlying="X", quantity=100,
                       acquired_on=date(2025, 1, 1), cost_per_share=D("10"),
                       fee=D("100.00"))
        self.assertEqual(adjusted_basis(lot).unit_price, D("11.00"))

    def test_no_shares_is_unavailable_not_an_error(self):
        """A ticker holding nothing has no per-share basis to report."""
        basis = adjusted_basis(self.LOT, quantity=0)
        self.assertFalse(basis.available)
        self.assertIsNone(basis.unit_price)
        self.assertIsNone(basis.min_call_strike)
        self.assertIsNone(basis.calls_contributed)
        self.assertIn("no shares", basis.reason)

    def test_partial_holding_follows_the_shares_that_remain(self):
        basis = adjusted_basis(self.LOT, acq_premium=D("600.00"), quantity=50)
        self.assertEqual(basis.quantity, 50)
        self.assertEqual(basis.lot_cost, D("6500.00"))
        self.assertEqual(basis.unit_price, D("118.00"))

    def test_min_call_strike_always_rounds_up(self):
        self.assertEqual(round_up_to_strike(D("100.7975")), D("101.00"))
        self.assertEqual(round_up_to_strike(D("100.00")), D("100.00"))
        self.assertEqual(round_up_to_strike(D("100.01")), D("100.50"))

    def test_blended_across_uneven_lots(self):
        lots = [
            ShareLot(id="a", underlying="ZETA", quantity=300,
                     acquired_on=date(2025, 8, 1), cost_per_share=D("24")),
            ShareLot(id="b", underlying="ZETA", quantity=1500,
                     acquired_on=date(2026, 2, 13), cost_per_share=D("29")),
        ]
        combined = blended([adjusted_basis(l) for l in lots])
        self.assertEqual(combined.quantity, 1800)
        self.assertEqual(combined.unit_price, D("28.17"))  # 50,700 / 1,800
        self.assertIsNone(combined.lot_id)

    def test_blended_of_nothing_is_none(self):
        self.assertIsNone(blended([]))
        self.assertIsNone(blended([adjusted_basis(self.LOT, quantity=0)]))


if __name__ == "__main__":
    unittest.main()


class TestPinnedLotDatedAfterSale(unittest.TestCase):
    """The message must say the lot is too new, not that it is empty."""

    def test_message_names_the_late_lot(self):
        from datetime import date
        from decimal import Decimal
        from bcoj.domain.types import ShareDisposal, ShareLot
        from bcoj.engine.shares import InsufficientSharesError, match
        lot = ShareLot(id="lot-late", underlying="ACME", quantity=400,
                       acquired_on=date(2026, 9, 18), cost_per_share=Decimal("10"))
        sale = ShareDisposal(id="d1", underlying="ACME", quantity=400,
                             disposed_on=date(2026, 9, 3),
                             proceeds_per_share=Decimal("12"),
                             specific_lot_ids=("lot-late",))
        with self.assertRaises(InsufficientSharesError) as ctx:
            match([lot], [sale])
        self.assertIn("acquired on 2026-09-18, after the sale", str(ctx.exception))


class TestRollPreview(unittest.TestCase):
    """The roll panel is the real roll run on a copy. Figures by hand:

    Leg: 10 short puts at 35, opened 3.00, fee 6.50 -> open cash 2,993.50.
    Roll: buy back 4.00 (fee 6.50) -> close cash -4,006.50, realizes -1,013.00.
    New: 12 puts at 34 for 5.00, fee 7.80 -> open cash 5,992.20.
    """

    def setUp(self):
        from bcoj.engine.decide import roll_preview
        self.roll_preview = roll_preview
        self.leg = position(quantity=10, open_price=D("3.00"), open_fee=D("6.50"),
                            expiry=date(2026, 2, 20))
        self.kw = dict(close_price=D("4.00"), close_fee=D("6.50"),
                       new_expiry=date(2026, 3, 20), new_strike=D("34"),
                       new_price=D("5.00"), new_fee=D("7.80"), new_quantity=12,
                       on=date(2026, 2, 6))

    def test_credit_roll_that_grows(self):
        pv = self.roll_preview([self.leg], self.leg, **self.kw)
        self.assertEqual(pv.closing_realized, D("-1013.00"))
        self.assertEqual(pv.this_roll, D("1985.70"))
        self.assertTrue(pv.is_credit)
        self.assertEqual(pv.carry_before, D("0.00"))
        self.assertEqual(pv.carry_after, D("-1013.00"))
        self.assertEqual(pv.net_credit_before, D("2993.50"))
        self.assertEqual(pv.net_credit_after, D("4979.20"))
        self.assertEqual(pv.break_even_before.price, D("32.01"))
        self.assertEqual(pv.break_even_after.price, D("29.85"))
        self.assertEqual(pv.at_risk_before, D("35000.00"))
        self.assertEqual(pv.at_risk_after, D("40800.00"))
        self.assertEqual(pv.to_recover_after, D("0.00"))
        self.assertFalse(pv.underwater_after)
        self.assertTrue(pv.target_after.applicable)
        self.assertEqual(pv.size_change, 2)
        self.assertTrue(pv.grows)
        self.assertEqual(pv.strike_change, D("-1.00"))
        self.assertEqual(pv.dte_after, 42)

    def test_debit_roll_that_goes_underwater(self):
        # Buy back at 8.00 and take only 3.00 for the new leg.
        kw = dict(self.kw, close_price=D("8.00"), new_price=D("3.00"))
        pv = self.roll_preview([self.leg], self.leg, **kw)
        self.assertEqual(pv.closing_realized, D("-5013.00"))
        self.assertEqual(pv.this_roll, D("-4414.30"))
        self.assertFalse(pv.is_credit)
        self.assertEqual(pv.net_credit_after, D("-1420.80"))
        self.assertEqual(pv.to_recover_after, D("1420.80"))
        self.assertTrue(pv.underwater_after)
        self.assertFalse(pv.target_after.applicable)
        # A net debit raises a put's break-even above the strike.
        self.assertEqual(pv.break_even_after.price, D("35.18"))

    def test_carry_from_earlier_legs_flows_through(self):
        earlier = closed(id="p0", quantity=10, open_price=D("3.00"), open_fee=D("6.50"),
                         close_price=D("0.50"), close_fee=D("6.50"),
                         status=Status.ROLLED)           # realized 2,487.00
        leg = position(quantity=10, open_price=D("3.00"), open_fee=D("6.50"),
                       expiry=date(2026, 2, 20), rolled_from_id="p0")
        pv = self.roll_preview([earlier, leg], leg, **self.kw)
        self.assertEqual(pv.carry_before, D("2487.00"))
        self.assertEqual(pv.carry_after, D("1474.00"))
        self.assertEqual(pv.net_credit_after, D("7466.20"))   # 5,992.20 + 1,474.00

    def test_the_journal_is_not_touched(self):
        self.roll_preview([self.leg], self.leg, **self.kw)
        self.assertTrue(self.leg.is_open)
        self.assertIsNone(self.leg.close_price)


class TestObligations(unittest.TestCase):
    def test_grouped_by_expiry_with_cash_and_shares(self):
        from bcoj.engine.decide import obligations
        rows = [
            position(id="a", quantity=10, strike=D("35"), expiry=date(2026, 3, 20)),
            position(id="b", underlying="BETA", quantity=2, strike=D("20"),
                     expiry=date(2026, 3, 20)),
            position(id="c", right=Right.CALL, quantity=3, strike=D("50"),
                     expiry=date(2026, 4, 17)),
            position(id="d", direction=Direction.LONG, expiry=date(2026, 3, 20)),
            closed(id="e", quantity=5, expiry=date(2026, 3, 20), close_price=D("0.10")),
        ]
        days = obligations(rows, today=date(2026, 3, 1))
        self.assertEqual([d.expiry for d in days], [date(2026, 3, 20), date(2026, 4, 17)])
        first, second = days
        self.assertEqual(first.dte, 19)
        self.assertEqual(first.cash_if_assigned, D("39000.00"))   # 35,000 + 4,000
        self.assertEqual(first.shares_to_deliver, 0)
        self.assertEqual(first.tickers, ("ACME", "BETA"))
        self.assertEqual([o.position.id for o in first.items], ["a", "b"])
        self.assertEqual(second.cash_if_assigned, D("0.00"))
        self.assertEqual(second.shares_to_deliver, 300)
        self.assertIsNotNone(first.items[0].break_even)


class TestConcentration(unittest.TestCase):
    def test_covered_calls_ride_on_their_shares_and_naked_ones_are_counted(self):
        from bcoj.engine.decide import concentration, total_exposure
        rows = [
            position(id="a", quantity=10, strike=D("35"), open_price=D("3.00"),
                     open_fee=D("6.50")),                                 # 35,000 at risk
            position(id="b", right=Right.CALL, quantity=2, strike=D("40"),
                     share_lot_id="lot1"),                                # covered: shares carry it
            position(id="c", underlying="GAMMA", right=Right.CALL, quantity=1,
                     strike=D("90")),                                     # naked
            # 200 - 0.65 open fee - 100 buy-back, no closing fee: realized 99.35
            closed(id="d", quantity=1, open_price=D("2.00"), close_price=D("1.00")),
        ]
        risks = concentration(rows, {"ACME": D("12000.00")})
        self.assertEqual([t.underlying for t in risks], ["ACME", "GAMMA"])
        acme, gamma = risks
        self.assertEqual(acme.at_risk, D("35000.00"))
        self.assertEqual(acme.shares_at_cost, D("12000.00"))
        self.assertEqual(acme.exposure, D("47000.00"))
        self.assertEqual(acme.naked_calls, 0)
        self.assertEqual(acme.open_positions, 2)
        self.assertEqual(acme.realized, D("99.35"))
        self.assertEqual(gamma.naked_calls, 1)
        self.assertEqual(gamma.exposure, D("0.00"))
        self.assertEqual(total_exposure(risks), D("47000.00"))
