"""HTML rendering. Plain functions returning strings; no template engine.

Every value that came from a human goes through ``esc``. There is no path that
interpolates unescaped input, which is the whole reason for building the markup
in functions rather than concatenating strings ad hoc.
"""

from datetime import date
from html import escape

from ..domain.money import fmt
from .static import VERSION

APP_NAME = "BC Options Journal"


def esc(value) -> str:
    return escape("" if value is None else str(value), quote=True)


def money(value, dash: str = "-") -> str:
    """A money cell, coloured by sign. Negatives in parentheses, never a minus."""
    if value is None:
        return f'<span class="dim">{esc(dash)}</span>'
    cls = "neg" if value < 0 else ("pos" if value > 0 else "dim")
    return f'<span class="{cls}">{esc(fmt(value))}</span>'


def num(value, dash: str = "-") -> str:
    return esc(dash) if value is None else esc(value)


def page(title: str, body: str, flash: str = "", nav_here: str = "") -> str:
    def tab(href: str, label: str, key: str) -> str:
        cls = ' class="here"' if key == nav_here else ""
        return f'<a href="{href}"{cls}>{esc(label)}</a>'

    banner = ""
    if flash:
        level = "warn" if flash.startswith("!") else "ok"
        banner = f'<div class="flash {level}">{esc(flash.lstrip("!"))}</div>'

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)} - {esc(APP_NAME)}</title>
<link rel="stylesheet" href="/static/app.css?v={VERSION["app.css"]}">
</head><body>
<header>
  <strong>{esc(APP_NAME)}</strong>
  <nav>
    {tab("/", "Positions", "positions")}
    {tab("/new", "New", "new")}
    {tab("/shares", "Shares", "shares")}
    {tab("/expiring", "Expiring", "expiring")}
    {tab("/audit", "History", "audit")}
  </nav>
</header>
<main>
{banner}
<h1>{esc(title)}</h1>
{body}
</main>
<script src="/static/app.js?v={VERSION["app.js"]}"></script>
</body></html>"""


# ---------------------------------------------------------------------------
# form controls


def field(
    label: str,
    name: str,
    value="",
    kind: str = "text",
    *,
    required: bool = False,
    step: str | None = None,
    hint: str = "",
    attrs: str = "",
    autofocus: bool = False,
) -> str:
    extra = attrs
    if step:
        extra += f' step="{esc(step)}"'
    if required:
        extra += " required"
    if autofocus:
        extra += " autofocus"
    note = f'<small>{esc(hint)}</small>' if hint else ""
    return f"""<label>
  <span>{esc(label)}</span>
  <input type="{esc(kind)}" name="{esc(name)}" id="f_{esc(name)}"
         value="{esc(value)}"{extra}>
  {note}
</label>"""


def select(label: str, name: str, options, value="", hint: str = "") -> str:
    items = "".join(
        f'<option value="{esc(v)}"{" selected" if str(v) == str(value) else ""}>'
        f"{esc(text)}</option>"
        for v, text in options
    )
    note = f'<small>{esc(hint)}</small>' if hint else ""
    return f"""<label>
  <span>{esc(label)}</span>
  <select name="{esc(name)}" id="f_{esc(name)}">{items}</select>
  {note}
</label>"""


def hidden(name: str, value) -> str:
    return f'<input type="hidden" name="{esc(name)}" value="{esc(value)}">'


def form(action: str, body: str, token: str, submit: str = "Save",
         method: str = "post", cls: str = "") -> str:
    return f"""<form method="{esc(method)}" action="{esc(action)}"
      class="{esc(cls)}">
{hidden("csrf", token)}
{body}
<div class="actions"><button type="submit">{esc(submit)}</button></div>
</form>"""


def fieldset(legend: str, body: str, hint: str = "") -> str:
    note = f'<p class="hint">{esc(hint)}</p>' if hint else ""
    return (f'<fieldset class="grid"><legend>{esc(legend)}</legend>'
            f"{note}{body}</fieldset>")


def datalist(list_id: str, values) -> str:
    items = "".join(f'<option value="{esc(v)}">' for v in values)
    return f'<datalist id="{esc(list_id)}">{items}</datalist>'


# ---------------------------------------------------------------------------
# problem reporting


def problems_block(problems) -> str:
    """Validation output. Errors block the save; warnings just say so."""
    if not problems:
        return ""
    rows = "".join(
        f'<li class="{esc(p.level)}"><b>{esc(p.field)}</b> {esc(p.message)}</li>'
        for p in problems
    )
    return f'<ul class="problems">{rows}</ul>'


def table(headers, rows, cls: str = "") -> str:
    """Rows are lists of cells. A row given as a string is emitted verbatim,
    which is how an action form gets slotted in directly beneath its position."""
    head = "".join(f"<th>{esc(h)}</th>" for h in headers)
    body = "".join(
        r if isinstance(r, str)
        else "<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>"
        for r in rows
    )
    if not rows:
        body = (f'<tr><td colspan="{len(headers)}" class="dim">'
                "nothing here yet</td></tr>")
    return (f'<table class="{esc(cls)}"><thead><tr>{head}</tr></thead>'
            f"<tbody>{body}</tbody></table>")


def strike_text(value) -> str:
    """A strike without the noise: 20 rather than 20.00, but 22.50 intact."""
    text = fmt(value)
    return text[:-3] if text.endswith(".00") else text


def contract(position) -> str:
    """One contract with each part visually distinct.

    Run together as plain text -- "-10 ACME 2026-09-18 20.00P" -- the quantity
    disappears into the digits around it. So it gets its own pill, signed and
    coloured by side. The rest stays in the conventional order: ticker, expiry,
    strike, right.
    """
    short = position.direction.value == "SHORT"
    side = "short" if short else "long"
    # An inline grid with fixed tracks, so every row's expiry and strike start
    # in the same place whatever the ticker's length. Strike and type share
    # the last track, left-aligned, so a short strike leaves no dead space.
    return (
        f'<span class="contract">'
        f'<span class="qty {side}" title="{esc(side)}'
        f' {position.quantity} contract(s)">'
        f'{"-" if short else "+"}{position.quantity}</span>'
        f'<b class="ticker">{esc(position.underlying)}</b>'
        f'<span class="exp">{esc(position.expiry)}</span>'
        f'<span class="strike">{esc(strike_text(position.strike))} '
        f'<span class="right">{esc(position.right.value[0])}</span></span>'
        f'</span>'
    )


def contract_text(position) -> str:
    """The same thing as plain text, for page titles and anywhere HTML is wrong."""
    side = "-" if position.direction.value == "SHORT" else "+"
    return (
        f"{side}{position.quantity} {position.underlying} {position.expiry} "
        f"{strike_text(position.strike)} {position.right.value[0]}"
    )


def status_badge(status) -> str:
    """Status as a coloured pill, so open and closed legs read differently at
    a glance instead of being two words in the same grey."""
    name = status.value.lower()
    return f'<span class="badge st-{esc(name)}">{esc(status.value.title())}</span>'


def dte_cell(days: int | None) -> str:
    if days is None:
        return '<span class="dim">-</span>'
    if days < 0:
        return f'<span class="neg">{days}d overdue</span>'
    if days == 0:
        return '<span class="neg">today</span>'
    cls = "warn-text" if days <= 7 else ""
    return f'<span class="{cls}">{days}d</span>'


def today_iso() -> str:
    return date.today().isoformat()
