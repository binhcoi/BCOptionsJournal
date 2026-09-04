"""Storage tests: migrations, round-tripping, rebuilds, batch reverts."""

import pathlib
import tempfile
import unittest
from decimal import Decimal

from bcoj.db import schema, store
from bcoj.domain.enums import MatchingRule
from bcoj.importer import shares as share_import, triage
from bcoj.importer.estimates import EstimatedLot
from bcoj.importer.legacy_csv import parse_file

D = Decimal
FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "synthetic_rows.csv"

# The fixture's OMEGA ticker is short 100 shares by construction.
OMEGA_ESTIMATE = (
    EstimatedLot(underlying="OMEGA", quantity=100, cost_per_share=D("20"),
                 note="fixture: reconstructs the missing acquisition"),
)


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = str(pathlib.Path(self._dir.name) / "journal.db")
        self.conn = store.open_db(self.path)
        self.report = parse_file(FIXTURE)

    def tearDown(self):
        self.conn.close()
        self._dir.cleanup()

    def _import(self, estimates=False):
        batch = store.record_batch(
            self.conn, str(FIXTURE), row_count=len(self.report.rows)
        )
        store.save_positions(self.conn, self.report.positions, batch_id=batch)
        derived = share_import.derive(
            self.report.rows,
            include_estimates=estimates,
            estimates=OMEGA_ESTIMATE if estimates else None,
        )
        store.save_shares(self.conn, derived.lots, derived.disposals, batch_id=batch)
        store.save_flags(self.conn, triage.triage(self.report.rows))
        return batch, derived


class TestSchema(StoreTestCase):
    def test_migration_applied(self):
        self.assertEqual(schema.current_version(self.conn), schema.SCHEMA_VERSION)

    def test_migrate_is_idempotent(self):
        schema.migrate(self.conn)
        schema.migrate(self.conn)
        self.assertEqual(schema.current_version(self.conn), schema.SCHEMA_VERSION)

    def test_defaults_seeded(self):
        self.assertEqual(store.get_setting(self.conn, "fee_rate"), "0.65")
        self.assertEqual(store.get_setting(self.conn, "profit_target_pct"), "0.50")
        self.assertIs(store.matching_rule(self.conn), MatchingRule.FIFO)

    def test_settings_round_trip(self):
        store.set_setting(self.conn, "profit_target_pct", "0.60")
        self.assertEqual(store.get_setting(self.conn, "profit_target_pct"), "0.60")

    def test_money_columns_are_text_not_real(self):
        """A REAL column would silently corrupt accounting."""
        cols = {
            r["name"]: r["type"]
            for r in self.conn.execute("PRAGMA table_info(positions)")
        }
        for name in ("strike", "open_price", "open_fee", "close_price", "close_fee"):
            self.assertEqual(cols[name], "TEXT", name)

    def test_position_cannot_be_its_own_parent(self):
        import sqlite3

        with self.assertRaises(sqlite3.IntegrityError):
            with self.conn:
                self.conn.execute(
                    "INSERT INTO positions (id, underlying, expiry, strike,"
                    " option_right, direction, quantity, opened_on, open_price,"
                    " status, rolled_from_id) VALUES"
                    " ('a','X','2026-01-16','45','PUT','SHORT',1,'2025-01-01',"
                    "'1.00','OPEN','a')"
                )


class TestRoundTrip(StoreTestCase):
    def test_positions_survive_a_round_trip(self):
        self._import()
        loaded = {p.id: p for p in store.load_positions(self.conn)}
        self.assertEqual(len(loaded), 23)

        original = {p.id: p for p in self.report.positions}
        for pid, before in original.items():
            after = loaded[pid]
            with self.subTest(position=pid):
                self.assertEqual(after.underlying, before.underlying)
                self.assertEqual(after.strike, before.strike)
                self.assertEqual(after.open_price, before.open_price)
                self.assertEqual(after.open_fee, before.open_fee)
                self.assertEqual(after.close_price, before.close_price)
                self.assertEqual(after.direction, before.direction)
                self.assertEqual(after.quantity, before.quantity)
                self.assertEqual(after.status, before.status)
                self.assertEqual(after.expiry, before.expiry)
                self.assertEqual(after.closed_on, before.closed_on)

    def test_decimals_come_back_exact(self):
        self._import()
        loaded = {p.id: p for p in store.load_positions(self.conn)}
        strike = next(
            p.strike for p in loaded.values() if p.strike == D("27.33")
        )
        self.assertEqual(strike, D("27.33"))
        self.assertIsInstance(strike, Decimal)

    def test_chain_links_are_preserved(self):
        self._import()
        loaded = {p.id: p for p in store.load_positions(self.conn)}
        child = next(p for p in loaded.values() if p.id.startswith("a1000006"))
        self.assertIsNotNone(child.rolled_from_id)
        self.assertIn(child.rolled_from_id, loaded)

    def test_forward_reference_in_file_order_still_links(self):
        """A roll may name a parent that appears later in the export."""
        self._import()
        expected = sum(
            1 for p in self.report.positions if p.rolled_from_id is not None
        )
        self.assertEqual(expected, 5)  # the fixture's chain links

        stored = self.conn.execute(
            "SELECT COUNT(*) AS n FROM positions WHERE rolled_from_id IS NOT NULL"
        ).fetchone()["n"]
        self.assertEqual(stored, expected)

    def test_shares_round_trip(self):
        _, derived = self._import()
        self.assertEqual(len(store.load_lots(self.conn)), len(derived.lots))
        self.assertEqual(
            len(store.load_disposals(self.conn)), len(derived.disposals)
        )

    def test_earmarked_lots_survive(self):
        self._import()
        earmarked = [
            d for d in store.load_disposals(self.conn) if d.specific_lot_ids
        ]
        self.assertTrue(earmarked)
        for disposal in earmarked:
            self.assertTrue(all(disposal.specific_lot_ids))


class TestAllocations(StoreTestCase):
    def test_rebuild_reports_a_share_deficit(self):
        self._import(estimates=False)
        errors = store.rebuild_allocations(self.conn)
        self.assertIn("OMEGA", errors)
        self.assertIn("missing", errors["OMEGA"])

    def test_rebuild_succeeds_with_estimates(self):
        self._import(estimates=True)
        errors = store.rebuild_allocations(self.conn)
        self.assertEqual(errors, {})

        total = self.conn.execute(
            "SELECT SUM(CAST(realized_pl AS REAL)) AS t FROM share_allocations"
        ).fetchone()["t"]
        # DELTA 1,200 + KAPPA 2,000 + ZETA 100 + OMEGA 1,801
        self.assertAlmostEqual(total, 5101.00, places=2)

    def test_rebuild_is_idempotent(self):
        self._import(estimates=True)
        store.rebuild_allocations(self.conn)
        first = self.conn.execute(
            "SELECT COUNT(*) AS n FROM share_allocations"
        ).fetchone()["n"]
        store.rebuild_allocations(self.conn)
        second = self.conn.execute(
            "SELECT COUNT(*) AS n FROM share_allocations"
        ).fetchone()["n"]
        self.assertEqual(first, second)

    def test_changing_the_rule_is_a_rebuild_not_a_migration(self):
        self._import(estimates=True)
        store.rebuild_allocations(self.conn, MatchingRule.FIFO)
        fifo = self.conn.execute(
            "SELECT SUM(CAST(realized_pl AS REAL)) AS t FROM share_allocations"
        ).fetchone()["t"]

        store.rebuild_allocations(self.conn, MatchingRule.LIFO)
        lifo = self.conn.execute(
            "SELECT SUM(CAST(realized_pl AS REAL)) AS t FROM share_allocations"
        ).fetchone()["t"]

        self.assertNotAlmostEqual(fifo, lifo, places=2)


class TestBatches(StoreTestCase):
    def test_reimporting_the_same_file_is_refused(self):
        self._import()
        with self.assertRaises(store.AlreadyImportedError):
            store.record_batch(self.conn, str(FIXTURE), row_count=23)

    def test_revert_removes_everything_the_batch_wrote(self):
        batch, _ = self._import(estimates=True)
        store.rebuild_allocations(self.conn)
        self.assertEqual(len(store.load_positions(self.conn)), 23)

        store.revert_batch(self.conn, batch)
        self.assertEqual(store.load_positions(self.conn), [])
        self.assertEqual(store.load_lots(self.conn), [])
        self.assertEqual(store.load_disposals(self.conn), [])
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) AS n FROM share_allocations"
            ).fetchone()["n"],
            0,
        )

    def test_revert_then_reimport_is_allowed(self):
        batch, _ = self._import()
        store.revert_batch(self.conn, batch)
        again = store.record_batch(self.conn, str(FIXTURE), row_count=23)
        self.assertNotEqual(again, batch)


class TestFlagsAndAudit(StoreTestCase):
    def test_flags_are_stored_and_resolvable(self):
        self._import()
        flags = store.open_flags(self.conn)
        self.assertTrue(flags)

        store.resolve_flag(self.conn, flags[0]["id"], "converted to a share lot")
        remaining = store.open_flags(self.conn)
        self.assertEqual(len(remaining), len(flags) - 1)

    def test_audit_log_records_before_and_after(self):
        store.log(
            self.conn,
            "position",
            "abc",
            "edit",
            before={"strike": "45"},
            after={"strike": "46"},
        )
        row = self.conn.execute("SELECT * FROM audit_log").fetchone()
        self.assertEqual(row["action"], "edit")
        self.assertIn("45", row["before"])
        self.assertIn("46", row["after"])


if __name__ == "__main__":
    unittest.main()


class TestTagsNotesAndViews(StoreTestCase):
    def _position(self, pid="t1", **kw):
        from datetime import date
        from decimal import Decimal
        from bcoj.domain.enums import Direction, Right
        from bcoj.domain.types import Position
        base = dict(id=pid, underlying="ACME", expiry=date(2026, 3, 20), strike=Decimal("35"),
                    right=Right.PUT, direction=Direction.SHORT, quantity=1,
                    opened_on=date(2026, 1, 5), open_price=Decimal("1.00"))
        base.update(kw)
        return Position(**base)

    def test_tags_normalize_and_round_trip(self):
        self.assertEqual(store.normalize_tags("wheel, #earnings  Wheel,income"),
                         ("wheel", "earnings", "income"))
        store.save_positions(self.conn, [self._position()])
        store.update_notes(self.conn, "t1", "sold into IV spike", ("wheel", "earnings"))
        p = store.load_position(self.conn, "t1")
        self.assertEqual(p.notes, "sold into IV spike")
        self.assertEqual(p.tags, ("earnings", "wheel"))          # alphabetical
        self.assertEqual([q.tags for q in store.load_positions(self.conn)], [("earnings", "wheel")])
        self.assertEqual(store.all_tags(self.conn), ["earnings", "wheel"])
        # Replacing tags drops the old links; the edit is audited.
        store.update_notes(self.conn, "t1", "kept", ("wheel",))
        self.assertEqual(store.load_position(self.conn, "t1").tags, ("wheel",))
        entries = [e for e in store.audit_entries(self.conn) if e["entity_id"] == "t1"]
        self.assertTrue(any(e["action"].startswith("update: notes") for e in entries))

    def test_tags_given_at_creation_are_kept(self):
        from bcoj.engine.actions import ActionResult
        p = self._position("t2", tags=("income",))
        store.apply(self.conn, ActionResult(created=[p]), "entered by hand")
        self.assertEqual(store.load_position(self.conn, "t2").tags, ("income",))

    def test_saved_views_upsert_by_name(self):
        a = store.save_view(self.conn, "Wheels", "show=open&tag=wheel")
        b = store.save_view(self.conn, "Wheels", "show=all&tag=wheel")
        self.assertEqual(a, b)
        views = store.load_views(self.conn)
        self.assertEqual([(v["name"], v["filter"]) for v in views], [("Wheels", "show=all&tag=wheel")])
        store.delete_view(self.conn, a)
        self.assertEqual(store.load_views(self.conn), [])
        with self.assertRaises(ValueError):
            store.save_view(self.conn, "  ", "show=open")


class TestSnapshots(StoreTestCase):
    def _position(self, pid):
        from datetime import date
        from decimal import Decimal
        from bcoj.domain.enums import Direction, Right
        from bcoj.domain.types import Position
        return Position(id=pid, underlying="ACME", expiry=date(2026, 3, 20), strike=Decimal("35"),
                        right=Right.PUT, direction=Direction.SHORT, quantity=1,
                        opened_on=date(2026, 1, 5), open_price=Decimal("1.00"))

    def test_snapshot_restore_and_the_safety_copy(self):
        store.save_positions(self.conn, [self._position("keep")])
        name = store.snapshot(self.conn, self.path, "before cleanup")
        self.assertTrue(name.startswith("journal-") and name.endswith("-before-cleanup.db"))
        self.assertTrue((store.snapshots_dir(self.path) / name).exists())
        self.assertEqual([s["name"] for s in store.list_snapshots(self.path)], [name])

        store.save_positions(self.conn, [self._position("later")])
        self.assertEqual(len(store.load_positions(self.conn)), 2)
        kept = store.restore(self.conn, self.path, name)
        self.assertEqual([p.id for p in store.load_positions(self.conn)], ["keep"])
        self.assertIn("before-restore", kept)
        names = {s["name"] for s in store.list_snapshots(self.path)}
        self.assertEqual(names, {name, kept})
        # And the safety copy brings "later" back.
        store.restore(self.conn, self.path, kept)
        self.assertEqual(len(store.load_positions(self.conn)), 2)

    def test_restore_refuses_names_it_does_not_know(self):
        with self.assertRaises(ValueError):
            store.restore(self.conn, self.path, "../journal.db")


class TestRepairingPositions(StoreTestCase):
    def _position(self, pid, **kw):
        from datetime import date
        from decimal import Decimal
        from bcoj.domain.enums import Direction, Right
        from bcoj.domain.types import Position
        base = dict(id=pid, underlying="ACME", expiry=date(2026, 3, 20), strike=Decimal("35"),
                    right=Right.PUT, direction=Direction.SHORT, quantity=1,
                    opened_on=date(2026, 1, 5), open_price=Decimal("1.00"))
        base.update(kw)
        return Position(**base)

    def test_delete_is_refused_while_linked_and_undoable_otherwise(self):
        from datetime import date
        store.save_positions(self.conn, [self._position("root"),
                                         self._position("next", rolled_from_id="root")])
        with self.assertRaises(store.InUseError):
            store.delete_position(self.conn, "root")
        store.delete_position(self.conn, "next", "removed by hand")
        self.assertIsNone(store.load_position(self.conn, "next"))
        entry = [e for e in store.audit_entries(self.conn) if e["entity_id"] == "next"][0]
        self.assertTrue(entry["action"].startswith("delete"))
        store.revert_audit_entry(self.conn, entry["id"])
        back = store.load_position(self.conn, "next")
        self.assertIsNotNone(back)
        self.assertEqual(back.rolled_from_id, "root")
        self.assertEqual(back.opened_on, date(2026, 1, 5))

    def test_redate_moves_only_what_is_given_and_keeps_order(self):
        from datetime import date
        store.save_positions(self.conn, [self._position("p")])
        store.redate_position(self.conn, "p", expiry=date(2026, 4, 17), note="dates moved")
        p = store.load_position(self.conn, "p")
        self.assertEqual((p.opened_on, p.expiry), (date(2026, 1, 5), date(2026, 4, 17)))
        with self.assertRaises(ValueError):
            store.redate_position(self.conn, "p", expiry=date(2025, 12, 1))


class TestGroupedUndo(StoreTestCase):
    def _position(self, pid="g1", **kw):
        from datetime import date
        from decimal import Decimal
        from bcoj.domain.enums import Direction, Right
        from bcoj.domain.types import Position
        base = dict(id=pid, underlying="ACME", expiry=date(2026, 3, 20), strike=Decimal("35"),
                    right=Right.PUT, direction=Direction.SHORT, quantity=10,
                    opened_on=date(2026, 1, 5), open_price=Decimal("3.00"))
        base.update(kw)
        return Position(**base)

    def test_undoing_a_split_takes_back_all_three_records(self):
        from datetime import date
        from bcoj.engine import actions
        parent = self._position()
        store.save_positions(self.conn, [parent])
        store.apply(self.conn, actions.split(parent, 4, on=date(2026, 2, 1)))
        self.assertEqual(len(store.load_positions(self.conn)), 3)
        entries = store.audit_entries(self.conn)
        split_rows = [e for e in entries if "split" in e["action"]]
        self.assertEqual(len(split_rows), 3)
        self.assertEqual(len({e["group_id"] for e in split_rows}), 1)
        # Undo through any one of them: the parent is open again, alone.
        store.revert_audit_entry(self.conn, split_rows[1]["id"])
        remaining = store.load_positions(self.conn)
        self.assertEqual([p.id for p in remaining], ["g1"])
        self.assertTrue(remaining[0].is_open)
        self.assertEqual(remaining[0].quantity, 10)

    def test_undo_is_refused_while_a_later_action_depends_on_it(self):
        from datetime import date
        from decimal import Decimal
        from bcoj.engine import actions
        parent = self._position()
        store.save_positions(self.conn, [parent])
        split = actions.split(parent, 4, on=date(2026, 2, 1))
        store.apply(self.conn, split)
        six = max(split.created, key=lambda p: p.quantity)
        store.apply(self.conn, actions.roll(store.load_position(self.conn, six.id),
                                            close_price=Decimal("4"), new_expiry=date(2026, 4, 17),
                                            new_strike=Decimal("34"), new_price=Decimal("5"),
                                            on=date(2026, 2, 6)))
        split_entry = [e for e in store.audit_entries(self.conn) if "split" in e["action"]][0]
        with self.assertRaises(ValueError) as ctx:
            store.revert_audit_entry(self.conn, split_entry["id"])
        self.assertIn("undo that one first", str(ctx.exception))
        # Undo the roll, then the split goes.
        roll_entry = [e for e in store.audit_entries(self.conn) if "rolled" in e["action"]][0]
        store.revert_audit_entry(self.conn, roll_entry["id"])
        store.revert_audit_entry(self.conn, split_entry["id"])
        self.assertEqual([p.id for p in store.load_positions(self.conn)], ["g1"])


class TestUndoOfOlderEntries(StoreTestCase):
    def _position(self, pid="o1", **kw):
        from datetime import date
        from decimal import Decimal
        from bcoj.domain.enums import Direction, Right
        from bcoj.domain.types import Position
        base = dict(id=pid, underlying="ACME", expiry=date(2026, 3, 20), strike=Decimal("35"),
                    right=Right.PUT, direction=Direction.SHORT, quantity=10,
                    opened_on=date(2026, 1, 5), open_price=Decimal("3.00"))
        base.update(kw)
        return Position(**base)

    def test_entries_written_before_grouping_are_matched_by_second_and_note(self):
        from datetime import date
        from bcoj.engine import actions
        parent = self._position()
        store.save_positions(self.conn, [parent])
        store.apply(self.conn, actions.split(parent, 4, on=date(2026, 2, 1)))
        # Pretend these rows predate grouping.
        with self.conn:
            self.conn.execute("UPDATE audit_log SET group_id = NULL, at = '2026-02-01T10:00:00+00:00'"
                              " WHERE action LIKE '%split%'")
        entry = [e for e in store.audit_entries(self.conn) if "split" in e["action"]][0]
        self.assertEqual(len(store.action_entries(self.conn, entry)), 3)
        outcome = store.revert_audit_entry(self.conn, entry["id"])
        self.assertTrue(outcome.startswith("split 10 into 4 and 6: "), outcome)
        self.assertIn("deleted 4 ACME 2026-03-20 35P", outcome)
        self.assertIn("restored 10 ACME 2026-03-20 35P", outcome)
        self.assertEqual([p.id for p in store.load_positions(self.conn)], ["o1"])
        # Undoing it a second time is refused.
        with self.assertRaises(ValueError) as ctx:
            store.revert_audit_entry(self.conn, entry["id"])
        self.assertIn("already undone", str(ctx.exception))
        self.assertEqual(store.action_state(self.conn, store.action_entries(self.conn, entry)), "undone")


class TestRedo(StoreTestCase):
    def _position(self, pid="r1", **kw):
        from datetime import date
        from decimal import Decimal
        from bcoj.domain.enums import Direction, Right
        from bcoj.domain.types import Position
        base = dict(id=pid, underlying="ACME", expiry=date(2026, 3, 20), strike=Decimal("35"),
                    right=Right.PUT, direction=Direction.SHORT, quantity=10,
                    opened_on=date(2026, 1, 5), open_price=Decimal("3.00"))
        base.update(kw)
        return Position(**base)

    def test_an_undone_split_can_be_done_again(self):
        from datetime import date
        from bcoj.engine import actions
        from bcoj.domain.enums import Status
        parent = self._position()
        store.save_positions(self.conn, [parent])
        store.apply(self.conn, actions.split(parent, 4, on=date(2026, 2, 1)))
        entry = [e for e in store.audit_entries(self.conn) if "split" in e["action"]][0]
        store.revert_audit_entry(self.conn, entry["id"])
        self.assertEqual(len(store.load_positions(self.conn)), 1)
        # Redo through the action's own entry: it is undone, so redo applies.
        outcome = store.redo_audit_entry(self.conn, entry["id"])
        self.assertTrue(outcome.startswith("split 10 into 4 and 6: "), outcome)
        positions = store.load_positions(self.conn)
        self.assertEqual(len(positions), 3)
        self.assertEqual(store.load_position(self.conn, "r1").status, Status.SPLIT)
        self.assertEqual(sorted(p.quantity for p in positions if p.is_open), [4, 6])
        self.assertEqual(store.action_state(self.conn, store.action_entries(self.conn, entry)), "in_effect")
        with self.assertRaises(ValueError) as ctx:
            store.redo_audit_entry(self.conn, entry["id"])      # in effect: nothing to redo
        self.assertIn("in effect", str(ctx.exception))
        # And it can be undone again, then redone again: two states, no dead ends.
        store.revert_audit_entry(self.conn, entry["id"])
        self.assertEqual(len(store.load_positions(self.conn)), 1)
        store.redo_audit_entry(self.conn, entry["id"])
        self.assertEqual(len(store.load_positions(self.conn)), 3)

    def test_an_undone_assignment_recreates_its_lot(self):
        from datetime import date
        from bcoj.engine import actions
        p = self._position("r2")
        store.save_positions(self.conn, [p])
        store.apply(self.conn, actions.assign(p, on=date(2026, 3, 20)))
        self.assertEqual(len(store.load_lots(self.conn)), 1)
        entry = [e for e in store.audit_entries(self.conn) if "assigned" in e["action"]][0]
        store.revert_audit_entry(self.conn, entry["id"])
        self.assertEqual(store.load_lots(self.conn), [])
        revert = [e for e in store.audit_entries(self.conn) if e["action"].startswith("revert")][0]
        store.redo_audit_entry(self.conn, revert["id"])
        self.assertEqual(len(store.load_lots(self.conn)), 1)
        self.assertFalse(store.load_position(self.conn, "r2").is_open)
