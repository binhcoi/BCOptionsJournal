"""Page handlers.

Each returns ``(status, body)`` for a page or raises ``Redirect``. All the
arithmetic lives in bcoj.engine and all the writing in bcoj.db.store, so these
are about one thing: making entry fast and mistakes hard.
"""

from datetime import date, timedelta
from decimal import Decimal

from ..db import store
from ..domain.enums import Direction, Right, Status
from ..domain.money import ZERO, fmt, parse_money, q2
from ..domain.types import Position
from ..engine import actions, validate
from ..engine.chains import ChainIndex
from ..engine.pnl import open_cash, realized_pl
from ..engine.risk import break_even, capital_at_risk, credit_to_recover, put_risk
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


# ---------------------------------------------------------------------------
# positions list


def positions_page(conn, query) -> tuple[int, str]:
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

    queue = validate.expiring(open_ones)
    banner = ""
    if queue:
        banner = (
            f'<div class="callout"><a href="/expiring">'
            f"{len(queue)} position(s) at or past expiry need an outcome"
            "</a></div>"
        )

    rows = []
    for p in sorted(listed, key=lambda p: (p.expiry, p.underlying)):
        carry = index.carry(p)
        chain = index.chain(p)
        risk = capital_at_risk(p)
        dte = (p.expiry - date.today()).days if p.is_open else None
        be = break_even(chain) if p.is_open else None
        tgt = target(p, carry) if p.is_open else None
        rows.append([
            f'<a href="/position/{r.esc(p.id)}">{r.contract(p)}</a>',
            r.dte_cell(dte),
            r.esc(p.status.value.title()),
            r.money(open_cash(p)),
            r.money(carry, dash="0.00"),
            r.money(None if p.is_open else realized_pl(p)),
            r.money(risk),
            r.money(be.price if be else None),
            r.money(tgt.price if tgt and tgt.applicable else None),
            f'{chain.leg_count}' if chain.leg_count > 1 else '<span class="dim">1</span>',
        ])

    totals = _portfolio_totals(conn, positions, index)
    tabs = " ".join(
        f'<a href="/?show={k}" class="{"here" if show == k else ""}">{label}</a>'
        for k, label in (("open", "Open"), ("closed", "Closed"), ("all", "All"))
    )

    body = f"""{banner}
{totals}
<div class="tabs">{tabs}</div>
{r.table(
    ["Contract", "DTE", "Status", "Credit", "Carry", "Realized",
     "At risk", "Break-even", "Target", "Legs"],
    rows, cls="positions")}
<p class="hint">Credit is what came in on opening. Carry is what the chain
brought forward. They are different scopes and are never added together.</p>"""
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

    facts = [
        ("Status", r.esc(position.status.value.title())),
        ("Opened", r.esc(position.opened_on)),
        ("Expiry", r.esc(position.expiry)),
        ("Credit received", r.money(open_cash(position))),
        ("Chain carry", r.money(carry, dash="0.00")),
        ("Capital at risk", r.money(capital_at_risk(position))),
        ("Put risk", r.money(put_risk(position))),
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

    legs = [
        [
            (f'<a href="/position/{r.esc(l.id)}">{r.contract(l)}</a>'
             if l.id != position.id else f"<b>{r.contract(l)}</b>"),
            r.esc(l.opened_on),
            r.esc(l.closed_on or "-"),
            r.esc(l.status.value.title()),
            r.money(open_cash(l)),
            r.money(None if l.is_open else realized_pl(l)),
        ]
        for l in chain.legs
    ]
    timeline = f"""<h2>Chain - {chain.leg_count} leg(s)</h2>
{r.table(["Leg", "Opened", "Closed", "Status", "Credit", "Realized"], legs)}
<div class="totals">
  <div><span>Chain realized</span><b>{r.money(chain.realized)}</b></div>
  <div><span>Days</span><b>{r.num(chain.days)}</b></div>
</div>"""

    actions_block = (
        _action_forms(position, token, query) if position.is_open
        else '<p class="dim">This position is closed. Nothing to do.</p>'
    )
    notes = (f"<h2>Notes</h2><p>{r.esc(position.notes)}</p>"
             if position.notes else "")

    body = f"""<div class="totals wide">{fact_rows}</div>
{actions_block}
{timeline}
{notes}
<p class="hint"><a href="/audit?entity={r.esc(position.id)}">History for this
position</a></p>"""
    return 200, r.page(r.contract_text(position), body, nav_here="positions")


def _action_forms(position, token, query) -> str:
    """The five things you do to an open position, plus split."""
    which = (query.get("do") or [""])[0]
    pid = r.esc(position.id)
    rate = Decimal("0.65") * position.quantity

    def tab(key, label):
        cls = ' class="here"' if which == key else ""
        return f'<a href="/position/{pid}?do={key}"{cls}>{label}</a>'

    tabs = " ".join([
        tab("close", "Close"), tab("roll", "Roll"), tab("expire", "Expire"),
        tab("assign", "Assign"), tab("split", "Split"),
    ])

    if which == "close":
        inner = r.form(f"/position/{pid}/close", "".join([
            r.field("Closed on", "closed_on", r.today_iso(), kind="date",
                    required=True),
            r.field("Close price / share", "close_price", "", kind="number",
                    step="0.01", required=True, autofocus=True),
            r.field("Fee", "close_fee", str(q2(rate)), kind="number",
                    step="0.01"),
        ]), token, submit="Close position", cls="grid")
    elif which == "roll":
        inner = r.form(f"/position/{pid}/roll", "".join([
            r.field("Rolled on", "on", r.today_iso(), kind="date",
                    required=True),
            r.field("Buy-back price", "close_price", "", kind="number",
                    step="0.01", required=True, autofocus=True),
            r.field("Buy-back fee", "close_fee", str(q2(rate)), kind="number",
                    step="0.01"),
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
        ]), token, submit="Roll", cls="grid")
    elif which == "expire":
        inner = r.form(f"/position/{pid}/expire", "".join([
            r.field("Expired on", "on", position.expiry.isoformat(),
                    kind="date", required=True),
        ]), token, submit="Expired worthless", cls="grid")
    elif which == "assign":
        acquiring = (position.direction is Direction.SHORT) == (
            position.right is Right.PUT
        )
        moves = (f"acquire {position.shares} shares at {fmt(position.strike)}"
                 if acquiring else
                 f"deliver {position.shares} shares at {fmt(position.strike)}")
        inner = f"""<p class="callout">This will also
        <b>{r.esc(moves)}</b>, so the stock side cannot be forgotten.</p>
{r.form(f"/position/{pid}/assign", "".join([
    r.field("Assigned on", "on", position.expiry.isoformat(), kind="date",
            required=True),
    r.field("Option fee", "close_fee", "0.00", kind="number", step="0.01"),
    r.field("Share fee", "share_fee", "0.00", kind="number", step="0.01"),
]), token, submit="Record assignment", cls="grid")}"""
    elif which == "split":
        inner = f"""<p class="hint">Divide this position so the halves can take
        different paths - part assigned, the rest rolled on. Chain history is
        divided pro-rata, not duplicated.</p>
{r.form(f"/position/{pid}/split", "".join([
    r.field("Contracts to peel off", "quantity", "",
            kind="number", step="1", required=True, autofocus=True,
            hint=f"Between 1 and {position.quantity - 1}"),
    r.field("On", "on", r.today_iso(), kind="date", required=True),
]), token, submit="Split", cls="grid")}"""
    else:
        inner = '<p class="hint">Pick an action.</p>'

    return f'<h2>Actions</h2><div class="tabs">{tabs}</div>{inner}'


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
    raise Redirect(f"/position/{position_id}", f"Closed - realized {fmt(pl)}")


def do_expire(conn, position_id, form) -> None:
    position = _load_open(conn, position_id)
    result = actions.expire(position, _date(form, "on", position.expiry))
    store.apply(conn, result)
    pl = realized_pl(result.updated[0])
    raise Redirect(f"/position/{position_id}",
                   f"Expired worthless - kept {fmt(pl)}")


def do_assign(conn, position_id, form) -> None:
    position = _load_open(conn, position_id)
    result = actions.assign(
        position,
        on=_date(form, "on", position.expiry),
        close_fee=_decimal(form, "close_fee", ZERO),
        share_fee=_decimal(form, "share_fee", ZERO),
    )
    errors = store.apply(conn, result)
    note = result.summary
    if errors:
        note = "!" + note + " - share matching blocked: " + \
               "; ".join(errors.values())
    raise Redirect(f"/position/{position_id}", note)


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
        raise Redirect(
            f"/position/{position_id}?do=roll",
            "!Roll refused: " + "; ".join(
                p.message for p in validate.errors(problems)
            ),
        )

    store.apply(conn, result)
    index = ChainIndex(store.load_positions(conn))
    chain = index.chain(store.load_position(conn, successor.id))
    raise Redirect(
        f"/position/{successor.id}",
        f"{result.summary}. Chain now carries {fmt(chain.carry)},"
        f" net credit {fmt(chain.net_credit)}",
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
        f"/position/{first.id}",
        f"{result.summary}. Both halves are now open;"
        f" the other is {second.quantity} contracts",
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
