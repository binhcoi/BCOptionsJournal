"""Stylesheet and script, served from memory.

Kept in Python rather than as files so the package has no data-file plumbing
and can be copied anywhere and run. Both are small and hand-written; there is
no third-party JavaScript to vendor, audit or keep current.

The JavaScript is progressive enhancement only. Every form works with it
disabled -- plain POSTs and redirects -- because the core job is recording
trades, and that should never depend on a script.
"""

CSS = """
:root {
  --bg: #fbfbfa; --panel: #fff; --ink: #1a1a1a; --dim: #6b7280;
  --line: #e3e3e0; --pos: #067647; --neg: #b42318; --accent: #1e4fd8;
  --warn-bg: #fffbeb; --warn-line: #f5d76e; --ok-bg: #ecfdf3;
  --neg-tint: #fdeceb; --pos-tint: #e7f6ee;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #16171a; --panel: #1e2024; --ink: #e8e8e6; --dim: #9aa0a8;
    --line: #2e3138; --pos: #4ade80; --neg: #f87171; --accent: #7aa2ff;
    --warn-bg: #2a2312; --warn-line: #6b5a1f; --ok-bg: #12261a;
    --neg-tint: #3a1d1d; --pos-tint: #14301f;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--ink);
  font: 15px/1.5 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
}
header {
  display: flex; align-items: center; gap: 1.5rem;
  padding: .7rem 1.2rem; border-bottom: 1px solid var(--line);
  background: var(--panel); position: sticky; top: 0; z-index: 5;
}
header strong { letter-spacing: -.01em; }
nav { display: flex; gap: .25rem; }
nav a, .tabs a {
  padding: .3rem .7rem; border-radius: 6px; text-decoration: none;
  color: var(--dim);
}
nav a:hover, .tabs a:hover { background: var(--bg); color: var(--ink); }
nav a.here, .tabs a.here { background: var(--accent); color: #fff; }
main { max-width: 1180px; margin: 0 auto; padding: 1.2rem; }
h1 { font-size: 1.35rem; margin: .2rem 0 1rem; }
h2 { font-size: 1.05rem; margin: 1.8rem 0 .6rem; }
a { color: var(--accent); }

.tabs { display: flex; gap: .25rem; margin: .8rem 0; flex-wrap: wrap; }

table { width: 100%; border-collapse: collapse; background: var(--panel);
  border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }
th, td { padding: .5rem .6rem; text-align: right; white-space: nowrap;
  border-bottom: 1px solid var(--line); }
th:first-child, td:first-child { text-align: left; }
th { font-size: .78rem; text-transform: uppercase; letter-spacing: .04em;
  color: var(--dim); font-weight: 600; }
tbody tr:last-child td { border-bottom: 0; }
tbody tr:hover { background: var(--bg); }
td:last-child, th:last-child { text-align: left; }
.wrap { overflow-x: auto; }

/* A contract is five facts, not one string. Laid out as an inline grid with
   fixed tracks so expiry, strike and type align down the table whatever the
   ticker's length; the quantity gets a pill so it cannot vanish into the
   digits beside it. Order is the conventional one: ticker, expiry, strike,
   type. */
.contract {
  display: inline-grid;
  grid-template-columns: 3.4em 6ch 10.5ch auto;
  column-gap: .55rem; align-items: baseline;
}
.qty {
  text-align: right; padding: .02rem .34rem; border-radius: 4px;
  font-variant-numeric: tabular-nums; font-weight: 700; font-size: .88em;
}
.qty.short { color: var(--neg); background: var(--neg-tint); }
.qty.long { color: var(--pos); background: var(--pos-tint); }
.exp, .strike { font-variant-numeric: tabular-nums; }
.strike, .right { font-weight: 600; }
td:first-child a { text-decoration: none; }
td:first-child a:hover .ticker { text-decoration: underline; }

.pos { color: var(--pos); }
.neg { color: var(--neg); }
.dim { color: var(--dim); }
.warn-text { color: #b45309; font-weight: 600; }

.totals { display: flex; flex-wrap: wrap; gap: .5rem; margin: 0 0 1rem; }
.totals > div {
  flex: 1 1 150px; background: var(--panel); border: 1px solid var(--line);
  border-radius: 8px; padding: .55rem .7rem;
}
.totals span { display: block; font-size: .72rem; text-transform: uppercase;
  letter-spacing: .04em; color: var(--dim); }
.totals b { font-variant-numeric: tabular-nums; font-size: 1.05rem; }
.totals.wide > div { flex: 1 1 190px; }

form.grid, .grid {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
  gap: .7rem; background: var(--panel); border: 1px solid var(--line);
  border-radius: 8px; padding: 1rem;
}
label { display: flex; flex-direction: column; gap: .2rem; }
label > span { font-size: .78rem; color: var(--dim); font-weight: 600; }
input, select, button {
  font: inherit; padding: .45rem .55rem; border: 1px solid var(--line);
  border-radius: 6px; background: var(--bg); color: var(--ink);
}
input:focus, select:focus { outline: 2px solid var(--accent);
  outline-offset: -1px; }
small { color: var(--dim); font-size: .74rem; }
.actions { grid-column: 1 / -1; display: flex; gap: .5rem; }
button {
  background: var(--accent); color: #fff; border-color: transparent;
  font-weight: 600; cursor: pointer; padding: .5rem 1.1rem;
}
button:hover { filter: brightness(1.08); }
form.inline { display: inline; background: none; border: 0; padding: 0; }
form.inline .actions { display: inline; }
form.inline button {
  background: none; color: var(--accent); font-weight: 400;
  padding: 0; text-decoration: underline;
}
a.btn {
  display: inline-block; padding: .15rem .5rem; border-radius: 5px;
  border: 1px solid var(--line); text-decoration: none; font-size: .85rem;
}
a.btn:hover { background: var(--accent); color: #fff; }

.flash, .callout {
  padding: .6rem .8rem; border-radius: 8px; margin: 0 0 1rem;
  border: 1px solid var(--line); background: var(--ok-bg);
}
.flash.warn, .callout { background: var(--warn-bg);
  border-color: var(--warn-line); }
.callout a { font-weight: 600; }

ul.problems { list-style: none; padding: 0; margin: 0 0 1rem; }
ul.problems li {
  padding: .45rem .7rem; border-radius: 6px; margin-bottom: .3rem;
  border: 1px solid var(--warn-line); background: var(--warn-bg);
}
ul.problems li.error { border-color: var(--neg); color: var(--neg); }

p.hint { color: var(--dim); font-size: .86rem; max-width: 68ch; }

/* Row-level action links, and the form that opens beneath a row. */
td a.act {
  font-size: .82rem; margin-right: .5rem; text-decoration: none;
  color: var(--dim);
}
td a.act:hover { color: var(--accent); text-decoration: underline; }
td a.act.here { color: var(--accent); font-weight: 600; }
tr.action-row > td { padding: 0; background: var(--bg); white-space: normal; }
tr.action-row:hover { background: var(--bg); }
.action-inline { padding: .8rem .9rem 1rem; border-left: 3px solid var(--accent); }
.action-inline h3 { margin: 0 0 .6rem; font-size: .95rem; }
.action-inline .callout, .action-inline p.hint { margin-top: 0; }

/* A roll is two trades. Show it as two. */
form.two-part { display: grid; gap: .8rem; }
fieldset.grid { margin: 0; min-width: 0; }
legend {
  font-size: .78rem; text-transform: uppercase; letter-spacing: .04em;
  color: var(--dim); font-weight: 700; padding: 0 .3rem;
}
fieldset p.hint { grid-column: 1 / -1; margin: 0; }

/* The family tree: greyed rows are the other branch of a split; a struck
   credit belongs to a divided position and is now carried by its halves. */
tr.superseded td { color: var(--dim); }
tr.branch td { color: var(--dim); }
tr.branch a { color: var(--dim); }
s { text-decoration-color: var(--dim); }
kbd { border: 1px solid var(--line); border-bottom-width: 2px;
  border-radius: 4px; padding: 0 .3rem; font-size: .85em; background: var(--bg); }
"""

JS = """
// Progressive enhancement only. Every form works without this.
(function () {
  'use strict';

  // Fee tracks the contract count, until it is edited by hand. Typing a fee
  // must never be undone by changing the quantity afterwards.
  var grid = document.querySelector('[data-fee-rate]');
  if (grid) {
    var rate = parseFloat(grid.getAttribute('data-fee-rate'));
    var qty = document.getElementById('f_quantity');
    var fee = document.getElementById('f_open_fee');
    if (qty && fee && !isNaN(rate)) {
      var touched = false;
      fee.addEventListener('input', function () { touched = true; });
      qty.addEventListener('input', function () {
        if (touched) return;
        var n = Math.abs(parseInt(qty.value, 10));
        if (n > 0) fee.value = (rate * n).toFixed(2);
      });
    }
  }

  // "+7" in a date box means seven days from today. Faster than a picker for
  // the weekly cadence most of these trades follow.
  document.querySelectorAll('input[type=date]').forEach(function (input) {
    input.addEventListener('keydown', function (event) {
      if (event.key !== 'Enter') return;
      var typed = input.value.trim();
      var relative = /^\\+(\\d+)$/.exec(typed);
      if (!relative) return;
      event.preventDefault();
      var when = new Date();
      when.setDate(when.getDate() + parseInt(relative[1], 10));
      input.value = when.toISOString().slice(0, 10);
    });
  });

  // Uppercase tickers as they are typed, without moving the cursor.
  var ticker = document.getElementById('f_underlying');
  if (ticker) {
    ticker.addEventListener('input', function () {
      var at = ticker.selectionStart;
      ticker.value = ticker.value.toUpperCase();
      ticker.setSelectionRange(at, at);
    });
  }
})();
"""

STATIC = {
    "app.css": ("text/css; charset=utf-8", CSS),
    "app.js": ("text/javascript; charset=utf-8", JS),
}


def _digest(text: str) -> str:
    import hashlib

    return hashlib.sha1(text.encode()).hexdigest()[:10]


# A fingerprint of the content, appended to the URL as ?v=..., so any edit to
# the stylesheet or script reaches the browser on the next load. Without this a
# cached copy can outlive the change by the whole cache window.
VERSION = {name: _digest(content) for name, (_, content) in STATIC.items()}
