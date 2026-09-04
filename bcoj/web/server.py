"""The web app: stdlib ``http.server``, a small router, no dependencies.

For a single user on localhost there is nothing for a framework to do here.
There is no concurrency to manage, no schema to publish, and validation is
domain logic that already exists. What that buys is a real deployment story:
``python3 -m bcoj.web`` and it runs, on any machine with Python.

Two protections that a localhost app genuinely needs:

* **CSRF tokens.** "Localhost" is not a security boundary -- any page in the
  browser can POST to it. Every form carries a per-process token.
* **Loopback binding**, with an optional password. Not a service.
"""

import gzip
import os
import re
import secrets
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ..db import store
from ..engine.actions import ActionError
from . import routes
from . import render as r
from .static import STATIC

MAX_BODY = 256 * 1024  # a form; anything larger is a mistake or an attack


class App:
    """Routing table and per-process state."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.csrf = secrets.token_urlsafe(32)
        self.password = os.environ.get("BCOJ_PASSWORD") or None
        self._routes = [
            ("GET", r"^/$", self._positions),
            ("GET", r"^/new$", self._new_form),
            ("POST", r"^/new$", self._new_submit),
            ("GET", r"^/expiring$", self._expiring),
            ("GET", r"^/risk$", self._risk),
            ("GET", r"^/reports$", self._reports),
            ("GET", r"^/data$", self._data),
            ("POST", r"^/data/snapshot$", self._snapshot),
            ("POST", r"^/data/restore$", self._restore),
            ("POST", r"^/data/flag/(\d+)/resolve$", self._resolve_flag),
            ("GET", r"^/export/(positions\.csv|shares\.csv|journal\.json|journal\.db)$", self._export),
            ("POST", r"^/views/save$", self._save_view),
            ("POST", r"^/views/([0-9a-zA-Z_-]+)/delete$", self._delete_view),
            ("POST", r"^/position/([0-9a-zA-Z_-]+)/notes$", self._notes),
            ("POST", r"^/position/([0-9a-zA-Z_-]+)/edit$", self._edit),
            ("POST", r"^/position/([0-9a-zA-Z_-]+)/reopen$", self._reopen),
            ("POST", r"^/position/([0-9a-zA-Z_-]+)/to-shares$", self._to_shares),
            ("POST", r"^/position/([0-9a-zA-Z_-]+)/delete$", self._delete_position),
            ("GET", r"^/position/([0-9a-zA-Z_-]+)/roll-preview$", self._roll_preview),
            ("GET", r"^/audit$", self._audit),
            ("POST", r"^/audit/(\d+)/revert$", self._revert),
            ("GET", r"^/position/([0-9a-zA-Z_-]+)$", self._position),
            ("POST", r"^/position/([0-9a-zA-Z_-]+)/close$", self._close),
            ("POST", r"^/position/([0-9a-zA-Z_-]+)/expire$", self._expire),
            ("POST", r"^/position/([0-9a-zA-Z_-]+)/assign$", self._assign),
            ("POST", r"^/position/([0-9a-zA-Z_-]+)/roll$", self._roll),
            ("POST", r"^/position/([0-9a-zA-Z_-]+)/split$", self._split),
            ("POST", r"^/position/([0-9a-zA-Z_-]+)/cover$", self._cover),
            ("GET", r"^/shares$", self._shares),
            ("GET", r"^/shares/new$", self._share_form),
            ("GET", r"^/shares/covers$", self._covers),
            ("POST", r"^/shares/covers/apply$", self._cover_all),
            ("POST", r"^/shares/buy$", self._buy_shares),
            ("POST", r"^/shares/sell$", self._sell_shares),
            ("POST", r"^/shares/buy-write$", self._buy_write),
            ("POST", r"^/shares/disposal/([0-9a-zA-Z_:-]+)/delete$", self._delete_disposal),
            ("POST", r"^/shares/lot/([0-9a-zA-Z_:-]+)/delete$", self._delete_lot),
            ("POST", r"^/shares/lot/([0-9a-zA-Z_:-]+)/date$", self._redate_lot),
            ("POST", r"^/shares/lot/([0-9a-zA-Z_:-]+)/edit$", self._edit_lot),
            ("POST", r"^/shares/disposal/([0-9a-zA-Z_:-]+)/edit$", self._edit_disposal),
            ("GET", r"^/shares/([A-Za-z0-9.\-]+)$", self._ticker),
            ("GET", r"^/shares/([A-Za-z0-9.\-]+)/data$", self._ticker_data),
        ]
        self._compiled = [
            (method, re.compile(pattern), handler)
            for method, pattern, handler in self._routes
        ]

    def connect(self):
        """A connection per request. sqlite3 objects are not thread-safe, and
        a personal app has no need to pool anything."""
        return store.open_db(self.db_path)

    def match(self, method: str, path: str):
        for want, pattern, handler in self._compiled:
            found = pattern.match(path)
            if found:
                if want != method:
                    continue
                return handler, found.groups()
        return None, ()

    # --- handlers -------------------------------------------------------

    def _positions(self, conn, query, form, args):
        return routes.positions_page(conn, query, self.csrf)

    def _new_form(self, conn, query, form, args):
        return routes.new_position_form(conn, self.csrf)

    def _new_submit(self, conn, query, form, args):
        try:
            routes.create_position(conn, form, self.csrf)
        except routes.Invalid as invalid:
            return routes.new_position_form(
                conn, self.csrf, invalid.form, invalid.problems
            )
        return 200, ""

    def _expiring(self, conn, query, form, args):
        return routes.expiring_page(conn, query)

    def _audit(self, conn, query, form, args):
        return routes.audit_page(conn, self.csrf, query)

    def _revert(self, conn, query, form, args):
        routes.do_revert(conn, args[0], form)
        return 200, ""

    def _position(self, conn, query, form, args):
        return routes.position_page(conn, args[0], self.csrf, query)

    def _close(self, conn, query, form, args):
        routes.do_close(conn, args[0], form)
        return 200, ""

    def _expire(self, conn, query, form, args):
        routes.do_expire(conn, args[0], form)
        return 200, ""

    def _assign(self, conn, query, form, args):
        routes.do_assign(conn, args[0], form)
        return 200, ""

    def _roll(self, conn, query, form, args):
        routes.do_roll(conn, args[0], form)
        return 200, ""

    def _split(self, conn, query, form, args):
        routes.do_split(conn, args[0], form)
        return 200, ""

    def _cover(self, conn, query, form, args):
        routes.do_cover(conn, args[0], form)
        return 200, ""

    def _shares(self, conn, query, form, args):
        return routes.shares_page(conn, self.csrf, query)

    def _ticker(self, conn, query, form, args):
        return routes.ticker_page(conn, args[0], self.csrf, query)

    def _risk(self, conn, query, form, args):
        return routes.risk_page(conn, query)

    def _reports(self, conn, query, form, args):
        return routes.reports_page(conn, query)

    def _data(self, conn, query, form, args):
        return routes.data_page(conn, self.db_path, self.csrf, query)

    def _snapshot(self, conn, query, form, args):
        routes.do_snapshot(conn, self.db_path, form)

    def _restore(self, conn, query, form, args):
        routes.do_restore(conn, self.db_path, form)

    def _resolve_flag(self, conn, query, form, args):
        routes.do_resolve_flag(conn, args[0], form)

    def _export(self, conn, query, form, args):
        return routes.export_file(conn, args[0])

    def _save_view(self, conn, query, form, args):
        routes.do_save_view(conn, form)

    def _delete_view(self, conn, query, form, args):
        routes.do_delete_view(conn, args[0], form)

    def _notes(self, conn, query, form, args):
        routes.do_notes(conn, args[0], form)

    def _edit(self, conn, query, form, args):
        routes.do_edit_position(conn, args[0], form)

    def _reopen(self, conn, query, form, args):
        routes.do_reopen_position(conn, args[0], form)

    def _to_shares(self, conn, query, form, args):
        routes.do_convert_position(conn, args[0], form)

    def _delete_position(self, conn, query, form, args):
        routes.do_delete_position(conn, args[0], form)

    def _roll_preview(self, conn, query, form, args):
        return routes.roll_preview_fragment(conn, args[0], query)

    def _ticker_data(self, conn, query, form, args):
        return routes.ticker_data_page(conn, args[0], self.csrf, query)

    def _share_form(self, conn, query, form, args):
        return routes.share_form(conn, self.csrf, query)

    def _covers(self, conn, query, form, args):
        return routes.covers_page(conn, self.csrf, query)

    def _cover_all(self, conn, query, form, args):
        routes.do_cover_all(conn, form)
        return 200, ""

    def _buy_shares(self, conn, query, form, args):
        routes.do_buy_shares(conn, form)
        return 200, ""

    def _sell_shares(self, conn, query, form, args):
        routes.do_sell_shares(conn, form)
        return 200, ""

    def _buy_write(self, conn, query, form, args):
        routes.do_buy_write(conn, form)
        return 200, ""

    def _delete_disposal(self, conn, query, form, args):
        routes.do_delete_disposal(conn, args[0], form)
        return 200, ""

    def _delete_lot(self, conn, query, form, args):
        routes.do_delete_lot(conn, args[0], form)

    def _redate_lot(self, conn, query, form, args):
        routes.do_redate_lot(conn, args[0], form)

    def _edit_lot(self, conn, query, form, args):
        routes.do_edit_lot(conn, args[0], form)

    def _edit_disposal(self, conn, query, form, args):
        routes.do_edit_disposal(conn, args[0], form)
        return 200, ""


class Handler(BaseHTTPRequestHandler):
    # HTTP/1.1 so the browser keeps a few connections open and reuses them.
    # Under HTTP/1.0 every fetch opened a new connection, and a hover-plus-
    # prefetch burst overflowed the listen backlog: the kernel dropped the
    # extra connections and the browser retried them a full second later.
    # Every response sets Content-Length, which keep-alive requires.
    protocol_version = "HTTP/1.1"

    def parse_request(self):
        # A kept-alive connection through a port forwarder has been seen to
        # deliver a run of NUL bytes ahead of the next request line. The
        # request behind them is fine; drop the padding rather than answer
        # 501 to a method called "\x00\x00GET".
        stripped = self.raw_requestline.lstrip(b"\x00\r\n")
        if stripped != self.raw_requestline:
            self.raw_requestline = stripped
            if not stripped:
                self.close_connection = True
                return False
        return super().parse_request()

    server_version = "bcoj"
    sys_version = ""
    app: App = None  # set by serve()

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        # One tidy line per request; the default is noisy and leaks nothing
        # useful for a local app. The stdlib also logs here when a kept-alive
        # connection delivers something that is not a request line -- before
        # any path exists -- so never assume one.
        command = getattr(self, "command", None) or "-"
        path = getattr(self, "path", None) or "-"
        if format == '"%s" %s %s' and len(args) >= 2:   # the stdlib's per-request line
            print(f"  {command} {path} -> {args[1]}")
        else:
            print(f"  {command} {path} -> " + (format % args if args else format))

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    # --- plumbing -------------------------------------------------------

    def _handle(self, method: str):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if not self._authorized():
            return

        if path.startswith("/static/"):
            return self._serve_static(path)

        handler, args = self.app.match(method, path)
        if handler is None:
            return self._send(404, r.page("Not found", "<p>No such page.</p>"))

        form = {}
        if method == "POST":
            form = self._read_form()
            if form is None:
                return
            if not secrets.compare_digest(
                (form.get("csrf") or [""])[0], self.app.csrf
            ):
                return self._send(
                    403,
                    r.page("Refused", "<p>This form is stale. Reload the page "
                                      "and try again.</p>"),
                )

        conn = self.app.connect()
        try:
            result = handler(conn, query, form, args)
            status, body = result[0], result[1]
            # A download says its own type and file name; a page does not.
            if len(result) > 2:
                content_type, filename = result[2], result[3]
                payload = body if isinstance(body, bytes) else body.encode()
                return self._write(status, payload, content_type,
                                   [("Content-Disposition", f'attachment; filename="{filename}"')])
        except routes.Redirect as redirect:
            location = redirect.location
            if redirect.flash:
                joiner = "&" if "?" in location else "?"
                location += joiner + urllib.parse.urlencode(
                    {"flash": redirect.flash}
                )
            self.send_response(303)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        except (routes.BadRequest, ActionError) as bad:
            # A refused action is the user's mistake, not the server's: say
            # what was wrong and offer the way back, without a stack trace.
            return self._send(
                400,
                r.page("Cannot do that", f"<p>{r.esc(bad)}</p>"
                                         '<p><a href="/">Back to positions</a></p>'),
            )
        except Exception as exc:  # noqa: BLE001 - a local app should say why
            import traceback

            traceback.print_exc()
            return self._send(
                500,
                r.page("Error", f"<pre>{r.esc(exc)}</pre>"
                                '<p><a href="/">Back</a></p>'),
            )
        finally:
            conn.close()

        flash = (query.get("flash") or [""])[0]
        if flash and body:
            body = body.replace("<main>\n", f"<main>\n{_flash(flash)}", 1)
        self._send(status, body)

    def _authorized(self) -> bool:
        """Optional single password, so a bound port is not wide open."""
        if not self.app.password:
            return True
        import base64

        header = self.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                decoded = base64.b64decode(header[6:]).decode()
                _, _, supplied = decoded.partition(":")
                if secrets.compare_digest(supplied, self.app.password):
                    return True
            except Exception:  # noqa: BLE001 - malformed header is a no
                pass
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="bcoj"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def _read_form(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self.close_connection = True   # the unread body must not be parsed as a request
            self._send(400, r.page("Bad request", "<p>Bad length.</p>"))
            return None
        if length > MAX_BODY:
            self.close_connection = True
            self._send(413, r.page("Too large", "<p>Form too large.</p>"))
            return None
        raw = self.rfile.read(length).decode("utf-8", "replace")
        return urllib.parse.parse_qs(raw, keep_blank_values=True)

    def _serve_static(self, path: str):
        asset = STATIC.get(path[len("/static/"):])
        if asset is None:
            return self._send(404, "not found", "text/plain")
        content_type, content = asset
        # The URL carries a content fingerprint, so this can be cached hard:
        # a changed file is a changed URL.
        self._write(200, content.encode(), content_type,
                    [("Cache-Control", "max-age=31536000, immutable")])

    def _send(self, status: int, body: str, content_type="text/html; charset=utf-8"):
        self._write(status, body.encode(), content_type,
                    [("X-Content-Type-Options", "nosniff"), ("Referrer-Policy", "no-referrer")])

    def _write(self, status: int, payload: bytes, content_type: str, headers=()) -> None:
        """Gzip anything worth gzipping. A 500-row table is half a megabyte
        raw and a twentieth of that compressed; over a forwarded port that is
        the difference between seconds and nothing."""
        accepts = self.headers.get("Accept-Encoding", "") if hasattr(self, "headers") else ""
        encoded = len(payload) > 1024 and "gzip" in accepts
        if encoded:
            payload = gzip.compress(payload, compresslevel=5)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        if encoded:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Vary", "Accept-Encoding")
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(payload)


def _flash(message: str) -> str:
    level = "warn" if message.startswith("!") else "ok"
    return f'<div class="flash {level}">{r.esc(message.lstrip("!"))}</div>\n'


class Server(ThreadingHTTPServer):
    """One thread per connection, a backlog deep enough for a browser's burst
    of parallel fetches, and threads that do not keep the process alive."""

    request_queue_size = 128
    daemon_threads = True
    allow_reuse_address = True


def serve(db_path: str, host: str = "127.0.0.1", port: int = 8000) -> None:
    app = App(db_path)
    Handler.app = app

    # Touch the database up front so a migration failure surfaces now rather
    # than on the first request.
    app.connect().close()

    httpd = Server((host, port), Handler)
    print(f"{r.APP_NAME}")
    print(f"  journal : {db_path}")
    print(f"  address : http://{host}:{port}/")
    if not app.password:
        print("  auth    : none (loopback only; set BCOJ_PASSWORD to require one)")
    else:
        print("  auth    : password required")
    print("  stop    : ctrl-c")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
