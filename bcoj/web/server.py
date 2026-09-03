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
            ("GET", r"^/audit$", self._audit),
            ("POST", r"^/audit/(\d+)/revert$", self._revert),
            ("GET", r"^/position/([0-9a-zA-Z_-]+)$", self._position),
            ("POST", r"^/position/([0-9a-zA-Z_-]+)/close$", self._close),
            ("POST", r"^/position/([0-9a-zA-Z_-]+)/expire$", self._expire),
            ("POST", r"^/position/([0-9a-zA-Z_-]+)/assign$", self._assign),
            ("POST", r"^/position/([0-9a-zA-Z_-]+)/roll$", self._roll),
            ("POST", r"^/position/([0-9a-zA-Z_-]+)/split$", self._split),
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
        return routes.positions_page(conn, query)

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


class Handler(BaseHTTPRequestHandler):
    server_version = "bcoj"
    sys_version = ""
    app: App = None  # set by serve()

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        # One tidy line per request; the default is noisy and leaks nothing
        # useful for a local app.
        print(f"  {self.command} {self.path} -> {args[1]}")

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
            status, body = handler(conn, query, form, args)
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
            self._send(400, r.page("Bad request", "<p>Bad length.</p>"))
            return None
        if length > MAX_BODY:
            self._send(413, r.page("Too large", "<p>Form too large.</p>"))
            return None
        raw = self.rfile.read(length).decode("utf-8", "replace")
        return urllib.parse.parse_qs(raw, keep_blank_values=True)

    def _serve_static(self, path: str):
        asset = STATIC.get(path[len("/static/"):])
        if asset is None:
            return self._send(404, "not found", "text/plain")
        content_type, content = asset
        body = content.encode()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # Vendored in-process; no revalidation needed within a session.
        self.send_header("Cache-Control", "max-age=300")
        self.end_headers()
        self.wfile.write(body)

    def _send(self, status: int, body: str, content_type="text/html; charset=utf-8"):
        payload = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(payload)


def _flash(message: str) -> str:
    level = "warn" if message.startswith("!") else "ok"
    return f'<div class="flash {level}">{r.esc(message.lstrip("!"))}</div>\n'


def serve(db_path: str, host: str = "127.0.0.1", port: int = 8000) -> None:
    app = App(db_path)
    Handler.app = app

    # Touch the database up front so a migration failure surfaces now rather
    # than on the first request.
    app.connect().close()

    httpd = ThreadingHTTPServer((host, port), Handler)
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
