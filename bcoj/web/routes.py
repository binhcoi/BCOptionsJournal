"""Page handlers.

Each returns ``(status, body)`` for a page or raises ``Redirect``. All the
arithmetic lives in bcoj.engine and all the writing in bcoj.db.store, so these
are about one thing: making entry fast and mistakes hard.
"""

from datetime import date, timedelta
from decimal import Decimal

from ..db import store
from ..domain.enums import Direction, Right, Status
from ..domain.money import ZERO, fmt, parse_money, price, q2
from ..domain.types import Position
from ..engine import actions, validate
from ..engine.chains import ChainIndex
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
POSITION_COLUMNS = ["", "Contract", "DTE", "Status", "Open price", "Close price",
                    "Credit", "Closing", "Realized", "Carry", "Break-even",
                    "At risk", "Legs"]


def _position_row(p, index, *, here: str = "/", act_id: str = "", which: str = "",
                  expanded: bool = False, in_chain: bool = False,
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
            note = ('<span class="fam-note">&rarr; split into '
                    + " + ".join(str(h.quantity) for h in halves) + "</span>")
    elif siblings > 1 and p.is_open and not in_chain:
        note = (f'<span class="fam-note" title="This chain has {siblings} open'
                f' positions">&#x2442; 1 of {siblings} open in this chain</span>')

    family_size = len(index.family(p))

    actions_html = ""
    if with_actions and p.is_open:
        actions_html = '<div class="row-actions">' + "".join(
            f'<a class="act act-{k}{" here" if p.id == act_id and k == which else ""}"'
            f' href="{here}&act={pid}&do={k}{anchor}">{label}</a>'
            for k, label in ACTIONS
        ) + "</div>"

    if in_chain and not (current and expanded):
        legs_html = ""      # the chain is what is being shown
    elif family_size > 1:
        target_url = f"{here}{anchor}" if expanded else f"{here}&chain={pid}{anchor}"
        title = "Hide the chain" if expanded else "Show the whole chain"
        text = "&#9650;" if expanded else str(family_size)
        legs_html = (f'<a class="legs{" here" if expanded else ""}"'
                     f' href="{target_url}" title="{title}">{text}</a>')
    else:
        legs_html = '<span class="dim">1</span>'

    # Always a link, so an expanded row still leads to its page. The row being
    # viewed is marked by an arrow in the gutter, not inline.
    label = f'<a href="/position/{pid}">{r.contract(p)}</a>'
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
        toggle = f' data-chain="{here}#row-{r.esc(collapse_to)}"'
    elif family_size > 1 and with_actions:
        toggle = f' data-chain="{here}&chain={pid}{anchor}"'

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
        f"<td>{label}{note}{actions_html}</td>"
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
    form_html = _action_form(p, which, token, index.carry(p), back=here)
    return (
        f'<tr class="action-row"><td colspan="{len(POSITION_COLUMNS)}">'
        f'<div class="action-inline"><h3>{dict(ACTIONS).get(which, which)}'
        f' &middot; {r.contract(p)}</h3>{form_html}</div></td></tr>'
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

    act_id = (query.get("act") or [""])[0]
    which = (query.get("do") or [""])[0]
    chain_id = (query.get("chain") or [""])[0]
    here = f"/?show={show}"

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

    rows = []
    for p in listed:
        if p.id in expanded_ids and p.id != chain_id:
            continue  # rendered as part of the expanded chain

        if p.id == chain_id:
            rows.append(_chain_head_row(index, expanded_legs, p, here))
            for leg in expanded_legs:
                rows.append(_position_row(
                    leg, index, here=here, act_id=act_id, which=which,
                    expanded=(leg.id == chain_id), in_chain=True,
                    current=(leg.id == chain_id), collapse_to=chain_id,
                ))
                if leg.id == act_id and which and leg.is_open:
                    rows.append(_action_row(leg, index, which, token, here))
            continue

        root_id = roots.get(p.id)
        rows.append(_position_row(
            p, index, here=here, act_id=act_id, which=which,
            siblings=size.get(root_id, 0) if root_id else 0,
            rail=rail_of.get(root_id),
        ))
        if p.id == act_id and which and p.is_open:
            rows.append(_action_row(p, index, which, token, here))

    totals = _portfolio_totals(conn, positions, index)
    tabs = " ".join(
        f'<a href="/?show={k}" class="{"here" if show == k else ""}">{label}</a>'
        for k, label in (("open", "Open"), ("closed", "Closed"), ("all", "All"))
    )

    table_html = r.table(POSITION_COLUMNS, rows, cls="positions")
    if (query.get("partial") or [""])[0] == "table":
        # Just the table, for the script that swaps it in place.
        return 200, table_html

    body = f"""{banner}
{totals}
<div class="tabs">{tabs}</div>
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
    cells = [
        ("Realized P/L", r.money(realized)),
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

    fields = "".join([
        r.field("Ticker", "underlying", _one(form, "underlying"),
                required=True, autofocus=True,
                attrs=' list="tickers" autocapitalize="characters"'
                      ' autocomplete="off"'),
        r.field("Opened", "opened_on", _one(form, "opened_on", r.today_iso()),
                kind="date", required=True),
        r.field("Expiry", "expiry",
                _one(form, "expiry", _next_friday().isoformat()),
                kind="date", required=True,
                hint="Defaults to the coming Friday"),
        r.select("Right", "right",
                 [("PUT", "Put"), ("CALL", "Call")], _one(form, "right", "PUT")),
        r.select("Side", "direction",
                 [("SHORT", "Short (sold to open)"),
                  ("LONG", "Long (bought to open)")],
                 _one(form, "direction", "SHORT")),
        r.field("Strike", "strike", _one(form, "strike"), kind="number",
                step="0.01", required=True),
        r.field("Contracts", "quantity", quantity, kind="number", step="1",
                required=True),
        r.field("Premium / share", "open_price", _one(form, "open_price"),
                kind="number", step="0.01", required=True),
        r.field("Fee", "open_fee", _one(form, "open_fee", default_fee),
                kind="number", step="0.01",
                hint=f"Auto-filled at {fmt(rate)}/contract; editable"),
        r.field("Notes", "notes", _one(form, "notes"),
                hint="Why this trade, in your words"),
    ])
    body = f"""{r.problems_block(problems)}
{r.datalist("tickers", tickers)}
{r.form("/new", f'<div class="grid" data-fee-rate="{r.esc(rate)}">{fields}</div>',
        token, submit="Add position")}
<p class="hint">Shortcuts: <kbd>+7</kbd>, <kbd>+14</kbd>, <kbd>+30</kbd> in the
expiry box jump that many days out. Submitting leaves you on a fresh form.</p>"""
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
        opened_on=_date(form, "opened_on", date.today()),
        open_price=_decimal(form, "open_price", label="Premium"),
        open_fee=_decimal(form, "open_fee", q2(rate * abs(quantity))),
        close_fee=_decimal(form, "open_fee", q2(rate * abs(quantity))),
        status=Status.OPEN,
        notes=_one(form, "notes"),
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

    fact_rows = "".join(
        f"<div><span>{r.esc(k)}</span><b>{v}</b></div>" for k, v in facts
    )

    if position.is_open:
        which = (query.get("do") or [""])[0]
        actions_block = (
            "<h2>Actions</h2>" + _action_tabs(position, which, here)
            + _action_form(position, which, token, carry, back=here)
        )
    else:
        actions_block = '<p class="dim">This position is closed. Nothing to do.</p>'

    notes = (f"<h2>Notes</h2><p>{r.esc(position.notes)}</p>"
             if position.notes else "")

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


def _action_form(position, which: str, token: str, carry, back: str) -> str:
    """The form for one action. Used inline on the positions page and on the
    position's own page; ``back`` is where to return afterwards."""
    pid = r.esc(position.id)
    rate = Decimal("0.65") * position.quantity
    tgt = target(position, carry)
    # The profit target is the natural default for a buy-back price: it is
    # what you were aiming at. Blank when the chain has no target to aim for.
    suggested = str(tgt.price) if tgt.applicable else ""
    nxt = r.hidden("next", back)

    if which == "close":
        return r.form(f"/position/{pid}/close", nxt + "".join([
            r.field("Closed on", "closed_on", r.today_iso(), kind="date",
                    required=True),
            r.field("Close price / share", "close_price", suggested,
                    kind="number", step="0.01", required=True, autofocus=True,
                    hint="Pre-filled with the 50% target" if suggested else ""),
            r.field("Fee", "close_fee", str(q2(rate)), kind="number",
                    step="0.01"),
        ]), token, submit="Close position", cls="grid")

    if which == "roll":
        closing = r.fieldset("1. Close this leg", "".join([
            r.field("Rolled on", "on", r.today_iso(), kind="date",
                    required=True),
            r.field("Buy-back price", "close_price", suggested, kind="number",
                    step="0.01", required=True, autofocus=True,
                    hint=f"Closes {r.contract_text(position)}"),
            r.field("Buy-back fee", "close_fee", str(q2(rate)), kind="number",
                    step="0.01"),
        ]))
        opening = r.fieldset("2. Open the new leg", "".join([
            r.field("New expiry", "new_expiry",
                    _next_friday(position.expiry).isoformat(), kind="date",
                    required=True),
            r.field("New strike", "new_strike", str(position.strike),
                    kind="number", step="0.01", required=True),
            r.field("New premium", "new_price", "", kind="number", step="0.01",
                    required=True),
            r.field("New fee", "new_fee", str(q2(rate)), kind="number",
                    step="0.01"),
            r.field("Contracts", "new_quantity", str(position.quantity),
                    kind="number", step="1",
                    hint="Rolls often resize; state it explicitly"),
        ]), hint=f"Same side and right as the leg being closed: "
                 f"{position.direction.value.lower()} "
                 f"{position.right.value.lower()}s.")
        return r.form(f"/position/{pid}/roll", nxt + closing + opening, token,
                      submit="Roll", cls="two-part")

    if which == "expire":
        return r.form(f"/position/{pid}/expire", nxt + "".join([
            r.field("Expired on", "on", position.expiry.isoformat(),
                    kind="date", required=True),
        ]), token, submit="Expired worthless", cls="grid")

    if which == "assign":
        acquiring = (position.direction is Direction.SHORT) == (
            position.right is Right.PUT
        )
        moves = (f"acquire {position.shares} shares at {fmt(position.strike)}"
                 if acquiring else
                 f"deliver {position.shares} shares at {fmt(position.strike)}")
        return f"""<p class="callout">This will also <b>{r.esc(moves)}</b>,
        so the stock side cannot be forgotten.</p>
{r.form(f"/position/{pid}/assign", nxt + "".join([
    r.field("Assigned on", "on", position.expiry.isoformat(), kind="date",
            required=True),
    r.field("Option fee", "close_fee", "0.00", kind="number", step="0.01"),
    r.field("Share fee", "share_fee", "0.00", kind="number", step="0.01"),
]), token, submit="Record assignment", cls="grid")}"""

    if which == "split":
        return f"""<p class="hint">Divide this position so the halves can take
        different paths - part assigned, the rest rolled on. Both halves keep
        the open date and price; chain history is divided pro-rata between
        them, and this record stays as the account of the split.</p>
{r.form(f"/position/{pid}/split", nxt + "".join([
    r.field("Contracts to peel off", "quantity", "",
            kind="number", step="1", required=True, autofocus=True,
            hint=f"Between 1 and {position.quantity - 1}"),
    r.field("On", "on", r.today_iso(), kind="date", required=True),
]), token, submit="Split", cls="grid")}"""

    return '<p class="hint">Pick an action.</p>'


def _action_tabs(position, which: str, base: str) -> str:
    links = []
    for key, label in ACTIONS:
        cls = ' class="here"' if which == key else ""
        links.append(f'<a href="{base}?do={key}"{cls}>{label}</a>')
    return '<div class="tabs">' + " ".join(links) + "</div>"


# ---------------------------------------------------------------------------
# action endpoints


def _load_open(conn, position_id) -> Position:
    position = store.load_position(conn, position_id)
    if position is None:
        raise BadRequest("no such position")
    return position


def do_close(conn, position_id, form) -> None:
    position = _load_open(conn, position_id)
    result = actions.close(
        position,
        _date(form, "closed_on", date.today()),
        _decimal(form, "close_price", label="Close price"),
        _decimal(form, "close_fee", ZERO),
    )
    store.apply(conn, result)
    pl = realized_pl(result.updated[0])
    raise Redirect(_next(form, f"/position/{position_id}"),
                   f"Closed {r.contract_text(position)} - realized {fmt(pl)}")


def do_expire(conn, position_id, form) -> None:
    position = _load_open(conn, position_id)
    result = actions.expire(position, _date(form, "on", position.expiry))
    store.apply(conn, result)
    pl = realized_pl(result.updated[0])
    raise Redirect(_next(form, f"/position/{position_id}"),
                   f"{r.contract_text(position)} expired worthless - kept {fmt(pl)}")


def do_assign(conn, position_id, form) -> None:
    position = _load_open(conn, position_id)
    result = actions.assign(
        position,
        on=_date(form, "on", position.expiry),
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
        on=_date(form, "on", date.today()),
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
        on=_date(form, "on", date.today()),
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
        if e["entity_type"] == "position" and not e["action"].startswith("revert"):
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
