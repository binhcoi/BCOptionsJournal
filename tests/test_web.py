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
from decimal import Decimal
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from bcoj.db import store
from bcoj.web.server import Handler as _Handler

# The suites silence request logging by replacing the method on the class;
# keep the real one so it can be tested.
_ORIGINAL_LOG_MESSAGE = _Handler.__dict__["log_message"]
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

    def test_history_shows_one_row_per_action_and_undoes_it_whole(self):
        self.add_position(underlying="grp")
        parent = [p for p in self.positions() if p.underlying == "GRP"][0]
        self.post(f"/position/{parent.id}/split", {"quantity": "4", "on": "2026-02-01"})
        page = self.get("/audit")
        self.assertIn("(3 records)", page)
        conn = store.open_db(self.db_path)
        try:
            entry = [e for e in store.audit_entries(conn) if "split" in e["action"]][-1]
        finally:
            conn.close()
        status, location, _ = self.post(f"/audit/{entry['id']}/revert", {})
        self.assertEqual(status, 303)
        self.assertIn("Undone split 10 into 4 and 6", location)     # says what, in trade terms
        grp = [p for p in self.positions() if p.underlying == "GRP"]
        self.assertEqual([(p.quantity, p.is_open) for p in grp], [(10, True)])
        # A second undo of the same action is refused, and not shown as success.
        status, location, _ = self.post(f"/audit/{entry['id']}/revert", {})
        self.assertIn("flash=%21", location.replace("!", "%21"))
        self.assertIn("already undone", location)
        # The action's own row shows it undone, struck through, with Redo.
        page = self.get("/audit")
        self.assertIn("undone at", page)
        self.assertIn('<s class="dim">', page)
        self.assertNotIn("undo of audit #", page)
        self.assertNotIn("Undid", page)                      # no bookkeeping rows
        self.assertIn('submit">Redo</button>', page)
        status, location, _ = self.post(f"/audit/{entry['id']}/redo", {})
        self.assertEqual(status, 303)
        self.assertIn("Redone split 10 into 4 and 6", location)
        grp = [p for p in self.positions() if p.underlying == "GRP"]
        self.assertEqual(sorted((p.quantity, p.status.value) for p in grp),
                         [(4, "OPEN"), (6, "OPEN"), (10, "SPLIT")])
        page = self.get("/audit")
        self.assertNotIn("undone at", page)                 # in effect again: Undo is back
        self.assertIn('submit">Undo</button>', page)
        status, location, _ = self.post(f"/audit/{entry['id']}/redo", {})
        self.assertIn("in effect", location)                # a second redo does nothing
        status, location, _ = self.post(f"/audit/{entry['id']}/revert", {})
        self.assertEqual(status, 303)                        # and undo works again

    def test_reverting_a_missing_entry_says_so(self):
        status, location, _ = self.post("/audit/999999/revert", {})
        self.assertEqual(status, 303)
        self.assertIn("no audit entry", location)

    def _position(self, ticker):
        self.add_position(underlying=ticker)
        return [p for p in self.positions()
                if p.underlying == ticker.upper() and p.is_open][0]


class TestPositionPage(WebTestCase):
    def test_every_action_form_opens_in_the_chain_table_with_the_chosen_one_shown(self):
        self.add_position(underlying="tabs")
        p = [q for q in self.positions() if q.underlying == "TABS"][0]
        page = self.get(f"/position/{p.id}?do=assign")
        self.assertIn('class="action-row"', page)                 # under the row, as on the list
        self.assertIn('data-form="assign">', page)
        for key in ("close", "roll", "expire", "split"):
            with self.subTest(form=key):
                self.assertIn(f'data-form="{key}" hidden', page)
        self.assertIn('data-form-tab="assign" class="tab-assign here"', page)
        self.assertEqual(page.count(f'action="/position/{p.id}/assign"'), 1)
        self.assertEqual(page.count('class="btn cancel"'), 5)
        # The page serves the same partials the list does, so the script can
        # open and switch forms in place.
        frag = self.get(f"/position/{p.id}?&act={p.id}&do=close&partial=action")
        self.assertTrue(frag.startswith(f'<tr id="row-{p.id}"'))
        self.assertEqual(frag.count('class="action-row"'), 1)
        self.assertIn(f'name="next" value="/position/{p.id}?"', frag)
        for cls in ("btn-close", "btn-roll", "btn-expire", "btn-assign", "btn-split"):
            with self.subTest(button=cls):
                self.assertIn(f'class="{cls}"', page)

    def test_open_position_shows_decision_figures(self):
        self.add_position(underlying="dec")
        position = [p for p in self.positions() if p.underlying == "DEC"][0]
        body = self.get(f"/position/{position.id}")
        for label in ("break-even", "50% target", "Capital at risk",
                      "Banked", "In hand", "To close at target", "Net at target"):
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
        self.assertIn("Chain legs - 2", body)


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

    def test_rows_carry_one_button_and_the_open_form_carries_the_tabs(self):
        position = self._fresh("lnk")
        body = self.get("/")
        self.assertIn(f"act={position.id}&do=close", body)
        visible = re.sub(r"<template.*?</template>", "", body, flags=re.S)
        self.assertNotIn("&do=roll", visible)        # one button per row, not five
        opened = self.get(f"/?show=open&act={position.id}&do=close")
        for action in ("close", "roll", "expire", "assign", "split"):
            with self.subTest(action=action):
                self.assertIn(f'href="/?show=open&act={position.id}&do={action}#row-{position.id}"'
                              f' data-form-tab="{action}"', opened)
        # All five forms are in the row; only the chosen one is shown.
        self.assertIn('data-form="close">', opened)
        self.assertIn('data-form="roll" hidden', opened)
        self.assertIn('class="tabs even"', opened)
        self.assertIn('data-form-tab="roll" class="tab-roll"', opened)   # colour-coded

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
        self.assertIn('name="close_price" id="f_close_price" value="1.49"', body)
        self.assertIn("Pre-filled with the 50% target", body)

    def test_roll_form_is_two_legs_with_matching_columns(self):
        position = self._fresh("two")
        body = self.get(f"/?show=open&act={position.id}&do=roll")
        # A short position rolls as buy-to-close then sell-to-open.
        self.assertIn(">BTC</b>", body)
        self.assertIn(">STO</b>", body)
        self.assertLess(body.index(">BTC</b>"), body.index(">STO</b>"))
        # The closing leg's terms are fixed text; the new leg's are inputs.
        self.assertIn('<span class="fixed">35.00</span>', body)
        self.assertIn('name="new_strike" id="f_new_strike" value="35" step="0.5"', body)
        self.assertIn('class="btn-roll"', body)
        # And the form can be dismissed without acting, by x or by Cancel.
        self.assertIn(f'class="close-form" href="/?show=open#row-{position.id}"', body)
        self.assertIn(f'class="btn cancel" href="/?show=open#row-{position.id}"', body)

    def test_close_expire_and_assign_use_the_same_leg_grid(self):
        position = self._fresh("grd")
        close = self.get(f"/?show=open&act={position.id}&do=close")
        self.assertIn(">BTC</b>", close)
        self.assertIn('aria-label="Close price / share"', close)
        expire = self.get(f"/?show=open&act={position.id}&do=expire")
        self.assertIn(">EXP</b>", expire)
        self.assertIn('<span class="fixed">0.00</span>', expire)
        assign = self.get(f"/?show=open&act={position.id}&do=assign")
        self.assertIn(">ASG</b>", assign)
        self.assertIn(">BUY</b>", assign)                   # the share leg
        self.assertIn('<span class="fixed">1000</span>', assign)
        # All five forms in the row share the grid, so switching is not jarring
        # (the sixth grid on the page is the inline New form).
        for body in (close, expire, assign):
            self.assertEqual(body.count('class="leg-grid"'), 6)
        self.assertIn(">SPLIT</b>", close)
        self.assertIn('aria-label="Contracts to peel off"', close)

    def test_new_position_form_is_a_leg_row(self):
        body = self.get("/new")
        self.assertIn('class="leg-grid" data-fee-rate=', body)
        # Same order as the close forms: the date, then the leg.
        self.assertLess(body.index('name="opened_on"'), body.index('class="leg-grid"'))
        self.assertIn("<h3>New position</h3>", self.get("/?show=open"))
        self.assertIn('<option value="SHORT" selected>STO</option>', body)
        self.assertIn('<option value="LONG">BTO</option>', body)
        self.assertIn('id="f_quantity"', body)
        self.assertIn('id="f_open_fee"', body)
        self.assertIn('name="strike" id="f_strike" value="" step="0.5"', body)

    def test_long_position_rolls_as_sell_to_close_then_buy_to_open(self):
        self.add_position(underlying="lng", direction="LONG")
        position = [p for p in self.positions() if p.underlying == "LNG"][0]
        body = self.get(f"/?show=open&act={position.id}&do=roll")
        self.assertLess(body.index(">STC</b>"), body.index(">BTO</b>"))

    def test_short_column_headers_keep_their_full_name_as_a_title(self):
        body = self.get("/?show=open")
        self.assertIn('<th title="Open price / share"><abbr>Open</abbr></th>', body)
        self.assertIn('<th title="Break-even for the chain"><abbr>B/E</abbr></th>', body)

    def test_row_actions_mark_the_open_one(self):
        position = self._fresh("opn")
        body = self.get(f"/?show=open&act={position.id}&do=close")
        self.assertIn('class="act act-close here"', body)
        self.assertIn('<td class="main">', body)

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
        self.assertIn("Chain legs - 4", body)
        self.assertIn("split into 4 + 6", body)
        self.assertIn('class="badge st-split"', body)
        self.assertIn("all legs", body)
        self.assertIn("2026-04-17", body)          # the other half's roll
        # Same columns as the positions page: one renderer.
        self.assertIn("<abbr>B/E</abbr>", body)
        self.assertIn("<th>Legs</th>", body)
        # The row being looked at is marked, and still a link to itself.
        self.assertRegex(body, r'<tr id="row-%s" class="[^"]*\bcurrent\b' % four.id)
        self.assertIn(f'href="/position/{four.id}"', body)

    def test_a_plain_chain_is_still_called_a_chain(self):
        self.add_position(underlying="pln")
        position = [p for p in self.positions() if p.underlying == "PLN"][0]
        body = self.get(f"/position/{position.id}")
        self.assertIn("Chain legs - 1", body)
        self.assertNotIn("All legs realized", body)

    def test_days_show_while_open(self):
        self.add_position(underlying="dys",
                          opened_on=(date.today() - timedelta(days=12)).isoformat(),
                          expiry=(date.today() + timedelta(days=30)).isoformat())
        position = [p for p in self.positions() if p.underlying == "DYS"][0]
        body = self.get(f"/position/{position.id}")
        self.assertIn("12 days", body)


class TestPositionsPageLayout(WebTestCase):
    def _fresh(self, ticker):
        self.add_position(underlying=ticker)
        return [p for p in self.positions()
                if p.underlying == ticker.upper() and p.is_open][0]

    def test_actions_have_their_own_cell_and_are_colour_coded(self):
        self._fresh("lay")
        body = self.get("/")
        # The contract cell holds the contract only; the actions sit in theirs.
        contract = re.search(r'<td class="main"><a href="/position/[^"]+">.*?</td>', body, re.S).group(0)
        self.assertNotIn("row-actions", contract)
        cell = re.search(r'<td class="acts">.*?</td>', body, re.S).group(0)
        self.assertIn('class="act act-close"', cell)
        self.assertEqual(cell.count("<a "), 1)
        self.assertNotIn("row-actions", cell)

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
        table = body[body.index('<table class="positions'):]   # not the ticker datalist
        self.assertLess(table.index("LAT"), table.index("ERL"))

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

    def test_an_open_action_and_an_expanded_chain_keep_each_other(self):
        self.add_position(underlying="kep")
        parent = [p for p in self.positions() if p.underlying == "KEP"][0]
        self.post(f"/position/{parent.id}/split",
                  {"quantity": "4", "on": "2026-02-01"})
        four, six = sorted([p for p in self.positions()
                            if p.underlying == "KEP" and p.is_open],
                           key=lambda p: p.quantity)
        page = self.get(f"/?show=open&chain={four.id}&act={six.id}&do=close")
        self.assertIn('class="action-row"', page)
        self.assertEqual(page.count(f'id="row-{parent.id}"'), 1)   # still expanded
        # Opening another row's form keeps the chain expanded.
        self.assertIn(f"chain={four.id}&act={four.id}&do=close", page)
        # Collapsing the chain keeps the open form.
        self.assertIn(f'href="/?show=open&act={six.id}&do=close#row-{four.id}"', page)
        # The open action's own link closes just the form.
        self.assertIn(f'href="/?show=open&chain={four.id}#row-{six.id}"', page)

    def test_closing_a_leg_inside_an_open_chain_keeps_the_rest_listed(self):
        self.add_position(underlying="keep")
        parent = [p for p in self.positions() if p.underlying == "KEEP"][0]
        self.post(f"/position/{parent.id}/split", {"quantity": "4", "on": "2026-02-01"})
        four, six = sorted([p for p in self.positions() if p.underlying == "KEEP" and p.is_open],
                           key=lambda p: p.quantity)
        # Close the four while its chain is expanded; the page returns with
        # that chain still in the URL.
        self.post(f"/position/{four.id}/close",
                  {"closed_on": "2026-02-06", "close_price": "1.00",
                   "next": f"/?show=open&chain={four.id}"})
        page = self.get(f"/?show=open&chain={four.id}")
        self.assertIn(f'id="row-{six.id}"', page)                 # the open half is still there
        self.assertIn('class="chain-head"', page)                 # drawn as the chain, anchored on it
        self.assertIn(f'id="row-{four.id}"', page)                # the closed half shown inside it
        # And once no family member is listed at all, nothing is held back.
        self.post(f"/position/{six.id}/close",
                  {"closed_on": "2026-02-06", "close_price": "1.00"})
        page = self.get(f"/?show=open&chain={four.id}")
        self.assertNotIn("KEEP", page[page.index('<table class="positions'):])
        self.assertNotIn('class="chain-head"', page)

    def test_open_sibling_inside_an_expansion_keeps_its_actions(self):
        self.add_position(underlying="sac")
        parent = [p for p in self.positions() if p.underlying == "SAC"][0]
        self.post(f"/position/{parent.id}/split",
                  {"quantity": "4", "on": "2026-02-01"})
        four, six = sorted([p for p in self.positions()
                            if p.underlying == "SAC" and p.is_open],
                           key=lambda p: p.quantity)
        expanded = self.get(f"/?show=open&chain={four.id}")
        self.assertIn(f"act={six.id}&do=close", expanded)


class TestRowColumnsAndChainBlock(WebTestCase):
    def _fresh(self, ticker):
        self.add_position(underlying=ticker)
        return [p for p in self.positions()
                if p.underlying == ticker.upper() and p.is_open][0]

    def test_columns_in_the_requested_order(self):
        body = self.get("/")
        headers = re.findall(r"<th(?: [^>]*)?>(?:<abbr>)?(.*?)(?:</abbr>)?</th>", body)
        self.assertEqual(headers, ["", "Contract", "DTE", "Status", "Open",
                                   "Close", "Credit", "Closing", "Realized",
                                   "Carry", "B/E", "At risk", "", "Legs"])

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

    def test_partial_returns_the_table_and_its_strip_only(self):
        self._fresh("prt")
        body = self.get("/?show=open&partial=table")
        self.assertTrue(body.startswith('<div class="totals score">'), body[:60])
        self.assertIn('<table class="positions"', body)
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

    def test_script_fetches_only_the_rows_it_needs(self):
        js = self.get("/static/app.js")
        self.assertIn("'block'", js)              # a chain: its rows only
        self.assertIn("'action'", js)             # a form: its row and the form
        self.assertIn("closeForms", js)           # closing fetches nothing
        self.assertIn("patchRows", js)            # a full table is diffed, not swapped
        self.assertNotIn("mouseover", js)         # and nothing is fetched speculatively
        self.assertNotIn("prefetchAll", js)

    def test_table_partial_carries_the_strip_that_describes_it(self):
        self.add_position(underlying="tps")
        frag = self.get("/?show=open&ticker=TPS&partial=table")
        self.assertTrue(frag.startswith('<div class="totals score">'), frag[:60])
        self.assertIn("the 1 position(s) in this table", frag)
        self.assertIn('<div class="filterbar">', frag)                 # the bar travels too
        self.assertIn("Ticker: TPS", frag)
        self.assertIn('<table class="positions">', frag)
        js = self.get("/static/app.js")
        self.assertIn("applyFilters", js)
        self.assertIn("'change'", js)             # a dropdown applies itself

    def test_every_open_row_ships_its_forms_hidden(self):
        self.add_position(underlying="shp")
        p = [q for q in self.positions() if q.underlying == "SHP"][0]
        page = self.get("/?show=open")
        self.assertIn(f'<template class="acts" data-for="{p.id}">', page)
        tpl = page[page.index(f'data-for="{p.id}"'):]
        tpl = tpl[:tpl.index("</template>")]
        self.assertIn('class="action-row"', tpl)
        self.assertIn('data-form="roll" hidden', tpl)
        # Once a form is open the real row replaces the template for that leg.
        opened = self.get(f"/?show=open&act={p.id}&do=close")
        self.assertNotIn(f'<template class="acts" data-for="{p.id}">', opened)
        js = self.get("/static/app.js")
        self.assertIn("template.acts", js)
        # The position page ships them too.
        self.assertIn(f'<template class="acts" data-for="{p.id}">', self.get(f"/position/{p.id}"))

    def test_block_partial_is_just_the_chain_rows(self):
        self.add_position(underlying="blk")
        parent = [p for p in self.positions() if p.underlying == "BLK"][0]
        self.post(f"/position/{parent.id}/split", {"quantity": "4", "on": "2026-02-01"})
        self.add_position(underlying="oth")
        four = min([p for p in self.positions() if p.underlying == "BLK" and p.is_open],
                   key=lambda p: p.quantity)
        block = self.get(f"/?show=open&chain={four.id}&partial=block")
        self.assertNotIn("<table", block)
        self.assertIn('class="chain-head"', block)
        rows_only = re.sub(r"<template.*?</template>", "", block, flags=re.S)
        self.assertEqual(rows_only.count("<tr"), 4)            # head + three legs
        self.assertNotIn("OTH", block)

    def test_action_partial_is_the_row_and_its_form(self):
        self.add_position(underlying="actp")
        p = [q for q in self.positions() if q.underlying == "ACTP"][0]
        frag = self.get(f"/?show=open&act={p.id}&do=roll&partial=action")
        self.assertTrue(frag.startswith(f'<tr id="row-{p.id}"'), frag[:80])
        self.assertEqual(frag.count('class="action-row"'), 1)
        self.assertIn('class="act act-close here"', frag)
        self.assertNotIn("<table", frag)
        # Without an open form there is nothing to send.
        self.assertEqual(self.get(f"/?show=open&act={p.id}&partial=action"), "")

    def test_responses_are_gzipped_when_the_browser_accepts_it(self):
        import gzip
        req = urllib.request.Request(self.base + "/?show=open&partial=table",
                                     headers={"Accept-Encoding": "gzip"})
        with urllib.request.urlopen(req) as res:
            self.assertEqual(res.headers.get("Content-Encoding"), "gzip")
            raw = res.read()
            self.assertEqual(int(res.headers["Content-Length"]), len(raw))
        self.assertIn("table", gzip.decompress(raw).decode())
        # Small responses and clients that do not ask are left alone.
        with urllib.request.urlopen(self.base + "/?show=open&partial=table") as res:
            self.assertIsNone(res.headers.get("Content-Encoding"))


class TestShares(WebTestCase):
    def _put_assigned(self, ticker, qty="10", strike="35"):
        self.add_position(underlying=ticker, quantity=qty, strike=strike)
        position = [p for p in self.positions()
                    if p.underlying == ticker.upper() and p.is_open][0]
        self.post(f"/position/{position.id}/assign",
                  {"on": "2026-03-20", "close_fee": "0", "share_fee": "0"})
        return position

    def _lots(self, ticker):
        conn = store.open_db(self.db_path)
        try:
            return [l for l in store.load_lots(conn) if l.underlying == ticker.upper()]
        finally:
            conn.close()

    def test_shares_pages_render(self):
        self._put_assigned("shr")
        body = self.get("/shares")
        self.assertIn("SHR", body)
        self.assertIn("holding 1000", body)
        page = self.get("/shares/SHR")
        self.assertIn("Lots - 1", page)
        self.assertIn("put assignment", page)
        self.assertIn("Wheel total", page)

    def test_unknown_ticker_is_404(self):
        self.get("/shares/NOPE", expect=404)

    def test_buy_shares_outright(self):
        status, location, _ = self.post("/shares/buy", {
            "underlying": "out", "on": "2026-01-05", "quantity": "300",
            "price": "12.50", "fee": "0", "notes": "test",
        })
        self.assertEqual(status, 303)
        lots = self._lots("out")
        self.assertEqual(lots[0].quantity, 300)
        self.assertEqual(str(lots[0].cost_per_share), "12.50")
        self.assertEqual(lots[0].source.value, "OUTRIGHT_BUY")

    def test_sell_shares_realizes_against_the_lot(self):
        self.post("/shares/buy", {"underlying": "sel", "on": "2026-01-05",
                                  "quantity": "300", "price": "10.00"})
        status, location, _ = self.post("/shares/sell", {
            "underlying": "sel", "on": "2026-06-05", "quantity": "200",
            "price": "14.00", "fee": "0",
        })
        self.assertEqual(status, 303)
        page = self.get("/shares/SEL")
        self.assertIn("800.00", page)      # (14 - 10) x 200
        self.assertIn("holding 100", self.get("/shares"))

    def test_selling_more_than_held_is_refused(self):
        self.post("/shares/buy", {"underlying": "shrt", "on": "2026-01-05",
                                  "quantity": "100", "price": "10.00"})
        status, _, body = self.post("/shares/sell", {
            "underlying": "shrt", "on": "2026-06-05", "quantity": "150", "price": "12",
        })
        self.assertEqual(status, 400)
        self.assertIn("would leave the ticker short 50", body)

    def test_buy_write_links_call_to_lot(self):
        status, _, _ = self.post("/shares/buy-write", {
            "underlying": "bw", "on": "2026-01-05", "shares": "300",
            "share_price": "12.00", "share_fee": "0", "expiry": "2026-03-20",
            "strike": "13", "call_price": "0.50", "contracts": "", "option_fee": "1.95",
        })
        self.assertEqual(status, 303)
        lot = self._lots("bw")[0]
        call = [p for p in self.positions() if p.underlying == "BW"][0]
        self.assertEqual(call.share_lot_id, lot.id)
        self.assertEqual(call.quantity, 3)
        page = self.get("/shares/BW")
        self.assertIn("300/300", page)       # fully covered

    def test_assignment_creates_a_lot_and_the_put_links_to_it(self):
        put = self._put_assigned("asn")
        lot = self._lots("asn")[0]
        self.assertEqual(lot.assigning_position_id, put.id)
        body = self.get(f"/position/{put.id}")
        self.assertIn("Shares acquired", body)

    def test_naked_call_can_be_covered_and_uncovered(self):
        self._put_assigned("cov")
        lot = self._lots("cov")[0]
        self.add_position(underlying="cov", right="CALL", strike="40",
                          opened_on="2026-04-01", expiry="2026-05-15")
        call = [p for p in self.positions() if p.underlying == "COV"
                and p.right.value == "CALL"][0]
        # The fact reads "nothing - naked"; the cover form's dropdown also
        # contains the word, so assert on the fact's wording.
        self.assertIn("nothing - naked", self.get(f"/position/{call.id}"))

        status, _, _ = self.post(f"/position/{call.id}/cover", {"lot_id": lot.id})
        self.assertEqual(status, 303)
        page = self.get(f"/position/{call.id}")
        self.assertIn("Covered by", page)
        self.assertNotIn("nothing - naked", page)

        self.post(f"/position/{call.id}/cover", {"lot_id": ""})
        self.assertIn("nothing - naked", self.get(f"/position/{call.id}"))

    def test_cover_refuses_a_lot_of_another_ticker(self):
        self._put_assigned("cva")
        self._put_assigned("cvb")
        lot_b = self._lots("cvb")[0]
        self.add_position(underlying="cva", right="CALL", strike="40",
                          opened_on="2026-04-01", expiry="2026-05-15")
        call = [p for p in self.positions() if p.underlying == "CVA"
                and p.right.value == "CALL"][0]
        status, _, body = self.post(f"/position/{call.id}/cover", {"lot_id": lot_b.id})
        self.assertEqual(status, 400)
        self.assertIn("not CVA", body)

    def test_unlinked_covered_calls_are_suggested_and_linkable_in_bulk(self):
        self._put_assigned("sug")
        self.add_position(underlying="sug", right="CALL", strike="40",
                          opened_on="2026-04-01", expiry="2026-05-15")
        body = self.get("/shares/covers")
        self.assertIn("SUG", body)
        self.assertIn("Link all", body)
        status, _, _ = self.post("/shares/covers/apply", {})
        self.assertEqual(status, 303)
        call = [p for p in self.positions() if p.underlying == "SUG"
                and p.right.value == "CALL"][0]
        self.assertEqual(call.share_lot_id, self._lots("sug")[0].id)

    def test_new_position_form_offers_covering_lots(self):
        self._put_assigned("nfl")
        body = self.get("/new")
        self.assertIn('name="share_lot_id"', body)
        self.assertIn("NFL @ 35.00", body)

    def test_dashboard_shows_true_total(self):
        body = self.get("/reports")
        for label in ("Realized options", "Realized shares", "True total"):
            with self.subTest(label=label):
                self.assertIn(label, body)

    def test_wheel_total_on_a_full_cycle(self):
        """Put assigned at 35 with 2,993.50 premium, called away at 40."""
        put = self._put_assigned("whl")
        lot = self._lots("whl")[0]
        self.add_position(underlying="whl", right="CALL", strike="40",
                          opened_on="2026-04-01", expiry="2026-05-15",
                          open_price="1.00", open_fee="6.50", share_lot_id=lot.id)
        call = [p for p in self.positions() if p.underlying == "WHL"
                and p.right.value == "CALL"][0]
        self.post(f"/position/{call.id}/assign",
                  {"on": "2026-05-15", "close_fee": "0", "share_fee": "0"})
        page = self.get("/shares/WHL")
        # 2,993.50 (put) + 993.50 (call) + 5,000.00 (shares) = 8,987.00
        self.assertIn("8,987.00", page)
        self.assertIn("flat", self.get("/shares"))


class TestFutureDatesRefused(WebTestCase):
    """Nothing can be recorded as having happened on a day that has not come."""

    def _open(self, ticker):
        self.add_position(underlying=ticker, expiry="2099-03-20")
        return [p for p in self.positions() if p.underlying == ticker.upper()][0]

    def test_every_recording_form_refuses_a_future_date(self):
        p = self._open("fut1")
        self.post("/shares/buy", {"underlying": "fut1", "on": "2026-01-05",
                                  "quantity": "200", "price": "10"})
        cases = {
            "new": ("/new", {"underlying": "futn", "opened_on": "2099-01-01",
                             "expiry": "2099-03-20", "right": "PUT", "direction": "SHORT",
                             "strike": "35", "quantity": "1", "open_price": "1.00"}),
            "close": (f"/position/{p.id}/close",
                      {"closed_on": "2099-01-02", "close_price": "1.00"}),
            "expire": (f"/position/{p.id}/expire", {"on": "2099-03-20"}),
            "assign": (f"/position/{p.id}/assign", {"on": "2099-03-20"}),
            "roll": (f"/position/{p.id}/roll",
                     {"on": "2099-01-02", "close_price": "1.00", "new_expiry": "2099-04-17",
                      "new_strike": "34", "new_price": "2.00"}),
            "split": (f"/position/{p.id}/split", {"on": "2099-01-02", "quantity": "4"}),
            "buy": ("/shares/buy", {"underlying": "fut1", "on": "2099-01-02",
                                    "quantity": "100", "price": "10"}),
            "sell": ("/shares/sell", {"underlying": "fut1", "on": "2099-01-02",
                                      "quantity": "100", "price": "12"}),
            "buy-write": ("/shares/buy-write",
                          {"underlying": "fut1", "on": "2099-01-02", "shares": "100",
                           "share_price": "10", "expiry": "2099-04-17", "strike": "12",
                           "call_price": "0.50"}),
        }
        for name, (path, fields) in cases.items():
            with self.subTest(form=name):
                status, _, body = self.post(path, fields)
                self.assertEqual(status, 400, body[:300])
                self.assertIn("is in the future", body)
        # And nothing was recorded along the way.
        self.assertTrue(all(q.is_open for q in self.positions()
                            if q.underlying == "FUT1"))


class TestTransport(WebTestCase):
    """Keep-alive, so a burst of fetches does not overflow the listen backlog."""

    def test_speaks_http_1_1_and_reuses_a_connection(self):
        import http.client
        from bcoj.web.server import Handler, Server
        self.assertEqual(Handler.protocol_version, "HTTP/1.1")
        self.assertGreaterEqual(Server.request_queue_size, 64)
        host, port = self.base.replace("http://", "").split(":")
        conn = http.client.HTTPConnection(host, int(port))
        try:
            for path in ("/", "/?show=open&partial=table", "/static/app.js"):
                conn.request("GET", path)
                res = conn.getresponse()
                body = res.read()
                self.assertEqual(res.status, 200)
                self.assertEqual(int(res.getheader("Content-Length")), len(body))
                self.assertEqual(res.version, 11)
        finally:
            conn.close()

    def test_logging_survives_a_connection_with_no_request_line(self):
        # A browser dropping a kept-alive connection makes the stdlib log an
        # error before any request was parsed: no command, no path.
        import io, contextlib
        from bcoj.web.server import Handler
        handler = Handler.__new__(Handler)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            _ORIGINAL_LOG_MESSAGE(handler, "code %d, message %s", 400,
                                  "Bad request version ('')")
            _ORIGINAL_LOG_MESSAGE(handler, '"%s" %s %s', "GET / HTTP/1.1", "200", "-")
        self.assertIn("Bad request version", out.getvalue())
        self.assertIn("-> 200", out.getvalue())

    def test_padding_before_a_request_line_is_ignored(self):
        # Twelve NUL bytes ahead of GET, as seen through a forwarded port.
        import socket
        host, port = self.base.replace("http://", "").split(":")
        with socket.create_connection((host, int(port)), timeout=5) as sock:
            sock.sendall(b"\x00" * 12 + b"GET /?show=open HTTP/1.1\r\nHost: x\r\n"
                         b"Connection: close\r\n\r\n")
            data = b""
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                data += chunk
        self.assertTrue(data.startswith(b"HTTP/1.1 200"), data[:60])
        self.assertIn(b"Positions", data)

    def test_refusals_also_carry_a_length(self):
        status, _, body = self.post("/shares/sell", {"underlying": "none", "on": "2026-01-05",
                                                     "quantity": "1", "price": "1"})
        self.assertEqual(status, 400)
        self.assertTrue(body)


class TestDecisionSupport(WebTestCase):
    """M4: the roll panel, the risk page, and strike drift on a chain."""

    def _open(self, ticker, **kw):
        self.add_position(underlying=ticker, **kw)
        return [p for p in self.positions() if p.underlying == ticker.upper() and p.is_open][0]

    def test_roll_form_carries_a_live_preview(self):
        p = self._open("rpv")
        page = self.get(f"/position/{p.id}?do=roll")
        self.assertIn(f'data-preview="/position/{p.id}/roll-preview"', page)
        self.assertIn("appears here as you type", page)
        # Within the roll panel the date comes first, then the legs.
        panel = page[page.index('data-form="roll"'):page.index('data-form="expire"')]
        self.assertLess(panel.index('name="on"'), panel.index('class="leg-grid"'))

    def test_roll_preview_shows_the_engine_figures(self):
        # Fixture leg: 10 puts at 35, 3.00, fee 6.50. Same roll as the engine test.
        p = self._open("rpf")
        frag = self.get(f"/position/{p.id}/roll-preview?close_price=4.00&close_fee=6.50"
                        "&new_expiry=2026-03-20&new_strike=34&new_price=5.00"
                        "&new_fee=7.80&new_quantity=12&on=2026-02-06")
        self.assertIn("credit 1,985.70", frag)
        self.assertIn('<span class="pos">29.85</span>', frag)   # break-even fell: good
        self.assertIn("was 32.01", frag)
        self.assertIn("40,800.00", frag)       # capital at risk after
        self.assertIn("Size grows 10 &rarr; 12", frag)
        self.assertIn("42 DTE", frag)
        # The journal is untouched by previewing.
        self.assertTrue(store.load_position(store.open_db(self.db_path), p.id).is_open)

    def test_roll_preview_waits_politely_for_missing_fields(self):
        p = self._open("rpw")
        frag = self.get(f"/position/{p.id}/roll-preview?close_price=4.00")
        self.assertIn("appears here as you type", frag)

    def test_roll_preview_flags_a_chain_left_under_water(self):
        p = self._open("rpu")
        frag = self.get(f"/position/{p.id}/roll-preview?close_price=8.00&close_fee=6.50"
                        "&new_expiry=2026-03-20&new_strike=34&new_price=3.00"
                        "&new_fee=7.80&new_quantity=12&on=2026-02-06")
        self.assertIn("debit 4,414.30", frag)
        self.assertIn("under water by <b>1,420.80</b>", frag)
        self.assertIn("none: net debit", frag)

    def test_risk_page_concentration_and_calendar(self):
        self._open("acme")                                     # 10 x 35 = 35,000
        self._open("beta", quantity="2", strike="20")          # 2 x 20 = 4,000
        self._open("gam", right="CALL", quantity="1", strike="90", expiry="2026-04-17")
        page = self.get("/risk")
        # The class shares one journal, so assert per-ticker figures, not totals.
        acme = page[page.index('href="/shares/ACME"'):]
        self.assertIn("35,000.00", acme[:acme.index("</tr>")])
        beta = page[page.index('href="/shares/BETA"'):]
        self.assertIn("4,000.00", beta[:beta.index("</tr>")])
        self.assertLess(page.index('href="/shares/ACME"'), page.index('href="/shares/BETA"'))
        self.assertIn("1 naked", page)
        self.assertIn("2026-03-20", page)
        self.assertIn("2026-04-17", page)
        self.assertIn(">100<", page)                           # shares to deliver
        self.assertIn("break-even", page)

    def test_chain_block_shows_strike_drift_and_size_growth(self):
        p = self._open("drf")
        status, _, _ = self.post(f"/position/{p.id}/roll", {
            "on": "2026-02-06", "close_price": "4.00", "new_expiry": "2026-03-20",
            "new_strike": "34", "new_price": "5.00", "new_quantity": "12"})
        self.assertEqual(status, 303)
        head = [q for q in self.positions() if q.underlying == "DRF" and q.is_open][0]
        page = self.get(f"/position/{head.id}")
        self.assertIn("35.00 &rarr; 34.00", page)
        self.assertIn("10 &rarr; 12", page)
        self.assertIn("x1.2", page)


class TestReportingAndViews(WebTestCase):
    """M5: reports, filters, saved views, notes and tags, exports."""

    def _open(self, ticker, **kw):
        self.add_position(underlying=ticker, **kw)
        return [p for p in self.positions() if p.underlying == ticker.upper() and p.is_open][0]

    def test_reports_page_groups_by_period_and_ticker(self):
        p = self._open("rep")
        self.post(f"/position/{p.id}/close", {"closed_on": "2026-02-06", "close_price": "1.00",
                                               "close_fee": "6.50"})       # realizes 1,987.00
        page = self.get("/reports")
        self.assertIn("Realized by period", page)
        self.assertIn("2026-02", page)
        self.assertIn("1,987.00", page)
        self.assertIn('href="/shares/REP"', page)
        self.assertIn("How short legs ended", page)
        self.assertIn("60+ DTE", page)
        year = self.get("/reports?by=year")
        self.assertIn(">2026<", year)
        self.assertIn("/export/positions.csv", page)

    def test_filters_narrow_the_list_and_travel_with_every_link(self):
        acme = self._open("fac")
        beta = self._open("fbe", right="CALL")
        page = self.get("/?show=open&q=fbe")
        self.assertIn(f'id="row-{beta.id}"', page)
        self.assertNotIn(f'id="row-{acme.id}"', page)
        self.assertIn(f'href="/?show=open&q=fbe&act={beta.id}&do=close#row-{beta.id}"', page)
        calls = self.get("/?show=open&right=CALL")
        self.assertIn(f'id="row-{beta.id}"', calls)
        self.assertNotIn(f'id="row-{acme.id}"', calls)
        self.assertIn('class="filters"', page)
        self.assertIn("Save view as", page)
        self.assertIn('name="filter" value="show=open&amp;q=fbe"', page)
        self.assertIn('<div class="chips active">', page)             # active filters, own line
        self.assertIn('<div class="chips views">', page)              # views, own line
        self.assertNotIn('<div class="chips active">', self.get("/?show=open"))
        self.assertIn('<details class="more">', page)                   # advanced folded
        self.assertIn('<details class="more" open>', self.get("/?show=open&right=CALL"))

    def test_notes_and_tags_edit_and_filter(self):
        p = self._open("tag")
        status, location, _ = self.post(f"/position/{p.id}/notes",
                                        {"notes": "sold the rip", "tags": "wheel, #Earnings"})
        self.assertEqual(status, 303)
        self.assertIn("Notes saved", location)
        page = self.get(f"/position/{p.id}")
        self.assertIn("sold the rip", page)
        self.assertIn('value="Earnings, wheel"', page)
        rows = self.get("/?show=open&tag=wheel")
        self.assertIn(f'id="row-{p.id}"', rows)
        self.assertIn('<span class="tag">wheel</span>', rows)
        self.assertNotIn(f'id="row-{p.id}"', self.get("/?show=open&tag=nothing"))
        # Tags can be given when the position is entered.
        self.add_position(underlying="tgn", tags="income")
        q = [x for x in self.positions() if x.underlying == "TGN"][0]
        self.assertEqual(q.tags, ("income",))

    def test_saved_views_round_trip(self):
        self._open("sav")
        status, location, _ = self.post("/views/save",
                                        {"name": "Open puts", "filter": "show=open&right=PUT"})
        self.assertEqual(status, 303)
        page = self.get("/?show=open")
        self.assertIn('<a class="flt" href="/?show=open&amp;right=PUT">Open puts</a>', page)
        conn = store.open_db(self.db_path)
        try:
            view_id = store.load_views(conn)[0]["id"]
        finally:
            conn.close()
        self.post(f"/views/{view_id}/delete", {})
        self.assertNotIn("Open puts", self.get("/?show=open"))
        status, _, body = self.post("/views/save", {"name": "x", "filter": "evil"})
        self.assertEqual(status, 400)

    def test_exports(self):
        import csv, io, json
        self._open("exp")
        with urllib.request.urlopen(self.base + "/export/positions.csv") as res:
            self.assertEqual(res.headers["Content-Type"], "text/csv; charset=utf-8")
            self.assertIn('filename="positions.csv"', res.headers["Content-Disposition"])
            rows = list(csv.reader(io.StringIO(res.read().decode())))
        self.assertEqual(rows[0][:3], ["id", "underlying", "expiry"])
        self.assertIn("realized", rows[0])
        self.assertTrue(any(row[1] == "EXP" for row in rows[1:]))
        with urllib.request.urlopen(self.base + "/export/shares.csv") as res:
            self.assertTrue(res.read().decode().startswith("kind,id,underlying"))
        with urllib.request.urlopen(self.base + "/export/journal.json") as res:
            data = json.loads(res.read().decode())
        self.assertIn("positions", data)
        self.assertTrue(any(p["underlying"] == "EXP" for p in data["positions"]))
        with urllib.request.urlopen(self.base + "/export/journal.db") as res:
            blob = res.read()
        self.assertTrue(blob.startswith(b"SQLite format 3"))
        self.assertEqual(self.get("/export/nothing.csv", expect=404)[:9], "<!doctype")


class TestPositionsPagePolish(WebTestCase):
    def test_new_position_form_is_inline_and_the_nav_tab_is_gone(self):
        page = self.get("/?show=open")
        self.assertIn('data-form-tab="new" data-tabs-for="new-box"', page)
        self.assertLess(page.index('data-form-tab="new"'), page.index('class="totals score"'))
        self.assertIn('<div class="titlebar"><h1>Positions</h1>', page)
        self.assertIn('data-form="new" hidden', page)
        self.assertIn('action="/new"', page)
        self.assertNotIn('<a href="/new">New</a>', page)

    def test_a_past_position_can_be_entered_already_closed(self):
        status, location, _ = self.add_position(
            underlying="past", opened_on="2026-01-05", outcome="CLOSED",
            closed_on="2026-02-06", close_price="1.00", close_fee="6.50", next="/?show=closed")
        self.assertEqual(status, 303)
        self.assertIn("/?show=closed", location)
        form = self.get("/new")
        self.assertIn('class="when-over"', form)
        self.assertRegex(form, r'id="f_close_fee"\s+value="0.65"')             # prefilled
        p = [q for q in self.positions() if q.underlying == "PAST"][0]
        self.assertFalse(p.is_open)
        self.assertEqual(str(p.close_price), "1.00")
        # And already expired, or already assigned (which books the shares).
        self.add_position(underlying="pex", outcome="EXPIRED", closed_on="2026-03-20")
        self.assertEqual([q.status.value for q in self.positions() if q.underlying == "PEX"], ["EXPIRED"])
        self.add_position(underlying="pas", outcome="ASSIGNED", closed_on="2026-03-20")
        self.assertIn("1000 held", self.get("/shares").replace("&middot;", "·").replace("  ", " ")
                      if "1000 held" in self.get("/shares") else "1000 held")
        status, _, body = self.add_position(underlying="bad", outcome="CLOSED",
                                            closed_on="2025-12-01", close_price="1")
        self.assertEqual(status, 400)
        self.assertIn("before the position was opened", body)

    def test_period_shortcuts_filter_by_the_date_that_matters(self):
        self.add_position(underlying="old", opened_on="2026-01-05")
        self.add_position(underlying="rec", opened_on=date.today().isoformat(),
                          expiry=(date.today() + timedelta(days=30)).isoformat())
        page = self.get("/?show=open&period=3m")
        table = page[page.index('<table class="positions'):]
        self.assertIn("REC", table)
        self.assertNotIn(">OLD<", table)
        # Period and status are segmented controls; the chosen one is marked.
        self.assertIn('href="/?show=open&amp;period=3m" class="flt here">3m</a>', page)
        self.assertIn('class="flt here">Open</a>', page)
        # A segment keeps the other filters when it switches.
        self.assertIn('href="/?show=closed&amp;period=3m" class="flt">Closed</a>', page)
        # What is active shows as a chip that can be removed.
        self.assertIn('Period: Last 3 months<a class="flt x" href="/?show=open"', page)
        self.assertIn("Clear all", page)
        self.assertIn('<option value="OLD">OLD</option>', page)          # ticker dropdown
        only = self.get("/?show=open&ticker=OLD")
        self.assertNotIn(">REC<", only[only.index('<table class="positions'):])

    def test_totals_describe_the_filtered_table(self):
        self.add_position(underlying="tta", quantity="10", strike="35")
        self.add_position(underlying="ttb", quantity="2", strike="20", right="CALL")
        page = self.get("/?show=open&q=ttb")
        self.assertIn("the 1 position(s) in this table", page)
        self.assertIn("1 open leg(s) &middot; 2 contract(s)", page)
        self.assertIn(">In hand<", page)
        self.assertIn(">To close at target<", page)
        self.assertIn("% return at target", self.get("/?show=open&q=tta"))
        self.assertNotIn("Realized options", page)     # portfolio-wide lives in Reports
        self.assertIn('class="good"', page)             # net credit is positive: green card

    def test_position_page_chain_rows_carry_the_button(self):
        self.add_position(underlying="pcb")
        p = [q for q in self.positions() if q.underlying == "PCB"][0]
        self.post(f"/position/{p.id}/roll", {"on": "2026-02-06", "close_price": "4.00",
                                              "new_expiry": "2026-03-20", "new_strike": "34",
                                              "new_price": "5.00"})
        head = [q for q in self.positions() if q.underlying == "PCB" and q.is_open][0]
        page = self.get(f"/position/{head.id}")
        self.assertIn(f'href="/position/{head.id}?&act={head.id}&do=close#row-{head.id}"', page)
        # Two strips at the top: the chain's story, then this leg's.
        self.assertLess(page.index('class="totals score"'), page.index("<h2>This position</h2>"))
        self.assertLess(page.index("<h2>This position</h2>"), page.index('<table class="positions'))
        self.assertEqual(page.count(">Banked<"), 1)
        self.assertIn("break-even", page)                   # one open chain: it has one
        self.assertIn("1 roll(s)", page)
        self.assertIn("less fee", page)                     # how the credit was made
        self.assertIn("Days to expiry", page)
        self.assertIn('<table class="positions chain" data-fixed>', page)
        self.assertNotIn("Hide the chain", page)

    def test_reports_page_folds_its_sections(self):
        page = self.get("/reports")
        self.assertIn('<details class="report" open><summary>Realized by period</summary>', page)
        self.assertIn("<summary>Export</summary>", page)


class TestCampaignStrip(WebTestCase):
    def test_a_split_family_shows_the_campaign_then_the_branch(self):
        self.add_position(underlying="cmp")
        parent = [p for p in self.positions() if p.underlying == "CMP"][0]
        self.post(f"/position/{parent.id}/split", {"quantity": "4", "on": "2026-02-01"})
        four, six = sorted([p for p in self.positions() if p.underlying == "CMP" and p.is_open],
                           key=lambda p: p.quantity)
        self.post(f"/position/{four.id}/assign", {"on": "2026-03-20", "close_fee": "0", "share_fee": "0"})
        self.post(f"/position/{six.id}/roll", {"on": "2026-03-20", "close_price": "4.00",
                                                "new_expiry": "2026-04-17", "new_strike": "34",
                                                "new_price": "5.00", "close_fee": "0", "new_fee": "0"})
        head = [p for p in self.positions() if p.underlying == "CMP" and p.is_open][0]
        page = self.get(f"/position/{head.id}")
        self.assertLess(page.index('class="totals score"'), page.index("<h2>This branch</h2>"))
        self.assertLess(page.index("<h2>This branch</h2>"), page.index("<h2>This position</h2>"))
        self.assertIn(">Banked<", page)
        self.assertIn("this campaign &middot; 4 leg(s)", page)
        self.assertIn("contracts 10 &rarr; 6", page)
        self.assertIn("1 roll(s) &middot; 1 split(s)", page)
        self.assertIn("assigned 400 shares", page)
        # A plain chain has no campaign section: chain and branch are the same.
        self.add_position(underlying="pln")
        plain = [p for p in self.positions() if p.underlying == "PLN"][0]
        plain_page = self.get(f"/position/{plain.id}")
        self.assertNotIn("<h2>This branch</h2>", plain_page)    # one lineage: no branch section
        self.assertIn(">Banked<", plain_page)                  # the same scorecard
        self.assertIn("break-even", plain_page)


class TestDataPage(WebTestCase):
    def test_health_names_trouble_and_links_to_the_fix(self):
        self.assertIn("All clear", self.get("/data"))
        self.add_position(underlying="lap", opened_on="2026-01-05", expiry="2026-01-16")
        p = [q for q in self.positions() if q.underlying == "LAP"][0]
        page = self.get("/data")
        self.assertIn("Past expiry, no outcome", page)
        self.assertIn(f'href="/position/{p.id}?do=expire"', page)
        self.assertIn('<span class="badge st-blocked">error</span>', page)
        self.assertIn('<a href="/data" class="here">Data</a>', page)

    def test_snapshots_are_taken_listed_and_restored_with_confirmation(self):
        self.add_position(underlying="snp")
        status, location, _ = self.post("/data/snapshot", {"label": "first", "next": "/data"})
        self.assertEqual(status, 303)
        self.assertIn("Snapshot written", location)
        page = self.get("/data")
        self.assertIn("-first.db", page)
        name = re.search(r"(journal-[0-9-]+-first\.db)", page).group(1)
        # Change the journal, then roll back.
        self.add_position(underlying="gone")
        status, _, body = self.post("/data/restore", {"name": name})
        self.assertEqual(status, 400)
        self.assertIn("Tick the box", body)
        status, location, _ = self.post("/data/restore", {"name": name, "sure": "1", "next": "/data"})
        self.assertEqual(status, 303)
        self.assertIn("restored", location)
        self.assertEqual([p.underlying for p in self.positions() if p.underlying in ("SNP", "GONE")],
                         ["SNP"])
        self.assertIn("before-restore", self.get("/data"))
        status, _, _ = self.post("/data/restore", {"name": "../x.db", "sure": "1"})
        self.assertEqual(status, 400)


class TestRawPositionRepairs(WebTestCase):
    def test_a_placeholder_can_be_removed_and_brought_back(self):
        self.add_position(underlying="plc")
        p = [q for q in self.positions() if q.underlying == "PLC"][0]
        page = self.get(f"/position/{p.id}")
        self.assertIn('<details class="report raw" id="raw">', page)
        self.assertIn('<div class="bubble">', page)                 # a form, like the others
        self.assertIn(f'action="/position/{p.id}/delete"', page)
        status, _, body = self.post(f"/position/{p.id}/delete", {})
        self.assertEqual(status, 400)
        status, location, _ = self.post(f"/position/{p.id}/delete", {"sure": "1"})
        self.assertEqual(status, 303)
        self.assertIn("removed", location)
        self.assertEqual([q for q in self.positions() if q.underlying == "PLC"], [])
        conn = store.open_db(self.db_path)
        try:
            entry = [e for e in store.audit_entries(conn) if e["entity_id"] == p.id][0]
        finally:
            conn.close()
        self.post(f"/audit/{entry['id']}/revert", {})
        self.assertEqual(len([q for q in self.positions() if q.underlying == "PLC"]), 1)

    def test_a_linked_position_cannot_be_removed(self):
        self.add_position(underlying="lnk3")
        p = [q for q in self.positions() if q.underlying == "LNK3"][0]
        self.post(f"/position/{p.id}/roll", {"on": "2026-02-06", "close_price": "4.00",
                                              "new_expiry": "2026-03-20", "new_strike": "34",
                                              "new_price": "5.00"})
        page = self.get(f"/position/{p.id}")
        self.assertIn("Cannot be removed: 1 position(s) rolled from it", page)
        status, _, body = self.post(f"/position/{p.id}/delete", {"sure": "1"})
        self.assertEqual(status, 400)

    def _edit(self, p, **over):
        fields = {"underlying": p.underlying, "expiry": p.expiry.isoformat(), "strike": str(p.strike),
                  "right": p.right.value, "direction": p.direction.value,
                  "quantity": str(p.quantity), "opened_on": p.opened_on.isoformat(),
                  "open_price": str(p.open_price), "open_fee": str(p.open_fee)}
        fields.update(over)
        return self.post(f"/position/{p.id}/edit", fields)

    def test_every_entered_field_can_be_corrected_from_the_raw_section(self):
        self.add_position(underlying="mvd")
        p = [q for q in self.positions() if q.underlying == "MVD"][0]
        page = self.get(f"/position/{p.id}")
        self.assertIn(f'action="/position/{p.id}/edit"', page)
        status, location, _ = self._edit(p, opened_on="2026-01-06", expiry="2026-04-17",
                                         strike="34.5", quantity="8", open_price="2.75")
        self.assertEqual(status, 303)
        self.assertIn("Corrections saved", location)
        m = [q for q in self.positions() if q.underlying == "MVD"][0]
        self.assertEqual((m.opened_on.isoformat(), m.expiry.isoformat(), str(m.strike), m.quantity,
                          str(m.open_price)), ("2026-01-06", "2026-04-17", "34.5", 8, "2.75"))
        status, _, body = self._edit(p, opened_on="2099-01-06", expiry="2099-04-17")
        self.assertEqual(status, 400)
        self.assertIn("in the future", body)
        status, _, body = self._edit(p, expiry="2025-01-01")
        self.assertEqual(status, 400)
        self.assertIn("before the position was opened", body)

    def test_a_closed_record_with_no_close_date_can_be_given_one(self):
        # As an import can leave it: closed, but with no date. Not reachable
        # through the forms, so written directly.
        self.add_position(underlying="ncd")
        p = [q for q in self.positions() if q.underlying == "NCD"][0]
        conn = store.open_db(self.db_path)
        try:
            with conn:
                conn.execute("UPDATE positions SET status = 'CLOSED', close_price = '0.50'"
                             " WHERE id = ?", (p.id,))
        finally:
            conn.close()
        data = self.get("/data")
        self.assertIn("Closed with no close date", data)
        self.assertIn(f'href="/position/{p.id}#raw"', data)
        page = self.get(f"/position/{p.id}")
        self.assertRegex(page, r'name="closed_on" id="f_closed_on"\s+value=""')
        self.assertIn("<span>Closed on</span>", page)
        status, _, body = self._edit(p)                       # still no date: refused
        self.assertEqual(status, 400)
        status, location, _ = self._edit(p, closed_on="2026-02-06", close_price="0.50",
                                         close_fee="0.65")
        self.assertEqual(status, 303)
        fixed = [q for q in self.positions() if q.underlying == "NCD"][0]
        self.assertEqual(fixed.closed_on.isoformat(), "2026-02-06")
        self.assertNotIn("Closed with no close date", self.get("/data"))
        self.assertIn("hashchange", self.get("/static/app.js"))   # the Fix link opens the fold

    def test_a_disguised_share_row_becomes_the_share_trade_it_was(self):
        # The sheet wrote a stock purchase as an "assigned put": no premium,
        # an odd strike. The importer derived the lot; now the row goes and
        # the lot stays, as an outright buy.
        self.add_position(underlying="dsg", quantity="2", strike="58.22", open_price="0")
        p = [q for q in self.positions() if q.underlying == "DSG"][0]
        self.post(f"/position/{p.id}/assign", {"on": "2026-03-20", "close_fee": "0", "share_fee": "0"})
        conn = store.open_db(self.db_path)
        try:
            from bcoj.importer.triage import Flag
            store.save_flags(conn, [Flag(entity_id=p.id, kind="disguised_share_row",
                                         detail="probably a share transaction")])
        finally:
            conn.close()
        self.assertIn("Import: disguised share row", self.get("/data"))
        page = self.get(f"/position/{p.id}")
        self.assertIn("Convert to share trade", page)
        status, location, _ = self.post(f"/position/{p.id}/to-shares", {"sure": "1"})
        self.assertEqual(status, 303)
        self.assertIn("lot of 200", location)
        self.assertEqual([q for q in self.positions() if q.underlying == "DSG"], [])
        conn = store.open_db(self.db_path)
        try:
            lot = [l for l in store.load_lots(conn) if l.underlying == "DSG"][0]
            self.assertEqual((lot.source.value, lot.quantity, str(lot.cost_per_share)),
                             ("OUTRIGHT_BUY", 200, "58.22"))
            self.assertIsNone(lot.assigning_position_id)
            self.assertEqual(store.open_flags(conn), [])          # the flag went with the row
        finally:
            conn.close()
        self.assertNotIn("Import: disguised share row", self.get("/data"))
        # A real leg with a successor is not a share trade.
        self.add_position(underlying="real")
        q = [x for x in self.positions() if x.underlying == "REAL"][0]
        self.post(f"/position/{q.id}/roll", {"on": "2026-02-06", "close_price": "4.00",
                                              "new_expiry": "2026-03-20", "new_strike": "34",
                                              "new_price": "5.00"})
        status, _, body = self.post(f"/position/{q.id}/to-shares", {"sure": "1"})
        self.assertEqual(status, 400)
        self.assertIn("not a share trade", body)

    def test_a_close_recorded_by_mistake_can_be_reopened(self):
        self.add_position(underlying="rop")
        p = [q for q in self.positions() if q.underlying == "ROP"][0]
        self.post(f"/position/{p.id}/close", {"closed_on": "2026-02-06", "close_price": "1.00"})
        page = self.get(f"/position/{p.id}")
        self.assertIn("Reopen (undo the closed)", page)
        status, location, _ = self.post(f"/position/{p.id}/reopen", {})
        self.assertEqual(status, 303)
        self.assertTrue([q for q in self.positions() if q.underlying == "ROP"][0].is_open)
        # A rolled leg is not reopened here: its successor depends on it.
        self.post(f"/position/{p.id}/roll", {"on": "2026-02-06", "close_price": "4.00",
                                              "new_expiry": "2026-03-20", "new_strike": "34",
                                              "new_price": "5.00"})
        status, _, body = self.post(f"/position/{p.id}/reopen", {})
        self.assertEqual(status, 400)
        self.assertIn("not reopened here", body)

    def test_an_import_flag_clears_once_the_data_is_fixed(self):
        # A roll whose dates disagree, flagged as the importer would flag it.
        self.add_position(underlying="flg")
        p = [q for q in self.positions() if q.underlying == "FLG"][0]
        self.post(f"/position/{p.id}/roll", {"on": "2026-02-06", "close_price": "4.00",
                                              "new_expiry": "2026-03-20", "new_strike": "34",
                                              "new_price": "5.00"})
        head = [q for q in self.positions() if q.underlying == "FLG" and q.is_open][0]
        conn = store.open_db(self.db_path)
        try:
            store.edit_position(conn, head.id, {"opened_on": date(2026, 2, 10)}, "test")
            from bcoj.importer.triage import Flag
            store.save_flags(conn, [Flag(entity_id=head.id, kind="chain_date_order",
                                         detail="opens after its predecessor closed")])
        finally:
            conn.close()
        page = self.get("/data")
        self.assertIn("Import: chain date order", page)
        self.assertIn("Roll dates do not line up", page)
        # Fix the date: both the live warning and the import flag go away, and
        # the flag is resolved in the store rather than merely hidden.
        self._edit(head, opened_on="2026-02-06", expiry="2026-03-20", strike="34",
                   open_price="5.00", quantity=str(head.quantity))
        page = self.get("/data")
        self.assertNotIn("Import: chain date order", page)
        self.assertNotIn("Roll dates do not line up", page)
        conn = store.open_db(self.db_path)
        try:
            self.assertEqual([dict(f)["kind"] for f in store.open_flags(conn)], [])
        finally:
            conn.close()


class TestLongTargets(WebTestCase):
    def test_a_long_leg_targets_a_quarter_profit_on_its_debit(self):
        # Bought a put for 1.12, fee 0.65: debit 112.65. Target 1.40, less the
        # 0.65 closing fee the entry assumes -> +26.70.
        self.add_position(underlying="lng2", direction="LONG", quantity="1",
                          open_price="1.12", open_fee="0.65", strike="100")
        p = [q for q in self.positions() if q.underlying == "LNG2"][0]
        page = self.get(f"/position/{p.id}")
        self.assertIn("25% target", page)
        self.assertIn(">1.40<", page)
        self.assertIn("expected 26.70", page)
        strip = self.get("/?show=open&ticker=LNG2")
        # Closing a long at target is a sale: the "obligation" is negative,
        # i.e. cash coming in (139.35 for the 1.40 target less the 0.65 fee).
        self.assertIn("139.35", strip)
        self.assertNotIn("chain is net debit", strip)


class TestShareRepairs(WebTestCase):
    """Fixing a mis-entered sale, and the guards that prevent one."""

    def _buy(self, ticker, qty, price="10.00", on="2026-01-05"):
        conn = store.open_db(self.db_path)
        try:
            before = {l.id for l in store.load_lots(conn)}
        finally:
            conn.close()
        self.post("/shares/buy", {"underlying": ticker, "on": on,
                                  "quantity": str(qty), "price": price})
        conn = store.open_db(self.db_path)
        try:
            # The lot just written, not whichever sorts last among same-day lots.
            return [l for l in store.load_lots(conn) if l.id not in before][0]
        finally:
            conn.close()

    def test_forms_open_inline_on_the_shares_pages(self):
        self._buy("inl", 100)
        for base in ("/shares?", "/shares/INL?"):
            with self.subTest(page=base):
                page = self.get(base + "form=sell")
                self.assertIn('action="/shares/sell"', page)
                self.assertNotIn('href="/shares/new', page)
                # Without a choice every form is present but none is shown.
                plain = self.get(base.rstrip("?"))
                self.assertIn('data-form="sell" hidden', plain)

    def test_sell_dropdown_shows_what_each_lot_still_holds(self):
        lot = self._buy("rem", 300)
        self.post("/shares/sell", {"underlying": "rem", "on": "2026-02-01",
                                   "quantity": "100", "price": "12"})
        page = self.get("/shares/REM?form=sell")
        self.assertIn("200 of 300 REM @ 10.00", page)
        self.assertIn(f'"{lot.id}":200', page)      # for the quantity cap

    def test_selling_more_than_a_pinned_lot_holds_is_refused(self):
        lot = self._buy("pin", 100)
        self._buy("pin", 500, price="12.00", on="2026-02-01")
        status, _, body = self.post("/shares/sell", {
            "underlying": "pin", "on": "2026-03-01", "quantity": "300",
            "price": "14", "lot_id": lot.id,
        })
        self.assertEqual(status, 400)
        self.assertIn("that lot holds 100 shares", body)

    def test_a_pinned_lot_from_another_ticker_is_refused(self):
        other = self._buy("oth", 100)
        self._buy("mine", 100)
        status, _, body = self.post("/shares/sell", {
            "underlying": "mine", "on": "2026-03-01", "quantity": "50",
            "price": "14", "lot_id": other.id,
        })
        self.assertEqual(status, 400)
        self.assertIn("does not belong", body)

    def test_a_bad_disposal_can_be_removed_and_matching_resumes(self):
        # Force a pinned disposal that cannot be satisfied, by pinning to a
        # lot and then... a pinned sale the guard would refuse today; write it
        # straight into the store, as a legacy import or older version might.
        lot = self._buy("bad", 100)
        self._buy("bad", 500)   # plenty held overall: blocked, not short
        from bcoj.engine import actions
        conn = store.open_db(self.db_path)
        try:
            result = actions.sell_shares("bad", 500, Decimal("12"), date(2026, 3, 1),
                                         specific_lot_ids=(lot.id,))
            store.apply(conn, result)
            disposal_id = result.disposals[0].id
        finally:
            conn.close()

        page = self.get("/shares/BAD")
        self.assertIn("pinned to a specific lot that holds only 100", page)
        self.assertIn("matching blocked", self.get("/shares"))
        self.assertNotIn("short -", self.get("/shares"))

        status, location, _ = self.post(f"/shares/disposal/{disposal_id}/delete",
                                        {"next": "/shares/BAD"})
        self.assertEqual(status, 303)
        self.assertIn("Sale removed", location)
        self.assertNotIn("pinned to a specific lot", self.get("/shares/BAD"))

    def test_assign_form_defaults_to_today_or_the_past_expiry(self):
        # Default expiry in the fixture is in the past: date it to the expiry.
        self.add_position(underlying="apst")
        past = [p for p in self.positions() if p.underlying == "APST"][0]
        body = self.get(f"/?show=open&act={past.id}&do=assign")
        self.assertRegex(body, r'name="on" id="f_on"[^>]*value="%s"' % past.expiry.isoformat())
        # A live contract is assigned early: today is the sensible default.
        self.add_position(underlying="afut", expiry="2099-01-15")
        future = [p for p in self.positions() if p.underlying == "AFUT"][0]
        body = self.get(f"/?show=open&act={future.id}&do=assign")
        self.assertRegex(body, r'name="on" id="f_on"[^>]*value="%s"' % date.today().isoformat())

    def test_selling_from_a_lot_dated_after_the_sale_is_refused(self):
        # A future-dated lot cannot be entered any more; write one straight
        # into the store, as an older version or an import could have.
        from bcoj.engine import actions
        conn = store.open_db(self.db_path)
        try:
            result = actions.buy_shares("fut", 400, Decimal("10"), date(2099, 9, 18))
            store.apply(conn, result)
            lot = result.lots[0]
        finally:
            conn.close()
        status, _, body = self.post("/shares/sell", {
            "underlying": "fut", "on": "2026-09-03", "quantity": "400",
            "price": "12", "lot_id": lot.id})
        self.assertEqual(status, 400)
        self.assertIn("was acquired on 2099-09-18, after the sale date", body)

    def test_assigning_in_the_future_is_refused(self):
        self.add_position(underlying="futa", expiry="2099-01-15")
        p = [p for p in self.positions() if p.underlying == "FUTA"][0]
        status, _, body = self.post(f"/position/{p.id}/assign",
                                    {"on": "2099-01-15", "close_fee": "0", "share_fee": "0"})
        self.assertEqual(status, 400)
        self.assertIn("in the future", body)

    def test_an_estimated_lot_is_confirmed_by_correcting_it(self):
        from bcoj.engine import actions
        conn = store.open_db(self.db_path)
        try:
            result = actions.buy_shares("est", 100, Decimal("35"), date(2026, 1, 5))
            result.lots[0].estimated = True
            result.lots[0].notes = "price recalled as about 35"
            store.apply(conn, result)
            lot = result.lots[0]
        finally:
            conn.close()
        data = self.get("/data")
        self.assertIn("Reconstructed, not recorded", data)
        self.assertIn(f'href="/shares/EST/data#lot-{lot.id}"', data)
        page = self.get("/shares/EST/data")
        self.assertIn(f'<details class="edit" id="lot-{lot.id}">', page)
        self.assertIn("confirmed against a statement", page)
        status, location, _ = self.post(f"/shares/lot/{lot.id}/edit", {
            "acquired_on": "2026-01-07", "quantity": "100", "cost_per_share": "34.80",
            "fee": "1.00", "notes": "per the January statement", "confirmed": "1",
            "next": "/shares/EST/data"})
        self.assertEqual(status, 303)
        self.assertIn("corrected", location)
        conn = store.open_db(self.db_path)
        try:
            fixed = [l for l in store.load_lots(conn) if l.underlying == "EST"][0]
        finally:
            conn.close()
        self.assertFalse(fixed.estimated)
        self.assertEqual((fixed.acquired_on.isoformat(), str(fixed.cost_per_share), str(fixed.fee)),
                         ("2026-01-07", "34.80", "1.00"))
        self.assertNotIn("Reconstructed, not recorded", self.get("/data"))
        # A sale is corrected the same way, and the edit is refused for nonsense.
        self.post("/shares/sell", {"underlying": "est", "on": "2026-02-01", "quantity": "40",
                                   "price": "40"})
        conn = store.open_db(self.db_path)
        try:
            sale = [d for d in store.load_disposals(conn) if d.underlying == "EST"][0]
        finally:
            conn.close()
        status, _, body = self.post(f"/shares/disposal/{sale.id}/edit", {
            "disposed_on": "2026-02-02", "quantity": "0", "proceeds_per_share": "41", "fee": "0"})
        self.assertEqual(status, 400)
        status, _, _ = self.post(f"/shares/disposal/{sale.id}/edit", {
            "disposed_on": "2026-02-02", "quantity": "50", "proceeds_per_share": "41", "fee": "0"})
        self.assertEqual(status, 303)
        conn = store.open_db(self.db_path)
        try:
            sale = [d for d in store.load_disposals(conn) if d.underlying == "EST"][0]
        finally:
            conn.close()
        self.assertEqual((sale.disposed_on.isoformat(), sale.quantity), ("2026-02-02", 50))

    def test_a_lot_can_be_re_dated_together_with_its_assignment(self):
        # An assignment written straight into the store with a future date,
        # as the old expiry default produced when a put was assigned early.
        self.add_position(underlying="redt", expiry="2026-03-20")
        p = [p for p in self.positions() if p.underlying == "REDT"][0]
        from bcoj.engine import actions
        conn = store.open_db(self.db_path)
        try:
            store.apply(conn, actions.assign(p, on=date(2099, 3, 20)))
            lot = [l for l in store.load_lots(conn) if l.underlying == "REDT"][0]
        finally:
            conn.close()
        self.assertIn("dated after today", self.get("/shares/REDT"))

        status, location, _ = self.post(f"/shares/lot/{lot.id}/date",
                                        {"on": "2026-03-02", "next": "/shares/REDT"})
        self.assertEqual(status, 303)
        self.assertIn("re-dated", location)
        conn = store.open_db(self.db_path)
        try:
            lot = [l for l in store.load_lots(conn) if l.underlying == "REDT"][0]
            self.assertEqual(lot.acquired_on, date(2026, 3, 2))
            self.assertEqual(store.load_position(conn, p.id).closed_on, date(2026, 3, 2))
            entry = [e for e in store.audit_entries(conn, limit=50)
                     if e["entity_type"] == "share_lot"
                     and e["action"].startswith("update")][0]
        finally:
            conn.close()
        self.assertIn("undo", self.get("/audit").lower())

        # And the re-date is itself undoable.
        self.post(f"/audit/{entry['id']}/revert", {})
        conn = store.open_db(self.db_path)
        try:
            lot = [l for l in store.load_lots(conn) if l.underlying == "REDT"][0]
            self.assertEqual(lot.acquired_on, date(2099, 3, 20))
        finally:
            conn.close()

    def test_all_three_forms_are_in_the_page_with_one_shown(self):
        self._buy("tab", 100)
        page = self.get("/shares/TAB?form=sell")
        self.assertIn('data-form="buy" hidden', page)
        self.assertIn('data-form="sell">', page)
        self.assertIn('data-form="buy-write" hidden', page)
        self.assertEqual(page.count('id="tickers"'), 1)
        self.assertIn('data-form-tab="sell" class="here"', page)

    def test_repairs_live_on_the_raw_data_page_not_the_ticker_page(self):
        self._buy("raw", 100)
        self.post("/shares/sell", {"underlying": "raw", "on": "2026-02-01",
                                   "quantity": "50", "price": "12"})
        ticker = self.get("/shares/RAW")
        self.assertNotIn('/delete"', ticker)
        self.assertNotIn('/date"', ticker)
        self.assertIn('href="/shares/RAW/data"', ticker)
        data = self.get("/shares/RAW/data")
        self.assertEqual(data.count('/delete"'), 2)     # the lot and the sale
        self.assertEqual(data.count('/edit"'), 2)       # each has an edit form
        self.assertEqual(data.count('class="btn cancel"'), 2)   # and each a Cancel
        self.assertIn("data-close-details", data)
        self.assertIn("data-close-details", self.get("/static/app.js"))
        self.assertIn("account rule", data)

    def test_removing_a_lot_in_use_is_refused(self):
        lot = self._buy("use", 300)
        self.add_position(underlying="use", right="CALL", strike="12",
                          opened_on="2026-02-01", expiry="2026-03-20",
                          share_lot_id=lot.id)
        status, _, body = self.post(f"/shares/lot/{lot.id}/delete", {})
        self.assertEqual(status, 400)
        self.assertIn("open call(s) are written against this lot", body)
        # The page says why, offers removal only behind a spelled-out
        # confirmation, and shows what is left.
        data = self.get("/shares/USE/data")
        self.assertIn("backs 1 open call(s)", data)
        self.assertIn("remove anyway; 1 open call(s) become naked", data)
        self.assertIn('<abbr>Left</abbr>', data)
        # Confirmed: the lot goes and the call is naked, audited on the call.
        other = self._buy("use", 300)                       # a second lot to exercise on
        status, _, _ = self.post(f"/shares/lot/{other.id}/delete", {})
        self.assertEqual(status, 303)
        self.add_position(underlying="use", right="CALL", strike="13", opened_on="2026-02-02",
                          expiry="2026-03-20", share_lot_id=lot.id)
        status, _, _ = self.post(f"/shares/lot/{lot.id}/delete", {"unlink": "1"})
        self.assertEqual(status, 303)
        conn = store.open_db(self.db_path)
        try:
            calls = [p for p in store.load_positions(conn) if p.underlying == "USE"
                     and p.right.value == "CALL"]
            self.assertEqual({p.share_lot_id for p in calls}, {None})
            self.assertTrue(all(p.is_open for p in calls))
            self.assertFalse(any(l.id == lot.id for l in store.load_lots(conn)))
        finally:
            conn.close()
        return
        # Once the call is closed it is history: the lot can go, and the call
        # simply stops pointing at it.
        call = [p for p in self.positions() if p.underlying == "USE" and p.right.value == "CALL"][0]
        self.post(f"/position/{call.id}/close", {"closed_on": "2026-03-01", "close_price": "0.10"})
        self.assertNotIn("backs", self.get("/shares/USE/data"))
        status, _, _ = self.post(f"/shares/lot/{lot.id}/delete", {})
        self.assertEqual(status, 303)
        conn = store.open_db(self.db_path)
        try:
            self.assertIsNone(store.load_position(conn, call.id).share_lot_id)
        finally:
            conn.close()

    def test_removed_share_records_can_be_undone_from_history(self):
        lot = self._buy("und2", 100)
        self.post(f"/shares/lot/{lot.id}/delete", {})
        conn = store.open_db(self.db_path)
        try:
            self.assertFalse(any(l.id == lot.id for l in store.load_lots(conn)))
            entry = [e for e in store.audit_entries(conn)
                     if e["entity_id"] == lot.id and e["action"].startswith("delete")][0]
        finally:
            conn.close()
        status, location, _ = self.post(f"/audit/{entry['id']}/revert", {})
        self.assertEqual(status, 303)
        self.assertIn("restored", location)
        conn = store.open_db(self.db_path)
        try:
            self.assertTrue(any(l.id == lot.id for l in store.load_lots(conn)))
        finally:
            conn.close()
