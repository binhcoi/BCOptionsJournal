"""Importer tests against a synthetic fixture.

The fixture is fabricated but internally consistent: every derived column in it
was computed by hand, so the reconciliation coming back clean means the engine
and independent arithmetic agree -- it is not the engine checking itself.

It exercises one row of every shape the real format produces: long and short,
calls and puts, expiry, assignment, buy-writes, multi-leg roll chains, an open
position with a profit target, a placeholder, a disguised share transaction, a
flattened partial, and a ticker whose share count goes negative.
"""

import pathlib
import unittest
from decimal import Decimal

from bcoj.domain.enums import MatchingRule, ShareSource, Status
from bcoj.importer import legacy_formulas, shares as share_import, triage
from bcoj.importer.estimates import EstimatedLot
from bcoj.importer.legacy_csv import derived_assignment, parse_file
from bcoj.importer.reconcile import EXACT, MISMATCH, reconcile, render

D = Decimal
FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "synthetic_rows.csv"

# The fixture's OMEGA ticker is short 100 shares by construction.
OMEGA_ESTIMATE = (
    EstimatedLot(underlying="OMEGA", quantity=100, cost_per_share=D("20"),
                 note="fixture: reconstructs the missing acquisition"),
)


class ImporterTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = parse_file(FIXTURE)
        cls.rows = cls.report.rows
        cls.by_id = {
            r.position.id.split("-")[0]: r for r in cls.rows if r.position is not None
        }

    def row(self, short_id):
        return self.by_id[short_id]


class TestParsing(ImporterTestCase):
    def test_header_matches_expected_layout(self):
        self.assertTrue(self.report.header_matched)

    def test_every_row_parses(self):
        self.assertEqual(
            len(self.report.failed), 0, [r.errors for r in self.report.failed]
        )
        self.assertEqual(len(self.report.parsed), 23)

    def test_signed_quantity_becomes_direction(self):
        long_put = self.row("a1000003").position
        self.assertEqual(long_put.direction.sign, -1)
        self.assertEqual(long_put.quantity, 1)
        self.assertEqual(long_put.signed_quantity, -1)

        short = self.row("a1000010").position
        self.assertEqual(short.direction.sign, 1)
        self.assertEqual(short.quantity, 4)

    def test_open_row_keeps_target_out_of_close_price(self):
        """Close_U on an open row is a target and must not become a close."""
        row = self.row("a1000014")
        self.assertEqual(row.position.status, Status.OPEN)
        self.assertIsNone(row.position.close_price)
        self.assertIsNone(row.position.closed_on)
        # Still available for the target calculation.
        self.assertEqual(row.sheet.close_u, D("1.49"))
        self.assertEqual(row.position.close_fee, D("6.50"))

    def test_buy_write_zero_is_not_a_share_purchase(self):
        self.assertIsNone(self.row("a1000001").buy_write)
        self.assertEqual(self.row("a1000012").buy_write, D("11.00"))

    def test_roll_links_resolve_by_guid(self):
        child = self.row("a1000006").position
        parent = self.row("a1000005").position
        self.assertEqual(child.rolled_from_id, parent.id)

    def test_missing_close_date_still_parses(self):
        row = self.row("a1000020")
        self.assertEqual(row.position.status, Status.CLOSED)
        self.assertIsNone(row.position.closed_on)
        self.assertEqual(row.position.close_price, D("0.20"))

    def test_unparseable_row_is_reported_not_dropped(self):
        from bcoj.importer.legacy_csv import parse_rows

        header = self.report.header
        bad = list(self.rows[0].raw)
        bad[9] = "not a number"
        report = parse_rows([list(header), bad])
        self.assertEqual(len(report.rows), 1)
        self.assertFalse(report.rows[0].ok)
        self.assertTrue(report.rows[0].errors)


class TestDerivedAssignment(ImporterTestCase):
    """The Assignment column is derivable, so it is a column to diff."""

    def test_matches_the_sheet_on_every_row(self):
        for row in self.rows:
            if row.position is None:
                continue
            with self.subTest(row=row.position.id):
                self.assertEqual(
                    derived_assignment(row), row.sheet.assignment or D("0")
                )

    def test_buy_write_and_called_away_net_on_one_row(self):
        """Bought 100 at 11.00, called away at 12.00: a net +100."""
        self.assertEqual(derived_assignment(self.row("a1000012")), D("100.00"))

    def test_assigned_put_is_a_cash_outflow(self):
        self.assertEqual(derived_assignment(self.row("a1000010")), D("-6800.00"))

    def test_assigned_call_is_a_cash_inflow(self):
        self.assertEqual(derived_assignment(self.row("a1000011")), D("8000.00"))

    def test_buy_write_with_the_call_closed_holds_the_shares(self):
        self.assertEqual(derived_assignment(self.row("a1000013")), D("-2800.00"))


class TestReconciliation(ImporterTestCase):
    def test_fixture_reconciles_perfectly(self):
        result = reconcile(self.rows)
        report = render(result)
        self.assertTrue(result.clean, report)
        counts = result.counts()
        self.assertEqual(counts[MISMATCH], 0, report)
        self.assertEqual(counts[EXACT], 23, report)

    def test_report_renders_a_verdict(self):
        text = render(reconcile(self.rows))
        self.assertIn("RECONCILIATION", text)
        self.assertIn("verdict: CLEAN", text)

    def test_a_wrong_cell_is_caught(self):
        """Proof the diff actually bites."""
        from bcoj.importer.legacy_csv import parse_rows

        tampered = list(self.row("a1000001").raw)
        tampered[18] = "999.99"          # P/L
        report = parse_rows([list(self.report.header), tampered])
        result = reconcile(report.rows)
        self.assertFalse(result.clean)
        fields = {d.field for d in result.failures()[0].problems()}
        self.assertIn("P/L", fields)


class TestLegacyPanel(ImporterTestCase):
    """Reproduce the sheet's summary panel, bugs included."""

    def test_portfolio_open_figures(self):
        panel = legacy_formulas.panel(self.rows)
        self.assertEqual(panel.current_premium, D("2992.85"))
        self.assertEqual(panel.expected_pl, D("1495.70"))
        self.assertEqual(panel.expected_closing, D("-1497.15"))
        self.assertEqual(panel.current_put_risk, D("35000.00"))
        self.assertEqual(panel.open_calls, 0)
        self.assertEqual(panel.open_puts, 11)   # 10 contracts plus a placeholder

    def test_adjusted_pl_is_realized_plus_assignment(self):
        panel = legacy_formulas.panel(self.rows, "DELTA")
        self.assertEqual(panel.realized_pl, D("1175.70"))
        self.assertEqual(panel.assignment, D("1200.00"))
        self.assertEqual(panel.adjusted_pl, D("2375.70"))

    def test_share_counts_per_symbol(self):
        for symbol, expected in (("DELTA", 0), ("ZETA", 200), ("OMEGA", -100)):
            with self.subTest(symbol=symbol):
                self.assertEqual(
                    legacy_formulas.panel(self.rows, symbol).shares, expected
                )

    def test_per_share_figures_withheld_without_shares(self):
        panel = legacy_formulas.panel(self.rows, "ACME")
        self.assertEqual(panel.shares, 0)
        self.assertIsNone(panel.adjusted_unit_price)
        self.assertIsNone(panel.adjusted_cover_call)

    def test_cover_call_double_count_is_quantified(self):
        """A row that is both a call and assigned satisfies both SUMIFs."""
        panel = legacy_formulas.panel(self.rows, "ZETA")
        self.assertEqual(panel.adjusted_unit_price, D("12.47"))
        self.assertEqual(panel.adjusted_cover_call, D("12.23"))
        self.assertEqual(panel.cover_call_double_count, D("48.70"))
        self.assertEqual(panel.adjusted_cover_call_corrected, D("12.47"))
        # The bug understates the basis; correcting it raises the figure.
        self.assertGreater(
            panel.adjusted_cover_call_corrected, panel.adjusted_cover_call
        )

    def test_unrealised_pl_double_counts_open_premium(self):
        panel = legacy_formulas.panel(self.rows)
        self.assertEqual(
            panel.unrealised_pl, panel.current_premium + panel.expected_pl
        )

    def test_symbols_are_listed(self):
        self.assertEqual(
            legacy_formulas.symbols(self.rows),
            ("ACME", "BETA", "DELTA", "GAMMA", "KAPPA", "OMEGA", "SIGMA", "ZETA"),
        )


class TestShareDerivation(ImporterTestCase):
    def test_lots_and_disposals_are_inferred(self):
        derived = share_import.derive(self.rows)
        self.assertEqual(len(derived.lots), 6)
        self.assertEqual(len(derived.disposals), 4)
        self.assertEqual(
            derived.underlyings(), ("DELTA", "KAPPA", "OMEGA", "ZETA")
        )

    def test_lot_sources_are_labelled(self):
        derived = share_import.derive(self.rows).for_underlying("ZETA")
        sources = {l.id.split("-")[0]: l.source for l in derived.lots}
        self.assertEqual(sources["a1000012"], ShareSource.BUY_WRITE)
        derived_delta = share_import.derive(self.rows).for_underlying("DELTA")
        self.assertEqual(
            derived_delta.lots[0].source, ShareSource.PUT_ASSIGNMENT
        )

    def test_wheel_share_pl(self):
        """Assigned 400 at 17, called away at 20: +1,200."""
        summary = self._summaries()
        self.assertEqual(summary["DELTA"].realized, D("1200.00"))
        self.assertEqual(summary["DELTA"].open_quantity, 0)

    def test_buy_write_shares_are_earmarked(self):
        """Bought 100 at 11 for that call; called away at 12 for +100."""
        summary = self._summaries()
        self.assertEqual(summary["ZETA"].realized, D("100.00"))
        self.assertEqual(summary["ZETA"].open_quantity, 200)
        self.assertEqual(summary["ZETA"].open_cost, D("2800.00"))

    def test_share_pl_reconciles_with_the_assignment_column(self):
        """Invariant: assignment cash + cost of shares still held = realized.

        This is what makes the sheet's "Adjusted P/L" right only when a ticker
        is flat: -2,700.00 + 2,800.00 = 100.00.
        """
        panel = legacy_formulas.panel(self.rows, "ZETA")
        summary = self._summaries()["ZETA"]
        self.assertEqual(summary.realized, panel.assignment + summary.open_cost)

    def test_deficit_ticker_is_blocked(self):
        summary = self._summaries()["OMEGA"]
        self.assertFalse(summary.balanced)
        self.assertIn("missing", summary.error)
        self.assertEqual(summary.net, -100)

    def test_estimate_unblocks_the_deficit_ticker(self):
        summary = self._summaries(estimates=OMEGA_ESTIMATE)["OMEGA"]
        self.assertTrue(summary.balanced)
        # 12,000 proceeds against 8,199 + 2,000 of cost.
        self.assertEqual(summary.realized, D("1801.00"))
        self.assertEqual(summary.open_quantity, 0)

    def test_estimates_do_not_stack_on_balanced_tickers(self):
        surplus = (
            EstimatedLot(underlying="DELTA", quantity=100,
                         cost_per_share=D("10"), note="should be ignored"),
        )
        derived = share_import.derive(
            self.rows, include_estimates=True, estimates=surplus
        )
        self.assertEqual([l for l in derived.lots if l.estimated], [])

    def test_estimate_never_exceeds_the_shortfall(self):
        oversized = (
            EstimatedLot(underlying="OMEGA", quantity=5000,
                         cost_per_share=D("20"), note="too many"),
        )
        derived = share_import.derive(
            self.rows, include_estimates=True, estimates=oversized
        )
        estimated = [l for l in derived.lots if l.estimated]
        self.assertEqual(len(estimated), 1)
        self.assertEqual(estimated[0].quantity, 100)

    def test_matching_rule_changes_a_partial_disposal(self):
        """Two lots at 10 and 30; 200 of 400 shares sold at 20.

        FIFO takes the cheap lot for +2,000 and leaves the dear one; LIFO does
        the reverse for -2,000. Four thousand dollars apart on the rule alone,
        with a different lot left behind either way.
        """
        derived = share_import.derive(self.rows).for_underlying("KAPPA")
        fifo = share_import.summarize(derived, MatchingRule.FIFO)[0]
        lifo = share_import.summarize(derived, MatchingRule.LIFO)[0]
        self.assertEqual(fifo.realized, D("2000.00"))
        self.assertEqual(lifo.realized, D("-2000.00"))
        self.assertEqual(fifo.open_quantity, lifo.open_quantity)
        self.assertEqual(fifo.open_cost, D("6000.00"))   # the 30 lot remains
        self.assertEqual(lifo.open_cost, D("2000.00"))   # the 10 lot remains

    def test_matching_rule_is_moot_when_every_lot_is_consumed(self):
        """A disposal that takes all the shares leaves no ordering to choose.

        This is why a reconstructed lot with an unknown acquisition date can
        still give a trustworthy figure: with nothing left over, FIFO and LIFO
        agree. (TestShareMatching covers the case where they diverge.)
        """
        derived = share_import.derive(
            self.rows, include_estimates=True, estimates=OMEGA_ESTIMATE
        ).for_underlying("OMEGA")
        fifo = share_import.summarize(derived, MatchingRule.FIFO)[0]
        lifo = share_import.summarize(derived, MatchingRule.LIFO)[0]
        self.assertEqual(fifo.open_quantity, 0)
        self.assertEqual(fifo.realized, lifo.realized)

    def _summaries(self, estimates=None):
        derived = share_import.derive(
            self.rows,
            include_estimates=estimates is not None,
            estimates=estimates,
        )
        return {s.underlying: s for s in share_import.summarize(derived)}


class TestTriage(ImporterTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.flags = triage.triage(cls.rows)
        cls.by_kind = {}
        for flag in cls.flags:
            cls.by_kind.setdefault(flag.kind, []).append(flag)

    def kinds_for(self, short_id):
        return {f.kind for f in self.flags if f.entity_id.startswith(short_id)}

    def test_expected_flag_counts(self):
        self.assertEqual(
            triage.summarize(self.flags),
            {
                "disguised_share_row": 1,
                "fee_unusual": 1,
                "missing_close_date": 1,
                "negative_share_count": 1,
                "placeholder_row": 1,
                "quantity_drop_on_roll": 1,
            },
        )

    def test_disguised_share_row_is_flagged(self):
        """Off-grid strike, no premium, expiry before open, P/L just fees."""
        self.assertIn(triage.DISGUISED_SHARE_ROW, self.kinds_for("a1000016"))

    def test_placeholder_row_is_flagged(self):
        self.assertIn(triage.PLACEHOLDER_ROW, self.kinds_for("a1000015"))

    def test_missing_close_date_is_flagged(self):
        self.assertIn(triage.MISSING_CLOSE_DATE, self.kinds_for("a1000020"))

    def test_flattened_partial_fingerprint(self):
        """Quantity drops 5 -> 3 across the roll, with a fee matching 1.

        Both halves of the fingerprint fire on the same row, which is what
        distinguishes a reshaped row from an ordinary reduced roll.
        """
        self.assertEqual(
            self.kinds_for("a1000019"),
            {triage.QUANTITY_DROP_ON_ROLL, triage.FEE_UNUSUAL},
        )

    def test_negative_share_count_is_flagged(self):
        flags = self.by_kind[triage.NEGATIVE_SHARE_COUNT]
        self.assertEqual([f.entity_id for f in flags], ["OMEGA"])
        self.assertIn("-100", flags[0].detail)

    def test_healthy_rows_are_not_flagged(self):
        for short_id in ("a1000001", "a1000004", "a1000006", "a1000011"):
            with self.subTest(row=short_id):
                self.assertEqual(self.kinds_for(short_id), set())

    def test_summary_counts_match_the_flags(self):
        tally = triage.summarize(self.flags)
        self.assertEqual(sum(tally.values()), len(self.flags))


if __name__ == "__main__":
    unittest.main()
