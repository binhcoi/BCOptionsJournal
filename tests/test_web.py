"""End-to-end web tests: a real server, real HTTP, a real database.

These drive the app the way a browser does -- GET a form, POST it, follow the
redirect -- so they cover the routing, form parsing, CSRF check, escaping and
storage together. A unit test of a handler would miss most of what can break
in that chain.
"""

import re
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from bcoj.db import store
from bcoj.web.server import App, Handler


class WebTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._dir = TemporaryDirectory()
        cls.db_path = str(Path(cls._dir.name) / "journal.db")
        cls.app = App(cls.db_path)
        Handler.app = cls.app
        Handler.log_message = lambda *a, **k: None  # keep test output clean

        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.base = "http://127.0.0.1:%d" % cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)
        cls._dir.cleanup()

    # --- helpers --------------------------------------------------------

    def get(self, path, expect=200):
        try:
            with urllib.request.urlopen(self.base + path) as response:
                body = response.read().decode()
                self.assertEqual(response.status, expect, body[:400])
                return body
        except urllib.error.HTTPError as error:
            body = error.read().decode()
            self.assertEqual(error.code, expect, body[:400])
            return body

    def post(self, path, fields, expect_redirect=True):
        """POST with a valid CSRF token; returns (status, location, body)."""
        payload = dict(fields)
        payload.setdefault("csrf", self.app.csrf)
        data = urllib.parse.urlencode(payload).encode()

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None

        def decode(location):
            # Flash messages ride in the query string, where a space is '+'.
            return urllib.parse.unquote_plus(location) if location else location

        opener = urllib.request.build_opener(NoRedirect)
        try:
            with opener.open(self.base + path, data=data) as response:
                return (response.status, decode(response.headers.get("Location")),
                        response.read().decode())
        except urllib.error.HTTPError as error:
            return (error.code, decode(error.headers.get("Location")),
                    error.read().decode())

    def positions(self):
        conn = store.open_db(self.db_path)
        try:
            return store.load_positions(conn)
        finally:
            conn.close()

    def add_position(self, **overrides):
        fields = {
            "underlying": "acme",
            "opened_on": date(2026, 1, 5).isoformat(),
            "expiry": date(2026, 3, 20).isoformat(),
            "right": "PUT",
            "direction": "SHORT",
            "strike": "35",
            "quantity": "10",
            "open_price": "3.00",
            "open_fee": "6.50",
            "notes": "",
        }
        fields.update(overrides)
        return self.post("/new", fields)


class TestPagesLoad(WebTestCase):
    def test_every_page_renders(self):
        for path in ("/", "/new", "/expiring", "/audit",
                     "/?show=all", "/?show=closed", "/expiring?within=7"):
            with self.subTest(path=path):
                body = self.get(path)
                self.assertIn("<!doctype html>", body)
                self.assertIn("BC Options Journal", body)

    def test_static_assets_served(self):
        self.assertIn("--accent", self.get("/static/app.css"))
        self.assertIn("progressive enhancement", self.get("/static/app.js").lower())

    def test_unknown_paths_are_404(self):
        self.get("/nope", expect=404)
        self.get("/static/nope.css", expect=404)
        self.get("/position/does-not-exist", expect=404)

    def test_security_headers_present(self):
        with urllib.request.urlopen(self.base + "/") as response:
            self.assertEqual(
                response.headers.get("X-Content-Type-Options"), "nosniff"
            )
            self.assertEqual(response.headers.get("Referrer-Policy"), "no-referrer")


class TestCsrf(WebTestCase):
    def test_post_without_a_token_is_refused(self):
        data = urllib.parse.urlencode({"underlying": "ACME"}).encode()
        try:
            urllib.request.urlopen(self.base + "/new", data=data)
            self.fail("expected 403")
        except urllib.error.HTTPError as error:
            self.assertEqual(error.code, 403)

    def test_post_with_a_wrong_token_is_refused(self):
        status, _, _ = self.post("/new", {"csrf": "nope", "underlying": "ACME"})
        self.assertEqual(status, 403)

    def test_a_refused_post_writes_nothing(self):
        before = len(self.positions())
        self.post("/new", {"csrf": "nope", "underlying": "ACME"})
        self.assertEqual(len(self.positions()), before)


class TestEntry(WebTestCase):
    def test_new_position_round_trip(self):
        status, location, _ = self.add_position()
        self.assertEqual(status, 303)
        self.assertIn("/new", location)

        found = [p for p in self.positions() if p.underlying == "ACME"]
        self.assertTrue(found)
        position = found[-1]
        self.assertEqual(position.quantity, 10)
        self.assertEqual(str(position.open_price), "3.00")
        self.assertTrue(position.is_open)

    def test_ticker_is_upper_cased(self):
        self.add_position(underlying="beta")
        self.assertTrue(any(p.underlying == "BETA" for p in self.positions()))

    def test_negative_quantity_means_long(self):
        """The habit the spreadsheet built, preserved in the form."""
        self.add_position(underlying="gamma", quantity="-2")
        position = [p for p in self.positions() if p.underlying == "GAMMA"][0]
        self.assertEqual(position.direction.value, "LONG")
        self.assertEqual(position.quantity, 2)

    def test_validation_error_re_renders_the_form_with_the_values(self):
        status, _, body = self.add_position(
            underlying="delta", expiry="2025-01-01"  # before the open date
        )
        self.assertEqual(status, 200)          # not a redirect: refused
        self.assertIn("problems", body)
        self.assertIn("before the open date", body)
        self.assertIn('value="DELTA"', body)
        self.assertFalse(any(p.underlying == "DELTA" for p in self.positions()))

    def test_warnings_do_not_block_but_are_reported(self):
        status, location, _ = self.add_position(
            underlying="zeta", strike="27.33"  # off the listed grid
        )
        self.assertEqual(status, 303)
        self.assertIn("warning", location)
        self.assertTrue(any(p.underlying == "ZETA" for p in self.positions()))

    def test_unparseable_number_is_a_clear_error(self):
        status, _, body = self.add_position(strike="thirty five")
        self.assertEqual(status, 400)
        self.assertIn("not a number", body)

    def test_entered_values_are_escaped_not_injected(self):
        self.add_position(underlying="sigma", notes='<script>alert(1)</script>')
        position = [p for p in self.positions() if p.underlying == "SIGMA"][0]
        body = self.get(f"/position/{position.id}")
        self.assertNotIn("<script>alert(1)</script>", body)
        self.assertIn("&lt;script&gt;", body)


class TestActions(WebTestCase):
    def _fresh(self, ticker):
        self.add_position(underlying=ticker)
        return [p for p in self.positions()
                if p.underlying == ticker.upper() and p.is_open][0]

    def test_close(self):
        position = self._fresh("clo")
        status, location, _ = self.post(
            f"/position/{position.id}/close",
            {"closed_on": "2026-02-10", "close_price": "1.50",
             "close_fee": "6.50"},
        )
        self.assertEqual(status, 303)
        self.assertIn("1,487.00", location)

        after = store.open_db(self.db_path)
        try:
            reloaded = store.load_position(after, position.id)
        finally:
            after.close()
        self.assertEqual(reloaded.status.value, "CLOSED")

    def test_expire(self):
        position = self._fresh("exp")
        status, location, _ = self.post(
            f"/position/{position.id}/expire", {"on": "2026-03-20"}
        )
        self.assertEqual(status, 303)
        self.assertIn("2,993.50", location)

    def test_assign_moves_stock_too(self):
        position = self._fresh("asg")
        status, location, _ = self.post(
            f"/position/{position.id}/assign",
            {"on": "2026-03-20", "close_fee": "0", "share_fee": "0"},
        )
        self.assertEqual(status, 303)
        self.assertIn("acquired 1000 shares", location)

        conn = store.open_db(self.db_path)
        try:
            lots = [l for l in store.load_lots(conn) if l.underlying == "ASG"]
        finally:
            conn.close()
        self.assertEqual(len(lots), 1)
        self.assertEqual(lots[0].quantity, 1000)
        self.assertEqual(str(lots[0].cost_per_share), "35")

    def test_roll_reports_the_new_chain_state(self):
        position = self._fresh("rol")
        status, location, _ = self.post(
            f"/position/{position.id}/roll",
            {"on": "2026-02-20", "close_price": "4.00", "close_fee": "6.50",
             "new_expiry": "2026-04-17", "new_strike": "33",
             "new_price": "4.50", "new_fee": "6.50", "new_quantity": "10"},
        )
        self.assertEqual(status, 303)
        message = location
        self.assertIn("carries (1,013.00)", message)
        self.assertIn("net credit 3,480.50", message)

        chain = [p for p in self.positions() if p.underlying == "ROL"]
        self.assertEqual(len(chain), 2)
        rolled = [p for p in chain if p.status.value == "ROLLED"][0]
        successor = [p for p in chain if p.is_open][0]
        self.assertEqual(successor.rolled_from_id, rolled.id)

    def test_roll_refuses_to_go_backwards_in_time(self):
        position = self._fresh("bad")
        status, location, _ = self.post(
            f"/position/{position.id}/roll",
            {"on": "2025-06-01", "close_price": "1.00",
             "new_expiry": "2026-04-17", "new_strike": "33",
             "new_price": "2.00"},
        )
        self.assertEqual(status, 303)
        self.assertIn("refused", location)
        # Still open, nothing written.
        self.assertEqual(
            len([p for p in self.positions() if p.underlying == "BAD"]), 1
        )

    def test_split_then_assign_one_half_and_roll_the_other(self):
        """The partial-assignment workflow, end to end over HTTP."""
        position = self._fresh("spl")
        status, location, _ = self.post(
            f"/position/{position.id}/split",
            {"quantity": "4", "on": "2026-02-01"},
        )
        self.assertEqual(status, 303)
        self.assertIn("split 10 into 4 and 6", location)

        rows = [p for p in self.positions() if p.underlying == "SPL"]
        halves = sorted([p for p in rows if p.is_open], key=lambda p: p.quantity)
        parent = [p for p in rows if p.status.value == "SPLIT"][0]
        self.assertEqual([h.quantity for h in halves], [4, 6])
        self.assertTrue(all(h.split_from_quantity == 10 for h in halves))
        self.assertEqual(parent.quantity, 10)

        four, six = halves
        assigned, _, _ = self.post(
            f"/position/{four.id}/assign",
            {"on": "2026-03-20", "close_fee": "0", "share_fee": "0"},
        )
        self.assertEqual(assigned, 303)
        rolled, _, _ = self.post(
            f"/position/{six.id}/roll",
            {"on": "2026-02-20", "close_price": "2.00",
             "new_expiry": "2026-04-17", "new_strike": "33",
             "new_price": "2.50", "new_quantity": "6"},
        )
        self.assertEqual(rolled, 303)

        conn = store.open_db(self.db_path)
        try:
            lots = [l for l in store.load_lots(conn) if l.underlying == "SPL"]
        finally:
            conn.close()
        self.assertEqual(lots[0].quantity, 400)   # only the assigned half

    def test_split_bounds_are_enforced(self):
        position = self._fresh("bnd")
        status, _, body = self.post(
            f"/position/{position.id}/split", {"quantity": "10"}
        )
        # A refused action is a bad request, not a crash.
        self.assertEqual(status, 400)
        self.assertIn("between 1 and 9", body)

    def test_acting_twice_on_one_position_is_refused(self):
        position = self._fresh("twi")
        self.post(f"/position/{position.id}/expire", {"on": "2026-03-20"})
        status, _, body = self.post(
            f"/position/{position.id}/close",
            {"closed_on": "2026-03-21", "close_price": "1.00"},
        )
        self.assertEqual(status, 400)
        self.assertIn("already EXPIRED", body)


class TestExpiryQueue(WebTestCase):
    def test_overdue_positions_appear_with_outcome_buttons(self):
        yesterday = date.today() - timedelta(days=1)
        self.add_position(
            underlying="ovr",
            opened_on=(yesterday - timedelta(days=30)).isoformat(),
            expiry=yesterday.isoformat(),
        )
        body = self.get("/expiring")
        self.assertIn("OVR", body)
        self.assertIn("overdue", body)
        self.assertIn("do=assign", body)
        self.assertIn("do=expire", body)

    def test_the_queue_is_announced_on_the_positions_page(self):
        self.assertIn("need an outcome", self.get("/"))


class TestAuditAndUndo(WebTestCase):
    def test_changes_are_logged(self):
        self.add_position(underlying="aud")
        body = self.get("/audit")
        self.assertIn("entered by hand", body)

    def test_undo_restores_the_previous_values(self):
        position = self._position("und")
        self.post(
            f"/position/{position.id}/close",
            {"closed_on": "2026-02-10", "close_price": "1.50"},
        )
        conn = store.open_db(self.db_path)
        try:
            self.assertEqual(
                store.load_position(conn, position.id).status.value, "CLOSED"
            )
            entry = [
                e for e in store.audit_entries(conn)
                if e["entity_id"] == position.id and e["before"]
            ][0]
        finally:
            conn.close()

        status, location, _ = self.post(f"/audit/{entry['id']}/revert", {})
        self.assertEqual(status, 303)
        self.assertIn("Undone", location)

        conn = store.open_db(self.db_path)
        try:
            self.assertEqual(
                store.load_position(conn, position.id).status.value, "OPEN"
            )
            # The undo is itself recorded.
            self.assertTrue(
                any(e["action"].startswith("revert")
                    for e in store.audit_entries(conn))
            )
        finally:
            conn.close()

    def test_undoing_a_create_removes_it(self):
        position = self._position("dele")
        conn = store.open_db(self.db_path)
        try:
            entry = [
                e for e in store.audit_entries(conn)
                if e["entity_id"] == position.id
            ][-1]
        finally:
            conn.close()
        self.post(f"/audit/{entry['id']}/revert", {})
        self.assertFalse(any(p.id == position.id for p in self.positions()))

    def test_reverting_a_missing_entry_says_so(self):
        status, location, _ = self.post("/audit/999999/revert", {})
        self.assertEqual(status, 303)
        self.assertIn("no audit entry", location)

    def _position(self, ticker):
        self.add_position(underlying=ticker)
        return [p for p in self.positions()
                if p.underlying == ticker.upper() and p.is_open][0]


class TestPositionPage(WebTestCase):
    def test_open_position_shows_decision_figures(self):
        self.add_position(underlying="dec")
        position = [p for p in self.positions() if p.underlying == "DEC"][0]
        body = self.get(f"/position/{position.id}")
        for label in ("Break-even", "50% target price", "Capital at risk",
                      "Net chain credit", "Chain carry"):
            with self.subTest(label=label):
                self.assertIn(label, body)

    def test_action_tabs_render_their_forms(self):
        self.add_position(underlying="tab")
        position = [p for p in self.positions() if p.underlying == "TAB"][0]
        for which, marker in (
            ("close", "Close price / share"),
            ("roll", "Buy-back price"),
            ("expire", "Expired on"),
            ("assign", "acquire 1000 shares"),
            ("split", "Contracts to peel off"),
        ):
            with self.subTest(which=which):
                body = self.get(f"/position/{position.id}?do={which}")
                self.assertIn(marker, body)

    def test_a_rolled_chain_shows_every_leg(self):
        self.add_position(underlying="chn")
        position = [p for p in self.positions() if p.underlying == "CHN"][0]
        self.post(
            f"/position/{position.id}/roll",
            {"on": "2026-02-20", "close_price": "4.00",
             "new_expiry": "2026-04-17", "new_strike": "33",
             "new_price": "4.50"},
        )
        successor = [p for p in self.positions()
                     if p.underlying == "CHN" and p.is_open][0]
        body = self.get(f"/position/{successor.id}")
        self.assertIn("Chain - 2 leg(s)", body)


class TestPassword(unittest.TestCase):
    """The optional password, since a bound port is not a boundary."""

    def test_unauthenticated_request_is_challenged(self):
        with TemporaryDirectory() as tmp:
            app = App(str(Path(tmp) / "j.db"))
            app.password = "secret"
            Handler.app = app
            Handler.log_message = lambda *a, **k: None
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            base = "http://127.0.0.1:%d" % httpd.server_address[1]
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                try:
                    urllib.request.urlopen(base + "/")
                    self.fail("expected 401")
                except urllib.error.HTTPError as error:
                    self.assertEqual(error.code, 401)
                    self.assertIn("Basic", error.headers.get("WWW-Authenticate"))

                manager = urllib.request.HTTPPasswordMgrWithDefaultRealm()
                manager.add_password(None, base, "user", "secret")
                opener = urllib.request.build_opener(
                    urllib.request.HTTPBasicAuthHandler(manager)
                )
                with opener.open(base + "/") as response:
                    self.assertEqual(response.status, 200)
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()


class TestInlineActions(WebTestCase):
    """Actions happen on the positions page itself, not on a separate one."""

    def _fresh(self, ticker):
        self.add_position(underlying=ticker)
        return [p for p in self.positions()
                if p.underlying == ticker.upper() and p.is_open][0]

    def test_rows_carry_action_links(self):
        self._fresh("lnk")
        body = self.get("/")
        for action in ("close", "roll", "expire", "assign", "split"):
            with self.subTest(action=action):
                self.assertIn(f"&do={action}", body)

    def test_requesting_an_action_opens_its_form_under_the_row(self):
        position = self._fresh("inl")
        body = self.get(f"/?show=open&act={position.id}&do=close")
        self.assertIn('class="action-row"', body)
        self.assertIn("Close price / share", body)
        # And the form returns to this page, not to a detail page.
        self.assertIn('name="next" value="/?show=open"', body)

    def test_close_price_is_prefilled_with_the_target(self):
        position = self._fresh("pre")
        body = self.get(f"/?show=open&act={position.id}&do=close")
        # 10 contracts at 3.00 less 6.50 fee is 2,993.50; half of it back at
        # 6.50 closing fee is a 1.49 buy-back.
        self.assertIn('name="close_price" id="f_close_price"\n         value="1.49"',
                      body)
        self.assertIn("Pre-filled with the 50% target", body)

    def test_roll_form_is_two_labelled_trades(self):
        position = self._fresh("two")
        body = self.get(f"/?show=open&act={position.id}&do=roll")
        self.assertIn("<legend>1. Close this leg</legend>", body)
        self.assertIn("<legend>2. Open the new leg</legend>", body)
        self.assertLess(body.index("1. Close this leg"),
                        body.index("2. Open the new leg"))

    def test_submitting_inline_returns_to_the_positions_page(self):
        position = self._fresh("ret")
        status, location, _ = self.post(
            f"/position/{position.id}/close",
            {"closed_on": "2026-02-10", "close_price": "1.50",
             "next": "/?show=open"},
        )
        self.assertEqual(status, 303)
        self.assertTrue(location.startswith("/?show=open"))
        self.assertIn("RET", location)   # the flash names the contract

    def test_next_must_be_a_local_path(self):
        position = self._fresh("ext")
        status, location, _ = self.post(
            f"/position/{position.id}/expire",
            {"on": "2026-03-20", "next": "https://evil.example/"},
        )
        self.assertEqual(status, 303)
        self.assertTrue(location.startswith("/position/"))

    def test_put_risk_no_longer_duplicates_capital_at_risk(self):
        position = self._fresh("dup")
        body = self.get(f"/position/{position.id}")
        self.assertIn("Capital at risk", body)
        self.assertNotIn("Put risk", body)


class TestChainView(WebTestCase):
    def test_split_shows_both_halves_and_what_became_of_each(self):
        self.add_position(underlying="fam")
        parent = [p for p in self.positions() if p.underlying == "FAM"][0]
        self.post(f"/position/{parent.id}/split", {"quantity": "4",
                                                    "on": "2026-02-01"})
        halves = sorted([p for p in self.positions()
                         if p.underlying == "FAM" and p.is_open],
                        key=lambda p: p.quantity)
        four, six = halves
        self.post(f"/position/{four.id}/assign",
                  {"on": "2026-03-20", "close_fee": "0", "share_fee": "0"})
        self.post(f"/position/{six.id}/roll",
                  {"on": "2026-02-20", "close_price": "2.00",
                   "new_expiry": "2026-04-17", "new_strike": "33",
                   "new_price": "2.50", "new_quantity": "6"})

        # From the assigned half, the whole chain is visible: the split
        # record, both halves, and what became of the other one.
        body = self.get(f"/position/{four.id}")
        self.assertIn("Chain - 4 leg(s)", body)
        self.assertIn("split into 4 + 6", body)
        self.assertIn('class="badge st-split"', body)
        self.assertIn("All legs realized", body)
        self.assertIn("2026-04-17", body)          # the other half's roll
        # Same columns as the positions page: one renderer.
        self.assertIn("<th>Break-even</th>", body)
        self.assertIn("<th>Legs</th>", body)
        # The row being looked at is marked, and still a link to itself.
        self.assertRegex(body, r'<tr id="row-%s" class="[^"]*\bcurrent\b' % four.id)
        self.assertIn(f'href="/position/{four.id}"', body)

    def test_a_plain_chain_is_still_called_a_chain(self):
        self.add_position(underlying="pln")
        position = [p for p in self.positions() if p.underlying == "PLN"][0]
        body = self.get(f"/position/{position.id}")
        self.assertIn("Chain - 1 leg(s)", body)
        self.assertNotIn("All legs realized", body)

    def test_days_show_while_open(self):
        self.add_position(underlying="dys",
                          opened_on=(date.today() - timedelta(days=12)).isoformat(),
                          expiry=(date.today() + timedelta(days=30)).isoformat())
        position = [p for p in self.positions() if p.underlying == "DYS"][0]
        body = self.get(f"/position/{position.id}")
        self.assertIn("<span>Days</span><b>12</b>", body)


class TestPositionsPageLayout(WebTestCase):
    def _fresh(self, ticker):
        self.add_position(underlying=ticker)
        return [p for p in self.positions()
                if p.underlying == ticker.upper() and p.is_open][0]

    def test_actions_sit_beneath_the_contract_and_are_colour_coded(self):
        self._fresh("lay")
        body = self.get("/")
        cell = re.search(r'<td><a href="/position/[^"]+">.*?</td>', body, re.S).group(0)
        self.assertIn('class="row-actions"', cell)
        for action in ("close", "roll", "expire", "assign", "split"):
            with self.subTest(action=action):
                self.assertIn(f'class="act act-{action}', cell)

    def test_status_is_a_badge(self):
        self._fresh("bdg")
        self.assertIn('class="badge st-open"', self.get("/"))

    def test_sibling_branches_share_a_rail_and_a_note(self):
        position = self._fresh("sib")
        self.post(f"/position/{position.id}/split",
                  {"quantity": "4", "on": "2026-02-01"})
        body = self.get("/")
        self.assertEqual(body.count('class="fam fam-'), 2)
        self.assertEqual(body.count("1 of 2 open in this chain"), 2)

    def test_leg_count_expands_the_chain_in_place(self):
        position = self._fresh("exp")
        self.post(f"/position/{position.id}/roll",
                  {"on": "2026-02-20", "close_price": "2.00",
                   "new_expiry": "2026-04-17", "new_strike": "33",
                   "new_price": "2.50"})
        successor = [p for p in self.positions()
                     if p.underlying == "EXP" and p.is_open][0]
        collapsed = self.get("/")
        self.assertIn(f"chain={successor.id}", collapsed)
        self.assertNotIn("chain-leg", collapsed)

        expanded = self.get(f"/?show=open&chain={successor.id}")
        # The rolled leg appears as an ordinary row, dimmed as history;
        # the open successor is marked as the row that was clicked.
        self.assertRegex(expanded, r'class="chain-leg leg-closed"')
        self.assertRegex(expanded, r'class="chain-leg leg-open current"')
        self.assertIn('class="badge st-rolled"', expanded)

    def test_open_and_closed_legs_are_styled_differently(self):
        position = self._fresh("sty")
        self.post(f"/position/{position.id}/roll",
                  {"on": "2026-02-20", "close_price": "2.00",
                   "new_expiry": "2026-04-17", "new_strike": "33",
                   "new_price": "2.50"})
        successor = [p for p in self.positions()
                     if p.underlying == "STY" and p.is_open][0]
        body = self.get(f"/position/{successor.id}")
        self.assertRegex(body, r'class="chain-leg leg-closed"')
        self.assertRegex(body, r'class="chain-leg leg-open current"')
        self.assertIn('class="badge st-rolled"', body)


class TestPositionsOrderingAndAnchors(WebTestCase):
    def _fresh(self, ticker, **kw):
        self.add_position(underlying=ticker, **kw)
        return [p for p in self.positions()
                if p.underlying == ticker.upper() and p.is_open][0]

    def test_closed_view_is_most_recent_first(self):
        early = self._fresh("erl")
        late = self._fresh("lat")
        self.post(f"/position/{early.id}/close",
                  {"closed_on": "2026-02-01", "close_price": "1.00"})
        self.post(f"/position/{late.id}/close",
                  {"closed_on": "2026-03-01", "close_price": "1.00"})
        body = self.get("/?show=closed")
        self.assertLess(body.index("LAT"), body.index("ERL"))

    def test_closed_legs_are_not_called_branches(self):
        position = self._fresh("nob")
        self.post(f"/position/{position.id}/split",
                  {"quantity": "4", "on": "2026-02-01"})
        halves = [p for p in self.positions()
                  if p.underlying == "NOB" and p.is_open]
        for half in halves:
            self.post(f"/position/{half.id}/expire", {"on": "2026-03-20"})
        body = self.get("/?show=closed")
        self.assertNotIn("open in this chain", body)
        self.assertNotIn('class="fam fam-', body)

    def test_open_rows_are_anchored_and_links_target_them(self):
        position = self._fresh("anc")
        body = self.get("/")
        self.assertIn(f'id="row-{position.id}"', body)
        self.assertIn(f"&do=close#row-{position.id}", body)


class TestChainExpansion(WebTestCase):
    def test_expanding_a_split_shows_each_leg_exactly_once(self):
        self.add_position(underlying="onc")
        parent = [p for p in self.positions() if p.underlying == "ONC"][0]
        self.post(f"/position/{parent.id}/split",
                  {"quantity": "4", "on": "2026-02-01"})
        halves = [p for p in self.positions()
                  if p.underlying == "ONC" and p.is_open]
        four = min(halves, key=lambda p: p.quantity)
        six = max(halves, key=lambda p: p.quantity)

        collapsed = self.get("/?show=open")
        self.assertEqual(collapsed.count(f'id="row-{four.id}"'), 1)
        self.assertEqual(collapsed.count(f'id="row-{six.id}"'), 1)

        expanded = self.get(f"/?show=open&chain={four.id}")
        # The sibling is now shown inside the chain, not also on its own.
        for pid in (parent.id, four.id, six.id):
            with self.subTest(row=pid[:8]):
                self.assertEqual(expanded.count(f'id="row-{pid}"'), 1)
        self.assertIn("split into 4 + 6", expanded)
        # Chain rows appear in order: the split record first.
        self.assertLess(expanded.index(f'id="row-{parent.id}"'),
                        expanded.index(f'id="row-{four.id}"'))

    def test_open_sibling_inside_an_expansion_keeps_its_actions(self):
        self.add_position(underlying="sac")
        parent = [p for p in self.positions() if p.underlying == "SAC"][0]
        self.post(f"/position/{parent.id}/split",
                  {"quantity": "4", "on": "2026-02-01"})
        four, six = sorted([p for p in self.positions()
                            if p.underlying == "SAC" and p.is_open],
                           key=lambda p: p.quantity)
        expanded = self.get(f"/?show=open&chain={four.id}")
        self.assertIn(f"act={six.id}&do=roll", expanded)


class TestRowColumnsAndChainBlock(WebTestCase):
    def _fresh(self, ticker):
        self.add_position(underlying=ticker)
        return [p for p in self.positions()
                if p.underlying == ticker.upper() and p.is_open][0]

    def test_columns_in_the_requested_order(self):
        body = self.get("/")
        headers = re.findall(r"<th>(.*?)</th>", body)
        self.assertEqual(headers, ["", "Contract", "DTE", "Status", "Open price",
                                   "Close price", "Credit", "Closing", "Realized",
                                   "Carry", "Break-even", "At risk", "Legs"])

    def test_open_position_projects_close_to_the_target(self):
        position = self._fresh("prj")
        body = self.get("/")
        row = re.search(rf'<tr id="row-{position.id}".*?</tr>', body, re.S).group(0)
        # 1.49 target close; 1,496.50 expected closing; both marked as projections.
        self.assertIn('<span class="proj" title="50% target">1.49</span>', row)
        self.assertIn('title="at the 50% target">(1,496.50)</span>', row)
        self.assertIn('<td class="unit">3.00</td>', row)     # the open price

    def test_closed_position_shows_what_happened(self):
        position = self._fresh("hap")
        self.post(f"/position/{position.id}/close",
                  {"closed_on": "2026-02-10", "close_price": "1.50",
                   "close_fee": "6.50"})
        body = self.get("/?show=closed")
        row = re.search(rf'<tr id="row-{position.id}".*?</tr>', body, re.S).group(0)
        self.assertIn('<td class="unit">1.50</td>', row)
        self.assertIn("(1,506.50)", row)          # closing cash
        self.assertIn("1,487.00", row)            # realized
        self.assertNotIn("proj", row)

    def test_assigned_row_shows_a_clean_zero_close(self):
        position = self._fresh("zro")
        self.post(f"/position/{position.id}/assign",
                  {"on": "2026-03-20", "close_fee": "0", "share_fee": "0"})
        body = self.get("/?show=closed")
        row = re.search(rf'<tr id="row-{position.id}".*?</tr>', body, re.S).group(0)
        self.assertIn('<td class="unit">0.00</td>', row)
        self.assertNotIn("-0.00", row)
        self.assertIn("2,993.50", row)            # the whole credit realized

    def test_expanded_chain_has_a_header_and_a_hide_control(self):
        position = self._fresh("hdr")
        self.post(f"/position/{position.id}/roll",
                  {"on": "2026-02-20", "close_price": "2.00",
                   "new_expiry": "2026-04-17", "new_strike": "33",
                   "new_price": "2.50"})
        successor = [p for p in self.positions()
                     if p.underlying == "HDR" and p.is_open][0]
        body = self.get(f"/?show=open&chain={successor.id}")
        self.assertIn('class="chain-head"', body)
        self.assertIn("Chain &middot; 2 leg(s)", body)
        # Two ways to collapse: the header, and the clicked row's own pill.
        self.assertEqual(body.count('title="Hide the chain"'), 2)
        self.assertIn(">&#9650;</a>", body)      # a glyph, not a word
        self.assertNotIn(">hide</a>", body)

    def test_detail_page_tags_the_position_being_viewed(self):
        position = self._fresh("tag")
        body = self.get(f"/position/{position.id}")
        # The marker sits in the gutter cell, not inside the contract cell.
        self.assertRegex(body, r'<td class="gutter"><span class="here-arrow"')
        self.assertNotIn("&#9656;", body)   # drawn from the rail, not a character
        self.assertNotIn("this position</span>", body)
        self.assertRegex(body, rf'<tr id="row-{position.id}" class="[^"]*\bcurrent\b')

    def test_rows_of_a_chain_toggle_it_on_click(self):
        position = self._fresh("tgl")
        self.post(f"/position/{position.id}/roll",
                  {"on": "2026-02-20", "close_price": "2.00",
                   "new_expiry": "2026-04-17", "new_strike": "33",
                   "new_price": "2.50"})
        successor = [p for p in self.positions()
                     if p.underlying == "TGL" and p.is_open][0]
        collapsed = self.get("/?show=open")
        self.assertIn(f'data-chain="/?show=open&chain={successor.id}#row-{successor.id}"',
                      collapsed)
        expanded = self.get(f"/?show=open&chain={successor.id}")
        # Every row of the expanded chain collapses it.
        self.assertEqual(expanded.count(f'data-chain="/?show=open#row-{successor.id}"'), 2)

    def test_single_leg_rows_are_not_toggles(self):
        position = self._fresh("one")
        body = self.get("/?show=open")
        row = re.search(rf'<tr id="row-{position.id}"[^>]*>', body).group(0)
        self.assertNotIn("data-chain", row)

    def test_expanded_current_row_still_links_to_its_page(self):
        position = self._fresh("lnk2")
        body = self.get(f"/?show=open&chain={position.id}")
        row = re.search(rf'<tr id="row-{position.id}".*?</tr>', body, re.S).group(0)
        self.assertIn(f'href="/position/{position.id}"', row)

    def test_carry_column_follows_realized(self):
        headers = re.findall(r"<th>(.*?)</th>", self.get("/"))
        self.assertEqual(headers.index("Carry"), headers.index("Realized") + 1)

    def test_whole_contract_underlines_on_hover(self):
        css = self.get("/static/app.css")
        self.assertIn("a:hover .contract > :not(.qty) { text-decoration: underline; }", css)
        self.assertNotIn("a:hover .ticker { text-decoration", css)


class TestPartialAndProjection(WebTestCase):
    def _fresh(self, ticker):
        self.add_position(underlying=ticker)
        return [p for p in self.positions()
                if p.underlying == ticker.upper() and p.is_open][0]

    def test_partial_returns_only_the_table(self):
        self._fresh("prt")
        body = self.get("/?show=open&partial=table")
        self.assertTrue(body.startswith('<table class="positions"'), body[:60])
        self.assertNotIn("<!doctype html>", body)
        self.assertNotIn("<header>", body)
        self.assertIn("PRT", body)

    def test_partial_expansion_carries_the_chain_rows(self):
        position = self._fresh("pch")
        self.post(f"/position/{position.id}/roll",
                  {"on": "2026-02-20", "close_price": "2.00",
                   "new_expiry": "2026-04-17", "new_strike": "33",
                   "new_price": "2.50"})
        successor = [p for p in self.positions()
                     if p.underlying == "PCH" and p.is_open][0]
        body = self.get(f"/?show=open&chain={successor.id}&partial=table")
        self.assertIn('class="chain-head"', body)
        self.assertNotIn("<main>", body)

    def test_open_row_projects_expected_realized(self):
        position = self._fresh("exr")
        body = self.get("/?show=open")
        row = re.search(rf'<tr id="row-{position.id}".*?</tr>', body, re.S).group(0)
        # 2,993.50 credit less 1,496.50 projected closing = 1,497.00, greyed.
        self.assertIn('title="expected at the 50% target">1,497.00</span>', row)

    def test_script_prefetches_and_swaps_partials(self):
        js = self.get("/static/app.js")
        self.assertIn("partial=table", js)
        self.assertIn("prefetchAll", js)
        self.assertIn("replaceWith", js)
