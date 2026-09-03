"""Tests for entry actions and validation. All figures hand-computed."""

import unittest
from datetime import date
from decimal import Decimal

from bcoj.domain.enums import (
    Direction,
    DisposalKind,
    Right,
    ShareSource,
    Status,
)
from bcoj.domain.money import ZERO
from bcoj.domain.types import Position
from bcoj.engine import actions, validate
from bcoj.engine.chains import ChainIndex
from bcoj.engine.pnl import open_cash, realized_pl
from bcoj.engine.risk import break_even

D = Decimal


def position(**kw) -> Position:
    base = dict(
        id="p1",
        underlying="ACME",
        expiry=date(2026, 3, 20),
        strike=D("35"),
        right=Right.PUT,
        direction=Direction.SHORT,
        quantity=10,
        opened_on=date(2026, 1, 5),
        open_price=D("3.00"),
        open_fee=D("6.50"),
        close_fee=D("6.50"),
        status=Status.OPEN,
    )
    base.update(kw)
    return Position(**base)


class TestClose(unittest.TestCase):
    def test_close_records_the_price_and_realizes(self):
        result = actions.close(
            position(), date(2026, 2, 10), D("1.50"), D("6.50")
        )
        closed = result.updated[0]
        self.assertEqual(closed.status, Status.CLOSED)
        self.assertEqual(closed.closed_on, date(2026, 2, 10))
        # 2,993.50 collected, 1,506.50 paid back.
        self.assertEqual(realized_pl(closed), D("1487.00"))

    def test_cannot_close_twice(self):
        closed = actions.close(position(), date(2026, 2, 10), D("1.50")).updated[0]
        with self.assertRaises(actions.ActionError) as ctx:
            actions.close(closed, date(2026, 2, 11), D("1.00"))
        self.assertIn("already CLOSED", str(ctx.exception))

    def test_negative_price_refused(self):
        with self.assertRaises(actions.ActionError):
            actions.close(position(), date(2026, 2, 10), D("-1"))


class TestExpire(unittest.TestCase):
    def test_expiry_realizes_the_whole_credit(self):
        result = actions.expire(position())
        expired = result.updated[0]
        self.assertEqual(expired.status, Status.EXPIRED)
        self.assertEqual(expired.closed_on, date(2026, 3, 20))
        self.assertEqual(expired.close_price, ZERO)
        self.assertEqual(expired.close_fee, ZERO)
        self.assertEqual(realized_pl(expired), open_cash(position()))
        self.assertEqual(realized_pl(expired), D("2993.50"))


class TestAssign(unittest.TestCase):
    def test_short_put_acquires_shares_at_the_strike(self):
        result = actions.assign(position())
        assigned = result.updated[0]
        self.assertEqual(assigned.status, Status.ASSIGNED)
        self.assertEqual(realized_pl(assigned), D("2993.50"))

        self.assertEqual(len(result.lots), 1)
        lot = result.lots[0]
        self.assertEqual(lot.quantity, 1000)
        self.assertEqual(lot.cost_per_share, D("35"))
        self.assertEqual(lot.source, ShareSource.PUT_ASSIGNMENT)
        self.assertEqual(lot.assigning_position_id, "p1")
        self.assertEqual(result.disposals, [])

    def test_short_call_delivers_shares_at_the_strike(self):
        result = actions.assign(
            position(right=Right.CALL, share_lot_id="lot-1")
        )
        self.assertEqual(result.lots, [])
        disposal = result.disposals[0]
        self.assertEqual(disposal.quantity, 1000)
        self.assertEqual(disposal.proceeds_per_share, D("35"))
        self.assertEqual(disposal.kind, DisposalKind.CALLED_AWAY)
        # Earmarked, so matching cannot reach past the covering lot.
        self.assertEqual(disposal.specific_lot_ids, ("lot-1",))

    def test_long_call_exercise_acquires(self):
        result = actions.assign(
            position(direction=Direction.LONG, right=Right.CALL)
        )
        self.assertEqual(result.lots[0].source, ShareSource.CALL_EXERCISE)
        self.assertEqual(result.disposals, [])

    def test_long_put_exercise_delivers(self):
        result = actions.assign(position(direction=Direction.LONG))
        self.assertEqual(result.lots, [])
        self.assertEqual(result.disposals[0].kind, DisposalKind.SOLD)

    def test_the_share_side_is_never_optional(self):
        """Every assignment moves stock; forgetting it is how sheets drift."""
        for direction in (Direction.SHORT, Direction.LONG):
            for right in (Right.PUT, Right.CALL):
                with self.subTest(direction=direction, right=right):
                    result = actions.assign(
                        position(direction=direction, right=right)
                    )
                    self.assertEqual(
                        len(result.lots) + len(result.disposals), 1
                    )


class TestRoll(unittest.TestCase):
    def _rolled(self, **kw):
        base = dict(
            close_price=D("4.00"),
            new_expiry=date(2026, 4, 17),
            new_strike=D("33"),
            new_price=D("4.50"),
            on=date(2026, 2, 20),
            close_fee=D("6.50"),
            new_fee=D("6.50"),
            new_id_="p2",
        )
        base.update(kw)
        return actions.roll(position(), **base)

    def test_roll_closes_one_leg_and_opens_the_next(self):
        result = self._rolled()
        old, new = result.updated[0], result.created[0]

        self.assertEqual(old.status, Status.ROLLED)
        self.assertEqual(realized_pl(old), D("-1013.00"))  # 2,993.50 - 4,006.50

        self.assertEqual(new.status, Status.OPEN)
        self.assertEqual(new.rolled_from_id, "p1")
        self.assertEqual(new.strike, D("33"))
        self.assertEqual(new.quantity, 10)
        self.assertEqual(open_cash(new), D("4493.50"))

    def test_chain_carries_the_loss_forward(self):
        result = self._rolled()
        index = ChainIndex(result.positions)
        new = result.created[0]
        self.assertEqual(index.carry(new), D("-1013.00"))
        # 4,493.50 collected against 1,013.00 carried.
        self.assertEqual(index.chain(new).net_credit, D("3480.50"))

    def test_a_resizing_roll_is_recorded(self):
        result = self._rolled(new_quantity=15)
        self.assertEqual(result.created[0].quantity, 15)
        self.assertIn("10->15", result.summary)

    def test_covering_link_and_target_are_inherited(self):
        result = actions.roll(
            position(share_lot_id="lot-1", target_pct=D("0.30")),
            close_price=D("1.00"), new_expiry=date(2026, 4, 17),
            new_strike=D("33"), new_price=D("2.00"), on=date(2026, 2, 20),
        )
        new = result.created[0]
        self.assertEqual(new.share_lot_id, "lot-1")
        self.assertEqual(new.target_pct, D("0.30"))

    def test_zero_quantity_refused(self):
        with self.assertRaises(actions.ActionError):
            self._rolled(new_quantity=0)


class TestSplit(unittest.TestCase):
    """The partial-assignment workflow, and the invariants that protect it."""

    def _split(self, quantity=4):
        return actions.split(
            position(), quantity, on=date(2026, 2, 1), ids=("a", "b")
        )

    def test_split_produces_two_halves_and_a_record(self):
        result = self._split()
        parent = result.updated[0]
        first, second = result.created

        self.assertEqual(parent.status, Status.SPLIT)
        self.assertTrue(parent.is_superseded)
        self.assertEqual(first.quantity, 4)
        self.assertEqual(second.quantity, 6)
        for child in (first, second):
            self.assertEqual(child.split_from_id, "p1")
            self.assertEqual(child.split_from_quantity, 10)
            self.assertEqual(child.opened_on, date(2026, 1, 5))
            self.assertEqual(child.open_price, D("3.00"))
            self.assertTrue(child.is_open)

    def test_fees_are_pro_rata_and_sum_exactly(self):
        first, second = self._split().created
        self.assertEqual(first.open_fee, D("2.60"))
        self.assertEqual(second.open_fee, D("3.90"))
        self.assertEqual(first.open_fee + second.open_fee, D("6.50"))

    def test_fee_remainder_lands_on_the_first_half(self):
        """An indivisible fee must still sum to what was actually paid."""
        result = actions.split(
            position(quantity=3, open_fee=D("1.00")), 1, ids=("a", "b")
        )
        first, second = result.created
        self.assertEqual(first.open_fee + second.open_fee, D("1.00"))

    def test_splitting_changes_no_total(self):
        """The invariant that makes the tombstone safe.

        Premium and realized P/L must be identical before and after, or a
        split silently inflates the book.
        """
        original = position()
        before_premium = open_cash(original)
        before_realized = realized_pl(original)

        result = self._split()
        after_premium = sum(open_cash(p) for p in result.created)
        after_realized = sum(realized_pl(p) for p in result.positions)

        self.assertEqual(after_premium, before_premium)
        self.assertEqual(after_realized, before_realized)
        # The superseded parent contributes nothing.
        self.assertEqual(realized_pl(result.updated[0]), ZERO)

    def test_carry_is_divided_not_duplicated(self):
        """Both halves inheriting the full history would double-count it."""
        parent = position(
            quantity=10, open_price=D("3.00"), open_fee=D("6.50"),
            rolled_from_id="prior",
        )
        prior = position(
            id="prior", quantity=10, open_price=D("0"), open_fee=D("1000.00"),
            close_price=D("0"), close_fee=D("0"),
            status=Status.ROLLED, closed_on=date(2026, 1, 5),
        )
        result = actions.split(parent, 4, ids=("a", "b"))
        index = ChainIndex([prior] + result.positions)

        self.assertEqual(index.carry(parent), D("-1000.00"))
        first, second = result.created
        self.assertEqual(index.carry(first), D("-400.00"))    # 4/10
        self.assertEqual(index.carry(second), D("-600.00"))   # 6/10
        self.assertEqual(
            index.carry(first) + index.carry(second), index.carry(parent)
        )

    def test_break_even_stays_sane_after_a_split(self):
        """Pro-rata carry is what keeps this meaningful.

        Giving each half the whole chain loss would spread it over a fraction
        of the contracts and report a break-even far too high.
        """
        parent = position(quantity=10, open_price=D("3.00"),
                          open_fee=D("0"), rolled_from_id="prior")
        prior = position(
            id="prior", quantity=10, open_price=D("0"), open_fee=D("1000.00"),
            close_price=D("0"), close_fee=D("0"),
            status=Status.ROLLED, closed_on=date(2026, 1, 5),
        )
        whole = break_even(ChainIndex([prior, parent]).chain(parent)).price

        result = actions.split(parent, 4, ids=("a", "b"))
        index = ChainIndex([prior] + result.positions)
        halves = [
            break_even(index.chain(child)).price for child in result.created
        ]
        # Both halves break even exactly where the undivided position did.
        self.assertEqual(halves, [whole, whole])
        self.assertEqual(whole, D("33.00"))   # 35 - (3,000 - 1,000) / 1,000

    def test_split_then_assign_one_half_and_roll_the_other(self):
        """The workflow this all exists for."""
        result = self._split()
        assigned_half, rolled_half = result.created

        assigned = actions.assign(assigned_half)
        self.assertEqual(assigned.lots[0].quantity, 400)
        self.assertEqual(assigned.updated[0].status, Status.ASSIGNED)

        rolled = actions.roll(
            rolled_half, close_price=D("2.00"), new_expiry=date(2026, 4, 17),
            new_strike=D("33"), new_price=D("2.50"), on=date(2026, 2, 1),
        )
        self.assertEqual(rolled.updated[0].status, Status.ROLLED)
        self.assertEqual(rolled.created[0].quantity, 6)

    def test_split_bounds(self):
        for bad in (0, 10, 11, -1):
            with self.subTest(quantity=bad):
                with self.assertRaises(actions.ActionError):
                    actions.split(position(), bad)

    def test_cannot_split_a_closed_position(self):
        closed = actions.close(position(), date(2026, 2, 1), D("1.00")).updated[0]
        with self.assertRaises(actions.ActionError):
            actions.split(closed, 4)


class TestShareActions(unittest.TestCase):
    def test_buy_shares_outright(self):
        result = actions.buy_shares("acme", 300, D("12.50"), date(2026, 1, 5))
        lot = result.lots[0]
        self.assertEqual(lot.underlying, "ACME")
        self.assertEqual(lot.quantity, 300)
        self.assertEqual(lot.source, ShareSource.OUTRIGHT_BUY)
        self.assertEqual(result.created, [])

    def test_sell_shares_outright(self):
        result = actions.sell_shares("acme", 300, D("14.00"), date(2026, 6, 5))
        self.assertEqual(result.disposals[0].kind, DisposalKind.SOLD)

    def test_buy_write_links_the_call_to_the_lot(self):
        result = actions.buy_write(
            "acme", shares=300, share_price=D("12.00"),
            expiry=date(2026, 3, 20), strike=D("13"), call_price=D("0.50"),
            on=date(2026, 1, 5), lot_id="lot-1", position_id="call-1",
        )
        lot, call = result.lots[0], result.created[0]
        self.assertEqual(call.share_lot_id, "lot-1")
        self.assertEqual(call.quantity, 3)
        self.assertEqual(call.right, Right.CALL)
        self.assertEqual(call.direction, Direction.SHORT)
        self.assertEqual(lot.source, ShareSource.BUY_WRITE)

    def test_buy_write_refuses_to_write_more_calls_than_it_covers(self):
        with self.assertRaises(actions.ActionError):
            actions.buy_write(
                "acme", shares=150, share_price=D("12.00"),
                expiry=date(2026, 3, 20), strike=D("13"),
                call_price=D("0.50"), on=date(2026, 1, 5), contracts=2,
            )

    def test_buy_write_needs_a_full_contract(self):
        with self.assertRaises(actions.ActionError):
            actions.buy_write(
                "acme", shares=50, share_price=D("12.00"),
                expiry=date(2026, 3, 20), strike=D("13"),
                call_price=D("0.50"), on=date(2026, 1, 5),
            )

    def test_negative_quantities_refused(self):
        with self.assertRaises(actions.ActionError):
            actions.buy_shares("acme", -1, D("10"), date(2026, 1, 5))
        with self.assertRaises(actions.ActionError):
            actions.sell_shares("acme", 0, D("10"), date(2026, 1, 5))


class TestValidation(unittest.TestCase):
    def messages(self, problems, level=None):
        return [
            p.message for p in problems
            if level is None or p.level == level
        ]

    def test_a_sound_position_passes(self):
        self.assertEqual(validate.errors(validate.validate_position(position())), [])

    def test_expiry_before_open_is_an_error(self):
        problems = validate.validate_position(
            position(expiry=date(2025, 1, 1))
        )
        self.assertTrue(validate.errors(problems))
        self.assertIn("expiry", [p.field for p in validate.errors(problems)])

    def test_close_before_open_is_an_error(self):
        closed = position(
            status=Status.CLOSED, closed_on=date(2025, 12, 1),
            close_price=D("1.00"),
        )
        self.assertTrue(validate.errors(validate.validate_position(closed)))

    def test_open_position_with_a_close_is_an_error(self):
        bad = position(close_price=D("1.00"))
        problems = validate.errors(validate.validate_position(bad))
        self.assertIn("status", [p.field for p in problems])

    def test_closed_position_without_a_date_is_an_error(self):
        bad = position(status=Status.CLOSED, close_price=D("1.00"))
        self.assertTrue(validate.errors(validate.validate_position(bad)))

    def test_off_grid_strike_is_only_a_warning(self):
        problems = validate.validate_position(position(strike=D("27.33")))
        self.assertEqual(validate.errors(problems), [])
        self.assertTrue(
            any(p.field == "strike" for p in validate.warnings(problems))
        )

    def test_unusual_fee_warns_but_a_wild_one_errors(self):
        mild = validate.validate_position(position(open_fee=D("0.10")))
        self.assertEqual(validate.errors(mild), [])
        self.assertTrue(validate.warnings(mild))

        wild = validate.validate_position(position(open_fee=D("300.00")))
        self.assertTrue(validate.errors(wild))

    def test_naked_short_call_warns(self):
        problems = validate.validate_position(position(right=Right.CALL))
        self.assertTrue(
            any("naked" in p.message for p in validate.warnings(problems))
        )
        covered = validate.validate_position(
            position(right=Right.CALL, share_lot_id="lot-1")
        )
        self.assertFalse(any("naked" in p.message for p in covered))

    def test_leg_opening_before_its_predecessor_is_an_error(self):
        parent = position(id="parent", opened_on=date(2026, 2, 1))
        child = position(
            id="child", opened_on=date(2026, 1, 5), rolled_from_id="parent"
        )
        problems = validate.validate_position(child, existing=[parent])
        self.assertIn("opened_on", [p.field for p in validate.errors(problems)])

    def test_rolling_into_a_different_ticker_is_an_error(self):
        parent = position(id="parent", underlying="BETA")
        child = position(id="child", rolled_from_id="parent",
                         opened_on=date(2026, 2, 1))
        self.assertTrue(
            validate.errors(validate.validate_position(child, existing=[parent]))
        )

    def test_missing_predecessor_only_warns(self):
        problems = validate.validate_position(
            position(rolled_from_id="absent"), existing=[]
        )
        self.assertEqual(validate.errors(problems), [])
        self.assertTrue(validate.warnings(problems))

    def test_duplicate_entry_warns(self):
        first = position(id="first")
        again = position(id="second")
        problems = validate.validate_position(again, existing=[first])
        self.assertTrue(
            any(p.field == "duplicate" for p in validate.warnings(problems))
        )

    def test_a_closed_lookalike_is_not_a_duplicate(self):
        earlier = position(
            id="first", status=Status.CLOSED, closed_on=date(2026, 2, 1),
            close_price=D("1.00"),
        )
        problems = validate.validate_position(position(id="second"),
                                              existing=[earlier])
        self.assertFalse(
            any(p.field == "duplicate" for p in problems)
        )


class TestExpiryQueue(unittest.TestCase):
    def test_past_expiry_positions_surface_first(self):
        overdue = position(id="overdue", expiry=date(2026, 1, 16))
        today_ = position(id="today", expiry=date(2026, 2, 20))
        future = position(id="future", expiry=date(2026, 6, 19))

        queue = validate.expiring(
            [overdue, today_, future], today=date(2026, 2, 20)
        )
        self.assertEqual([p.id for p, _ in queue], ["overdue", "today"])
        self.assertEqual(queue[0][1], -35)
        self.assertEqual(queue[1][1], 0)

    def test_window_widens_the_queue(self):
        soon = position(id="soon", expiry=date(2026, 2, 27))
        queue = validate.expiring(
            [soon], today=date(2026, 2, 20), within_days=7
        )
        self.assertEqual([p.id for p, _ in queue], ["soon"])

    def test_closed_positions_never_appear(self):
        closed = position(
            status=Status.EXPIRED, expiry=date(2026, 1, 16),
            closed_on=date(2026, 1, 16), close_price=ZERO,
        )
        self.assertEqual(
            validate.expiring([closed], today=date(2026, 2, 20)), []
        )


if __name__ == "__main__":
    unittest.main()
