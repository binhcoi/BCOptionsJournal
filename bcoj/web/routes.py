"""Page handlers.

Each returns ``(status, body)`` for a page or raises ``Redirect``. All the
arithmetic lives in bcoj.engine and all the writing in bcoj.db.store, so these
are about one thing: making entry fast and mistakes hard.
"""

import csv
import io
import json
import os
import sqlite3
import tempfile
import urllib.parse
from dataclasses import asdict
from datetime import date, timedelta
from decimal import Decimal

from ..db import store
from ..domain.enums import Direction, Right, Status
from ..domain.money import ZERO, fmt, parse_money, price, q2
from ..domain.types import Position
from ..engine import actions, decide, reports, validate, wheels
from ..engine.chains import ChainIndex
from ..engine.shares import InsufficientSharesError, match as match_lots
from ..engine.pnl import close_cash, open_cash, realized_pl
from ..engine.risk import break_even, capital_at_risk, credit_to_recover
from ..engine.targets import target
from . import render as r


class Redirect(Exception):
    def __init__(self, location: str, flash: str = ""):
        self.location = location
        self.flash = flash


class BadRequest(Exception):
    pass


# ---------------------------------------------------------------------------
# form parsing


def _one(form, name, default=""):
    values = form.get(name)
    return values[0].strip() if values else default


def _decimal(form, name, default=None, label=None):
    raw = _one(form, name)
    if raw == "":
        if default is None:
            raise BadRequest(f"{label or name} is required")
        return default
    try:
        value = parse_money(raw)
    except ValueError:
        raise BadRequest(f"{label or name}: {raw!r} is not a number") from None
    if value is None:
        if default is None:
            raise BadRequest(f"{label or name} is required")
        return default
    return value


def _int(form, name, default=None, label=None):
    raw = _one(form, name)
    if raw == "":
        if default is None:
            raise BadRequest(f"{label or name} is required")
        return default
    try:
        return int(raw)
    except ValueError:
        raise BadRequest(f"{label or name}: {raw!r} is not a whole number") from None


def _date(form, name, default=None, label=None):
    raw = _one(form, name)
    if raw == "":
        if default is None:
            raise BadRequest(f"{label or name} is required")
        return default
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise BadRequest(f"{label or name}: {raw!r} is not a date") from None


def _fee_rate(conn) -> Decimal:
    return Decimal(store.get_setting(conn, "fee_rate", "0.65"))


def _next_friday(from_day: date | None = None) -> date:
    day = from_day or date.today()
    return day + timedelta(days=(4 - day.weekday()) % 7 or 7)

ACTIONS = (
    ("close", "Close"),
    ("roll", "Roll"),
    ("expire", "Expire"),
    ("assign", "Assign"),
    ("split", "Split"),
)


def _next(form, fallback: str) -> str:
    """Where to land after an action: the page the form was on."""
    where = _one(form, "next")
    # Only ever a local path; never an absolute URL from the form.
    return where if where.startswith("/") and not where.startswith("//") else fallback


# ---------------------------------------------------------------------------
# positions list


# The first column is a gutter: it holds the row markers (the arrow for the
# position a page is about) so they never push the contract out of line.
POSITION_COLUMNS = ["", "Contract", "DTE", "Status", ("Open", "Open price / share"),
                    ("Close", "Close price / share"), "Credit", "Closing", "Realized",
                    "Carry", ("B/E", "Break-even for the chain"),
                    ("At risk", "Capital at risk"), "", "Legs"]


def _position_row(p, index, *, here: str = "/", act_id: str = "", which: str = "",
                  chain_id: str = "", expanded: bool = False, in_chain: bool = False,
                  current: bool = False, siblings: int = 0, rail=None,
                  with_actions: bool = True, collapse_to: str = "") -> str:
    """One position as a table row. The single renderer for positions.

    Used for the positions list, for the legs revealed when a chain is
    expanded there, and for the chain on a position's own page -- so the
    figures a chain shows are exactly the figures the list shows.

    For an open position the close price and closing cash are the profit
    target, shown as projections; once closed they are what happened.
    """
    carry = index.carry(p)
    chain = index.chain(p)
    dte = (p.expiry - date.today()).days if p.is_open else None
    be = break_even(chain) if p.is_open else None
    pid = r.esc(p.id)
    anchor = f"#row-{pid}"

    classes = []
    if in_chain:
        classes += ["chain-leg", "leg-open" if p.is_open else "leg-closed"]
    if current:
        classes.append("current")
    if rail is not None and not in_chain:
        classes.append(f"fam fam-{rail}")
    cls = f' class="{" ".join(classes)}"' if classes else ""

    note = ""
    if p.is_superseded:
        halves = sorted(index.successors(p), key=lambda h: h.quantity)
        if halves:
            parts = [str(h.quantity) for h in halves]
            note = (f'<span class="fam-note" title="split into {" + ".join(parts)}">'
                    f'&#x2442; {"+".join(parts)}</span>')
    elif siblings > 1 and p.is_open and not in_chain:
        note = (f'<span class="fam-note" title="1 of {siblings} open in this chain">'
                f"&#x2442;{siblings}</span>")

    family_size = len(index.family(p))

    # Every link out of a row keeps the other state the page is showing: an
    # action link keeps the expanded chain, a chain link keeps the open form.
    act_q = f"&act={r.esc(act_id)}&do={r.esc(which)}" if act_id and which else ""
    chain_q = f"&chain={r.esc(chain_id)}" if chain_id else ""

    actions_html = ""
    if with_actions and p.is_open:
        # One small button per row. It opens the Close form; the form's own
        # tabs switch to any other action. Clicking it again closes the form.
        is_open = bool(p.id == act_id and which)
        href = (f"{here}{chain_q}{anchor}" if is_open
                else f"{here}{chain_q}&act={pid}&do=close{anchor}")
        actions_html = (f'<a class="act act-close{" here" if is_open else ""}" href="{href}"'
                        ' title="Close, roll, expire, assign or split">Close</a>')

    if in_chain and not (current and expanded):
        legs_html = ""      # the chain is what is being shown
    elif family_size > 1:
        target_url = (f"{here}{act_q}{anchor}" if expanded
                      else f"{here}{act_q}&chain={pid}{anchor}")
        title = "Hide the chain" if expanded else "Show the whole chain"
        text = "&#9650;" if expanded else str(family_size)
        legs_html = (f'<a class="legs{" here" if expanded else ""}"'
                     f' href="{target_url}" title="{title}">{text}</a>')
    else:
        legs_html = ""        # a lone position has no chain to show

    # Always a link, so an expanded row still leads to its page. The row being
    # viewed is marked by an arrow in the gutter, not inline.
    label = f'<a href="/position/{pid}">{r.contract(p)}</a>'
    tags_html = "".join(f'<span class="tag">{r.esc(t)}</span>' for t in p.tags)
    gutter = ""
    if current:
        # No <b> around the contract: its grid tracks are sized in ch, and a
        # bold "0" is wider, so the whole row would drift out of line.
        gutter = '<span class="here-arrow" title="This position"></span>'

    # Clicking anywhere on a row that belongs to a chain toggles the chain;
    # links and buttons inside it still do their own thing.
    toggle = ""
    if in_chain and collapse_to:
        # Collapse lands on the row that was clicked: a closed leg's own row
        # is gone once the chain folds, so anchoring to it would go nowhere.
        toggle = f' data-chain="{here}{act_q}#row-{r.esc(collapse_to)}"'
    elif family_size > 1 and with_actions:
        toggle = f' data-chain="{here}{act_q}&chain={pid}{anchor}"'

    # Prices and cash flows. Open positions project to the target.
    if p.is_open:
        tgt = target(p, carry)
        if tgt.applicable:
            close_price = f'<span class="proj" title="50% target">{r.esc(price(tgt.price))}</span>'
            closing = (f'<span class="proj" title="at the 50% target">'
                       f"{r.esc(fmt(tgt.expected_closing))}</span>")
            # What this leg alone would realize at the target: its credit plus
            # the projected closing. Chain carry is its own column.
            leg_expected = q2(open_cash(p) + tgt.expected_closing)
            realized_cell = (f'<span class="proj" title="expected at the 50% target">'
                             f"{r.esc(fmt(leg_expected))}</span>")
        else:
            close_price = closing = realized_cell = '<span class="dim">-</span>'
    elif p.is_superseded:
        close_price = closing = realized_cell = '<span class="dim">&mdash;</span>'
    else:
        close_price = r.esc(price(p.close_price))
        closing = r.money(close_cash(p))
        realized_cell = r.money(realized_pl(p))

    return (
        f'<tr id="row-{pid}"{cls}{toggle}>'
        f'<td class="gutter">{gutter}</td>'
        f'<td class="main">{label}{note}{tags_html}</td>'
        f"<td>{r.dte_cell(dte)}</td>"
        f"<td>{r.status_badge(p.status)}</td>"
        f'<td class="unit">{r.esc(price(p.open_price))}</td>'
        f'<td class="unit">{close_price}</td>'
        f"<td>{r.money(open_cash(p))}</td>"
        f"<td>{closing}</td>"
        f"<td>{realized_cell}</td>"
        f'<td>{r.money(carry, dash="0.00")}</td>'
        f"<td>{r.money(be.price if be else None)}</td>"
        f"<td>{r.money(capital_at_risk(p))}</td>"
        f'<td class="acts">{actions_html}</td>'
        f"<td>{legs_html}</td></tr>"
    )


def _chain_head_row(index, legs, clicked, here) -> str:
    """A header for an expanded chain: what it is, its total, and Hide.

    Marks where the block starts, so its rows are not mistaken for the
    positions around them.
    """
    realized = q2(sum((realized_pl(leg) for leg in legs), ZERO))
    chain = index.chain(clicked)
    pid = r.esc(clicked.id)
    return (
        f'<tr class="chain-head"><td colspan="{len(POSITION_COLUMNS)}">'
        f"<span>Chain &middot; {len(legs)} leg(s) &middot; "
        f"realized {r.money(realized)} &middot; {chain.days} days</span>"
        f'<a class="legs here" href="{here}#row-{pid}" title="Hide the chain">&#9650;</a>'
        f"</td></tr>"
    )


def _action_row(p, index, which, token, here) -> str:
    """Every action form for one row, with tabs to switch between them in
    place. ``here`` already carries the page's chain state."""
    pid = r.esc(p.id)
    dismiss = f"{here}#row-{pid}"
    panels = _action_panels(
        p, which, token, index.carry(p), back=here, box_id=f"actions-{pid}",
        tab_href=lambda key: f"{here}&act={pid}&do={key}#row-{pid}",
        dismiss_href=dismiss, dismiss_attrs="", panel_close=False,
    )
    close = (f'<a class="close-form" href="{dismiss}" '
             'title="Close this form" aria-label="Close this form">&times;</a>')
    return (
        f'<tr class="action-row"><td colspan="{len(POSITION_COLUMNS)}">'
        f'<div class="action-inline">{close}<h3>{r.contract(p)}</h3>{panels}</div></td></tr>'
    )


def positions_page(conn, query, token: str = "") -> tuple[int, str]:
    positions = store.load_positions(conn)
    index = ChainIndex(positions)
    open_ones = [p for p in positions if p.is_open]

    show = (query.get("show") or ["open"])[0]
    if show == "all":
        listed = positions
    elif show == "closed":
        listed = [p for p in positions if not p.is_open and not p.is_superseded]
    else:
        listed = open_ones
    listed = _apply_filters(listed, query)

    act_id = (query.get("act") or [""])[0]
    which = (query.get("do") or [""])[0]
    chain_id = (query.get("chain") or [""])[0]
    # Every link on the page keeps the filter, so opening a form or a chain
    # never loses the view being looked at.
    filter_qs = _filter_qs(query)
    here = f"/?show={show}" + (f"&{filter_qs}" if filter_qs else "")

    if show == "closed":
        listed = sorted(listed, key=lambda p: (p.closed_on or p.expiry, p.underlying),
                        reverse=True)
    elif show == "all":
        listed = sorted(listed, key=lambda p: (p.opened_on, p.underlying),
                        reverse=True)
    else:
        listed = sorted(listed, key=lambda p: (p.expiry, p.underlying, -p.quantity))

    # Open positions sharing a root belong to one chain; mark them.
    roots = {p.id: index.root(p).id for p in open_ones}
    size: dict[str, int] = {}
    for root_id in roots.values():
        size[root_id] = size.get(root_id, 0) + 1
    rail_of = {root_id: i % 4 for i, root_id in enumerate(
        sorted(k for k, n in size.items() if n > 1))}

    # An expanded chain is shown whole, in order, where its clicked row was.
    # Its legs are then not listed again elsewhere in the table.
    expanded_target = index.get(chain_id) if chain_id else None
    expanded_legs = ([leg for leg, _ in index.family(expanded_target)]
                     if expanded_target else [])
    expanded_ids = {leg.id for leg in expanded_legs}

    queue = validate.expiring(open_ones)
    banner = ""
    if queue:
        banner = (f'<div class="callout"><a href="/expiring">'
                  f"{len(queue)} position(s) at or past expiry need an outcome"
                  "</a></div>")

    act_q = f"&act={r.esc(act_id)}&do={r.esc(which)}" if act_id and which else ""
    chain_q = f"&chain={r.esc(chain_id)}" if chain_id else ""

    # Where the expanded chain and the open form land in ``rows``: the script
    # asks for just those slices, so expanding a chain in a 500-row list moves
    # a few kilobytes rather than the whole table again.
    rows = []
    block = (0, 0)
    act_row_at = action_at = None
    for p in listed:
        if p.id in expanded_ids and p.id != chain_id:
            continue  # rendered as part of the expanded chain

        if p.id == chain_id:
            start = len(rows)
            rows.append(_chain_head_row(index, expanded_legs, p, here + act_q))
            for leg in expanded_legs:
                if leg.id == act_id:
                    act_row_at = len(rows)
                rows.append(_position_row(
                    leg, index, here=here, act_id=act_id, which=which,
                    chain_id=chain_id, expanded=(leg.id == chain_id), in_chain=True,
                    current=(leg.id == chain_id), collapse_to=chain_id,
                ))
                if leg.id == act_id and which and leg.is_open:
                    action_at = len(rows)
                    rows.append(_action_row(leg, index, which, token, here + chain_q))
            block = (start, len(rows))
            continue

        root_id = roots.get(p.id)
        if p.id == act_id:
            act_row_at = len(rows)
        rows.append(_position_row(
            p, index, here=here, act_id=act_id, which=which, chain_id=chain_id,
            siblings=size.get(root_id, 0) if root_id else 0,
            rail=rail_of.get(root_id),
        ))
        if p.id == act_id and which and p.is_open:
            action_at = len(rows)
            rows.append(_action_row(p, index, which, token, here + chain_q))

    partial = (query.get("partial") or [""])[0]
    if partial == "block":
        return 200, "".join(rows[block[0]:block[1]])
    if partial == "action":
        if act_row_at is None or action_at is None:
            return 200, ""
        return 200, rows[act_row_at] + rows[action_at]

    totals = _portfolio_totals(conn, positions, index)
    tabs = " ".join(
        f'<a href="/?show={k}" class="{"here" if show == k else ""}">{label}</a>'
        for k, label in (("open", "Open"), ("closed", "Closed"), ("all", "All"))
    )

    table_html = r.table(POSITION_COLUMNS, rows, cls="positions")
    if partial == "table":
        # Just the table, for the script that swaps it in place.
        return 200, table_html

    body = f"""{banner}
{totals}
<div class="tabs">{tabs}</div>
{_filter_bar(conn, query, show, token)}
{table_html}
<p class="hint">Credit is what came in on opening. Carry is what the chain
brought forward. They are different scopes and are never added together.
Click a row or its leg count to show the whole chain in place; again to hide.</p>"""
    return 200, r.page("Positions", body, nav_here="positions")


def _portfolio_totals(conn, positions, index) -> str:
    """Three figures that are known, side by side and never summed.

    Unrealised P/L is not computable without marks, so it is not shown.
    """
    open_ones = [p for p in positions if p.is_open]
    premium = q2(sum((open_cash(p) for p in open_ones), ZERO))
    realized = q2(sum((realized_pl(p) for p in positions), ZERO))
    carry = q2(sum((index.carry(p) for p in open_ones), ZERO))
    at_risk = q2(sum(
        (capital_at_risk(p) or ZERO for p in open_ones), ZERO
    ))
    expected = q2(sum(
        (target(p, index.carry(p)).expected_pl for p in open_ones), ZERO
    ))
    lots = store.load_lots(conn)
    disposals = store.load_disposals(conn)
    tickers = wheels.by_ticker(index, positions, lots, disposals, store.matching_rule(conn))
    shares_pl = wheels.realized_shares(tickers)
    blocked = [t.underlying for t in tickers if t.error]
    total_cell = r.money(q2(realized + shares_pl))
    if blocked:
        total_cell += (f' <a href="/shares" class="neg" title="{r.esc(", ".join(blocked))}'
                       ' cannot be matched">&#9888;</a>')
    cells = [
        ("Realized options", r.money(realized)),
        ("Realized shares", r.money(shares_pl)),
        ("True total", total_cell),
        ("Open premium", r.money(premium)),
        ("Expected at target", r.money(expected)),
        ("Chain carry", r.money(carry)),
        ("Capital at risk", r.money(at_risk)),
        ("Open positions", r.esc(len(open_ones))),
    ]
    return '<div class="totals">' + "".join(
        f'<div><span>{r.esc(label)}</span><b>{value}</b></div>'
        for label, value in cells
    ) + "</div>"


# ---------------------------------------------------------------------------
# new position


def new_position_form(conn, token, form=None, problems=()) -> tuple[int, str]:
    form = form or {}
    rate = _fee_rate(conn)
    tickers = store.recent_underlyings(conn)

    quantity = _one(form, "quantity", "1")
    try:
        default_fee = str(q2(rate * int(quantity or 1)))
    except ValueError:
        default_fee = str(rate)

    side = _one(form, "direction", "SHORT")
    side_select = (
        '<select name="direction" id="f_direction" aria-label="Side" title="Side">'
        + "".join(f'<option value="{v}"{" selected" if v == side else ""}>{t}</option>'
                  for v, t in (("SHORT", "STO"), ("LONG", "BTO")))
        + "</select>")
    right = _one(form, "right", "PUT")
    right_select = (
        '<select name="right" id="f_right" aria-label="Right" title="Right">'
        + "".join(f'<option value="{v}"{" selected" if v == right else ""}>{t}</option>'
                  for v, t in (("PUT", "Put"), ("CALL", "Call")))
        + "</select>")
    leg = [
        side_select,
        _box("underlying", _one(form, "underlying"), kind="text", label="Ticker",
             autofocus=True, attrs=' list="tickers" autocapitalize="characters"'
                                   ' autocomplete="off"'),
        _box("expiry", _one(form, "expiry", _next_friday().isoformat()), kind="date",
             label="Expiry"),
        _box("strike", _one(form, "strike"), step="0.5", label="Strike"),
        right_select,
        _box("quantity", quantity, step="1", label="Contracts", attrs=' min="1"'),
        _box("open_price", _one(form, "open_price"), label="Premium / share"),
        _box("open_fee", _one(form, "open_fee", default_fee), label="Fee", required=False),
    ]
    grid = _leg_grid([leg], attrs=f' data-fee-rate="{r.esc(rate)}"')
    rest = '<div class="grid">' + "".join([
        r.field("Opened", "opened_on", _one(form, "opened_on", r.today_iso()),
                kind="date", required=True),
        r.field("Notes", "notes", _one(form, "notes"),
                hint="Why this trade, in your words"),
        r.field("Tags", "tags", _one(form, "tags"), attrs=' list="all-tags" autocomplete="off"',
                hint="Comma separated"),
        r.select("Cover with lot", "share_lot_id",
                 [("", "- not covered -")] + [
                     (l.id, _lot_label(l)) for l in sorted(
                         store.load_lots(conn), key=lambda l: (l.underlying, l.acquired_on))
                 ], _one(form, "share_lot_id"),
                 hint="For a short call written against shares you hold"),
    ]) + "</div>"
    body = f"""{r.problems_block(problems)}
{r.datalist("tickers", tickers)}{r.datalist("all-tags", store.all_tags(conn))}
{r.form("/new", grid + rest, token, submit="Add position", cls="two-part")}
<p class="hint">STO sells to open (short), BTO buys to open (long). Fee is
auto-filled at {fmt(rate)} per contract and editable. Shortcuts: <kbd>+7</kbd>,
<kbd>+14</kbd>, <kbd>+30</kbd> in the expiry box jump that many days out.
Submitting leaves you on a fresh form.</p>"""
    return 200, r.page("New position", body, nav_here="new")


def create_position(conn, form, token) -> None:
    rate = _fee_rate(conn)

    # Normalise before anything else, so a refused entry redisplays exactly
    # what would have been saved rather than the raw keystrokes.
    form = dict(form)
    form["underlying"] = [_one(form, "underlying").upper()]

    quantity = _int(form, "quantity", label="Contracts")
    if quantity == 0:
        raise BadRequest("Contracts must not be zero")

    # A negative quantity is long, matching the habit the spreadsheet built.
    direction = Direction(_one(form, "direction", "SHORT"))
    if quantity < 0:
        direction = Direction.LONG

    position = Position(
        id=actions.new_id(),
        underlying=_one(form, "underlying").upper(),
        expiry=_date(form, "expiry", label="Expiry"),
        strike=_decimal(form, "strike", label="Strike"),
        right=Right(_one(form, "right", "PUT")),
        direction=direction,
        quantity=abs(quantity),
        opened_on=_past(_date(form, "opened_on", date.today()), "Opened on"),
        open_price=_decimal(form, "open_price", label="Premium"),
        open_fee=_decimal(form, "open_fee", q2(rate * abs(quantity))),
        close_fee=_decimal(form, "open_fee", q2(rate * abs(quantity))),
        status=Status.OPEN,
        notes=_one(form, "notes"),
        tags=store.normalize_tags(_one(form, "tags")),
        share_lot_id=_one(form, "share_lot_id") or None,
    )

    existing = store.load_positions(conn)
    problems = validate.validate_position(position, existing, rate)
    if validate.errors(problems):
        raise Invalid(problems, form)

    store.apply(conn, actions.ActionResult(created=[position]), "entered by hand")
    warned = validate.warnings(problems)
    note = f"Added {position.underlying} {fmt(position.strike)}" \
           f"{position.right.value[0]}"
    if warned:
        note = "!" + note + f" - {len(warned)} warning(s): " + \
               "; ".join(p.message for p in warned)
    raise Redirect("/new", note)


class Invalid(Exception):
    """Validation refused the entry; re-show the form with what was typed."""

    def __init__(self, problems, form):
        self.problems = problems
        self.form = form


# ---------------------------------------------------------------------------
# one position


def position_page(conn, position_id, token, query) -> tuple[int, str]:
    position = store.load_position(conn, position_id)
    if position is None:
        return 404, r.page("Not found", "<p>No such position.</p>")

    positions = store.load_positions(conn)
    index = ChainIndex(positions)
    chain = index.chain(position)
    carry = index.carry(position)
    here = f"/position/{r.esc(position.id)}"

    facts = [
        ("Status", r.esc(position.status.value.title())),
        ("Opened", r.esc(position.opened_on)),
        ("Expiry", r.esc(position.expiry)),
        ("Credit received", r.money(open_cash(position))),
        ("Chain carry", r.money(carry, dash="0.00")),
        ("Capital at risk", r.money(capital_at_risk(position))),
    ]
    if position.is_open:
        tgt = target(position, carry)
        be = break_even(chain)
        facts += [
            ("Net chain credit", r.money(chain.net_credit)),
            ("Break-even", r.money(be.price if be else None)),
            ("50% target price", r.money(tgt.price if tgt.applicable else None)),
            ("Expected at target", r.money(tgt.expected_pl)),
        ]
        recover = credit_to_recover(chain)
        if recover > 0:
            facts.append(("Credit needed to recover", r.money(recover)))
    else:
        facts.append(("Realized", r.money(realized_pl(position))))

    lots = store.load_lots(conn)
    lot_by_id = {l.id: l for l in lots}
    if position.right is Right.CALL and position.direction is Direction.SHORT:
        covering = lot_by_id.get(position.share_lot_id) if position.share_lot_id else None
        if covering:
            facts.append(("Covered by", f'<a href="/shares/{r.esc(covering.underlying)}">'
                                        f"{r.esc(_lot_label(covering))}</a>"))
        else:
            facts.append(("Covered by", '<span class="neg">nothing - naked</span>'))
    created = [l for l in lots if l.assigning_position_id == position.id
               and position.right is Right.PUT]
    if created:
        facts.append(("Shares acquired", f'<a href="/shares/{r.esc(created[0].underlying)}">'
                                         f"{r.esc(_lot_label(created[0]))}</a>"))

    fact_rows = "".join(
        f"<div><span>{r.esc(k)}</span><b>{v}</b></div>" for k, v in facts
    )

    if position.is_open:
        which = (query.get("do") or [""])[0]
        actions_block = ("<h2>Actions</h2>"
                         + _action_panels(position, which, token, carry, here))
        if position.right is Right.CALL and position.direction is Direction.SHORT:
            candidates = [l for l in lots if l.underlying == position.underlying]
            if candidates:
                options = [("", "- none (naked) -")] + [(l.id, _lot_label(l)) for l in candidates]
                actions_block += ("<h3>Covering lot</h3>" + r.form(
                    f"/position/{r.esc(position.id)}/cover",
                    r.hidden("next", here) + r.select("Shares this call is written against",
                                                      "lot_id", options, position.share_lot_id or ""),
                    token, submit="Save", cls="grid"))
    else:
        actions_block = '<p class="dim">This position is closed. Nothing to do.</p>'

    notes = ("<h2>Notes</h2>" + r.form(
        f"/position/{r.esc(position.id)}/notes",
        r.hidden("next", here)
        + r.textarea("Notes", "notes", position.notes, hint="Why this trade, in your words")
        + r.field("Tags", "tags", ", ".join(position.tags),
                  attrs=' list="all-tags" autocomplete="off"',
                  hint="Comma separated, e.g. wheel, earnings")
        + r.datalist("all-tags", store.all_tags(conn)),
        token, submit="Save notes", cls="grid"))

    body = f"""<div class="totals wide">{fact_rows}</div>
{actions_block}
{_chain_block(index, position, chain)}
{notes}
<p class="hint"><a href="/audit?entity={r.esc(position.id)}">History for this
position</a></p>"""
    return 200, r.page(r.contract_text(position), body, nav_here="positions")


def _chain_block(index, position, chain) -> str:
    """Every leg of this position's chain, as the same rows the list uses.

    A lineage shows one path; this shows the whole chain including the other
    half of any split and what became of it, in order, with this position
    marked. Same columns, same figures, as the positions page.
    """
    legs = [leg for leg, _ in index.family(position)]
    has_split = any(leg.is_superseded for leg in legs)
    rows = [
        _position_row(leg, index, in_chain=True,
                      current=(leg.id == position.id), with_actions=False)
        for leg in legs
    ]

    totals = [("This chain realized", r.money(chain.realized)),
              ("Days", r.num(chain.days))]
    if len(legs) > 1:
        root = legs[0]
        drift = q2(position.strike - root.strike)
        totals.append(("Strike", f"{r.esc(price(root.strike))} &rarr; "
                                 f"{r.esc(price(position.strike))}"
                                 + (f' <small>{"down" if drift < 0 else "up"} '
                                    f"{r.esc(price(abs(drift)))}</small>" if drift else "")))
        grew = position.quantity - root.quantity
        totals.append(("Size", f"{root.quantity} &rarr; {position.quantity}"
                               + (f' <small class="neg">x{position.quantity / root.quantity:.1f}</small>'
                                  if grew > 0 else "")))
    explain = ""
    if has_split:
        everything = q2(sum((realized_pl(leg) for leg in legs), ZERO))
        totals.insert(1, ("All legs realized", r.money(everything)))
        explain = ('<p class="hint">A split position stays as the record of the '
                   'split and realizes nothing itself; its credit is carried by '
                   'the halves, which follow it here.</p>')

    return f"""<h2>Chain - {len(legs)} leg(s)</h2>
{r.table(POSITION_COLUMNS, rows, cls="positions chain")}
<div class="totals">{"".join(
    f"<div><span>{r.esc(k)}</span><b>{v}</b></div>" for k, v in totals)}</div>
{explain}"""


def _action_form(position, which: str, token: str, carry, back: str,
                 cancel: str = "", cancel_attrs: str = "") -> str:
    """The form for one action. Used inline on the positions page and on the
    position's own page; ``back`` is where to return afterwards and ``cancel``
    where to go without acting."""
    pid = r.esc(position.id)
    dismiss = dict(cancel=cancel, cancel_attrs=cancel_attrs)
    rate = Decimal("0.65") * position.quantity
    tgt = target(position, carry)
    # The profit target is the natural default for a buy-back price: it is
    # what you were aiming at. Blank when the chain has no target to aim for.
    suggested = str(tgt.price) if tgt.applicable else ""
    nxt = r.hidden("next", back)

    if which == "close":
        row = [_closing_tag(position), *_fixed_terms(position),
               _box("close_price", suggested, label="Close price / share", autofocus=True),
               _box("close_fee", str(q2(rate)), label="Fee", required=False)]
        hint = ('<p class="hint">Pre-filled with the 50% target.</p>' if suggested
                else '<p class="hint">Closes the leg at the price entered.</p>')
        when = r.field("Closed on", "closed_on", r.today_iso(), kind="date", required=True)
        return r.form(f"/position/{pid}/close", nxt + when + _leg_grid([row]) + hint,
                      token, submit="Close position", cls="two-part",
                      submit_cls="btn-close", **dismiss)

    if which == "roll":
        grid = _roll_grid(position, suggested, q2(rate))
        when = r.field("Rolled on", "on", r.today_iso(), kind="date", required=True)
        preview = (f'<div class="preview" data-preview="/position/{pid}/roll-preview">'
                   + _roll_preview_placeholder() + "</div>")
        return r.form(f"/position/{pid}/roll", nxt + when + grid + preview, token,
                      submit="Roll", cls="two-part", submit_cls="btn-roll", **dismiss)

    if which == "expire":
        row = [_tag("EXP", "Expired worthless", "expire"), *_fixed_terms(position),
               _fixed("0.00"), _blank()]
        when = r.field("Expired on", "on", position.expiry.isoformat(), kind="date",
                       required=True)
        hint = '<p class="hint">Books the full premium; nothing changes hands.</p>'
        return r.form(f"/position/{pid}/expire", nxt + when + _leg_grid([row]) + hint, token, submit="Expired worthless", cls="two-part",
                      submit_cls="btn-expire", **dismiss)

    if which == "assign":
        acquiring = (position.direction is Direction.SHORT) == (
            position.right is Right.PUT
        )
        moves = (f"acquire {position.shares} shares at {fmt(position.strike)}"
                 if acquiring else
                 f"deliver {position.shares} shares at {fmt(position.strike)}")
        option_row = [_tag("ASG", "Assigned", "assign"), *_fixed_terms(position),
                      _fixed("0.00"),
                      _box("close_fee", "0.00", label="Option fee", required=False)]
        share_row = [_tag("BUY" if acquiring else "SELL",
                          "Shares " + ("bought" if acquiring else "delivered"), "shares"),
                     _fixed(r.esc(position.underlying)), _blank(), _blank(),
                     _fixed("Shares"), _fixed(r.esc(position.shares)),
                     _fixed(r.esc(price(position.strike))),
                     _box("share_fee", "0.00", label="Share fee", required=False)]
        # Date defaults to today, or to the expiry once it has passed: an
        # assignment is normally noticed the morning after and dated to expiry.
        hint = f'<p class="hint">Also records the shares: {r.esc(moves)}.</p>'
        return f"""{r.form(f"/position/{pid}/assign", nxt
        + r.field("Assigned on", "on", min(date.today(), position.expiry).isoformat(),
                  kind="date", required=True)
        + _leg_grid([option_row, share_row]) + hint, token, submit="Record assignment", cls="two-part", submit_cls="btn-assign",
        **dismiss)}"""

    if which == "split":
        row = [_tag("SPLIT", "Split the position", "split"), *_fixed_terms(position)[:4],
               _box("quantity", "", step="1", label="Contracts to peel off", autofocus=True,
                    attrs=f' min="1" max="{position.quantity - 1}"'),
               _blank(), _blank()]
        hint = (f'<p class="hint">Peel off 1 to {position.quantity - 1} of '
                f"{position.quantity} contracts so the halves can take different paths; "
                "history is divided pro-rata.</p>")
        return r.form(f"/position/{pid}/split", nxt
                      + r.field("On", "on", r.today_iso(), kind="date", required=True)
                      + _leg_grid([row]) + hint,
                      token, submit="Split", cls="two-part", submit_cls="btn-split", **dismiss)

    return '<p class="hint">Pick an action.</p>'


LEG_COLUMNS = ("", "Ticker", "Expiry", "Strike", "Right", "Qty", "Price", "Fee")


def _fixed(text) -> str:
    """A term that belongs to an existing contract and cannot change."""
    return f'<span class="fixed">{text}</span>'


def _blank() -> str:
    return '<span class="fixed dim">-</span>'


def _tag(tag: str, full: str, cls: str) -> str:
    return f'<b class="leg-tag {cls}" title="{r.esc(full)}">{r.esc(tag)}</b>'


def _box(name, value, *, label, step="0.01", kind="number", required=True,
         autofocus=False, attrs="") -> str:
    req = " required" if required else ""
    auto = " autofocus" if autofocus else ""
    stp = f' step="{step}"' if kind == "number" else ""
    return (f'<input type="{kind}" name="{name}" id="f_{name}" value="{r.esc(value)}"'
            f'{stp} aria-label="{r.esc(label)}" title="{r.esc(label)}"{req}{auto}{attrs}>')


def _leg_grid(rows, attrs: str = "") -> str:
    """Trades laid out as a broker shows them: one row per leg, one column
    per term. Fixed terms are grey text, open ones are inputs, and every form
    that records a trade uses the same columns so the eye never re-learns."""
    head = "".join(f"<span>{h}</span>" for h in LEG_COLUMNS)
    body = "".join(f'<div class="rg-row">{"".join(row)}</div>' for row in rows)
    return f'<div class="leg-grid"{attrs}><div class="rg-head">{head}</div>{body}</div>'


def _fixed_terms(position) -> list[str]:
    """Ticker, expiry, strike, right and quantity of an existing contract."""
    return [_fixed(r.esc(position.underlying)), _fixed(r.esc(position.expiry)),
            _fixed(r.esc(price(position.strike))), _fixed(r.esc(position.right.value.title())),
            _fixed(r.esc(position.quantity))]


def _closing_tag(position) -> str:
    short = position.direction is Direction.SHORT
    return (_tag("BTC", "Buy to close", "close") if short
            else _tag("STC", "Sell to close", "close"))


def _roll_grid(position, suggested: str, fee) -> str:
    """Two legs: close the current contract, open its successor."""
    short = position.direction is Direction.SHORT
    open_tag = (_tag("STO", "Sell to open", "open") if short
                else _tag("BTO", "Buy to open", "open"))
    closing = [_closing_tag(position), *_fixed_terms(position),
               _box("close_price", suggested, label="Buy-back price", autofocus=True),
               _box("close_fee", str(fee), label="Buy-back fee", required=False)]
    opening = [open_tag, _fixed(r.esc(position.underlying)),
               _box("new_expiry", _next_friday(position.expiry).isoformat(),
                    kind="date", label="New expiry"),
               _box("new_strike", str(position.strike), step="0.5", label="New strike"),
               _fixed(r.esc(position.right.value.title())),
               _box("new_quantity", str(position.quantity), step="1", label="Contracts",
                    required=False, attrs=' min="1"'),
               _box("new_price", "", label="New premium"),
               _box("new_fee", str(fee), label="New fee", required=False)]
    return _leg_grid([closing, opening])


def _action_panels(position, which: str, token: str, carry, back: str, *,
                   box_id: str = "actions", tab_href=None, dismiss_href: str = "",
                   dismiss_attrs: str = " data-form-close", panel_close: bool = True) -> str:
    """Tabs plus every action form, only the chosen one shown.

    All five forms are in the page so switching is a toggle with nothing to
    fetch. Without script the tab links still work. On a position's own page
    each panel carries its own x; inside the positions table the row's header
    has one, and dismissing goes through the table swap instead.
    """
    tab_href = tab_href or (lambda key: f"{back}?do={key}#{box_id}")
    dismiss_href = dismiss_href or f"{back}#{box_id}"
    links, panels = [], []
    for key, label in ACTIONS:
        cls = f' class="tab-{key}{" here" if which == key else ""}"'
        links.append(f'<a href="{tab_href(key)}" data-form-tab="{key}"{cls}>{label}</a>')
        shown = "" if which == key else " hidden"
        close = ""
        if panel_close:
            close = (f'<a class="close-form" href="{dismiss_href}" data-form-close '
                     'title="Close this form" aria-label="Close this form">&times;</a>')
        panels.append(f'<div data-form="{key}"{shown}>{close}'
                      + _action_form(position, key, token, carry, back,
                                     cancel=dismiss_href, cancel_attrs=dismiss_attrs)
                      + "</div>")
    return (f'<div class="tabs even" data-tabs-for="{box_id}">' + " ".join(links) + "</div>"
            + f'<div id="{box_id}" class="form-box">' + "".join(panels) + "</div>")


# ---------------------------------------------------------------------------
# action endpoints


def _roll_preview_placeholder() -> str:
    return '<p class="hint">The chain before and after appears here as you type.</p>'


def _roll_preview_html(pv) -> str:
    """The decision panel: before and after, from the engine's own roll."""
    def was(before, after, better) -> str:
        """The change, coloured by whether it is good news. ``better`` says
        which direction is good for this figure; None means neither."""
        if before is None:
            return ""
        delta = q2(after - before)
        if delta == 0:
            return f"<small>unchanged from {fmt(before)}</small>"
        cls = "dim" if better is None else ("pos" if (delta > 0) == better else "neg")
        arrow = "&uarr;" if delta > 0 else "&darr;"
        return (f'<small class="{cls}">{arrow} {fmt(abs(delta))} '
                f"<span class=\"dim\">was {fmt(before)}</span></small>")

    def signed(value) -> str:
        return r.money(value, dash="0.00")

    roll_cls = "pos" if pv.is_credit else "neg"
    roll_word = "credit" if pv.is_credit else "debit"
    tgt = pv.target_after
    target_cell = (f'<span class="pos">{r.esc(price(tgt.price))}</span> <small>expected '
                   f"{fmt(tgt.expected_pl)}</small>" if tgt.applicable
                   else '<span class="neg">none: net debit</span>')
    # For a short put a lower break-even is better; for a short call, higher.
    be_better_up = pv.new_leg.right is Right.CALL
    be_after = pv.break_even_after.price if pv.break_even_after else None
    be_before = pv.break_even_before.price if pv.break_even_before else None
    be_cls = "dim"
    if be_after is not None and be_before is not None and be_after != be_before:
        be_cls = "pos" if (be_after > be_before) == be_better_up else "neg"
    cells = [
        ("This roll", f'<span class="{roll_cls}">{roll_word} {fmt(abs(pv.this_roll))}</span>'),
        ("Closing this leg books", signed(pv.closing_realized)),
        ("Chain carry after", signed(pv.carry_after) + was(pv.carry_before, pv.carry_after, True)),
        ("Net credit after", signed(pv.net_credit_after)
                             + was(pv.net_credit_before, pv.net_credit_after, True)),
        ("Break-even after", (f'<span class="{be_cls}">{fmt(be_after)}</span>' if be_after is not None
                              else '<span class="dim">-</span>')
                             + (was(be_before, be_after, be_better_up)
                                if be_after is not None and be_before is not None else "")),
        ("Capital at risk after", (f'<span class="{"neg" if pv.at_risk_before is not None and pv.at_risk_after is not None and pv.at_risk_after > pv.at_risk_before else "dim"}">'
                                   f"{fmt(pv.at_risk_after) if pv.at_risk_after is not None else '-'}</span>"
                                   + (was(pv.at_risk_before, pv.at_risk_after, False)
                                      if pv.at_risk_before is not None and pv.at_risk_after is not None else ""))),
        ("Target on the new leg", target_cell),
        ("Credit still to recover", (f'<span class="neg">{fmt(pv.to_recover_after)}</span>'
                                     if pv.underwater_after else '<span class="pos">0.00</span>')),
        ("New leg", f"{pv.dte_after} DTE"),
    ]
    flags = []
    if pv.grows:
        old, new = pv.closing_leg.quantity, pv.new_leg.quantity
        flags.append(f"<b>Size grows {old} &rarr; {new} contracts (x{new / old:.2f}).</b> "
                     "This is how a position quietly grows several-fold, one roll "
                     "at a time.")
    if pv.strike_change:
        flags.append(f"Strike {r.esc(price(pv.closing_leg.strike))} &rarr; "
                     f"{r.esc(price(pv.new_leg.strike))} "
                     f"({'down' if pv.strike_change < 0 else 'up'} "
                     f"{r.esc(price(abs(pv.strike_change)))}).")
    if pv.underwater_after:
        flags.append(f"The chain stays under water by <b>{fmt(pv.to_recover_after)}</b> "
                     "after this roll: that much credit is still owed before it "
                     "nets positive.")
    flag_html = "".join(f'<p class="callout">{f}</p>' for f in flags)
    return ('<div class="totals">' + "".join(
        f"<div><span>{r.esc(k)}</span><b>{v}</b></div>" for k, v in cells
    ) + "</div>" + flag_html)


def roll_preview_fragment(conn, position_id, query) -> tuple[int, str]:
    """What the roll form shows as it is typed. Never an error: while a field
    is still blank the panel just says what it is waiting for."""
    position = store.load_position(conn, position_id)
    if position is None or not position.is_open:
        return 200, '<p class="hint">This position is no longer open.</p>'
    try:
        pv = decide.roll_preview(
            store.load_positions(conn), position,
            close_price=_decimal(query, "close_price", label="Buy-back price"),
            new_expiry=_date(query, "new_expiry", label="New expiry"),
            new_strike=_decimal(query, "new_strike", label="New strike"),
            new_price=_decimal(query, "new_price", label="New premium"),
            on=_date(query, "on", date.today()),
            close_fee=_decimal(query, "close_fee", ZERO),
            new_fee=_decimal(query, "new_fee", ZERO),
            new_quantity=_int(query, "new_quantity", position.quantity),
        )
    except (BadRequest, actions.ActionError, ValueError):
        return 200, _roll_preview_placeholder()
    return 200, _roll_preview_html(pv)


def risk_page(conn, query) -> tuple[int, str]:
    """Where the capital sits and what each expiry could demand.

    Everything here is worst case by construction: with no market data the app
    cannot know what will be assigned, so it shows what would be owed if
    everything at or below strike were.
    """
    positions = store.load_positions(conn)
    index = ChainIndex(positions)
    lots = store.load_lots(conn)
    disposals = store.load_disposals(conn)
    tickers = wheels.by_ticker(index, positions, lots, disposals, store.matching_rule(conn))
    shares_at_cost = {t.underlying: t.held_cost for t in tickers if not t.error}
    blocked = [t.underlying for t in tickers if t.error]

    risks = [t for t in decide.concentration(positions, shares_at_cost)
             if t.open_positions or t.shares_at_cost or t.naked_calls]
    total = decide.total_exposure(risks)
    options_at_risk = q2(sum((t.at_risk for t in risks), ZERO))
    shares_total = q2(sum((t.shares_at_cost for t in risks), ZERO))
    naked = sum(t.naked_calls for t in risks)

    def pct(part) -> Decimal | None:
        if total <= 0:
            return None
        return (part / total * 100).quantize(Decimal("0.1"))

    cells = [
        ("Total exposure", r.money(total)),
        ("Options at risk", r.money(options_at_risk)),
        ("Shares at cost", r.money(shares_total)),
    ]
    if risks and total > 0:
        top = risks[0]
        cells.append(("Largest", f"{r.esc(top.underlying)} <small>{pct(top.exposure)}%</small>"))
    if naked:
        cells.append(("Naked calls", f'<span class="neg">{naked}</span>'))
    totals = '<div class="totals">' + "".join(
        f"<div><span>{r.esc(k)}</span><b>{v}</b></div>" for k, v in cells) + "</div>"

    conc_rows = []
    for t in risks:
        share = pct(t.exposure)
        bar = ("" if share is None else
               f'<span class="share"><span class="bar" style="width:{min(share, 100)}%"></span>'
               f"{share}%</span>")
        name = f'<a href="/shares/{r.esc(t.underlying)}">{r.esc(t.underlying)}</a>'
        if t.naked_calls:
            name += (f' <span class="badge st-blocked" title="short calls with no shares '
                     f'behind them: no ceiling on the risk">{t.naked_calls} naked</span>')
        if t.underlying in blocked:
            name += ' <span class="badge st-blocked" title="share matching blocked">shares?</span>'
        conc_rows.append([
            name, bar, r.money(t.exposure), r.money(t.at_risk), r.money(t.shares_at_cost),
            r.money(t.open_premium), r.money(t.carry, dash="0.00"), r.money(t.realized),
            r.esc(t.open_positions),
        ])

    days = decide.obligations(positions)
    cal_rows = []
    running = ZERO
    for day in days:
        running = q2(running + day.cash_if_assigned)
        items = "<br>".join(
            f'<a href="/position/{r.esc(o.position.id)}">{r.contract(o.position)}</a>'
            + (f' <small>break-even {fmt(o.break_even.price)}</small>' if o.break_even else "")
            for o in day.items
        )
        cal_rows.append([
            r.esc(day.expiry), r.dte_cell(day.dte),
            r.money(day.cash_if_assigned, dash="-"), r.money(running),
            r.esc(day.shares_to_deliver) if day.shares_to_deliver else '<span class="dim">-</span>',
            items,
        ])

    body = f"""{totals}
<h2>Concentration</h2>
{r.table(["Ticker", "Share", "Exposure", "Options at risk", "Shares at cost",
          "Open premium", "Chain carry", "Ticker realized", "Open"], conc_rows, cls="risk")}
<p class="hint">Exposure is options at risk plus shares held at cost. A covered
call rides on its shares and is not counted twice; a naked call has no ceiling
and is flagged rather than summed. <b>Ticker realized</b> is every leg ever
closed under that ticker, rolled legs included, while <b>chain carry</b> is what
the open legs still carry forward: both are right, at different scopes.</p>
<h2>Obligation calendar</h2>
{r.table(["Expiry", "DTE", "Cash if puts assigned", "Cumulative", "Shares to deliver",
          "Positions"], cal_rows, cls="calendar")}
<p class="hint">Worst case by construction: the cash needed if every short put
expiring that day were assigned, and the shares every short call would have to
deliver. Cumulative is the cash needed if every expiry through that date went
that way.</p>"""
    return 200, r.page("Risk", body, nav_here="risk")


# ---------------------------------------------------------------------------
# filtering and saved views

FILTER_KEYS = ("q", "right", "side", "tag", "since", "until")


def _filter_qs(query) -> str:
    """The filter part of a query string, in a fixed order."""
    parts = [(k, (query.get(k) or [""])[0].strip()) for k in FILTER_KEYS]
    return urllib.parse.urlencode([(k, v) for k, v in parts if v])


def _apply_filters(listed, query):
    q = (query.get("q") or [""])[0].strip().casefold()
    right = (query.get("right") or [""])[0].strip().upper()
    side = (query.get("side") or [""])[0].strip().upper()
    tag = (query.get("tag") or [""])[0].strip().casefold()
    since = (query.get("since") or [""])[0].strip()
    until = (query.get("until") or [""])[0].strip()
    out = []
    for p in listed:
        if q and q not in p.underlying.casefold() and q not in p.notes.casefold() \
                and not any(q in t.casefold() for t in p.tags):
            continue
        if right and p.right.value != right:
            continue
        if side and p.direction.value != side:
            continue
        if tag and tag not in {t.casefold() for t in p.tags}:
            continue
        if since and p.opened_on.isoformat() < since:
            continue
        if until and p.opened_on.isoformat() > until:
            continue
        out.append(p)
    return out


def _filter_bar(conn, query, show: str, token: str) -> str:
    get = lambda k: (query.get(k) or [""])[0]  # noqa: E731
    tags = store.all_tags(conn)
    tag_options = [("", "any tag")] + [(t, t) for t in tags]
    fields = "".join([
        r.hidden("show", show),
        r.field("Find", "q", get("q"), attrs=' placeholder="ticker, note or tag"'),
        r.select("Right", "right", [("", "any"), ("PUT", "Put"), ("CALL", "Call")], get("right")),
        r.select("Side", "side", [("", "any"), ("SHORT", "Short"), ("LONG", "Long")], get("side")),
        r.select("Tag", "tag", tag_options, get("tag")) if tags else "",
        r.field("Opened from", "since", get("since"), kind="date"),
        r.field("to", "until", get("until"), kind="date"),
    ])
    active = _filter_qs(query)
    clear = f' <a class="btn cancel" href="/?show={show}">Clear</a>' if active else ""
    bar = (f'<form class="filters" method="get" action="/">{fields}'
           f'<div class="actions"><button type="submit">Filter</button>{clear}</div></form>')

    chips = []
    for v in store.load_views(conn):
        current = " here" if v["filter"] == f"show={show}" + (f"&{active}" if active else "") else ""
        chips.append(
            f'<span class="chip{current}"><a href="/?{r.esc(v["filter"])}">{r.esc(v["name"])}</a>'
            + r.form(f"/views/{r.esc(v['id'])}/delete", r.hidden("next", f"/?show={show}"),
                     token, submit="\u00d7", cls="inline") + "</span>")
    save = ""
    if active:
        save = r.form("/views/save",
                      r.hidden("next", f"/?show={show}&{active}")
                      + r.hidden("filter", f"show={show}&{active}")
                      + r.field("Save this view as", "name", "", required=True,
                                attrs=' placeholder="a name"'),
                      token, submit="Save view", cls="inline save-view")
    views = (f'<div class="chips">{"".join(chips)}{save}</div>' if chips or save else "")
    return bar + views


def do_save_view(conn, form) -> None:
    filter_ = _one(form, "filter")
    if not filter_.startswith("show="):
        raise BadRequest("that is not a view")
    try:
        store.save_view(conn, _one(form, "name"), filter_)
    except ValueError as exc:
        raise BadRequest(str(exc)) from None
    raise Redirect(_next(form, "/"), "View saved")


def do_delete_view(conn, view_id, form) -> None:
    store.delete_view(conn, view_id)
    raise Redirect(_next(form, "/"), "View removed")


def do_notes(conn, position_id, form) -> None:
    try:
        store.update_notes(conn, position_id, _one(form, "notes"),
                           store.normalize_tags(_one(form, "tags")))
    except ValueError as exc:
        raise BadRequest(str(exc)) from None
    raise Redirect(_next(form, f"/position/{position_id}"), "Notes saved")


# ---------------------------------------------------------------------------
# reports and exports


def reports_page(conn, query) -> tuple[int, str]:
    positions = store.load_positions(conn)
    index = ChainIndex(positions)
    lots = store.load_lots(conn)
    disposals = store.load_disposals(conn)
    rule = store.matching_rule(conn)
    events = reports.share_events(lots, disposals, rule)
    tickers = wheels.by_ticker(index, positions, lots, disposals, rule)

    by = (query.get("by") or ["month"])[0]
    if by not in reports.GRANULARITIES:
        by = "month"

    open_ones = [p for p in positions if p.is_open]
    puts = sum(1 for p in open_ones if p.right is Right.PUT)
    calls = sum(1 for p in open_ones if p.right is Right.CALL)
    held = sum(t.held for t in tickers if not t.error)
    counts = '<div class="totals">' + "".join(
        f"<div><span>{r.esc(k)}</span><b>{v}</b></div>" for k, v in (
            ("Open puts", puts), ("Open calls", calls), ("Shares held", held),
            ("Tickers with shares", sum(1 for t in tickers if t.held > 0)),
        )) + "</div>"

    tabs = " ".join(
        f'<a href="/reports?by={g}" class="{"here" if by == g else ""}">{g.title()}</a>'
        for g in reports.GRANULARITIES)
    period_rows = [[r.esc(row.key), r.money(row.options), r.money(row.shares),
                    f"<b>{r.money(row.total)}</b>", r.esc(row.legs), r.money(row.running)]
                   for row in reversed(reports.by_period(positions, events, by))]

    ticker_rows = [[f'<a href="/shares/{r.esc(t.underlying)}">{r.esc(t.underlying)}</a>',
                    r.money(t.options), r.money(t.shares), f"<b>{r.money(t.total)}</b>",
                    r.money(t.open_premium), r.money(t.at_risk), r.esc(t.open_count),
                    r.esc(t.closed_legs)]
                   for t in reports.by_ticker(positions, events)]

    def outcome_rows(groups):
        rows = []
        total = reports.overall(groups)
        for o in list(groups) + ([total] if total and len(groups) > 1 else []):
            rows.append([
                f"<b>{r.esc(o.label)}</b>" if o.label == "All" else r.esc(o.label),
                r.esc(o.legs), r.esc(o.closed), r.esc(o.rolled), r.esc(o.expired), r.esc(o.assigned),
                r.num(o.hit_rate, "-") + ("%" if o.hit_rate is not None else ""),
                r.num(o.win_rate, "-") + ("%" if o.win_rate is not None else ""),
                r.num(o.capture, "-") + ("%" if o.capture is not None else ""),
                r.money(o.avg_credit), r.num(o.avg_days, "-"), r.money(o.realized),
            ])
        return rows
    outcome_head = ["", "Legs", "Closed", "Rolled", "Expired", "Assigned",
                    ("Hit", "Closed at or past the target, or expired worthless"),
                    ("Win", "Realized above zero"),
                    ("Capture", "Realized as a share of premium taken in"),
                    "Avg credit", "Avg days", "Realized"]
    by_ticker_outcomes = reports.target_performance(positions, index=index)
    by_dte_outcomes = reports.target_performance(positions, key=reports.dte_bucket, index=index)

    body = f"""{_portfolio_totals(conn, positions, index)}
{counts}
<h2>Realized by period</h2>
<div class="tabs">{tabs}</div>
{r.table(["Period", "Options", "Shares", "Total", "Legs closed", "Running total"], period_rows)}
<p class="hint">Options are booked on the leg's close date, shares on the sale's
date. Legs still open are not here, at any value.</p>
<h2>By ticker</h2>
{r.table(["Ticker", "Options", "Shares", "Total", "Open premium", "At risk", "Open", "Legs closed"],
         ticker_rows)}
<h2>How short legs ended</h2>
<h3>By ticker</h3>
{r.table(outcome_head, outcome_rows(by_ticker_outcomes))}
<h3>By days to expiry when opened</h3>
{r.table(outcome_head, outcome_rows(by_dte_outcomes))}
<p class="hint">Hit means the leg ended at or past its target: closed at or
under the target price, or expired worthless. A roll or an assignment is
neither a hit nor a miss on its own; what the chain finally does is.</p>
<h2>Export</h2>
<p><a href="/export/positions.csv">positions.csv</a> &middot;
<a href="/export/shares.csv">shares.csv</a> &middot;
<a href="/export/journal.json">journal.json</a> &middot;
<a href="/export/journal.db">journal.db</a> (full SQLite backup)</p>
<p class="hint">The CSV files carry the computed columns too: realized, carry,
break-even, capital at risk. The .db file is the whole journal; copy it
somewhere safe.</p>"""
    return 200, r.page("Reports", body, nav_here="reports")


def _plain(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    if hasattr(value, "value"):
        return value.value
    if isinstance(value, tuple):
        return list(value)
    return value


def export_file(conn, name: str):
    positions = store.load_positions(conn)
    lots = store.load_lots(conn)
    disposals = store.load_disposals(conn)

    if name == "positions.csv":
        index = ChainIndex(positions)
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["id", "underlying", "expiry", "strike", "right", "direction", "quantity",
                    "multiplier", "opened_on", "open_price", "open_fee", "closed_on",
                    "close_price", "close_fee", "status", "rolled_from_id", "split_from_id",
                    "share_lot_id", "tags", "notes", "open_cash", "realized", "carry",
                    "chain_root", "break_even", "capital_at_risk"])
        for p in positions:
            chain = index.chain(p)
            be = break_even(chain) if p.is_open else None
            w.writerow([p.id, p.underlying, p.expiry, p.strike, p.right.value, p.direction.value,
                        p.quantity, p.multiplier, p.opened_on, p.open_price, p.open_fee,
                        p.closed_on or "", p.close_price if p.close_price is not None else "",
                        p.close_fee, p.status.value, p.rolled_from_id or "", p.split_from_id or "",
                        p.share_lot_id or "", " ".join(p.tags), p.notes, open_cash(p),
                        realized_pl(p), index.carry(p), chain.root.id,
                        be.price if be else "", capital_at_risk(p) if p.is_open else ""])
        return 200, out.getvalue(), "text/csv; charset=utf-8", name

    if name == "shares.csv":
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["kind", "id", "underlying", "date", "quantity", "price_per_share", "fee",
                    "source_or_kind", "position_id", "specific_lot_ids", "estimated", "notes"])
        for l in lots:
            w.writerow(["lot", l.id, l.underlying, l.acquired_on, l.quantity, l.cost_per_share,
                        l.fee, l.source.value, l.assigning_position_id or "", "",
                        "yes" if l.estimated else "", l.notes])
        for d in disposals:
            w.writerow(["disposal", d.id, d.underlying, d.disposed_on, d.quantity,
                        d.proceeds_per_share, d.fee, d.kind.value, d.disposing_position_id or "",
                        " ".join(d.specific_lot_ids), "", d.notes])
        return 200, out.getvalue(), "text/csv; charset=utf-8", name

    if name == "journal.json":
        payload = {
            "positions": [{k: _plain(v) for k, v in asdict(p).items()} for p in positions],
            "lots": [{k: _plain(v) for k, v in asdict(l).items()} for l in lots],
            "disposals": [{k: _plain(v) for k, v in asdict(d).items()} for d in disposals],
        }
        return 200, json.dumps(payload, indent=1), "application/json", name

    if name == "journal.db":
        # A consistent copy through SQLite's own backup API, never a raw file
        # read that could catch a write half way.
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            target = sqlite3.connect(path)
            try:
                conn.backup(target)
            finally:
                target.close()
            with open(path, "rb") as f:
                data = f.read()
        finally:
            os.unlink(path)
        return 200, data, "application/vnd.sqlite3", name

    raise BadRequest("no such export")


def _past(on: date, label: str) -> date:
    """The journal records what has happened. A future date is a typo or a
    form default gone wrong, and a lot or a close dated ahead of today makes
    every figure that depends on it wrong for weeks. Refused, every time."""
    if on > date.today():
        raise BadRequest(
            f"{label} {on} is in the future. The journal records what has "
            "happened; date it today or earlier."
        )
    return on


def _load_open(conn, position_id) -> Position:
    position = store.load_position(conn, position_id)
    if position is None:
        raise BadRequest("no such position")
    return position


def do_close(conn, position_id, form) -> None:
    position = _load_open(conn, position_id)
    result = actions.close(
        position,
        _past(_date(form, "closed_on", date.today()), "Closed on"),
        _decimal(form, "close_price", label="Close price"),
        _decimal(form, "close_fee", ZERO),
    )
    store.apply(conn, result)
    pl = realized_pl(result.updated[0])
    raise Redirect(_next(form, f"/position/{position_id}"),
                   f"Closed {r.contract_text(position)} - realized {fmt(pl)}")


def do_expire(conn, position_id, form) -> None:
    position = _load_open(conn, position_id)
    result = actions.expire(position, _past(_date(form, "on", position.expiry), "Expired on"))
    store.apply(conn, result)
    pl = realized_pl(result.updated[0])
    raise Redirect(_next(form, f"/position/{position_id}"),
                   f"{r.contract_text(position)} expired worthless - kept {fmt(pl)}")


def do_assign(conn, position_id, form) -> None:
    position = _load_open(conn, position_id)
    on = _past(_date(form, "on", position.expiry), "Assigned on")
    result = actions.assign(
        position,
        on=on,
        close_fee=_decimal(form, "close_fee", ZERO),
        share_fee=_decimal(form, "share_fee", ZERO),
    )
    errors = store.apply(conn, result)
    note = f"{r.contract_text(position)} {result.summary}"
    if errors:
        note = "!" + note + " - share matching blocked: " + \
               "; ".join(errors.values())
    raise Redirect(_next(form, f"/position/{position_id}"), note)


def do_roll(conn, position_id, form) -> None:
    position = _load_open(conn, position_id)
    result = actions.roll(
        position,
        close_price=_decimal(form, "close_price", label="Buy-back price"),
        new_expiry=_date(form, "new_expiry", label="New expiry"),
        new_strike=_decimal(form, "new_strike", label="New strike"),
        new_price=_decimal(form, "new_price", label="New premium"),
        on=_past(_date(form, "on", date.today()), "Rolled on"),
        close_fee=_decimal(form, "close_fee", ZERO),
        new_fee=_decimal(form, "new_fee", ZERO),
        new_quantity=_int(form, "new_quantity", position.quantity),
    )
    successor = result.created[0]
    problems = validate.validate_position(
        successor, store.load_positions(conn), _fee_rate(conn)
    )
    if validate.errors(problems):
        back = _next(form, f"/position/{position_id}")
        joiner = "&" if "?" in back else "?"
        raise Redirect(
            f"{back}{joiner}act={position_id}&do=roll" if back.startswith("/?")
            else f"/position/{position_id}?do=roll",
            "!Roll refused: " + "; ".join(
                p.message for p in validate.errors(problems)
            ),
        )

    store.apply(conn, result)
    index = ChainIndex(store.load_positions(conn))
    chain = index.chain(store.load_position(conn, successor.id))
    raise Redirect(
        _next(form, f"/position/{successor.id}"),
        f"{r.contract_text(position)} {result.summary}. Chain now carries "
        f"{fmt(chain.carry)}, net credit {fmt(chain.net_credit)}",
    )


def do_split(conn, position_id, form) -> None:
    position = _load_open(conn, position_id)
    result = actions.split(
        position,
        _int(form, "quantity", label="Contracts to peel off"),
        on=_past(_date(form, "on", date.today()), "Split on"),
    )
    store.apply(conn, result)
    first, second = result.created
    raise Redirect(
        _next(form, f"/position/{first.id}"),
        f"{r.contract_text(position)} {result.summary}. Both halves are open"
        f" and can now take different paths",
    )


# ---------------------------------------------------------------------------
# expiry queue


def expiring_page(conn, query) -> tuple[int, str]:
    window = int((query.get("within") or ["0"])[0])
    positions = store.load_positions(conn)
    index = ChainIndex(positions)
    queue = validate.expiring(
        [p for p in positions if p.is_open], within_days=window
    )

    rows = []
    for position, days in queue:
        pid = r.esc(position.id)
        carry = index.carry(position)
        rows.append([
            f'<a href="/position/{pid}">{r.contract(position)}</a>',
            r.dte_cell(days),
            r.money(open_cash(position)),
            r.money(carry, dash="0.00"),
            r.money(capital_at_risk(position)),
            f'<a class="btn" href="/position/{pid}?do=expire">Expired</a> '
            f'<a class="btn" href="/position/{pid}?do=assign">Assigned</a> '
            f'<a class="btn" href="/position/{pid}?do=roll">Roll</a>',
        ])

    tabs = " ".join(
        f'<a href="/expiring?within={w}" class="{"here" if window == w else ""}">'
        f"{label}</a>"
        for w, label in ((0, "At or past expiry"), (7, "Next 7 days"),
                         (30, "Next 30 days"))
    )
    body = f"""<p class="hint">With no market data the app cannot know whether
an expired short option expired worthless or was assigned - so it asks. This
queue is where that gets settled.</p>
<div class="tabs">{tabs}</div>
{r.table(["Contract", "DTE", "Credit", "Carry", "At risk", "Outcome"], rows)}"""
    return 200, r.page("Expiring", body, nav_here="expiring")


# ---------------------------------------------------------------------------
# audit


def audit_page(conn, token, query) -> tuple[int, str]:
    entity = (query.get("entity") or [None])[0]
    entries = store.audit_entries(conn, limit=300, entity_id=entity)

    rows = []
    for e in entries:
        undo = ""
        if (e["entity_type"] in ("position", "share_lot", "share_disposal")
                and not e["action"].startswith("revert")):
            undo = r.form(f"/audit/{e['id']}/revert", "", token,
                          submit="Undo", cls="inline")
        rows.append([
            r.esc(e["at"].replace("T", " ")),
            r.esc(e["entity_type"]),
            (f'<a href="/position/{r.esc(e["entity_id"])}">'
             f'{r.esc(e["entity_id"][:8])}</a>'
             if e["entity_type"] == "position" else r.esc(e["entity_id"][:8])),
            r.esc(e["action"]),
            undo,
        ])

    scope = (f' for <a href="/position/{r.esc(entity)}">{r.esc(entity[:8])}</a>'
             if entity else "")
    body = f"""<p class="hint">Every change is recorded{scope}. Undo restores
the values a change replaced, and is itself recorded.</p>
{r.table(["When (UTC)", "Kind", "Entity", "What", ""], rows, cls="audit")}"""
    return 200, r.page("History", body, nav_here="audit")


def do_revert(conn, entry_id, form) -> None:
    try:
        outcome = store.revert_audit_entry(conn, int(entry_id))
    except ValueError as exc:
        raise Redirect("/audit", f"!{exc}") from None
    raise Redirect("/audit", f"Undone: {outcome}")


# ---------------------------------------------------------------------------
# shares


def _share_context(conn):
    positions = store.load_positions(conn)
    index = ChainIndex(positions)
    lots = store.load_lots(conn)
    disposals = store.load_disposals(conn)
    rule = store.matching_rule(conn)
    tickers = wheels.by_ticker(index, positions, lots, disposals, rule)
    views = [v for t in tickers for v in t.lots]
    suggestions = wheels.suggest_covers(index, positions, views)
    return positions, index, lots, disposals, tickers, suggestions


def _lot_label(lot, remaining=None) -> str:
    qty = lot.quantity if remaining is None else remaining
    tag = " (estimated)" if lot.estimated else ""
    held = "" if remaining is None or remaining == lot.quantity else f" of {lot.quantity}"
    return f"{qty}{held} {lot.underlying} @ {price(lot.cost_per_share)} from {lot.acquired_on}{tag}"


def _remaining_by_lot(conn, underlying: str) -> dict[str, int]:
    """Shares each lot of a ticker still holds under the account rule."""
    lots = [l for l in store.load_lots(conn) if l.underlying == underlying]
    disposals = [d for d in store.load_disposals(conn) if d.underlying == underlying]
    try:
        result = match_lots(lots, disposals, store.matching_rule(conn))
    except InsufficientSharesError:
        return {l.id: l.quantity for l in lots}
    return {state.lot.id: state.remaining for state in result.remaining}


def _share_form_body(conn, kind: str, underlying: str, token: str, back: str) -> str:
    """The buy / sell / buy-write form, for embedding on a page."""
    rate = _fee_rate(conn)
    common = [
        r.field("Ticker", "underlying", underlying, required=True, autofocus=not underlying,
                attrs=' list="tickers" autocapitalize="characters" autocomplete="off"'),
        r.field("Date", "on", r.today_iso(), kind="date", required=True),
    ]
    if kind == "sell":
        lots = [l for l in store.load_lots(conn) if not underlying or l.underlying == underlying]
        remaining = _remaining_by_lot(conn, underlying) if underlying else {}
        options = [("", "Account rule (FIFO)")]
        attrs = []
        for l in sorted(lots, key=lambda l: (l.underlying, l.acquired_on)):
            left = remaining.get(l.id, l.quantity)
            if left <= 0:
                continue          # nothing left to sell from it
            options.append((l.id, _lot_label(l, left)))
            attrs.append(f'"{r.esc(l.id)}":{left}')
        fields = common + [
            r.field("Shares", "quantity", "", kind="number", step="1", required=True,
                    attrs=' min="1"'),
            r.field("Price / share", "price", "", kind="number", step="0.01", required=True),
            r.field("Fee", "fee", "0.00", kind="number", step="0.01"),
            r.select("From lot", "lot_id", options, "",
                     hint="Leave on the account rule unless the broker matched a specific lot"),
            f'<script type="application/json" id="lot-remaining">{{{",".join(attrs)}}}</script>',
        ]
        action, submit = "/shares/sell", "Record sale"
    elif kind == "buy-write":
        fields = common + [
            r.field("Shares bought", "shares", "", kind="number", step="1", required=True),
            r.field("Share price", "share_price", "", kind="number", step="0.01", required=True),
            r.field("Share fee", "share_fee", "0.00", kind="number", step="0.01"),
            r.field("Call expiry", "expiry", _next_friday().isoformat(), kind="date", required=True),
            r.field("Call strike", "strike", "", kind="number", step="0.5", required=True),
            r.field("Call premium / share", "call_price", "", kind="number", step="0.01", required=True),
            r.field("Contracts", "contracts", "", kind="number", step="1",
                    hint="Defaults to shares / 100"),
            r.field("Option fee", "option_fee", str(rate), kind="number", step="0.01"),
        ]
        action, submit = "/shares/buy-write", "Record buy-write"
    else:
        fields = common + [
            r.field("Shares", "quantity", "", kind="number", step="1", required=True),
            r.field("Price / share", "price", "", kind="number", step="0.01", required=True),
            r.field("Fee", "fee", "0.00", kind="number", step="0.01"),
            r.field("Notes", "notes", ""),
        ]
        action, submit = "/shares/buy", "Record purchase"
    return r.form(action, r.hidden("next", back) + "".join(fields), token,
                  submit=submit, cls="grid", cancel=f"{back}#record",
                  cancel_attrs=" data-form-close")


def _share_forms(conn, kind: str, underlying: str, token: str, back: str,
                 base: str, param: str = "form") -> str:
    """Tab strip plus all three forms, only the chosen one shown.

    Every form is in the page, so switching is a toggle rather than a reload
    that lands the reader back at the top. Without script the links still
    work, and the fragment brings the reader back to the forms.
    """
    links, panels = [], []
    for key, label in (("buy", "Buy shares"), ("sell", "Sell shares"),
                       ("buy-write", "Buy-write")):
        cls = ' class="here"' if kind == key else ""
        links.append(f'<a href="{base}{param}={key}#record" data-form-tab="{key}"{cls}>'
                     f"{label}</a>")
        shown = "" if kind == key else " hidden"
        close = (f'<a class="close-form" href="{back}#record" data-form-close '
                 'title="Close this form" aria-label="Close this form">&times;</a>')
        panels.append(f'<div class="share-form" data-form="{key}"{shown}>{close}'
                      + _share_form_body(conn, key, underlying, token, back) + "</div>")
    return ('<div class="tabs" data-tabs-for="record">' + " ".join(links) + "</div>"
            + r.datalist("tickers", store.recent_underlyings(conn))
            + '<div id="record" class="form-box">' + "".join(panels) + "</div>")


def shares_page(conn, token, query) -> tuple[int, str]:
    positions, index, lots, disposals, tickers, suggestions = _share_context(conn)

    rows = []
    for t in tickers:
        name = r.esc(t.underlying)
        blended = t.blended
        if t.error and t.held < 0:
            state = (f'<span class="badge st-blocked" title="{r.esc(t.error)}">'
                     f"short {-t.held} shares</span>")
        elif t.error:
            state = (f'<span class="badge st-blocked" title="{r.esc(t.error)}">'
                     f"{t.held} held &middot; matching blocked</span>")
        elif t.held > 0:
            state = f'<span class="badge st-open">holding {t.held}</span>'
        else:
            state = '<span class="badge st-closed">flat</span>'
        rows.append([
            f'<a href="/shares/{name}"><b class="ticker">{name}</b></a>',
            state,
            r.money(t.held_cost if t.held > 0 else None),
            r.money(blended.after_calls if blended else None),
            r.money(blended.min_call_strike if blended else None),
            r.money(t.realized if not t.error else None),
            r.money(t.option_premium),
            r.money(t.total if not t.error else None),
            r.esc(len(t.lots)),
        ])

    realized = wheels.realized_shares(tickers)
    held_cost = q2(sum((t.held_cost for t in tickers if t.held > 0 and not t.error), ZERO))
    blocked = [t for t in tickers if t.error]
    totals = [
        ("Realized on shares", r.money(realized)),
        ("Cost of shares held", r.money(held_cost)),
        ("Tickers holding stock", r.esc(sum(1 for t in tickers if t.held > 0))),
    ]
    if blocked:
        totals.append(("Blocked tickers", f'<span class="neg">{len(blocked)}</span>'))

    warn = ""
    if blocked:
        warn = "".join(
            f'<div class="callout"><a href="/shares/{r.esc(t.underlying)}">'
            f"{r.esc(t.underlying)}</a>: {r.esc(t.error)}</div>" for t in blocked)
    hint = ""
    if suggestions:
        hint = (f'<div class="callout"><a href="/shares/covers">{len(suggestions)} '
                "short call(s) look like covered calls but are not linked to a lot"
                "</a> - link them so their premium counts toward the shares' basis.</div>")

    kind = (query.get("form") or [""])[0]
    body = f"""{warn}{hint}
<div class="totals">{"".join(f"<div><span>{r.esc(k)}</span><b>{v}</b></div>" for k, v in totals)}</div>
{_share_forms(conn, kind, "", token, "/shares", "/shares?")}
{r.table(["Ticker", "Position", "Held at cost", "Adj. basis", "Min call",
          "Share P/L", "Option premium", "Wheel total", "Lots"], rows, cls="shares")}
<p class="hint">Adjusted basis is what the shares held really cost after the
premium that acquired them and the calls written against them. Min call is the
lowest strike that does not lock in a loss. Wheel total is acquisition premium
plus call premium plus share P/L, realized only.</p>"""
    return 200, r.page("Shares", body, nav_here="shares")


def ticker_page(conn, underlying, token, query) -> tuple[int, str]:
    positions, index, lots, disposals, tickers, suggestions = _share_context(conn)
    name = underlying.upper()
    match_ = [t for t in tickers if t.underlying == name]
    if not match_:
        return 404, r.page("Not found", f"<p>No shares recorded for {r.esc(name)}.</p>")
    t = match_[0]
    by_id = {p.id: p for p in positions}

    facts = [
        ("Held", r.esc(t.held) if not t.error else f'<span class="neg">{t.held}</span>'),
        ("Cost of shares held", r.money(t.held_cost if t.held > 0 else None)),
        ("Share P/L realized", r.money(t.realized if not t.error else None)),
        ("Option premium on these lots", r.money(t.option_premium)),
        ("Wheel total", r.money(t.total if not t.error else None)),
    ]
    b = t.blended
    if b:
        facts += [("Adjusted basis", r.money(b.unit_price)),
                  ("After covered calls", r.money(b.after_calls)),
                  ("Min call strike", r.money(b.min_call_strike))]
    fact_html = "".join(f"<div><span>{r.esc(k)}</span><b>{v}</b></div>" for k, v in facts)

    warn = ""
    if t.error:
        warn = (f'<div class="callout">{r.esc(t.error)}<br>Fix it in the '
                f'<a href="/shares/{name}/data">raw data</a>: remove or re-record the '
                "sale, or record the missing purchase. Matching resumes once every "
                "sale can be matched.</div>")

    lot_rows = []
    today = date.today()
    for v in t.lots:
        lot = v.lot
        src = lot.source.value.replace("_", " ").lower()
        badge = f'<span class="badge st-{"open" if v.is_open else "closed"}">{r.esc(src)}</span>'
        if lot.estimated:
            badge += ' <span class="badge st-split" title="reconstructed from memory">estimated</span>'
        acq = "-"
        if v.acquisition is not None:
            head = v.acquisition.head
            acq = (f'<a href="/position/{r.esc(head.id)}">{r.money(v.acq_premium)}</a>')
        calls = r.money(v.cc_premium) if v.call_chains else '<span class="dim">-</span>'
        if v.call_chains:
            calls += f' <span class="dim">({len(v.call_chains)})</span>'
        cover = ""
        if v.is_open:
            cover = f"{v.covered}/{v.remaining}"
            if v.over_covered:
                cover = f'<span class="neg" title="more calls than shares">{cover}</span>'
            elif v.uncovered == 0 and v.covered:
                cover = f'<span class="pos">{cover}</span>'
        # A lot dated after today is almost always an assignment recorded on
        # the expiry date when it happened early. Flag it; the fix is in the
        # raw data, not among the figures.
        when = r.esc(lot.acquired_on)
        if lot.acquired_on > today:
            when = (f'<a class="neg" href="/shares/{name}/data" title="dated after '
                    f'today: an assignment recorded with the expiry date? Fix it in '
                    f'the raw data">{when}</a>')
        lot_rows.append([
            when,
            badge,
            r.esc(lot.quantity),
            r.esc(v.remaining) if v.is_open else '<span class="dim">0</span>',
            r.esc(price(lot.cost_per_share)),
            acq,
            calls,
            cover or '<span class="dim">-</span>',
            r.money(v.share_realized if v.disposed else None),
            r.money(v.basis.unit_price if v.basis.available else None),
            r.money(v.basis.after_calls if v.basis.available else None),
            r.money(v.total),
            r.esc(v.days),
        ])

    disp_rows = []
    for d in sorted(disposals, key=lambda d: d.disposed_on, reverse=True):
        if d.underlying != name:
            continue
        via = by_id.get(d.disposing_position_id) if d.disposing_position_id else None
        how = (f'<a href="/position/{r.esc(via.id)}">{r.contract(via)}</a>' if via
               else r.esc(d.kind.value.replace("_", " ").lower()))
        pinned = ""
        if d.specific_lot_ids:
            pinned = ' <span class="dim" title="pinned to a specific lot">&#128204;</span>'
        disp_rows.append([r.esc(d.disposed_on), r.esc(d.quantity),
                          r.esc(price(d.proceeds_per_share)), r.money(d.proceeds),
                          how + pinned])

    my_suggestions = {pid: lot_id for pid, lot_id in suggestions.items()
                      if by_id[pid].underlying == name}
    suggest_html = ""
    if my_suggestions:
        items = "".join(
            f"<li>{r.contract(by_id[pid])} &rarr; "
            f"{r.esc(_lot_label(next(l for l in lots if l.id == lot_id)))}"
            f'{r.form(f"/position/{r.esc(pid)}/cover", r.hidden("lot_id", lot_id) + r.hidden("next", f"/shares/{name}"), token, submit="Link", cls="inline")}</li>'
            for pid, lot_id in my_suggestions.items()
        )
        suggest_html = f"""<h2>Calls that look covered but are not linked</h2>
<p class="hint">Each was written while exactly one lot of {r.esc(name)} was held.
Linking it counts its premium toward that lot's basis.</p>
<ul class="suggest">{items}</ul>"""

    kind = (query.get("form") or [""])[0]
    forms = "<h2>Record</h2>" + _share_forms(conn, kind, name, token, f"/shares/{name}",
                                              f"/shares/{name}?")

    body = f"""{warn}
<div class="totals wide">{fact_html}</div>
<h2>Lots - {len(t.lots)}</h2>
{r.table(["Acquired", "Source", "Qty", "Held", "Cost/sh", "Acq. premium",
          "Call premium", "Covered", "Share P/L", "Adj. basis", "After calls",
          "Wheel", "Days"], lot_rows, cls="lots")}
<h2>Disposals - {len(disp_rows)}</h2>
{r.table(["Date", "Qty", "Price", "Proceeds", "Via"], disp_rows)}
{suggest_html}
{forms}
<p class="hint">Something entered wrong? <a href="/shares/{name}/data">Fix the raw
data</a> - remove or re-date a record there.</p>"""
    return 200, r.page(f"{name} shares", body, nav_here="shares")


def ticker_data_page(conn, underlying, token, query) -> tuple[int, str]:
    """The raw share records of one ticker, with the repairs.

    Removing or re-dating a record is not a daily action; it is fixing data
    that was entered wrong. So it lives here, one link from the ticker page,
    rather than beside the figures where it would invite misuse.
    """
    name = underlying.upper()
    lots = sorted(store.load_lots(conn, name), key=lambda l: (l.acquired_on, l.id))
    disposals = sorted(store.load_disposals(conn, name),
                       key=lambda d: (d.disposed_on, d.id))
    by_id = {p.id: p for p in store.load_positions(conn, name)}
    back = f"/shares/{name}/data"
    today = date.today()

    warn = ""
    try:
        match_lots(lots, disposals, store.matching_rule(conn))
    except InsufficientSharesError as exc:
        warn = f'<div class="callout">{r.esc(exc)}</div>'

    linked = {p.share_lot_id for p in by_id.values() if p.share_lot_id}
    pinned_to = {lot_id for d in disposals for lot_id in d.specific_lot_ids}

    def via(pid):
        p = by_id.get(pid) if pid else None
        return (f'<a href="/position/{r.esc(p.id)}">{r.contract(p)}</a>' if p
                else '<span class="dim">-</span>')

    lot_rows = []
    for lot in lots:
        redate = r.form(
            f"/shares/lot/{r.esc(lot.id)}/date",
            r.hidden("next", back)
            + f'<input type="date" name="on" value="{min(lot.acquired_on, today)}"'
            ' required aria-label="Acquired on">',
            token, submit="re-date", cls="inline")
        when = r.esc(lot.acquired_on)
        if lot.acquired_on > today:
            when = f'<span class="neg" title="dated after today">{when}</span>'
        if lot.id in linked:
            remove = '<span class="dim" title="calls are written against it">in use</span>'
        elif lot.id in pinned_to:
            remove = '<span class="dim" title="a sale is pinned to it">in use</span>'
        else:
            remove = r.form(f"/shares/lot/{r.esc(lot.id)}/delete", r.hidden("next", back),
                            token, submit="remove", cls="inline")
        flags = " ".join(f for f in (
            "estimated" if lot.estimated else "", r.esc(lot.notes)) if f)
        lot_rows.append([
            f"{when} {redate}", r.esc(lot.source.value.replace("_", " ").lower()),
            r.esc(lot.quantity), r.esc(price(lot.cost_per_share)), r.money(lot.fee),
            via(lot.assigning_position_id), flags or '<span class="dim">-</span>', remove,
        ])

    disp_rows = []
    for d in disposals:
        lot_text = "account rule"
        if d.specific_lot_ids:
            names = [_lot_label(l) for l in lots if l.id in d.specific_lot_ids]
            lot_text = "&#128204; " + r.esc("; ".join(names) or "a lot no longer present")
        disp_rows.append([
            r.esc(d.disposed_on), r.esc(d.kind.value.replace("_", " ").lower()),
            r.esc(d.quantity), r.esc(price(d.proceeds_per_share)), r.money(d.fee),
            via(d.disposing_position_id), lot_text,
            r.form(f"/shares/disposal/{r.esc(d.id)}/delete", r.hidden("next", back),
                   token, submit="remove", cls="inline"),
        ])

    body = f"""<p class="hint"><a href="/shares/{name}">&larr; {name} shares</a></p>
{warn}
<p class="hint">These are the records as entered. Remove one that is wrong, or move
a lot's date; its assignment moves with it. Every change is logged and can be
undone from <a href="/audit">History</a>.</p>
<h2>Lots - {len(lot_rows)}</h2>
{r.table(["Acquired", "Source", "Qty", "Cost/sh", "Fee", "From", "Flags", ""], lot_rows)}
<h2>Disposals - {len(disp_rows)}</h2>
{r.table(["Date", "Kind", "Qty", "Price", "Fee", "Via", "Lot", ""], disp_rows)}"""
    return 200, r.page(f"{name} raw data", body, nav_here="shares")


def covers_page(conn, token, query) -> tuple[int, str]:
    positions, index, lots, disposals, tickers, suggestions = _share_context(conn)
    by_id = {p.id: p for p in positions}
    lot_by_id = {l.id: l for l in lots}
    rows = []
    for pid, lot_id in sorted(suggestions.items(), key=lambda kv: by_id[kv[0]].opened_on):
        p = by_id[pid]
        rows.append([
            f'<a href="/position/{r.esc(pid)}">{r.contract(p)}</a>',
            r.esc(p.opened_on),
            r.status_badge(p.status),
            r.esc(_lot_label(lot_by_id[lot_id])),
            r.form(f"/position/{r.esc(pid)}/cover",
                   r.hidden("lot_id", lot_id) + r.hidden("next", "/shares/covers"),
                   token, submit="Link", cls="inline"),
        ])
    apply_all = ""
    if suggestions:
        apply_all = r.form("/shares/covers/apply", r.hidden("next", "/shares/covers"),
                           token, submit=f"Link all {len(suggestions)}", cls="inline")
    body = f"""<p class="hint">Short calls written while exactly one lot of the same
ticker was held, and not yet linked to it. Imported history has no links at
all, so this is how the calls written against your shares get their premium
counted toward the shares' basis. Where several lots could fit, nothing is
proposed - guessing would misplace premium.</p>
{apply_all}
{r.table(["Call", "Opened", "Status", "Proposed lot", ""], rows)}"""
    return 200, r.page("Unlinked covered calls", body, nav_here="shares")


def do_cover(conn, position_id, form) -> None:
    position = store.load_position(conn, position_id)
    if position is None:
        raise BadRequest("no such position")
    lot_id = _one(form, "lot_id")
    lots = {l.id: l for l in store.load_lots(conn)}
    if lot_id and lot_id not in lots:
        raise BadRequest("no such lot")
    if lot_id and lots[lot_id].underlying != position.underlying:
        raise BadRequest(f"that lot is {lots[lot_id].underlying}, not {position.underlying}")
    if position.right is not Right.CALL:
        raise BadRequest("only a call can be covered by shares")

    from dataclasses import replace
    updated = replace(position, share_lot_id=lot_id or None)
    what = "linked to lot" if lot_id else "unlinked from its lot"
    store.apply(conn, actions.ActionResult(updated=[updated]), f"call {what}")
    raise Redirect(_next(form, f"/position/{position_id}"),
                   f"{r.contract_text(position)} {what}")


def do_cover_all(conn, form) -> None:
    positions, index, lots, disposals, tickers, suggestions = _share_context(conn)
    by_id = {p.id: p for p in positions}
    from dataclasses import replace
    updated = [replace(by_id[pid], share_lot_id=lot_id) for pid, lot_id in suggestions.items()]
    if updated:
        store.apply(conn, actions.ActionResult(updated=updated),
                    f"linked {len(updated)} covered call(s) to their lots")
    raise Redirect(_next(form, "/shares"), f"Linked {len(updated)} call(s)")


def share_form(conn, token, query) -> tuple[int, str]:
    kind = (query.get("kind") or ["buy"])[0]
    underlying = (query.get("underlying") or [""])[0].upper()
    back = f"/shares/{underlying}" if underlying else "/shares"
    body = _share_forms(conn, kind, underlying, token, back,
                        f"/shares/new?underlying={underlying}&", param="kind")
    return 200, r.page({"sell": "Sell shares", "buy-write": "Buy-write"}.get(kind, "Buy shares"),
                       body, nav_here="shares")


def _check_not_short(conn, underlying: str, selling: int) -> None:
    lots = [l for l in store.load_lots(conn) if l.underlying == underlying]
    disposals = [d for d in store.load_disposals(conn) if d.underlying == underlying]
    held = sum(l.quantity for l in lots) - sum(d.quantity for d in disposals)
    if selling > held:
        raise BadRequest(
            f"you hold {held} {underlying}; selling {selling} would leave the "
            f"ticker short {selling - held}. Record the missing purchase first."
        )


def do_buy_shares(conn, form) -> None:
    underlying = _one(form, "underlying").upper()
    result = actions.buy_shares(
        underlying, _int(form, "quantity", label="Shares"),
        _decimal(form, "price", label="Price"), _past(_date(form, "on", date.today()), "Date"),
        fee=_decimal(form, "fee", ZERO),
    )
    result.lots[0].notes = _one(form, "notes")
    store.apply(conn, result)
    raise Redirect(_next(form, f"/shares/{underlying}"), result.summary)


def do_sell_shares(conn, form) -> None:
    underlying = _one(form, "underlying").upper()
    quantity = _int(form, "quantity", label="Shares")
    if quantity <= 0:
        raise BadRequest("Shares must be at least 1")
    _check_not_short(conn, underlying, quantity)
    on = _past(_date(form, "on", date.today()), "Date")
    lot_id = _one(form, "lot_id")
    if lot_id:
        left = _remaining_by_lot(conn, underlying).get(lot_id)
        if left is None:
            raise BadRequest("that lot does not belong to this ticker")
        if quantity > left:
            raise BadRequest(
                f"that lot holds {left} shares; selling {quantity} from it is not "
                "possible. Sell fewer, or leave the lot on the account rule."
            )
        lot = next((l for l in store.load_lots(conn, underlying) if l.id == lot_id), None)
        if lot is not None and lot.acquired_on > on:
            raise BadRequest(
                f"that lot was acquired on {lot.acquired_on}, after the sale date {on}. "
                "Shares cannot be sold before they were bought: date the sale on or "
                f"after {lot.acquired_on}, or re-date the lot on the ticker page."
            )
    result = actions.sell_shares(
        underlying, quantity, _decimal(form, "price", label="Price"),
        on, fee=_decimal(form, "fee", ZERO),
        specific_lot_ids=(lot_id,) if lot_id else (),
    )
    errors = store.apply(conn, result)
    note = result.summary + (" - matching blocked: " + "; ".join(errors.values()) if errors else "")
    raise Redirect(_next(form, f"/shares/{underlying}"), ("!" if errors else "") + note)


def do_buy_write(conn, form) -> None:
    underlying = _one(form, "underlying").upper()
    contracts = _int(form, "contracts", 0) or None
    result = actions.buy_write(
        underlying, shares=_int(form, "shares", label="Shares"),
        share_price=_decimal(form, "share_price", label="Share price"),
        expiry=_date(form, "expiry", label="Call expiry"),
        strike=_decimal(form, "strike", label="Call strike"),
        call_price=_decimal(form, "call_price", label="Call premium"),
        on=_past(_date(form, "on", date.today()), "Date"), contracts=contracts,
        share_fee=_decimal(form, "share_fee", ZERO),
        option_fee=_decimal(form, "option_fee", ZERO),
    )
    store.apply(conn, result)
    raise Redirect(_next(form, f"/shares/{underlying}"), result.summary)


def do_delete_disposal(conn, disposal_id, form) -> None:
    try:
        store.delete_disposal(conn, disposal_id, "removed by hand")
    except ValueError as exc:
        raise BadRequest(str(exc)) from None
    raise Redirect(_next(form, "/shares"), "Sale removed; matching rebuilt")


def do_delete_lot(conn, lot_id, form) -> None:
    try:
        store.delete_lot(conn, lot_id, "removed by hand")
    except store.InUseError as exc:
        raise BadRequest(str(exc)) from None
    except ValueError as exc:
        raise BadRequest(str(exc)) from None
    raise Redirect(_next(form, "/shares"), "Lot removed; matching rebuilt")


def do_redate_lot(conn, lot_id, form) -> None:
    on = _past(_date(form, "on", label="Acquired on"), "Acquired on")
    try:
        store.redate_lot(conn, lot_id, on, "re-dated by hand")
    except ValueError as exc:
        raise BadRequest(str(exc)) from None
    raise Redirect(_next(form, "/shares"), f"Lot re-dated to {on}; matching rebuilt")
