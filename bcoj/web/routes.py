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

from .. import __version__
from ..db import store, schema
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
from . import auth
from . import render as r
from .static import THEMES


class Redirect(Exception):
    def __init__(self, location: str, flash: str = "", headers=()):
        self.location = location
        self.flash = flash
        self.headers = tuple(headers)


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
        classes.append(f"camp camp-{rail}")
    cls = f' class="{" ".join(classes)}"' if classes else ""

    note = ""
    if p.is_superseded:
        halves = sorted(index.successors(p), key=lambda h: h.quantity)
        if halves:
            parts = [str(h.quantity) for h in halves]
            note = (f'<span class="camp-note" title="split into {" + ".join(parts)}">'
                    f'&#x2442; {"+".join(parts)}</span>')
    elif siblings > 1 and p.is_open and not in_chain:
        note = (f'<span class="camp-note" title="1 of {siblings} open in this chain">'
                f"&#x2442;{siblings}</span>")

    campaign_size = len(index.campaign(p))

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
    elif campaign_size > 1:
        target_url = (f"{here}{act_q}{anchor}" if expanded
                      else f"{here}{act_q}&chain={pid}{anchor}")
        title = "Hide the chain" if expanded else "Show the whole chain"
        text = "&#9650;" if expanded else str(campaign_size)
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
    elif campaign_size > 1 and with_actions:
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
    expanded_legs = ([leg for leg, _ in index.campaign(expanded_target)]
                     if expanded_target else [])
    expanded_ids = {leg.id for leg in expanded_legs}
    # The block is drawn where the clicked leg sits in the list. If that leg
    # is no longer listed -- it was just closed while its chain was open --
    # anchor on the first leg of the campaign that is, or expand nothing: the
    # campaign's rows must never be held back and then not drawn.
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




# ---------------------------------------------------------------------------
# the facts table
#
# One vocabulary for every page. A row is a figure with its usual trading
# name; a column is a scope (this leg, its branch, the campaign; options,
# shares, both). A figure is never shown twice under two names.

REALIZED = "Realized P/L"
OPEN_PREMIUM = "Open premium"
TO_CLOSE = "Cost to close"
AT_TARGET = "P/L at target"
AT_RISK = "Capital at risk"
BREAK_EVEN = "Break-even"
TARGET_PRICE = "Target price"
CONTRACTS = "Contracts"
STRIKE = "Strike"
OPENED = "Opened"
ENDS = "Expires"
DAYS = "Days"
LEGS = "Legs"
CLOSED_LEGS = "Legs won"
SHARES = "Shares"
ROW_ORDER = (REALIZED, OPEN_PREMIUM, TO_CLOSE, AT_TARGET, AT_RISK, BREAK_EVEN, TARGET_PRICE,
             CONTRACTS, STRIKE, OPENED, ENDS, DAYS, LEGS, CLOSED_LEGS, SHARES)


def _sign(value):
    return None if value is None or value == 0 else (1 if value > 0 else -1)


def _cell(html, tone=None):
    """One value, nothing after it. A row reads the same in every column."""
    return (html, tone)


def _money(value, tone="sign", dash: str = "-"):
    if value is None:
        return None
    return _cell(r.money(value, dash=dash), _sign(value) if tone == "sign" else tone)


def _facts(title: str, note: str, rows, chips: str = "") -> str:
    """The strip: equal cells across the page, label over figure. Every figure
    is present, a dash where it does not apply, so the strip has the same shape
    wherever it appears. ``rows`` are (label, cell) with cell (html, tone) or
    None. ``chips`` is an optional line of facts under the figures."""
    cap = f'<p class="cap">{r.esc(title)}' + (f" <small>{note}</small>" if note else "") + "</p>"
    cells = []
    for label, c in rows:
        if c is None:
            cells.append(f'<div><span>{r.esc(label)}</span><b class="none">-</b></div>')
        else:
            cells.append(f'<div><span>{r.esc(label)}</span><b>{c[0]}</b></div>')
    return (f'<div class="strip"><div class="facts wide">{cap}'
            f'<div class="cells" style="--n:{len(cells)}">{"".join(cells)}</div>{chips}</div></div>')


def _chips(items) -> str:
    """A line of short facts. ``items`` are html or (html, "warn")."""
    out = []
    for it in items:
        html, cls = (it, "") if isinstance(it, str) else it
        out.append(f'<span class="chip{" " + cls if cls else ""}">{html}</span>')
    return f'<div class="chips facts">{"".join(out)}</div>' if out else ""


def _columns(title: str, note: str, col: dict, chips: str = "") -> str:
    """A label -> cell dict laid out in ROW_ORDER."""
    order = [l for l in ROW_ORDER if l in col] + [l for l in col if l not in ROW_ORDER]
    return _facts(title, note, [(l, col[l]) for l in order], chips)


def _kv(title: str, note: str, pairs, chips: str = "") -> str:
    """A strip from (label, html[, tone]) pairs."""
    rows = [(p[0], (p[1], p[2] if len(p) > 2 else None)) for p in pairs]
    return _facts(title, note, rows, chips)


def _score_column(sc) -> dict:
    """The scorecard as a column: always the same eight figures, None where
    one does not apply, so every strip has the same shape."""
    open_ = bool(sc.open_legs)
    return {
        REALIZED: _money(sc.banked, dash="0.00"),
        OPEN_PREMIUM: _money(sc.in_hand, dash="0.00") if open_ else None,
        TO_CLOSE: _money(-sc.to_close, dash="0.00") if open_ else None,
        AT_TARGET: _cell(f"<b>{r.money(sc.at_target, dash='0.00')}</b>", _sign(sc.at_target)) if open_ else None,
        AT_RISK: _money(sc.at_risk, dash="0.00", tone=None) if open_ else None,
        BREAK_EVEN: _money(sc.break_even, tone=None),
        CONTRACTS: _cell(str(sc.contracts)) if open_ else None,
    }


def _scorecard_html(sc, scope: str) -> str:
    """Positions page: one column, the table in front of you."""
    return _columns("This table", scope, _score_column(sc))


def _campaign_strip(index, camp, sc, chain, lineage=None, conn=None) -> str:
    """The strip for the campaign, or for one branch of it when ``lineage``
    is given, with a line of chips saying how the trade has moved."""
    legs = lineage or list(camp.legs)
    root, head = legs[0], chain.head
    opens = [l for l in legs if l.is_open]
    whole = lineage is None
    chips = [f"{len(legs)} leg(s) since {root.opened_on}"]
    if whole and camp.rolls:
        chips.append(f"{camp.rolls} roll(s)")
    if whole and camp.splits:
        chips.append(f"{camp.splits} split(s)")
    days = ((date.today() if opens else max((l.closed_on or l.expiry) for l in legs)) - root.opened_on).days
    chips.append(f"{days} days")
    now = sum(l.quantity for l in opens) if opens else head.quantity
    if now != root.quantity:
        chips.append((f"{root.quantity} &rarr; {now} contracts (x{now / root.quantity:.1f})",
                      "warn" if now > root.quantity else ""))
    strikes = sorted({l.strike for l in (opens or [head])})
    if len(legs) > 1 and strikes != [root.strike]:
        drift = q2(strikes[-1] - root.strike)
        chips.append(f"strike {r.esc(price(root.strike))} &rarr; "
                     f"{' / '.join(r.esc(price(x)) for x in strikes)} "
                     f"({'down' if drift < 0 else 'up'} {r.esc(price(abs(drift)))})")
    if whole and opens and camp.at_risk_now != camp.at_risk_start:
        chips.append(f"at risk {fmt(camp.at_risk_start)} &rarr; {fmt(camp.at_risk_now)}")
    if head.is_open:
        owed = credit_to_recover(chain)
        if owed > 0:
            chips.append((f"{fmt(owed)} still to recover before it nets positive", "warn"))
    if sc.closed_legs:
        chips.append(f"{sc.wins} of {sc.closed_legs} closed leg(s) won")
    if whole and camp.assigned_legs:
        chips.append(f"{camp.assigned_shares} shares assigned")
    if conn is not None and head.is_open and head.right is Right.CALL and head.direction is Direction.SHORT:
        held = _shares_held(conn, head.underlying)
        called = sum(q.shares for q in store.load_positions(conn, head.underlying)
                     if q.is_open and q.right is Right.CALL and q.direction is Direction.SHORT)
        link = f'<a href="/shares/{r.esc(head.underlying)}">{held} shares held</a>'
        if held <= 0:
            chips.append(("no shares behind it: naked", "warn"))
        elif called > held:
            chips.append((f"{link}, {called - held} uncovered", "warn"))
        else:
            chips.append(link)
    title = "Campaign" if whole else "This branch"
    return _columns(title, "", _score_column(sc), _chips(chips))


def _portfolio_totals(conn, positions, index) -> str:
    """Reports dashboard: three strips, each with only what applies to it.
    Portfolio puts options and shares together; the other two keep them apart."""
    sc = scorecard(index, positions)
    lots = store.load_lots(conn)
    disposals = store.load_disposals(conn)
    tickers = wheels.by_ticker(index, positions, lots, disposals, store.matching_rule(conn))
    clean = [t for t in tickers if not t.error]
    shares_pl = wheels.realized_shares(tickers)
    held_cost = q2(sum((t.held_cost for t in clean if t.held > 0), ZERO))
    held = sum(t.held for t in clean)
    holding = sum(1 for t in clean if t.held > 0)
    wheel = q2(sum((t.total for t in clean), ZERO))
    blocked = [t.underlying for t in tickers if t.error]
    open_ones = [p for p in positions if p.is_open]
    puts = sum(1 for p in open_ones if p.right is Right.PUT)
    calls = sum(1 for p in open_ones if p.right is Right.CALL)

    # Cash the open puts could still demand and money already spent on shares
    # are different things; they sit side by side and are never added.
    portfolio = [
        (REALIZED, r.money(q2(sc.banked + shares_pl), dash="0.00")),
        (AT_TARGET, f"<b>{r.money(q2(sc.at_target + shares_pl), dash='0.00')}</b>"),
        (AT_RISK, r.money(sc.at_risk, dash="0.00")),
        ("Shares at cost", r.money(held_cost, dash="0.00")),
    ]
    options = _score_column(sc)
    del options[BREAK_EVEN]
    options["Open positions"] = _cell(f"{len(open_ones)}")
    options["Puts / calls"] = _cell(f"{puts} / {calls}")
    shares = [
        (REALIZED, r.money(shares_pl, dash="0.00")),
        ("Shares held", r.esc(held)),
        ("At cost", r.money(held_cost if held_cost else None)),
        ("Tickers holding", r.esc(holding)),
        ("Wheel total", r.money(wheel, dash="0.00")),
    ]
    warn = ""
    if blocked:
        warn = _chips([(f'<a href="/shares">{len(blocked)} ticker(s) cannot be matched: '
                        f"{r.esc(', '.join(blocked))}</a>", "warn")])
    return (_kv("Portfolio", "options and shares together", portfolio)
            + _columns("Options", "", options)
            + _kv("Shares", "", shares, warn))


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
    camp = campaign(index, position)
    # The same scorecard as the positions page, over this campaign, then the
    # campaign's shape in a line of chips.
    # The same one-line strip as the positions page, scoped to the campaign.
    # The leg itself is the highlighted row in the table below; a branch of a
    # split campaign gets a second strip of its own.
    sc = scorecard(index, [position])
    strips = _campaign_strip(index, camp, sc, chain, conn=conn)
    if camp.splits:
        lineage = list(index.lineage(position))
        scb = scorecard(index, lineage, whole_campaigns=False)
        strips += _campaign_strip(index, camp, scb, chain, lineage, conn=conn)

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
    legs = [leg for leg, _ in index.campaign(position)]
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
    """The decision strip: the chain after the roll, each figure with how it
    moved, from the engine's own roll run on a copy."""
    def was(before, after, better) -> str:
        """The change under the figure, coloured by whether it is good news.
        ``better`` says which direction is good; None means neither."""
        if before is None or after is None:
            return ""
        delta = q2(after - before)
        if delta == 0:
            return "<small>unchanged</small>"
        cls = "dim" if better is None else ("pos" if (delta > 0) == better else "neg")
        arrow = "&uarr;" if delta > 0 else "&darr;"
        return (f'<small class="{cls}">{arrow} {fmt(abs(delta))} '
                f"<span class=\"dim\">was {fmt(before)}</span></small>")

    def signed(value) -> str:
        return r.money(value, dash="0.00")

    roll_word = "credit" if pv.is_credit else "debit"
    tgt = pv.target_after
    target = (f'<span class="pos">{r.esc(price(tgt.price))}</span>'
              f"<small>expected {fmt(tgt.expected_pl)}</small>" if tgt.applicable
              else '<span class="neg">none: net debit</span>')
    # For a short put a lower break-even is better; for a short call, higher.
    be_up_good = pv.new_leg.right is Right.CALL
    be_after = pv.break_even_after.price if pv.break_even_after else None
    be_before = pv.break_even_before.price if pv.break_even_before else None
    be_cls = "dim"
    if be_after is not None and be_before is not None and be_after != be_before:
        be_cls = "pos" if (be_after > be_before) == be_up_good else "neg"
    risk_cls = ("neg" if pv.at_risk_before is not None and pv.at_risk_after is not None
                and pv.at_risk_after > pv.at_risk_before else "dim")
    cells = [
        (f"Roll {roll_word}", f'<span class="{"pos" if pv.is_credit else "neg"}">'
                              f"{fmt(abs(pv.this_roll))}</span>"),
        ("Closing books", signed(pv.closing_realized)),
        ("Carry after", signed(pv.carry_after) + was(pv.carry_before, pv.carry_after, True)),
        ("Net credit after", signed(pv.net_credit_after)
                             + was(pv.net_credit_before, pv.net_credit_after, True)),
        ("Break-even after", (f'<span class="{be_cls}">{fmt(be_after)}</span>' if be_after is not None
                              else '<span class="dim">-</span>') + was(be_before, be_after, be_up_good)),
        ("At risk after", (f'<span class="{risk_cls}">{fmt(pv.at_risk_after)}</span>'
                           if pv.at_risk_after is not None else '<span class="dim">-</span>')
                          + was(pv.at_risk_before, pv.at_risk_after, False)),
        ("Target", target),
        ("To recover", f'<span class="neg">{fmt(pv.to_recover_after)}</span>' if pv.underwater_after
                       else '<span class="pos">0.00</span>'),
        ("New leg", f"{pv.dte_after} DTE"),
    ]
    chips = []
    if pv.grows:
        old, new = pv.closing_leg.quantity, pv.new_leg.quantity
        chips.append((f"<b>Size grows {old} &rarr; {new}</b> contracts (x{new / old:.2f})", "warn"))
    if pv.strike_change:
        chips.append(f"Strike {r.esc(price(pv.closing_leg.strike))} &rarr; "
                     f"{r.esc(price(pv.new_leg.strike))} "
                     f"({'down' if pv.strike_change < 0 else 'up'} "
                     f"{r.esc(price(abs(pv.strike_change)))})")
    if pv.underwater_after:
        chips.append((f"Still under water by <b>{fmt(pv.to_recover_after)}</b>: that much "
                      "credit is owed before the chain nets positive", "warn"))
    return _kv("After this roll", "the chain as the engine would record it", cells, _chips(chips))


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
        cells.append(("Largest", f"{r.esc(top.underlying)} {pct(top.exposure)}%"))
    if naked:
        cells.append(("Naked calls", f'<span class="neg">{naked}</span>', -1))
    totals = _kv("Exposure", "", cells)

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
    counts = ""

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
<p class="hint">Exports are under <a href="/options">Options</a>.</p>"""
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
    issues = health.check(positions, lots, disposals, blocked=blocked, import_flags=flags,
                          fee_rate=_fee_rate(conn))
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


def data_page(conn, token: str, query) -> tuple[int, str]:
    issues = _health_issues(conn)
    flags = {f["id"]: f for f in (dict(x) for x in store.open_flags(conn))}
    errors = sum(1 for i in issues if i.severity == health.ERROR)
    warnings = len(issues) - errors

    strip = _kv("Journal", "", [("Errors", r.esc(errors), -1 if errors else None),
                                ("Warnings", r.esc(warnings), None)])

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

    body = f"""{strip}
<h2>Health</h2>
<p class="hint">Records that disagree with each other or with the calendar. Each
row leads to the page where it can be fixed; nothing here is changed for you.
Snapshots and exports are under <a href="/options">Options</a>.</p>
{health_html}"""
    return 200, r.page("Data", body, nav_here="data")


# ---------------------------------------------------------------------------
# login, options


def login_page(conn, token: str, query, problem: str = "") -> tuple[int, str]:
    nxt = (query.get("next") or ["/"])[0]
    if not nxt.startswith("/") or nxt.startswith("//"):
        nxt = "/"
    hint = ""
    if auth.password_is_default(conn):
        hint = (f'<p class="callout">First login: the password is <b>{auth.DEFAULT_PASSWORD}</b>. '
                "You will be asked to change it.</p>")
    error = f'<p class="callout">{r.esc(problem)}</p>' if problem else ""
    body = hint + error + r.form(
        "/login",
        r.hidden("next", nxt)
        + r.field("Password", "password", kind="password", required=True, autofocus=True),
        token, submit="Log in", cls="login")
    return (200 if not problem else 403), r.page("Log in", body, bare=True)


def do_login(conn, token: str, form):
    if not auth.check_password(conn, _one(form, "password")):
        return login_page(conn, token, {"next": [_one(form, "next", "/")]}, "Wrong password.")
    cookie = auth.cookie_header(auth.issue_token(conn))
    where = _next(form, "/")
    if auth.password_is_default(conn):
        raise Redirect("/options", "!Set a password before anything else. The default is public.",
                       headers=[("Set-Cookie", cookie)])
    raise Redirect(where, headers=[("Set-Cookie", cookie)])


def do_logout() -> None:
    raise Redirect("/login", headers=[("Set-Cookie", auth.clear_cookie_header())])


def do_change_password(conn, form) -> None:
    current, new, again = _one(form, "current"), _one(form, "new"), _one(form, "again")
    if not auth.check_password(conn, current):
        raise BadRequest("The current password is wrong")
    if len(new) < 8:
        raise BadRequest("The new password needs at least 8 characters")
    if new == auth.DEFAULT_PASSWORD:
        raise BadRequest("That is the default password; choose another")
    if new != again:
        raise BadRequest("The two copies of the new password differ")
    auth.set_password(conn, new)
    # The secret rotated with the password, so this session is over too.
    raise Redirect("/login", "Password changed. Log in again.",
                   headers=[("Set-Cookie", auth.clear_cookie_header())])


def do_set_theme(conn, form) -> None:
    theme = _one(form, "theme", "system")
    if theme not in THEMES:
        raise BadRequest("No such theme")
    store.set_setting(conn, "theme", theme)
    raise Redirect("/options", f"Theme: {theme}")


def options_page(conn, db_path: str, token: str, query) -> tuple[int, str]:
    """One panel of settings: what each row is about on the left, the control
    on the right. Every row has the same shape."""
    must_change = auth.password_is_default(conn)

    def row(title: str, hint: str, body: str, anchor: str = "") -> str:
        idattr = f' id="{anchor}"' if anchor else ""
        return (f'<section class="opt"{idattr}><div class="opt-head"><h2>{r.esc(title)}</h2>'
                f'<p class="hint">{hint}</p></div><div class="opt-body">{body}</div></section>')

    theme = r.form("/options/theme",
                   '<select name="theme" id="f_theme" aria-label="Theme">'
                   + "".join(f'<option value="{n}"{" selected" if n == store.get_setting(conn, "theme", "system") else ""}>'
                             f'{"System (follow the device)" if n == "system" else n.capitalize()}</option>'
                             for n in THEMES)
                   + "</select>",
                   token, submit="Save", cls="row theme")

    pw_hint = ('<p class="callout">The default password is public. Set your own now.</p>'
               if must_change else "")
    password = r.form(
        "/options/password",
        r.field("Current password", "current", kind="password", required=True,
                autofocus=must_change)
        + r.field("New password", "new", kind="password", required=True,
                  attrs=' minlength="8" autocomplete="new-password"')
        + r.field("New password again", "again", kind="password", required=True,
                  attrs=' minlength="8" autocomplete="new-password"'),
        token, submit="Change password", cls="stack")
    logout = r.form("/logout", "", token, submit="Log out", cls="row plain")

    snaps = store.list_snapshots(db_path)
    snap_rows = []
    for s in snaps:
        restore_form = r.form(
            "/options/restore",
            r.hidden("name", s["name"]) + r.hidden("next", "/options")
            + '<label class="check"><input type="checkbox" name="sure" value="1"> '
              "replace the journal with this</label>",
            token, submit="Restore", cls="inline restore")
        link = f'<a href="/snapshot/{r.esc(s["name"])}" download>{r.esc(s["name"])}</a>'
        snap_rows.append([r.esc(s["at"]), link, f"{s['bytes'] // 1024} KB", restore_form])
    take = r.form("/options/snapshot",
                  r.hidden("next", "/options")
                  + '<input type="text" name="label" aria-label="Label" placeholder="label, optional">',
                  token, submit="Take a snapshot", cls="row")
    listing = (r.table(["Taken", "File", "Size", ""], snap_rows, cls="snapshots") if snaps
               else '<p class="hint">None yet.</p>')

    about = (f'<dl class="kv"><dt>Version</dt><dd>{r.esc(__version__)}</dd>'
             f'<dt>Schema</dt><dd>{r.esc(schema.current_version(conn))}</dd>'
             f'<dt>Journal</dt><dd><code>{r.esc(db_path)}</code></dd>'
             f'<dt>Snapshots</dt><dd><code>{r.esc(store.snapshots_dir(db_path))}</code></dd></dl>')

    body = '<div class="options">' + "".join([
        row("Theme", "System follows the device; the others hold.", theme),
        row("Password", "At least 8 characters. Changing it logs every browser out. Forgotten: start "
                        "the server once with <code>BCOJ_PASSWORD=new-password</code>.",
            pw_hint + password + logout, anchor="password"),
        row("Snapshots", "A complete, consistent copy of the journal. Click a name to download. "
                         "Restoring snapshots the current journal first, so it can be undone.",
            take + listing),
        row("Export", "The engine's answers alongside the entered values.",
            '<p class="links"><a href="/export/positions.csv">positions.csv</a>'
            '<a href="/export/shares.csv">shares.csv</a>'
            '<a href="/export/journal.json">journal.json</a>'
            '<a href="/export/journal.db">journal.db</a></p>'),
        row("About", "", about),
    ]) + "</div>"
    return 200, r.page("Options", body, nav_here="options")


def snapshot_file(db_path: str, name: str):
    """A snapshot by name, only ever one that the listing knows."""
    if name not in {s["name"] for s in store.list_snapshots(db_path)}:
        raise BadRequest("no such snapshot")
    with open(store.snapshots_dir(db_path) / name, "rb") as f:
        return 200, f.read(), "application/vnd.sqlite3", name


def do_snapshot(conn, db_path: str, form) -> None:
    name = store.snapshot(conn, db_path, _one(form, "label"))
    raise Redirect(_next(form, "/options"), f"Snapshot written: {name}")


def do_restore(conn, db_path: str, form) -> None:
    if _one(form, "sure") != "1":
        raise BadRequest("Tick the box to confirm: restoring replaces the whole journal")
    try:
        kept = store.restore(conn, db_path, _one(form, "name"))
    except ValueError as exc:
        raise BadRequest(str(exc)) from None
    raise Redirect(_next(form, "/options"),
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
    since = (query.get("since") or [""])[0].strip()
    until = (query.get("until") or [""])[0].strip()
    period = (query.get("period") or [""])[0].strip()
    for value in (since, until):
        if value:
            _date({"d": [value]}, "d", label="Date")      # refuse anything but a date
    window = _period_window(period) if period else None
    if period and window is None:
        raise BadRequest("No such period")
    if window:
        # A period is the same shortcut the positions page offers; it fills
        # the range rather than adding a second kind of filter.
        since = window[0].isoformat()
        until = (window[1] - timedelta(days=1)).isoformat() if window[1] else ""
    entries = store.audit_entries(conn, limit=300, entity_id=entity, since=since, until=until)

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
        what = _describe_group(conn, group)
        if state == "undone":
            at = store.action_state_at(conn, group) or ""
            what = (f'<s class="dim">{what}</s> <small class="neg">undone at '
                    f'{_local_time(at, short=True)}</small>')
        rows.append([_local_time(first["at"]), what, undo])

    scope = (f' for <a href="/position/{r.esc(entity)}">{r.esc(entity[:8])}</a>'
             if entity else "")
    def audit_href(**params) -> str:
        keep = {"entity": entity} if entity else {}
        keep.update({k: v for k, v in params.items() if v})
        return "/audit" + ("?" + urllib.parse.urlencode(keep) if keep else "")

    shortcuts = list(PERIOD_SEGMENTS) + [("tm", "This month"), ("tq", "This quarter")]
    seg = '<nav class="seg">' + "".join(
        f'<a href="{audit_href(period=code)}" class="flt{" here" if code == period or (not code and not (period or since or until)) else ""}">{label}</a>'
        for code, label in shortcuts) + "</nav>"
    filters = (seg + f'<form method="get" action="/audit" class="filters">'
               + (r.hidden("entity", entity) if entity else "")
               + r.field("From", "since", since, kind="date")
               + r.field("To", "until", until, kind="date")
               + '<button type="submit">Apply</button>'
               + (f' <a class="btn cancel" href="{audit_href()}">Clear</a>' if since or until else "")
               + "</form>")
    empty = ('<p class="hint">Nothing recorded in that range.</p>' if not rows and (since or until)
             else "")
    body = f"""<p class="hint">Every change is recorded{scope}. Undo takes back the whole
action a row belongs to, a split is three records, or nothing, and is itself
recorded. An undone action stays in the list, struck through, with Redo to
put it back. An action that a later one depends on cannot be undone until that
later one is.</p>
<div class="filterbar">{filters}</div>
{empty}{r.table(["When", "What", ""], rows, cls="audit") if rows else ""}"""
    return 200, r.page("History", body, nav_here="audit")


def _local_time(stamp: str, short: bool = False) -> str:
    """A UTC stamp the script shows in the reader's own time zone. Without
    script it reads as UTC, and says so."""
    if not stamp:
        return ""
    shown = stamp[11:16] if short else stamp[:16].replace("T", " ")
    return f'<time datetime="{r.esc(stamp)}" title="UTC">{r.esc(shown)}{"" if short else " UTC"}</time>'


_VERBS = {"entered by hand": "Recorded", "entered by hand, already over": "Recorded, already over",
          "create": "Recorded", "update": "Changed", "delete": "Removed"}
# Fields worth naming when they change, in reading order.
_CHANGE_FIELDS = (("status", "status"), ("close_price", "close"), ("close_fee", "close fee"),
                  ("closed_on", "closed on"), ("quantity", "contracts"), ("strike", "strike"),
                  ("expiry", "expiry"), ("open_price", "open"), ("open_fee", "fee"),
                  ("opened_on", "opened"), ("cost_per_share", "cost"),
                  ("proceeds_per_share", "proceeds"), ("acquired_on", "acquired"),
                  ("disposed_on", "sold on"), ("notes", "notes"))


def _entity_words(e) -> str:
    """The record an audit entry is about, as a trader would say it, linked
    to where it lives. From the entry's own snapshot, so a removed record
    still has a name."""
    data = {}
    for blob in (e["after"], e["before"]):
        if blob:
            try:
                data = json.loads(blob)
            except ValueError:
                data = {}
            if data:
                break
    kind, eid = e["entity_type"], e["entity_id"]
    under = r.esc(data.get("underlying", ""))
    if kind == "position":
        if data:
            right = (data.get("option_right") or "?")[:1]
            words = (f'{under} {r.esc(price(data.get("strike")))}{right} {r.esc(data.get("expiry", ""))}'
                     f' &times;{r.esc(data.get("quantity", ""))}')
            side = "sold" if data.get("direction") == "SHORT" else "bought"
            tail = f' <span class="dim">{side} at {r.esc(_words(data.get("open_price")))}'
            if data.get("open_fee") not in (None, "", "0", "0.00"):
                tail += f', fee {r.esc(_words(data.get("open_fee")))}'
            tail += "</span>"
        else:
            words, tail = eid[:8], ""
        return f'<a href="/position/{r.esc(eid)}">{words}</a>{tail}'
    if kind == "share_lot" and data:
        words = f'{r.esc(data.get("quantity", ""))} {under} shares at {r.esc(price(data.get("cost_per_share")))}'
    elif kind == "share_disposal" and data:
        words = f'{r.esc(data.get("quantity", ""))} {under} shares sold at {r.esc(price(data.get("proceeds_per_share")))}'
    else:
        return r.esc(f"{kind.replace('_', ' ')} {eid[:8]}")
    return f'<a href="/shares/{under}">{words}</a>' if under else words


def _changes(e, said: str = "") -> str:
    """For an update, the fields that moved: 'close 0.50, closed on ...'.
    A status the verb already says ("Closed") is not repeated."""
    if not (e["before"] and e["after"]):
        return ""
    try:
        before, after = json.loads(e["before"]), json.loads(e["after"])
    except ValueError:
        return ""
    parts = []
    for key, label in _CHANGE_FIELDS:
        if key in after and before.get(key) != after.get(key):
            old, new = before.get(key), after.get(key)
            if key == "status" and new:
                if str(new).lower() not in said.lower():
                    parts.append(r.esc(str(new).lower()))
            elif old in (None, "") or key in ("closed_on", "close_price", "close_fee"):
                parts.append(f"{label} {r.esc(_words(new))}")
            else:
                parts.append(f"{label} {r.esc(_words(old))} &rarr; {r.esc(_words(new))}")
    return ", ".join(parts)


def _words(value) -> str:
    if value is None or value == "":
        return "none"
    try:
        return price(Decimal(str(value))) if "." in str(value) else str(value)
    except (ValueError, ArithmeticError):
        return str(value)


def _describe_group(conn, group) -> str:
    """One action in words: what was done, to which record(s), and what
    moved. The note the action was recorded with names it."""
    first = group[-1]
    verb, _, note = first["action"].partition(": ")
    label = _VERBS.get(note) or (note[:1].upper() + note[1:] if note else _VERBS.get(verb, verb))
    lines = [_entity_words(e) for e in group[::-1]]
    detail = _changes(first, label) if verb == "update" and len(group) == 1 else ""
    out = f"<b>{r.esc(label)}</b> " + ("<br>".join(lines) if len(lines) > 1 else lines[0])
    if len(group) > 1:
        out += f' <span class="dim">({len(group)} records)</span>'
    if detail:
        out += f' <span class="dim">{detail}</span>'
    return out


def _describe_action(conn, entry) -> str:
    """An audit entry in words: the action's own note, and for an undo or
    redo, the note of the action it undid or redid. Used by flashes."""
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
        basis = t.basis
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
            r.money(basis.unit_price if basis else None),
            r.money(basis.min_call_strike if basis else None),
            r.money(t.realized if not t.error else None),
            r.money(t.option_pl),
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
        totals.append(("Blocked tickers", f'<span class="neg">{len(blocked)}</span>', -1))

    warn = ""
    if blocked:
        warn = "".join(
            f'<div class="callout"><a href="/shares/{r.esc(t.underlying)}">'
            f"{r.esc(t.underlying)}</a>: {r.esc(t.error)}</div>" for t in blocked)
    hint = ""

    kind = (query.get("form") or [""])[0]
    body = f"""{warn}{hint}
{_kv("All tickers", "", totals)}
{_share_forms(conn, kind, "", token, "/shares", "/shares?")}
{r.table(["Ticker", "Position", "Held at cost", "Adj. basis", "Min call",
          "Share P/L", "Option P/L", "Wheel total", "Lots"], rows, cls="shares")}
<p class="hint">Shares are held at what was paid: the strike for an assignment,
the fill for a purchase. Option P/L is every closed option on the ticker,
lifetime. Adjusted basis is the cost of the shares held less option P/L and the
P/L on shares already sold, per share held; negative means they are paid for.
Min call is the lowest strike that does not lock in a loss. Wheel total is
option P/L plus share P/L, realized only.</p>"""
    return 200, r.page("Shares", body, nav_here="shares")


def ticker_page(conn, underlying, token, query) -> tuple[int, str]:
    positions, index, lots, disposals, tickers, suggestions = _share_context(conn)
    name = underlying.upper()
    match_ = [t for t in tickers if t.underlying == name]
    if not match_:
        return 404, r.page("Not found", f"<p>No shares recorded for {r.esc(name)}.</p>")
    t = match_[0]
    by_id = {p.id: p for p in positions}

    b = t.basis
    facts = [
        ("Held", r.esc(t.held) if not t.error else f'<span class="neg">{t.held}</span>'),
        ("Cost of shares held", r.money(t.held_cost if t.held > 0 else None)),
        ("Share P/L", r.money(t.realized if not t.error else None)),
        ("Option P/L", r.money(t.option_pl)),
        ("Wheel total", r.money(t.total if not t.error else None)),
        ("Adjusted basis", r.money(b.unit_price) if b else '<span class="dim">-</span>'),
        ("Min call strike", r.money(b.min_call_strike) if b else '<span class="dim">-</span>'),
        ("Open calls", (f'<span class="{"neg" if t.uncovered_shares else "pos"}">'
                        f"{t.open_call_shares} of {max(t.held, 0)} shares</span>"
                        if t.open_call_shares else '<span class="dim">none</span>')),
    ]
    fact_html = _kv(name, f"{len(t.lots)} lot(s)", facts)

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
        origin = by_id.get(lot.assigning_position_id) if lot.assigning_position_id else None
        if origin is not None:
            badge += f' <a class="dim" href="/position/{r.esc(origin.id)}">{r.contract(origin)}</a>'
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
            cover or '<span class="dim">-</span>',
            r.money(v.share_realized if v.disposed else None),
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
{fact_html}
<h2>Lots - {len(t.lots)}</h2>
{r.table(["Acquired", "Source", "Qty", "Held", "Cost/sh", "Covered", "Share P/L", "Days"],
         lot_rows, cls="lots")}
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
