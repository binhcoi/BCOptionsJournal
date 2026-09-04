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
from ..engine import health
from ..engine.campaign import campaign
from ..engine.scorecard import scorecard
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
                  with_actions: bool = True, collapse_to: str = "",
                  pill_href: str = "", pill_attrs: str = "") -> str:
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
        href = pill_href or (f"{here}{chain_q}{anchor}" if is_open
                             else f"{here}{chain_q}&act={pid}&do=close{anchor}")
        actions_html = (f'<a class="act act-close{" here" if is_open else ""}" href="{href}"'
                        f'{pill_attrs} title="Close, roll, expire, assign or split">Close</a>')

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


def _chain_head_row(index, legs, clicked, here, hide: bool = True) -> str:
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
        + (f'<a class="legs here" href="{here}#row-{pid}" title="Hide the chain">&#9650;</a>'
           if hide else "")
        + "</td></tr>"
    )


def _action_template(p, index, token, here) -> str:
    """A row's forms, shipped hidden with the row. A round trip through a
    forwarded port is a third of a second; opening a form should be none."""
    return (f'<template class="acts" data-for="{r.esc(p.id)}">'
            + _action_row(p, index, "close", token, here) + "</template>")


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
    listed = _apply_filters(listed, query, show)

    act_id = (query.get("act") or [""])[0]
    which = (query.get("do") or [""])[0]
    chain_id = (query.get("chain") or [""])[0]
    # Every link on the page keeps the filter, so opening a form or a chain
    # never loses the view being looked at.
    filter_qs = _filter_qs(query)
    here = f"/?show={show}" + (f"&{filter_qs}" if filter_qs else "")

    # Expiry orders every view: soonest first while open, latest first once
    # over (ties by close date, so the most recent activity leads).
    if show == "open":
        listed = sorted(listed, key=lambda p: (p.expiry, p.underlying, -p.quantity))
    else:
        listed = sorted(listed, key=lambda p: (p.expiry, p.closed_on or p.opened_on, p.underlying),
                        reverse=True)

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
    # The block is drawn where the clicked leg sits in the list. If that leg
    # is no longer listed -- it was just closed while its chain was open --
    # anchor on the first family member that is, or expand nothing: the
    # family's rows must never be held back and then not drawn.
    anchor_id = chain_id
    if chain_id and chain_id not in {p.id for p in listed}:
        anchor = next((p for p in listed if p.id in expanded_ids), None)
        anchor_id = anchor.id if anchor else ""
        if anchor is None:
            expanded_legs, expanded_ids = [], set()

    queue = validate.expiring(open_ones)
    banner = ""
    if queue:
        banner = (f'<div class="callout"><a href="/?show=open&amp;due=0">'
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
        if p.id in expanded_ids and p.id != anchor_id:
            continue  # rendered as part of the expanded chain

        if p.id == anchor_id and expanded_legs:
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
                elif leg.is_open:
                    rows.append(_action_template(leg, index, token, here + chain_q))
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
        elif p.is_open:
            rows.append(_action_template(p, index, token, here + chain_q))

    partial = (query.get("partial") or [""])[0]
    if partial == "block":
        return 200, "".join(rows[block[0]:block[1]])
    if partial == "action":
        if act_row_at is None or action_at is None:
            return 200, ""
        return 200, rows[act_row_at] + rows[action_at]

    sc = scorecard(index, listed, whole_campaigns=False)
    totals = _scorecard_html(sc, f"the {len(listed)} position(s) in this table: "
                                 f"{sc.closed_legs} closed, {sc.open_legs} open")

    table_html = r.table(POSITION_COLUMNS, rows, cls="positions")
    filter_bar = _filter_bar(conn, query, show, token, positions)
    if partial == "table":
        # The table, the strip that describes it and the filter bar that
        # produced it, for the script that swaps them in place.
        return 200, totals + filter_bar + table_html

    new_panel = (f'<div id="new-box" class="form-box"><div data-form="new" hidden>'
                 f'<a class="close-form" href="{here}" data-form-close title="Close this form" '
                 'aria-label="Close this form">&times;</a><h3>New position</h3>'
                 + _new_form_body(conn, token, {}, (), back=here) + "</div></div>")
    new_button = ('<a class="btn new" href="/new" data-form-tab="new" data-tabs-for="new-box">'
                  "+ New position</a>")
    body = f"""{new_panel}{banner}
{totals}
{filter_bar}
{table_html}
<p class="hint">Credit is what came in on opening. Carry is what the chain
brought forward. They are different scopes and are never added together.
Click a row or its leg count to show the whole chain in place; again to hide.</p>"""
    return 200, r.page("Positions", body, nav_here="positions", toolbar=new_button)


def _cards(cells, cls: str = "totals") -> str:
    """Summary cards. Each cell is (label, html, sign) and the sign, when
    given, tints the card so the strip reads at a glance."""
    out = []
    for label, html, sign in cells:
        tone = ""
        if sign is not None and sign != 0:
            tone = ' class="good"' if sign > 0 else ' class="bad"'
        out.append(f"<div{tone}><span>{r.esc(label)}</span><b>{html}</b></div>")
    return f'<div class="{cls}">' + "".join(out) + "</div>"


def _table_totals(listed, index) -> str:
    """What is in the table, and only that: filtering changes these figures.

    Built for a glance: the headline on each card is the figure that decides
    something, the small line under it says what it is made of.
    """
    def sub(text) -> str:
        return f"<small>{text}</small>"

    open_ones = [p for p in listed if p.is_open]
    closed = [p for p in listed if not p.is_open]
    premium = q2(sum((open_cash(p) for p in open_ones), ZERO))
    carry = q2(sum((index.carry(p) for p in open_ones), ZERO))
    net = q2(premium + carry)
    at_risk = q2(sum((capital_at_risk(p) or ZERO for p in open_ones), ZERO))
    expected = q2(sum((target(p, index.carry(p)).expected_pl for p in open_ones), ZERO))
    realized = q2(sum((realized_pl(p) for p in closed), ZERO))

    puts = sum(1 for p in open_ones if p.right is Right.PUT)
    calls = len(open_ones) - puts
    contracts = sum(p.quantity for p in open_ones)
    what = (f"{puts} put(s) &middot; {calls} call(s) &middot; {contracts} contract(s)"
            if open_ones else f"{len(closed)} closed leg(s)")
    cells = [("In this table", f"{len(listed)} position(s)" + sub(what), None)]

    if open_ones:
        cells.append(("Net credit", r.money(net)
                      + sub(f"premium {fmt(premium)} &middot; carry {fmt(carry)}"), net))
        if net > 0:
            capture = f"{q2(expected / net * 100)}% of net credit"
        elif expected > 0:
            capture = "profit target on the debit paid"
        else:
            capture = "chain is net debit"
        cells.append(("Expected at target", r.money(expected) + sub(capture), expected))
        ret = (f"{q2(expected / at_risk * 100)}% expected return" if at_risk > 0
               else "no cash committed")
        cells.append(("Capital at risk", r.money(at_risk) + sub(ret), None))
        soon = min(open_ones, key=lambda p: p.expiry)
        dte = (soon.expiry - date.today()).days
        within = sum(1 for p in open_ones if (p.expiry - date.today()).days <= 30)
        tone = -1 if dte <= 7 else None
        cells.append(("Next expiry", r.esc(soon.expiry)
                      + sub(f"{dte}d &middot; {within} within 30 days"), tone))
    if closed:
        per_leg = q2(realized / len(closed))
        cells.append(("Realized by these legs", r.money(realized)
                      + sub(f"{len(closed)} leg(s) &middot; {fmt(per_leg)} per leg"), realized))
    return _cards(cells)


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
        ("Realized options", r.money(realized), realized),
        ("Realized shares", r.money(shares_pl), shares_pl),
        ("True total", total_cell, realized + shares_pl),
        ("Open premium", r.money(premium), premium),
        ("Expected at target", r.money(expected), expected),
        ("Chain carry", r.money(carry), carry),
        ("Capital at risk", r.money(at_risk), None),
        ("Open positions", r.esc(len(open_ones)), None),
    ]
    return _cards(cells)


# ---------------------------------------------------------------------------
# new position


def new_position_form(conn, token, form=None, problems=()) -> tuple[int, str]:
    body = _new_form_body(conn, token, form or {}, problems, back="/new")
    body += f"""<p class="hint">STO sells to open (short), BTO buys to open (long). Fee is
auto-filled at {fmt(_fee_rate(conn))} per contract and editable. Shortcuts: <kbd>+7</kbd>,
<kbd>+14</kbd>, <kbd>+30</kbd> in the expiry box jump that many days out.
Submitting leaves you on a fresh form.</p>"""
    return 200, r.page("New position", body, nav_here="positions")


def _new_form_body(conn, token, form, problems, back: str) -> str:
    """The entry form: one leg row, and a second row for a position that is
    already over -- a past trade being caught up on."""
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
    outcome = _one(form, "outcome", "OPEN")
    outcome_select = (
        '<select name="outcome" id="f_outcome" aria-label="Outcome" title="Outcome">'
        + "".join(f'<option value="{v}"{" selected" if v == outcome else ""}>{t}</option>'
                  for v, t in (("OPEN", "Open"), ("CLOSED", "Closed"), ("EXPIRED", "Expired"),
                               ("ASSIGNED", "Assigned")))
        + "</select>")
    grid = _leg_grid([leg], attrs=f' data-fee-rate="{r.esc(rate)}"')
    when = r.field("Opened", "opened_on", _one(form, "opened_on", r.today_iso()),
                   kind="date", required=True)
    # A short call sold against shares bought at the same time is a buy-write:
    # the shares are recorded with it and the call is covered from the start.
    with_shares = _one(form, "with_shares", "NONE")
    shares_row = '<div class="grid compact">' + "".join([
        r.select("Shares", "with_shares", [("NONE", "none"), ("BUY", "bought with it (buy-write)")],
                 with_shares),
        '<span class="when-shares">'
        + r.field("Shares bought", "shares", _one(form, "shares"), kind="number", step="1")
        + r.field("Share price", "share_price", _one(form, "share_price"), kind="number", step="0.01")
        + r.field("Share fee", "share_fee", _one(form, "share_fee", "0.00"), kind="number", step="0.01")
        + "</span>",
    ]) + "</div>"
    # A compact row for a trade being caught up on: how it ended. The closing
    # fields show only once an outcome is picked.
    over = '<div class="grid compact">' + "".join([
        r.select("Outcome", "outcome", [("OPEN", "Still open"), ("CLOSED", "Closed"),
                                        ("EXPIRED", "Expired"), ("ASSIGNED", "Assigned")], outcome),
        '<span class="when-over">'
        + r.field("Closed on", "closed_on", _one(form, "closed_on"), kind="date")
        + r.field("Close price", "close_price", _one(form, "close_price"), kind="number", step="0.01")
        + r.field("Close fee", "close_fee", _one(form, "close_fee", default_fee), kind="number",
                  step="0.01")
        + "</span>",
    ]) + "</div>"
    rest = over + '<div class="grid compact">' + "".join([
        r.field("Notes", "notes", _one(form, "notes"), attrs=' placeholder="why this trade"'),
        r.field("Tags", "tags", _one(form, "tags"), attrs=' list="all-tags" autocomplete="off"'
                                                        ' placeholder="wheel, earnings"'),

    ]) + "</div>"
    return (r.problems_block(problems)
            + r.datalist("tickers", tickers) + r.datalist("all-tags", store.all_tags(conn))
            + r.form("/new", r.hidden("next", back) + when + grid + shares_row + rest, token,
                     submit="Add position", cls="two-part", submit_cls="btn-open"))
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
    )

    existing = store.load_positions(conn)
    problems = validate.validate_position(position, existing, rate)
    if validate.errors(problems):
        raise Invalid(problems, form)

    if _one(form, "with_shares", "NONE").upper() == "BUY":
        if position.right is not Right.CALL or position.direction is not Direction.SHORT:
            raise BadRequest("A buy-write is a short call sold against shares bought with it")
        if _one(form, "outcome", "OPEN").upper() != "OPEN":
            raise BadRequest("Record the buy-write first, then close or assign the call above")
        result = actions.buy_write(
            position.underlying, shares=_int(form, "shares", label="Shares bought"),
            share_price=_decimal(form, "share_price", label="Share price"),
            expiry=position.expiry, strike=position.strike, call_price=position.open_price,
            on=position.opened_on, contracts=position.quantity,
            share_fee=_decimal(form, "share_fee", ZERO), option_fee=position.open_fee,
        )
        call = result.created[0]
        call.notes, call.tags = position.notes, position.tags
        store.apply(conn, result, "entered by hand")
        raise Redirect(_next(form, "/new"), f"Added {result.summary}")

    outcome = _one(form, "outcome", "OPEN").upper()
    ended = None
    if outcome != "OPEN":
        on = _past(_date(form, "closed_on", label="Closed on"), "Closed on")
        if on < position.opened_on:
            raise BadRequest("Closed on cannot be before the position was opened")
        fee = _decimal(form, "close_fee", ZERO)
        if outcome == "CLOSED":
            ended = actions.close(position, on, _decimal(form, "close_price", label="Close price"), fee)
        elif outcome == "EXPIRED":
            ended = actions.expire(position, on, close_fee=fee)
        elif outcome == "ASSIGNED":
            ended = actions.assign(position, on=on, close_fee=fee)
        else:
            raise BadRequest("Outcome must be open, closed, expired or assigned")

    store.apply(conn, actions.ActionResult(created=[position]), "entered by hand")
    if ended is not None:
        store.apply(conn, ended, "entered by hand, already over")
    warned = validate.warnings(problems)
    note = f"Added {position.underlying} {fmt(position.strike)}" \
           f"{position.right.value[0]}" + (f", {outcome.lower()}" if ended else "")
    if warned:
        note = "!" + note + f" - {len(warned)} warning(s): " + \
               "; ".join(p.message for p in warned)
    raise Redirect(_next(form, "/new"), note)


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

    lots = store.load_lots(conn)
    fam = campaign(index, position)
    # The same scorecard as the positions page, over this campaign, then the
    # campaign's shape in a line of chips.
    strips = _scorecard_html(scorecard(index, [position]),
                             f"this campaign &middot; {len(fam.legs)} leg(s) since {fam.started}",
                             extra_chips=_campaign_chips(fam))
    if fam.splits:
        # Several branches grew from one trade; this is the lineage that leads here.
        strips += ("<h2>This branch</h2>"
                   + _cards(_chain_cards(index, position, chain), "totals wide"))
    strips += ("<h2>This position</h2>"
               + _cards(_position_cards(position, chain, carry, lots, conn), "totals wide"))

    # Actions open in the chain table below, exactly as on the positions
    # page. ``?do=X`` alone means this position; ``act`` may name another leg.
    which = (query.get("do") or [""])[0]
    act_id = (query.get("act") or [position.id if which else ""])[0]
    chain_html = _chain_block(index, position, chain, act_id, which, token)
    partial = (query.get("partial") or [""])[0]
    if partial in ("table", "block"):
        return 200, chain_html
    if partial == "action":
        return 200, _chain_action_partial(index, position, act_id, which, token)

    actions_block = ""
    notes = ("<h2>Notes</h2>" + r.form(
        f"/position/{r.esc(position.id)}/notes",
        r.hidden("next", here)
        + r.textarea("Notes", "notes", position.notes, hint="Why this trade, in your words")
        + r.field("Tags", "tags", ", ".join(position.tags),
                  attrs=' list="all-tags" autocomplete="off"',
                  hint="Comma separated, e.g. wheel, earnings")
        + r.datalist("all-tags", store.all_tags(conn)),
        token, submit="Save notes", cls="grid"))

    raw = _raw_position_block(conn, position, token, here)
    body = f"""{strips}
{chain_html}
{actions_block}
{notes}
{raw}
<p class="hint"><a href="/audit?entity={r.esc(position.id)}">History for this
position</a></p>"""
    return 200, r.page(r.contract_text(position), body, nav_here="positions")


def _chain_rows(index, position, act_id: str, which: str, token: str):
    """The chain's rows, with the open form under its leg. ``here`` ends in
    "?" so the shared row code can append "&act=..." as it does on the list;
    the page then serves the same partials the list does."""
    here = f"/position/{r.esc(position.id)}?"
    legs = [leg for leg, _ in index.family(position)]
    rows = [_chain_head_row(index, legs, position, here, hide=False)]
    act_row_at = action_at = None
    for leg in legs:
        if leg.id == act_id:
            act_row_at = len(rows)
        rows.append(_position_row(leg, index, here=here, act_id=act_id, which=which,
                                  in_chain=True, current=(leg.id == position.id)))
        if leg.id == act_id and which and leg.is_open:
            action_at = len(rows)
            rows.append(_action_row(leg, index, which, token, here))
        elif leg.is_open:
            rows.append(_action_template(leg, index, token, here))
    return legs, rows, act_row_at, action_at


def _chain_action_partial(index, position, act_id, which, token) -> str:
    _, rows, act_row_at, action_at = _chain_rows(index, position, act_id, which, token)
    if act_row_at is None or action_at is None:
        return ""
    return rows[act_row_at] + rows[action_at]


def _chain_block(index, position, chain, act_id: str = "", which: str = "", token: str = "") -> str:
    """Every leg of this position's chain, as the same rows the list uses.

    A lineage shows one path; this shows the whole chain including the other
    half of any split and what became of it, in order, with this position
    marked. Same columns, same figures, same buttons, same forms, as the
    positions page.
    """
    legs, rows, _, _ = _chain_rows(index, position, act_id, which, token)
    has_split = any(leg.is_superseded for leg in legs)
    explain = ""
    if has_split:
        explain = ('<p class="hint">A split position stays as the record of the '
                   'split and realizes nothing itself; its credit is carried by '
                   'the halves, which follow it here.</p>')

    return f"""<h2>Chain legs - {len(legs)}</h2>
{r.table(POSITION_COLUMNS, rows, cls="positions chain").replace('<table class="positions chain">', '<table class="positions chain" data-fixed>', 1)}
{explain}"""


def _sub(text) -> str:
    return f"<small>{text}</small>"


def _chain_cards(index, position, chain) -> list:
    """What the whole chain has done, wherever this leg sits in it."""
    legs = [leg for leg, _ in index.family(position)]
    root = legs[0]
    head = chain.head
    cells = []
    detail = f"{len(legs)} leg(s) &middot; {chain.days} days"
    if any(leg.is_superseded for leg in legs):
        everything = q2(sum((realized_pl(leg) for leg in legs), ZERO))
        detail += f" &middot; all legs {fmt(everything)}"
    cells.append(("Chain realized", r.money(chain.realized) + _sub(detail), chain.realized))

    if head.is_open:
        cells.append(("Net chain credit", r.money(chain.net_credit)
                      + _sub(f"premium {fmt(open_cash(head))} &middot; carry {fmt(chain.carry)}"),
                      chain.net_credit))
        be = break_even(chain)
        if be:
            cells.append(("Break-even", r.money(be.price)
                          + _sub(f"strike {fmt(be.strike)} less {fmt(be.per_share)} per share"),
                          None))
        recover = credit_to_recover(chain)
        if recover > 0:
            cells.append(("Credit to recover", r.money(recover)
                          + _sub("before the chain nets positive"), -1))
    if len(legs) > 1:
        drift = q2(head.strike - root.strike)
        cells.append(("Strike", f"{r.esc(price(root.strike))} &rarr; {r.esc(price(head.strike))}"
                      + (_sub(f'{"down" if drift < 0 else "up"} {price(abs(drift))}') if drift
                         else _sub("unchanged")), None))
        grew = head.quantity - root.quantity
        cells.append(("Size", f"{root.quantity} &rarr; {head.quantity}"
                      + (_sub(f"x{head.quantity / root.quantity:.1f}") if grew > 0
                         else _sub("contracts")), -1 if grew > 0 else None))
    return cells


def _raw_position_block(conn, position, token: str, here: str) -> str:
    """The record as entered, every field open to correction, plus reopen and
    removal. Folded, because this is fixing a mistake, not trading."""
    pid = r.esc(position.id)
    p = position
    side_select = (
        '<select name="direction" id="f_direction" aria-label="Side" title="Side">'
        + "".join(f'<option value="{v}"{" selected" if v == p.direction.value else ""}>{t}</option>'
                  for v, t in (("SHORT", "STO"), ("LONG", "BTO"))) + "</select>")
    right_select = (
        '<select name="right" id="f_right" aria-label="Right" title="Right">'
        + "".join(f'<option value="{v}"{" selected" if v == p.right.value else ""}>{t}</option>'
                  for v, t in (("PUT", "Put"), ("CALL", "Call"))) + "</select>")
    leg = [side_select,
           _box("underlying", p.underlying, kind="text", label="Ticker",
                attrs=' autocapitalize="characters" autocomplete="off"'),
           _box("expiry", p.expiry.isoformat(), kind="date", label="Expiry"),
           _box("strike", str(p.strike), step="0.5", label="Strike"),
           right_select,
           _box("quantity", str(p.quantity), step="1", label="Contracts", attrs=' min="1"'),
           _box("open_price", str(p.open_price), label="Premium / share"),
           _box("open_fee", str(p.open_fee), label="Fee", required=False)]
    # Same shape as the close forms: the date first, the leg, then -- for a
    # position that is over -- how it closed, whether or not a close date was
    # ever recorded (that missing date is exactly what gets fixed here).
    when = r.field("Opened", "opened_on", p.opened_on.isoformat(), kind="date", required=True)
    closing = ""
    if not p.is_open and p.status is not Status.SPLIT:
        closing = '<div class="grid compact">' + "".join([
            r.field(f"{p.status.value.title()} on", "closed_on",
                    p.closed_on.isoformat() if p.closed_on else "", kind="date", required=True),
            r.field("Close price", "close_price",
                    str(p.close_price) if p.close_price is not None else "", kind="number",
                    step="0.01"),
            r.field("Close fee", "close_fee", str(p.close_fee), kind="number", step="0.01"),
        ]) + "</div>"
    edit = r.form(f"/position/{pid}/edit", r.hidden("next", here) + when + _leg_grid([leg]) + closing,
                  token, submit="Save corrections", cls="two-part")

    reopen = ""
    if p.status in (Status.CLOSED, Status.EXPIRED):
        reopen = r.form(f"/position/{pid}/reopen", r.hidden("next", here), token,
                        submit=f"Reopen (undo the {p.status.value.lower()})", cls="inline reopen")
    reasons = store.position_links(conn, p.id)
    option_links = [x for x in reasons if "rolled" in x or "split" in x]
    convert = ""
    if not option_links:
        convert = r.form(
            f"/position/{pid}/to-shares",
            r.hidden("next", f"/shares/{r.esc(p.underlying)}/data")
            + '<label class="check"><input type="checkbox" name="sure" value="1"> '
              "this row is really a stock trade</label>",
            token, submit="Convert to share trade", cls="inline convert")
    if reasons:
        remove = ('<p class="hint">Cannot be removed: ' + r.esc("; ".join(reasons))
                  + ". Remove or relink those first.</p>")
    else:
        remove = r.form(
            f"/position/{pid}/delete",
            r.hidden("next", "/?show=all")
            + '<label class="check"><input type="checkbox" name="sure" value="1"> '
              "remove this position from the journal</label>",
            token, submit="Remove", cls="inline restore")
    return f"""<details class="report raw" id="raw"><summary>Raw data</summary>
<div class="bubble">
<h3>{r.contract(p)} <small class="dim">as entered</small></h3>
{edit}
<div class="raw-actions">{reopen}{convert}{remove}</div>
<p class="hint">Every change here is logged and can be undone from History. Status
and chain links are not edited here: close, roll, expire, assign and split above keep
the chain and the shares consistent. Convert is for a row the sheet wrote as an option
that was really a stock purchase or sale: the shares it moved stay, as an outright
trade, and the row goes. Removing is for a record that should never have existed,
such as a placeholder.</p>
</div>
</details>"""


def do_edit_position(conn, position_id, form) -> None:
    p = store.load_position(conn, position_id)
    if p is None:
        raise BadRequest("no such position")
    changes = {
        "underlying": _one(form, "underlying").upper() or p.underlying,
        "expiry": _date(form, "expiry", label="Expiry"),
        "strike": _decimal(form, "strike", label="Strike"),
        "right": Right(_one(form, "right", p.right.value)),
        "direction": Direction(_one(form, "direction", p.direction.value)),
        "quantity": _int(form, "quantity", label="Contracts"),
        "opened_on": _past(_date(form, "opened_on", label="Opened"), "Opened"),
        "open_price": _decimal(form, "open_price", label="Premium"),
        "open_fee": _decimal(form, "open_fee", ZERO),
    }
    if not p.is_open and p.status is not Status.SPLIT:
        changes["closed_on"] = _past(_date(form, "closed_on", label="Closed on"), "Closed on")
        changes["close_price"] = (_decimal(form, "close_price", label="Close price")
                                  if _one(form, "close_price") else None)
        changes["close_fee"] = _decimal(form, "close_fee", p.close_fee)
    try:
        store.edit_position(conn, position_id, changes, "corrected by hand")
    except ValueError as exc:
        raise BadRequest(str(exc)) from None
    raise Redirect(_next(form, f"/position/{position_id}"), "Corrections saved")


def do_convert_position(conn, position_id, form) -> None:
    if _one(form, "sure") != "1":
        raise BadRequest("Tick the box to confirm converting the row to a share trade")
    try:
        moved = store.convert_to_share_trade(conn, position_id, "converted to a share trade")
    except (store.InUseError, ValueError) as exc:
        raise BadRequest(str(exc)) from None
    raise Redirect(_next(form, "/shares"), f"Converted: {moved}; the option row is gone")


def do_reopen_position(conn, position_id, form) -> None:
    try:
        store.reopen_position(conn, position_id, "reopened by hand")
    except ValueError as exc:
        raise BadRequest(str(exc)) from None
    raise Redirect(_next(form, f"/position/{position_id}"), "Position reopened")


def do_redate_position(conn, position_id, form) -> None:
    kw = {}
    for name, label in (("opened_on", "Opened"), ("expiry", "Expiry"), ("closed_on", "Closed on")):
        if _one(form, name):
            kw[name] = _date(form, name, label=label)
            if name != "expiry":
                _past(kw[name], label)
    try:
        store.redate_position(conn, position_id, note="dates moved by hand", **kw)
    except ValueError as exc:
        raise BadRequest(str(exc)) from None
    raise Redirect(_next(form, f"/position/{position_id}"), "Dates moved")


def do_delete_position(conn, position_id, form) -> None:
    if _one(form, "sure") != "1":
        raise BadRequest("Tick the box to confirm removing the position")
    try:
        store.delete_position(conn, position_id, "removed by hand")
    except store.InUseError as exc:
        raise BadRequest(str(exc)) from None
    except ValueError as exc:
        raise BadRequest(str(exc)) from None
    raise Redirect(_next(form, "/?show=all"), "Position removed; undo from History if needed")


def _scorecard_html(sc, scope: str, extra_chips=()) -> str:
    """One figure per card, nothing to decode; the finer stats are chips."""
    def tone(value):
        return None if value == 0 else (1 if value > 0 else -1)

    cards = [
        ("Banked", r.money(sc.banked, dash="0.00"), tone(sc.banked)),
        ("In hand", r.money(sc.in_hand, dash="0.00"), tone(sc.in_hand)),
        ("To close at target", (f'<span class="neg">({fmt(sc.to_close)})</span>' if sc.to_close > 0
                                else r.money(-sc.to_close, dash="0.00")), -1 if sc.to_close > 0 else None),
        ("Net at target", r.money(sc.at_target, dash="0.00"), tone(sc.at_target)),
        ("At risk", r.money(sc.at_risk, dash="0.00"), None),
    ]
    chips = []
    if sc.closed_legs:
        kept = sc.kept
        if kept is not None:
            cls = "pos" if kept >= 50 else ("neg" if kept < 0 else "")
            chips.append((f"kept {kept}% of premium", cls))
        chips.append((f"won {sc.wins} of {sc.closed_legs} closed", ""))
        chips.append((f"avg {sc.avg_days} days", ""))
    if sc.open_legs:
        chips.append((f"{sc.open_legs} open leg(s) &middot; {sc.contracts} contract(s)", ""))
        if sc.return_at_target is not None:
            chips.append((f"{sc.return_at_target}% return at target", ""))
        if sc.break_even is not None:
            chips.append((f"break-even {fmt(sc.break_even)}", ""))
    chips.extend(extra_chips)
    chips.append((scope, "dim"))
    return (_cards(cards, "totals score")
            + '<div class="chips facts">' + "".join(
                f'<span class="chip{" " + cls if cls else ""}">{text}</span>' for text, cls in chips)
            + "</div>")


def _campaign_chips(fam) -> list:
    """The campaign's shape, as chips: size, strikes, assignments, span."""
    chips = []
    if fam.is_open:
        grew = fam.contracts_now - fam.contracts_start
        growth = f" &middot; x{fam.contracts_now / fam.contracts_start:.1f}" if grew > 0 else ""
        chips.append((f"contracts {fam.contracts_start} &rarr; {fam.contracts_now}{growth}",
                      "neg" if grew > 0 else ""))
        strikes = " / ".join(price(x) for x in fam.strikes_now)
        chips.append((f"strikes {r.esc(price(fam.root.strike))} &rarr; {r.esc(strikes)}", ""))
        if fam.at_risk_now != fam.at_risk_start:
            chips.append((f"at risk was {fmt(fam.at_risk_start)} at the start",
                          "neg" if fam.at_risk_now > fam.at_risk_start else ""))
    if fam.assigned_legs:
        chips.append((f"assigned {fam.assigned_shares} shares &middot; {fmt(fam.assigned_cash)} at strike", ""))
    chips.append((f"{fam.days} days &middot; {fam.rolls} roll(s) &middot; {fam.splits} split(s)", ""))
    return chips


def _campaign_cards(fam, word: str = "Campaign") -> list:
    """Everything that grew from one opening trade, summed once."""
    cells = [(f"{word} so far", r.money(fam.net_so_far)
              + _sub(f"realized {fmt(fam.realized)} &middot; premium in hand "
                     f"{fmt(fam.open_premium)}"), fam.net_so_far)]
    if fam.is_open:
        cells.append(("If all close at target", r.money(fam.expected_at_target)
                      + _sub(f"buy-backs would cost {fmt(fam.cost_to_close_at_target)}"),
                      fam.expected_at_target))
        grew = fam.contracts_now - fam.contracts_start
        growth = (f" &middot; x{fam.contracts_now / fam.contracts_start:.1f}" if grew > 0 else "")
        cells.append(("Contracts", f"{fam.contracts_start} &rarr; {fam.contracts_now}"
                      + _sub(f"{len(fam.open_legs)} open leg(s){growth}"), -1 if grew > 0 else None))
        cells.append(("Capital at risk", r.money(fam.at_risk_now)
                      + _sub(f"was {fmt(fam.at_risk_start)} at the start"),
                      -1 if fam.at_risk_now > fam.at_risk_start else None))
        strikes = " / ".join(price(s) for s in fam.strikes_now)
        cells.append(("Strikes", f"{r.esc(price(fam.root.strike))} &rarr; {r.esc(strikes)}"
                      + _sub("start &rarr; open now"), None))
    if fam.assigned_legs:
        cells.append(("Assigned", f"{fam.assigned_shares} shares"
                      + _sub(f"{fam.assigned_legs} leg(s) &middot; {fmt(fam.assigned_cash)} at strike"),
                      None))
    cells.append(("Span", f"{fam.days} days"
                  + _sub(f"since {fam.started} &middot; {len(fam.legs)} legs &middot; "
                         f"{fam.rolls} roll(s) &middot; {fam.splits} split(s)"), None))
    return cells


def _position_cards(position, chain, carry, lots, conn=None) -> list:
    """This leg alone: its terms, its cash, and where it stands."""
    cells = [("Status", r.status_badge(position.status)
              + _sub(f"opened {position.opened_on} &middot; expiry {position.expiry}"), None)]
    if position.is_open:
        dte = (position.expiry - date.today()).days
        cells.append(("Days to expiry", f"{dte}d" + _sub(f"expires {position.expiry}"),
                      -1 if dte <= 7 else None))
    cells.append(("Credit received", r.money(open_cash(position))
                  + _sub(f"{position.quantity} x {price(position.open_price)} x "
                         f"{position.multiplier} less fee {fmt(position.open_fee)}"),
                  open_cash(position)))
    risk = capital_at_risk(position)
    if position.direction is Direction.LONG:
        why = "the debit paid"
    elif position.right is Right.PUT:
        why = f"{position.shares} shares at {fmt(position.strike)}"
    else:
        why = "shares behind the call" if risk is not None else "naked: no ceiling"
    cells.append(("Capital at risk", r.money(risk) + _sub(why), None))

    if position.is_open:
        tgt = target(position, carry)
        label = f"{int(tgt.pct * 100)}% target"
        why = ("profit on the debit paid" if position.direction is Direction.LONG
               else "of the chain's net credit")
        cells.append((label, (r.esc(price(tgt.price)) if tgt.applicable
                              else '<span class="dim">none: net debit</span>')
                      + _sub(f"expected {fmt(tgt.expected_pl)} &middot; {why}"), tgt.expected_pl))
    else:
        cells.append(("Realized", r.money(realized_pl(position))
                      + _sub(f"closed {position.closed_on} at "
                             f"{price(position.close_price) if position.close_price is not None else '-'}"),
                      realized_pl(position)))

    lot_by_id = {l.id: l for l in lots}
    if position.right is Right.CALL and position.direction is Direction.SHORT:
        # Coverage is the ticker's: shares held against the shares every open
        # call controls, delivered oldest lot first if assigned.
        held = _shares_held(conn, position.underlying)
        called = sum(p.shares for p in store.load_positions(conn, position.underlying)
                     if p.is_open and p.right is Right.CALL and p.direction is Direction.SHORT)
        if held <= 0:
            cells.append(("Shares behind it", '<span class="neg">none - naked</span>', -1))
        else:
            short = max(called - held, 0)
            text = (f'<a href="/shares/{r.esc(position.underlying)}">{held} held</a>'
                    + _sub(f"{called} called across open calls"
                           + (f" &middot; <span class='neg'>{short} uncovered</span>" if short else "")))
            cells.append(("Shares behind it", text, -1 if short else None))
    created = [l for l in lots if l.assigning_position_id == position.id
               and position.right is Right.PUT]
    if created:
        cells.append(("Shares acquired", f'<a href="/shares/{r.esc(created[0].underlying)}">'
                                         f"{r.esc(_lot_label(created[0]))}</a>", None))
    return cells


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

    shares_held = {t.underlying: t.held for t in tickers if not t.error}
    risks = [t for t in decide.concentration(positions, shares_at_cost, shares_held)
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

FILTER_KEYS = ("ticker", "due", "q", "right", "side", "tag", "since", "until", "period")
DUE = (("0", "at or past expiry"), ("7", "within 7 days"), ("30", "within 30 days"))
PERIODS = (("3m", "Last 3 months", 91), ("6m", "Last 6 months", 182),
           ("ytd", "This year", None), ("1y", "Last 12 months", 365))


def _quarter_start(day: date) -> date:
    return date(day.year, 3 * ((day.month - 1) // 3) + 1, 1)


def _add_months(day: date, months: int) -> date:
    y, m = divmod(day.month - 1 + months, 12)
    return date(day.year + y, m + 1, 1)


def _period_window(code: str, today: date | None = None):
    """The (start, end) a period code covers, end exclusive; None for none.

    Rolling: 3m, 6m, 1y. Calendar: ytd, tm (this month), tq (this quarter),
    m:YYYY-MM, q:YYYY-Qn, y:YYYY."""
    today = today or date.today()
    if not code:
        return None
    for key, _, days in PERIODS:
        if key == code and days is not None:
            return today - timedelta(days=days), None
    if code == "ytd":
        return date(today.year, 1, 1), None
    if code == "tm":
        return date(today.year, today.month, 1), None
    if code == "tq":
        return _quarter_start(today), None
    try:
        if code.startswith("m:"):
            y, m = (int(x) for x in code[2:].split("-"))
            start = date(y, m, 1)
            return start, _add_months(start, 1)
        if code.startswith("q:"):
            y, q = code[2:].split("-Q")
            start = date(int(y), 3 * (int(q) - 1) + 1, 1)
            return start, _add_months(start, 3)
        if code.startswith("y:"):
            y = int(code[2:])
            return date(y, 1, 1), date(y + 1, 1, 1)
    except (ValueError, TypeError):
        return None
    return None


def _period_label(code: str) -> str:
    for key, label, _ in PERIODS:
        if key == code:
            return label
    fixed = {"ytd": "This year", "tm": "This month", "tq": "This quarter"}
    if code in fixed:
        return fixed[code]
    window = _period_window(code)
    if window and code.startswith("m:"):
        return window[0].strftime("%b %Y")
    if window and code.startswith("q:"):
        return f"Q{(window[0].month - 1) // 3 + 1} {window[0].year}"
    if window and code.startswith("y:"):
        return str(window[0].year)
    return code


def _filter_qs(query) -> str:
    """The filter part of a query string, in a fixed order."""
    parts = [(k, (query.get(k) or [""])[0].strip()) for k in FILTER_KEYS]
    return urllib.parse.urlencode([(k, v) for k, v in parts if v])


def _apply_filters(listed, query, show: str = "open"):
    window = _period_window((query.get("period") or [""])[0].strip())
    due = (query.get("due") or [""])[0].strip()
    due_days = int(due) if due.isdigit() else None
    q = (query.get("q") or [""])[0].strip().casefold()
    ticker = (query.get("ticker") or [""])[0].strip().upper()
    right = (query.get("right") or [""])[0].strip().upper()
    side = (query.get("side") or [""])[0].strip().upper()
    tag = (query.get("tag") or [""])[0].strip().casefold()
    since = (query.get("since") or [""])[0].strip()
    until = (query.get("until") or [""])[0].strip()
    out = []
    for p in listed:
        if ticker and p.underlying != ticker:
            continue
        if due_days is not None and (not p.is_open or (p.expiry - date.today()).days > due_days):
            continue
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
        if window:
            # The date that matters is the one the list is about: when a
            # closed leg closed, when an open one was opened; either for all.
            when = (p.closed_on or p.opened_on) if show != "open" else p.opened_on
            start, stop = window
            if when < start or (stop is not None and when >= stop):
                continue
        out.append(p)
    return out


ADVANCED = ("due", "right", "side", "tag", "since", "until")
LABELS = {"ticker": "Ticker", "due": "Due", "q": "Find", "right": "Right", "side": "Side",
          "tag": "Tag", "since": "Opened from", "until": "Opened to", "period": "Period"}


def _with(query, show: str, **changes) -> str:
    """The list URL with the current filters, some of them changed. An empty
    value drops that filter."""
    params = [("show", show)]
    for k in FILTER_KEYS:
        v = changes[k] if k in changes else (query.get(k) or [""])[0].strip()
        if v:
            params.append((k, v))
    return r.esc("/?" + urllib.parse.urlencode(params))


def _same_state(a: str, b: str) -> bool:
    """Two query strings naming the same view, however they were encoded."""
    return urllib.parse.parse_qs(a, keep_blank_values=False) == \
        urllib.parse.parse_qs(b, keep_blank_values=False)


PRESET_VIEWS = (
    ("Expiring", "show=open&due=0"),
    ("This month", "show=all&period=tm"),
    ("This quarter", "show=all&period=tq"),
    ("This year", "show=all&period=ytd"),
    ("Last 12 months", "show=all&period=1y"),
)


PERIOD_SEGMENTS = (("", "All time"), ("3m", "3m"), ("6m", "6m"), ("ytd", "YTD"), ("1y", "12m"))
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _filter_bar(conn, query, show: str, token: str, positions=()) -> str:
    """Views on one line, filters on the next.

    A view is a whole destination -- status and filters together -- and a
    filter narrows the table in front of you. Keeping them apart is what lets
    "This quarter" mean everything that happened this quarter rather than
    whatever status happened to be showing."""
    get = lambda k: (query.get(k) or [""])[0].strip()  # noqa: E731
    tags = store.all_tags(conn)
    tickers = store.recent_underlyings(conn, limit=500)      # most recently traded first
    active = _filter_qs(query)
    current = f"show={show}" + (f"&{active}" if active else "")

    # --- views: one stacked control. The top row is the presets and the two
    # most recent saved views. Rows beneath appear only when they mean
    # something: the due windows while Expiring is on; the years when the
    # Views label is opened, then a picked year's quarters and months; the
    # older saved views and saving in the last row of the opened stack.
    def view_chip(label, state, extra_cls: str = "", on: bool | None = None) -> str:
        here = _same_state(state, current) if on is None else on
        return (f'<a class="flt chip view{extra_cls}{" here" if here else ""}" href="/?{r.esc(state)}">'
                f"{label}</a>")

    due, period = get("due"), get("period")
    expiring_on = show == "open" and due in dict(DUE)
    row1 = [view_chip("Expiring", "show=open&due=0", on=expiring_on)]
    row1 += [view_chip(label, state) for label, state in PRESET_VIEWS
             if label not in ("Expiring", "Last 12 months")]
    saved_all = store.load_views(conn)

    def saved_chip(v) -> str:
        here_cls = " here" if _same_state(v["filter"], current) else ""
        return (f'<span class="chip view saved{here_cls}"><a class="flt" href="/?{r.esc(v["filter"])}">'
                f'{r.esc(v["name"])}</a>'
                + r.form(f"/views/{r.esc(v['id'])}/delete", r.hidden("next", f"/?show={show}"),
                         token, submit="\u00d7", cls="inline") + "</span>")

    row1 += [saved_chip(v) for v in saved_all[:2]]

    years = sorted({p.opened_on.year for p in positions} | {date.today().year})
    picked_year = None
    if show == "all" and period[:2] in ("y:", "q:", "m:"):
        try:
            picked_year = int(period[2:6])
        except ValueError:
            picked_year = None
    shown = picked_year is not None
    label = (f'<a class="views-label{" here" if shown else ""}" href="#allviews" '
             'data-toggle="allviews" title="More views">Views <span class="caret"></span></a>')

    rows = [f'<div class="chips views">{label}{"".join(row1)}</div>']
    if expiring_on:
        rows.append('<div class="chips views sub" id="duerow"><span class="lbl">Due</span>'
                    + "".join(view_chip(text, f"show=open&due={code}") for code, text in DUE)
                    + "</div>")
    year_row = ('<div class="chips views sub"><span class="lbl">Year</span>'
                + "".join(view_chip(str(y), f"show=all&period=y:{y}", " year", on=(picked_year == y))
                          for y in years) + "</div>")
    sub_row = ""
    if picked_year is not None:
        subs = [view_chip(f"Q{q}", f"show=all&period=q:{picked_year}-Q{q}") for q in range(1, 5)]
        subs += [view_chip(name, f"show=all&period=m:{picked_year}-{m:02d}")
                 for m, name in enumerate(MONTHS, start=1)]
        sub_row = f'<div class="chips views sub"><span class="lbl">{picked_year}</span>{"".join(subs)}</div>'
    older = "".join(saved_chip(v) for v in saved_all[2:])
    save = ('<details class="save"><summary title="Keep the current status and filters under a name">'
            "Save view as&hellip;</summary>"
            + r.form("/views/save",
                     r.hidden("next", f"/?{current}") + r.hidden("filter", current)
                     + r.field("Name", "name", "", required=True, attrs=' placeholder="a name"'),
                     token, submit="Save", cls="inline save-view")
            + "</details>")
    saved_row = (f'<div class="chips views sub"><span class="lbl">Saved</span>'
                 f'{older or "<span class=dim>the newest two are above</span>"}{save}</div>')
    rows.append(f'<div id="allviews"{"" if shown else " hidden"}>{year_row}{sub_row}{saved_row}</div>')
    views_line = f'<div class="viewstack">{"".join(rows)}</div>'

    # --- filters
    def seg(items, key, current_value) -> str:
        links = []
        for value, label in items:
            cls = ' class="flt here"' if value == current_value else ' class="flt"'
            href = _with(query, value, **{}) if key == "show" else _with(query, show, **{key: value})
            links.append(f'<a href="{href}"{cls}>{label}</a>')
        return '<nav class="seg">' + "".join(links) + "</nav>"

    status = seg((("open", "Open"), ("closed", "Closed"), ("all", "All")), "show", show)
    advanced_n = sum(1 for k in ADVANCED if get(k))
    # In the order they are reached for: the ticker first, the rare ones in More.
    more = "".join([
        r.select("Due", "due", [("", "any time")] + list(DUE), get("due")),
        r.select("Right", "right", [("", "any"), ("PUT", "Put"), ("CALL", "Call")], get("right")),
        r.select("Side", "side", [("", "any"), ("SHORT", "Short"), ("LONG", "Long")], get("side")),
        r.select("Tag", "tag", [("", "any")] + [(t, t) for t in tags], get("tag")) if tags else "",
        r.field("Opened from", "since", get("since"), kind="date"),
        r.field("to", "until", get("until"), kind="date"),
        '<div class="actions"><button type="submit">Apply</button></div>',
    ])
    form = (f'<form class="filters" method="get" action="/">'
            + r.hidden("show", show)
            + r.select("Ticker", "ticker", [("", "any")] + [(t, t) for t in tickers], get("ticker"))
            + r.field("Find", "q", get("q"), attrs=' placeholder="note or tag" autocomplete="off"')
            + seg(PERIOD_SEGMENTS, "period",
                  get("period") if get("period") in dict(PERIOD_SEGMENTS) else None)
            + f'<details class="more"><summary>More{f" ({advanced_n})" if advanced_n else ""}</summary>'
            f'<div class="more-fields">{more}</div></details>'
            + "</form>")

    on = []
    for k in FILTER_KEYS:
        v = get(k)
        if not v:
            continue
        shown = _period_label(v) if k == "period" else (dict(DUE).get(v, v) if k == "due" else v)
        on.append(f'<span class="chip on">{r.esc(LABELS[k])}: {r.esc(shown)}'
                  f'<a class="flt x" href="{_with(query, show, **{k: ""})}" '
                  f'title="Remove this filter">&times;</a></span>')
    if on:
        on.append(f'<a class="flt clear" href="/?show={show}">Clear all</a>')
    active_line = f'<div class="chips active">{"".join(on)}</div>' if on else ""
    return (f'<div class="filterbar">{views_line}<div class="fbar">{status}{form}</div>'
            f"{active_line}</div>")


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
    all_periods = list(reversed(reports.by_period(positions, events, by)))
    show_all = (query.get("all") or [""])[0] == "1"
    shown = all_periods if show_all or len(all_periods) <= 12 else all_periods[:12]
    period_rows = [[r.esc(row.key), r.money(row.options), r.money(row.shares),
                    f"<b>{r.money(row.total)}</b>", r.esc(row.legs), r.money(row.running)]
                   for row in shown]
    more = ""
    if len(shown) < len(all_periods):
        more = (f'<p class="hint"><a href="/reports?by={by}&all=1">Show all '
                f"{len(all_periods)} periods</a></p>")

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
<details class="report" open><summary>Realized by period</summary>
<div class="tabs">{tabs}</div>
{r.table(["Period", "Options", "Shares", "Total", "Legs closed", "Running total"], period_rows)}
{more}
<p class="hint">Options are booked on the leg's close date, shares on the sale's
date. Legs still open are not here, at any value.</p>
</details>
<details class="report"><summary>By ticker</summary>
{r.table(["Ticker", "Options", "Shares", "Total", "Open premium", "At risk", "Open", "Legs closed"],
         ticker_rows)}
</details>
<details class="report"><summary>How short legs ended, by ticker</summary>
{r.table(outcome_head, outcome_rows(by_ticker_outcomes))}
<p class="hint">Hit means the leg ended at or past its target: closed at or
under the target price, or expired worthless. A roll or an assignment is
neither a hit nor a miss on its own; what the chain finally does is.</p>
</details>
<details class="report"><summary>How short legs ended, by days to expiry when opened</summary>
{r.table(outcome_head, outcome_rows(by_dte_outcomes))}
</details>
<details class="report"><summary>Export</summary>
<p><a href="/export/positions.csv">positions.csv</a> &middot;
<a href="/export/shares.csv">shares.csv</a> &middot;
<a href="/export/journal.json">journal.json</a> &middot;
<a href="/export/journal.db">journal.db</a> (full SQLite backup)</p>
<p class="hint">The CSV files carry the computed columns too: realized, carry,
break-even, capital at risk. The .db file is the whole journal; copy it
somewhere safe.</p>
</details>"""
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


# ---------------------------------------------------------------------------
# data health, snapshots, restore


def _health_issues(conn) -> list:
    positions = store.load_positions(conn)
    index = ChainIndex(positions)
    lots = store.load_lots(conn)
    disposals = store.load_disposals(conn)
    rule = store.matching_rule(conn)
    tickers = wheels.by_ticker(index, positions, lots, disposals, rule)
    blocked = {t.underlying: t.error for t in tickers if t.error}
    flags = [dict(f) for f in store.open_flags(conn)]
    issues = health.check(positions, lots, disposals, blocked=blocked, import_flags=flags)
    # An import flag whose live check no longer fires was fixed: close it.
    for f in health.cleared_flags(flags, issues):
        store.resolve_flag(conn, f["id"], "fixed in the data")
    return issues


HEALTH_TITLES = {
    "future_date": "Dated in the future",
    "past_expiry": "Past expiry, no outcome",
    "blocked_shares": "Share matching blocked",
    "dangling_link": "Points at a missing record",
    "roll_dates": "Roll dates do not line up",
    "split_sum": "Split halves do not add up",
    "duplicate": "Possible duplicate",
    "estimated": "Reconstructed, not recorded",
    "date_order": "Dates out of order",
    "missing_close": "Closed with no close date",
}


def data_page(conn, db_path: str, token: str, query) -> tuple[int, str]:
    issues = _health_issues(conn)
    flags = {f["id"]: f for f in (dict(x) for x in store.open_flags(conn))}
    errors = sum(1 for i in issues if i.severity == health.ERROR)
    warnings = len(issues) - errors

    cells = [("Errors", r.esc(errors), -1 if errors else None),
             ("Warnings", r.esc(warnings), None),
             ("Snapshots", r.esc(len(store.list_snapshots(db_path))), None)]
    strip = _cards(cells)

    if not issues:
        health_html = '<p class="callout ok">All clear: nothing in the journal disagrees with itself.</p>'
    else:
        rows = []
        for i in issues:
            title = HEALTH_TITLES.get(i.kind)
            if title is None and i.kind.startswith("import:"):
                title = "Import: " + i.kind[7:].replace("_", " ")
            badge = (f'<span class="badge st-blocked">error</span>' if i.severity == health.ERROR
                     else '<span class="badge st-assigned">warning</span>')
            who = r.esc(i.entity_id[:8]) if i.entity_id else '<span class="dim">-</span>'
            fix = f'<a class="btn" href="{r.esc(i.href)}">Fix</a>'
            if i.kind.startswith("import:"):
                # Imported flags are advisory; once looked at they can be dismissed.
                for fid, f in flags.items():
                    if f["entity_id"] == i.entity_id and i.kind == "import:" + f["kind"]:
                        fix += " " + r.form(f"/data/flag/{fid}/resolve", r.hidden("next", "/data"),
                                            token, submit="Dismiss", cls="inline")
                        break
            rows.append([badge, r.esc(title or i.kind), r.esc(i.entity_type.replace("_", " ")),
                         who, r.esc(i.detail), fix])
        health_html = r.table(["", "What", "Kind", "Record", "Detail", ""], rows, cls="health")

    snaps = store.list_snapshots(db_path)
    snap_rows = []
    for s in snaps:
        restore_form = r.form(
            "/data/restore",
            r.hidden("name", s["name"]) + r.hidden("next", "/data")
            + '<label class="check"><input type="checkbox" name="sure" value="1"> '
              "replace the journal with this snapshot</label>",
            token, submit="Restore", cls="inline restore")
        snap_rows.append([r.esc(s["at"]), r.esc(s["name"]), f"{s['bytes'] // 1024} KB", restore_form])
    take = r.form("/data/snapshot",
                  r.hidden("next", "/data")
                  + r.field("Label", "label", "", attrs=' placeholder="optional, e.g. before-cleanup"'),
                  token, submit="Take a snapshot now", cls="inline save-view")
    folder = store.snapshots_dir(db_path)

    body = f"""{strip}
<h2>Health</h2>
<p class="hint">Records that disagree with each other or with the calendar. Each
row leads to the page where it can be fixed; nothing here is changed for you.</p>
{health_html}
<h2>Snapshots</h2>
<p class="hint">A snapshot is a complete copy of the journal, taken through
SQLite's backup API so it is consistent even mid-write. They live in
<code>{r.esc(folder)}</code>. Restoring replaces the journal with a snapshot
after snapshotting what it replaces, so a restore can itself be undone.</p>
{take}
{r.table(["Taken", "File", "Size", ""], snap_rows)}
<h2>Export</h2>
<p><a href="/export/positions.csv">positions.csv</a> &middot;
<a href="/export/shares.csv">shares.csv</a> &middot;
<a href="/export/journal.json">journal.json</a> &middot;
<a href="/export/journal.db">journal.db</a> (download the whole journal)</p>"""
    return 200, r.page("Data", body, nav_here="data")


def do_snapshot(conn, db_path: str, form) -> None:
    name = store.snapshot(conn, db_path, _one(form, "label"))
    raise Redirect(_next(form, "/data"), f"Snapshot written: {name}")


def do_restore(conn, db_path: str, form) -> None:
    if _one(form, "sure") != "1":
        raise BadRequest("Tick the box to confirm: restoring replaces the whole journal")
    try:
        kept = store.restore(conn, db_path, _one(form, "name"))
    except ValueError as exc:
        raise BadRequest(str(exc)) from None
    raise Redirect(_next(form, "/data"),
                   f"Journal restored from {_one(form, 'name')}; the journal as it was is kept as {kept}")


def do_resolve_flag(conn, flag_id, form) -> None:
    store.resolve_flag(conn, int(flag_id), "dismissed on the Data page")
    raise Redirect(_next(form, "/data"), "Flag dismissed")


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


def expiring_redirect(query) -> None:
    """The old expiring page is the positions page with the Due filter: the
    same rows, the same buttons, the same forms."""
    window = (query.get("within") or ["0"])[0]
    raise Redirect(f"/?show=open&due={window}")


# ---------------------------------------------------------------------------
# audit


def audit_page(conn, token, query) -> tuple[int, str]:
    entity = (query.get("entity") or [None])[0]
    entries = store.audit_entries(conn, limit=300, entity_id=entity)

    # One row per action: the entries of a group are shown together and
    # undone together, because that is the only undo that leaves the journal
    # consistent (a split is three records). Entries older than grouping are
    # matched the way the store matches them: same second, same note.
    def same_action(a, b) -> bool:
        if a["group_id"] or b["group_id"]:
            return bool(a["group_id"]) and a["group_id"] == b["group_id"]
        na, nb = (x["action"].split(": ", 1)[1] if ": " in x["action"] else "" for x in (a, b))
        return bool(na) and na == nb and a["at"] == b["at"]

    groups: list[list] = []
    for e in entries:
        if e["action"].startswith(("revert", "redo")):
            continue        # bookkeeping: the action's own row shows its state
        if groups and same_action(groups[-1][0], e):
            groups[-1].append(e)
        else:
            groups.append([e])

    def who(e) -> str:
        return (f'<a href="/position/{r.esc(e["entity_id"])}">{r.esc(e["entity_id"][:8])}</a>'
                if e["entity_type"] == "position" else r.esc(e["entity_id"][:8]))

    rows = []
    for group in groups:
        first = group[-1]          # oldest entry of the action: its note names it
        kinds_ok = all(e["entity_type"] in ("position", "share_lot", "share_disposal") for e in group)
        state = store.action_state(conn, group)
        undo = ""
        if kinds_ok:
            if state == "undone":
                undo = r.form(f"/audit/{first['id']}/redo", "", token, submit="Redo", cls="inline")
            else:
                undo = r.form(f"/audit/{first['id']}/revert", "", token, submit="Undo", cls="inline")
        kinds = sorted({e["entity_type"].replace("_", " ") for e in group})
        what = _describe_action(conn, first)
        if len(group) > 1:
            what += f' <span class="dim">({len(group)} records)</span>'
        if state == "undone":
            when = (store.action_state_at(conn, group) or "")[11:16]
            what = f'<s class="dim">{what}</s> <small class="neg">undone at {r.esc(when)}</small>'
        rows.append([
            r.esc(first["at"].replace("T", " ")),
            r.esc(", ".join(kinds)),
            "<br>".join(who(e) for e in group[::-1]),
            what,
            undo,
        ])

    scope = (f' for <a href="/position/{r.esc(entity)}">{r.esc(entity[:8])}</a>'
             if entity else "")
    body = f"""<p class="hint">Every change is recorded{scope}. Undo takes back the whole
action a row belongs to -- a split is three records -- or nothing, and is itself
recorded. An undone action stays in the list, struck through, with Redo to
put it back. An action that a later one depends on cannot be undone until that
later one is.</p>
{r.table(["When (UTC)", "Kind", "Entity", "What", ""], rows, cls="audit")}"""
    return 200, r.page("History", body, nav_here="audit")


def _describe_action(conn, entry) -> str:
    """An audit entry in words a reader knows: the action's own note, and for
    an undo or redo, the note of the action it undid or redid."""
    verb, _, note = entry["action"].partition(": ")
    if verb in ("revert", "redo") and "#" in note:
        try:
            target = conn.execute("SELECT action FROM audit_log WHERE id = ?",
                                  (int(note.rsplit("#", 1)[1]),)).fetchone()
        except ValueError:
            target = None
        original = target["action"].partition(": ")[2] if target else ""
        word = "Undid" if verb == "revert" else "Redid"
        return f"<b>{word}</b> {r.esc(original) if original else r.esc(note)}"
    label = {"create": "recorded", "update": "changed", "delete": "removed"}.get(verb, verb)
    return f'<span class="dim">{r.esc(label)}</span> {r.esc(note or verb)}'


def do_redo(conn, entry_id, form) -> None:
    try:
        outcome = store.redo_audit_entry(conn, int(entry_id))
    except ValueError as exc:
        raise Redirect("/audit", f"!{exc}") from None
    raise Redirect("/audit", f"Redone {outcome}")


def do_revert(conn, entry_id, form) -> None:
    try:
        outcome = store.revert_audit_entry(conn, int(entry_id))
    except ValueError as exc:
        raise Redirect("/audit", f"!{exc}") from None
    raise Redirect("/audit", f"Undone {outcome}")


# ---------------------------------------------------------------------------
# shares


def _share_context(conn):
    positions = store.load_positions(conn)
    index = ChainIndex(positions)
    lots = store.load_lots(conn)
    disposals = store.load_disposals(conn)
    rule = store.matching_rule(conn)
    tickers = wheels.by_ticker(index, positions, lots, disposals, rule)
    return positions, index, lots, disposals, tickers, {}


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
    """The buy / sell / buy-write form on the same leg grid as every option
    form: date first, one row per leg, the extras below."""
    rate = _fee_rate(conn)
    when = r.field("Date", "on", r.today_iso(), kind="date", required=True)
    ticker_box = _box("underlying", underlying, kind="text", label="Ticker",
                      autofocus=not underlying,
                      attrs=' list="tickers" autocapitalize="characters" autocomplete="off"')
    shares_word = _fixed("Shares")
    extras = ""

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
        row = [_tag("SELL", "Shares sold", "shares"), ticker_box, _blank(), _blank(), shares_word,
               _box("quantity", "", step="1", label="Shares", attrs=' min="1"'),
               _box("price", "", label="Price / share"),
               _box("fee", "0.00", label="Fee", required=False)]
        extras = (r.select("From lot", "lot_id", options, "",
                           hint="Leave on the account rule unless the broker matched a specific lot")
                  + f'<script type="application/json" id="lot-remaining">{{{",".join(attrs)}}}</script>')
        rows, action, submit, cls = [row], "/shares/sell", "Record sale", "btn-close"
    elif kind == "buy-write":
        call = [_tag("STO", "Sell to open", "open"), ticker_box,
                _box("expiry", _next_friday().isoformat(), kind="date", label="Call expiry"),
                _box("strike", "", step="0.5", label="Call strike"), _fixed("Call"),
                _box("contracts", "", step="1", label="Contracts", required=False,
                     attrs=' min="1" placeholder="shares/100"'),
                _box("call_price", "", label="Call premium / share"),
                _box("option_fee", str(rate), label="Option fee", required=False)]
        buy = [_tag("BUY", "Shares bought", "shares"), _fixed("same"), _blank(), _blank(), shares_word,
               _box("shares", "", step="1", label="Shares bought", attrs=' min="1"'),
               _box("share_price", "", label="Share price"),
               _box("share_fee", "0.00", label="Share fee", required=False)]
        rows, action, submit, cls = [call, buy], "/shares/buy-write", "Record buy-write", "btn-open"
    else:
        row = [_tag("BUY", "Shares bought", "shares"), ticker_box, _blank(), _blank(), shares_word,
               _box("quantity", "", step="1", label="Shares", attrs=' min="1"'),
               _box("price", "", label="Price / share"),
               _box("fee", "0.00", label="Fee", required=False)]
        extras = r.field("Notes", "notes", "")
        rows, action, submit, cls = [row], "/shares/buy", "Record purchase", "btn-open"

    body = when + _leg_grid(rows) + (f'<div class="grid compact">{extras}</div>' if extras else "")
    return r.form(action, r.hidden("next", back) + body, token, submit=submit, cls="two-part",
                  submit_cls=cls, cancel=f"{back}#record", cancel_attrs=" data-form-close")


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
        ("Open calls", (f'<span class="{"neg" if t.uncovered_shares else "pos"}">'
                        f"{t.open_call_shares} of {max(t.held, 0)} shares</span>"
                        if t.open_call_shares else '<span class="dim">none</span>')),
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

    suggest_html = ""

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

    left = _remaining_by_lot(conn, name)
    pinned_to: dict[str, int] = {}
    for d in disposals:
        for lot_id in d.specific_lot_ids:
            pinned_to[lot_id] = pinned_to.get(lot_id, 0) + 1

    def via(pid):
        p = by_id.get(pid) if pid else None
        return (f'<a href="/position/{r.esc(p.id)}">{r.contract(p)}</a>' if p
                else '<span class="dim">-</span>')

    lot_rows = []
    for lot in lots:
        when = ('<span class="neg" title="dated after today">future</span> '
                if lot.acquired_on > today else "")
        # Why a lot cannot be removed is said, not hidden in a tooltip.
        if lot.id in pinned_to:
            remove = f'<span class="dim inuse">in use: {pinned_to[lot.id]} sale(s) pinned to it</span>'
        else:
            remove = r.form(f"/shares/lot/{r.esc(lot.id)}/delete", r.hidden("next", back),
                            token, submit="remove", cls="inline")
        # Every entered value in one form, folded behind "edit". Confirming
        # against a statement is what turns an estimate into a record.
        confirm = ("" if not lot.estimated else
                   '<label class="check"><input type="checkbox" name="confirmed" value="1"> '
                   "confirmed against a statement (no longer an estimate)</label>")
        edit = (f'<details class="edit" id="lot-{r.esc(lot.id)}"><summary>edit</summary>'
                + '<div class="bubble">' + r.form(
                    f"/shares/lot/{r.esc(lot.id)}/edit",
                    r.hidden("next", back) + '<div class="grid compact">' + "".join([
                        r.field("Acquired", "acquired_on", lot.acquired_on.isoformat(),
                                kind="date", required=True),
                        r.field("Shares", "quantity", str(lot.quantity), kind="number", step="1",
                                required=True),
                        r.field("Cost / share", "cost_per_share", str(lot.cost_per_share),
                                kind="number", step="0.01", required=True),
                        r.field("Fee", "fee", str(lot.fee), kind="number", step="0.01"),
                        r.field("Notes", "notes", lot.notes),
                    ]) + "</div>" + confirm,
                    token, submit="Save corrections", cls="two-part",
                    cancel=back, cancel_attrs=" data-close-details")
                + "</div></details>")
        flags = ""
        if lot.estimated:
            flags += ('<span class="badge st-assigned" title="reconstructed from memory">'
                      "estimated</span>")
        if lot.notes:
            flags += f' <small class="dim note">{r.esc(lot.notes)}</small>'
        remaining = left.get(lot.id, lot.quantity)
        lot_rows.append([
            f"{when}{r.esc(lot.acquired_on)}", r.esc(lot.source.value.replace("_", " ").lower()),
            r.esc(lot.quantity),
            (r.esc(remaining) if remaining else '<span class="dim">0</span>'),
            r.esc(price(lot.cost_per_share)), r.money(lot.fee),
            via(lot.assigning_position_id), flags or '<span class="dim">-</span>',
            f'<span class="rowtools">{edit}{remove}</span>',
        ])

    disp_rows = []
    for d in disposals:
        lot_text = "account rule"
        if d.specific_lot_ids:
            names = [_lot_label(l) for l in lots if l.id in d.specific_lot_ids]
            lot_text = "&#128204; " + r.esc("; ".join(names) or "a lot no longer present")
        edit = (f'<details class="edit" id="sale-{r.esc(d.id)}"><summary>edit</summary>'
                + '<div class="bubble">' + r.form(
                    f"/shares/disposal/{r.esc(d.id)}/edit",
                    r.hidden("next", back) + '<div class="grid compact">' + "".join([
                        r.field("Sold on", "disposed_on", d.disposed_on.isoformat(), kind="date",
                                required=True),
                        r.field("Shares", "quantity", str(d.quantity), kind="number", step="1",
                                required=True),
                        r.field("Price / share", "proceeds_per_share", str(d.proceeds_per_share),
                                kind="number", step="0.01", required=True),
                        r.field("Fee", "fee", str(d.fee), kind="number", step="0.01"),
                        r.field("Notes", "notes", d.notes),
                    ]) + "</div>",
                    token, submit="Save corrections", cls="two-part",
                    cancel=back, cancel_attrs=" data-close-details")
                + "</div></details>")
        remove = r.form(f"/shares/disposal/{r.esc(d.id)}/delete", r.hidden("next", back),
                        token, submit="remove", cls="inline")
        disp_rows.append([
            r.esc(d.disposed_on), r.esc(d.kind.value.replace("_", " ").lower()),
            r.esc(d.quantity), r.esc(price(d.proceeds_per_share)), r.money(d.fee),
            via(d.disposing_position_id), lot_text, f'<span class="rowtools">{edit}{remove}</span>',
        ])

    body = f"""<p class="hint"><a href="/shares/{name}">&larr; {name} shares</a></p>
{warn}
<p class="hint">These are the records as entered. Edit one that is wrong, or remove
it. A lot marked <em>estimated</em> was reconstructed from memory: check it against
a statement, correct it, and confirm. Every change is logged and can be undone
from <a href="/audit">History</a>.</p>
<h2>Lots - {len(lot_rows)}</h2>
{r.table(["Acquired", "Source", "Qty", ("Left", "Shares of this lot still held"), "Cost/sh", "Fee",
          "From", "Notes", ""], lot_rows, cls="rawdata")}
<h2>Disposals - {len(disp_rows)}</h2>
{r.table(["Date", "Kind", "Qty", "Price", "Fee", "Via", "Lot", ""], disp_rows, cls="rawdata")}"""
    return 200, r.page(f"{name} raw data", body, nav_here="shares")


def _shares_held(conn, underlying: str) -> int:
    lots = [l for l in store.load_lots(conn) if l.underlying == underlying]
    disposals = [d for d in store.load_disposals(conn) if d.underlying == underlying]
    return sum(l.quantity for l in lots) - sum(d.quantity for d in disposals)


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
        store.delete_lot(conn, lot_id, "removed by hand", unlink_open=False)
    except store.InUseError as exc:
        raise BadRequest(str(exc)) from None
    except ValueError as exc:
        raise BadRequest(str(exc)) from None
    raise Redirect(_next(form, "/shares"), "Lot removed; matching rebuilt")


def do_edit_lot(conn, lot_id, form) -> None:
    changes = {
        "acquired_on": _past(_date(form, "acquired_on", label="Acquired"), "Acquired"),
        "quantity": _int(form, "quantity", label="Shares"),
        "cost_per_share": _decimal(form, "cost_per_share", label="Cost per share"),
        "fee": _decimal(form, "fee", ZERO),
        "notes": _one(form, "notes"),
    }
    if _one(form, "confirmed") == "1":
        changes["estimated"] = False
    try:
        current = next((l for l in store.load_lots(conn) if l.id == lot_id), None)
        if current is not None and current.acquired_on != changes["acquired_on"]:
            # The assignment that produced the lot moves with it.
            store.redate_lot(conn, lot_id, changes["acquired_on"], "corrected by hand")
        store.edit_lot(conn, lot_id, changes, "corrected by hand")
    except ValueError as exc:
        raise BadRequest(str(exc)) from None
    raise Redirect(_next(form, "/shares"), "Lot corrected; matching rebuilt")


def do_edit_disposal(conn, disposal_id, form) -> None:
    changes = {
        "disposed_on": _past(_date(form, "disposed_on", label="Sold on"), "Sold on"),
        "quantity": _int(form, "quantity", label="Shares"),
        "proceeds_per_share": _decimal(form, "proceeds_per_share", label="Price per share"),
        "fee": _decimal(form, "fee", ZERO),
        "notes": _one(form, "notes"),
    }
    try:
        store.edit_disposal(conn, disposal_id, changes, "corrected by hand")
    except ValueError as exc:
        raise BadRequest(str(exc)) from None
    raise Redirect(_next(form, "/shares"), "Sale corrected; matching rebuilt")


def do_redate_lot(conn, lot_id, form) -> None:
    on = _past(_date(form, "on", label="Acquired on"), "Acquired on")
    try:
        store.redate_lot(conn, lot_id, on, "re-dated by hand")
    except ValueError as exc:
        raise BadRequest(str(exc)) from None
    raise Redirect(_next(form, "/shares"), f"Lot re-dated to {on}; matching rebuilt")
