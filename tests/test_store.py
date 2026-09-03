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
