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
  --accent-tint: #e8eefc; --roll: #6d28d9; --roll-tint: #efe9fb;
  --amber: #b45309; --amber-tint: #fdf1de;
  --chain-bg: #f3f3f1; --current-tint: #fff7d6;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #16171a; --panel: #1e2024; --ink: #e8e8e6; --dim: #9aa0a8;
    --line: #2e3138; --pos: #4ade80; --neg: #f87171; --accent: #7aa2ff;
    --warn-bg: #2a2312; --warn-line: #6b5a1f; --ok-bg: #12261a;
    --neg-tint: #3a1d1d; --pos-tint: #14301f;
    --accent-tint: #1c2540; --roll: #b79cff; --roll-tint: #2a2140;
    --amber: #fbbf24; --amber-tint: #3a2e12;
    --chain-bg: #1a1c21; --current-tint: #2e2a14;
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
details.inline { display: inline-block; vertical-align: middle; }
details.inline summary { cursor: pointer; list-style: none; opacity: .5; padding: 0 .2rem; }
details.inline summary::-webkit-details-marker { display: none; }
details.inline[open] summary { opacity: 1; }

table { width: 100%; border-collapse: collapse; background: var(--panel);
  border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }
th, td { padding: .5rem .6rem; text-align: right; white-space: nowrap;
  border-bottom: 1px solid var(--line); }
th:first-child, td:first-child, th:nth-child(2), td:nth-child(2) { text-align: left; }
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
  /* Tracks are in ch, which follows the font. Pin the weight here so no
     ancestor (a bold row, a heading) can change the track widths. */
  font-weight: 400; font-size: 1em;
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

/* Actions sit beneath the contract, colour-coded by what they do:
   blue closes, purple rolls, green keeps the credit, amber moves stock,
   grey divides. */
.row-actions { margin-top: .3rem; display: flex; gap: .35rem; flex-wrap: wrap; }
.row-actions a.act {
  font-size: .76rem; padding: .05rem .5rem; border-radius: 999px;
  text-decoration: none; font-weight: 600; border: 1px solid transparent;
}
.act-close  { color: var(--accent); background: var(--accent-tint); }
.act-roll   { color: var(--roll);   background: var(--roll-tint); }
.act-expire { color: var(--pos);    background: var(--pos-tint); }
.act-assign { color: var(--amber);  background: var(--amber-tint); }
.act-split  { color: var(--dim);    background: var(--bg); border-color: var(--line); }
.row-actions a.act:hover, .row-actions a.act.here {
  border-color: currentColor;
}

/* Status pills. Open is the one that matters; everything else is history. */
.badge {
  display: inline-block; padding: .05rem .5rem; border-radius: 999px;
  font-size: .76rem; font-weight: 600; letter-spacing: .01em;
}
.st-open     { color: var(--accent); background: var(--accent-tint); }
.st-rolled   { color: var(--dim);    background: var(--bg); border: 1px solid var(--line); }
.st-closed   { color: var(--dim);    background: var(--bg); border: 1px solid var(--line); }
.st-expired  { color: var(--pos);    background: var(--pos-tint); }
.st-assigned { color: var(--amber);  background: var(--amber-tint); }
.st-split    { color: var(--dim);    background: var(--bg); border: 1px dashed var(--line); }
.st-blocked  { color: var(--neg);    background: var(--neg-tint); }
ul.suggest { list-style: none; padding: 0; }
ul.suggest li { padding: .4rem 0; border-bottom: 1px solid var(--line); }
ul.suggest form.inline { margin-left: .8rem; }
h3 { font-size: .95rem; margin: 1.2rem 0 .4rem; }

/* Open positions that are branches of one family share a coloured rail. */
tr.fam > td:first-child { border-left: 3px solid var(--line); }
tr.fam-0 > td:first-child { border-left-color: var(--accent); }
tr.fam-1 > td:first-child { border-left-color: var(--roll); }
tr.fam-2 > td:first-child { border-left-color: var(--amber); }
tr.fam-3 > td:first-child { border-left-color: var(--pos); }
.fam-note { display: block; font-size: .74rem; color: var(--dim); margin-top: .1rem; }
a.legs { text-decoration: none; font-weight: 600; padding: .05rem .45rem;
  border-radius: 999px; border: 1px solid var(--line);
  display: inline-block; min-width: 1.9em; text-align: center; }
a.legs.here { font-size: .7em; padding: .2rem .45rem; }
a.legs:hover, a.legs.here { background: var(--accent); color: #fff; border-color: transparent; }

/* Legs revealed by expanding a chain, in the same row format as the list.
   A header row marks where the block begins; every row in it shares a tint
   and a rail so the block's extent is unambiguous. Closed legs are history
   and step back; open legs step forward; the row this page or click is about
   gets its own tint and an explicit tag. */
tr.chain-head td {
  background: var(--chain-bg); border-left: 3px solid var(--accent);
  font-size: .8rem; color: var(--dim); text-align: left;
  padding: .35rem .6rem; white-space: normal;
}
tr.chain-head td span { margin-right: .8rem; }
tr.chain-head a.legs { float: right; }
tr.chain-leg td { background: var(--chain-bg); }
tr.chain-leg > td:first-child { border-left: 3px solid var(--accent); }
tr.chain-leg.leg-closed td { color: var(--dim); }
tr.chain-leg.leg-closed .qty { opacity: .6; }
tr.chain-leg.leg-closed a, tr.chain-leg.leg-closed .ticker { color: var(--dim); }
tr.chain-leg.leg-open td { background: var(--accent-tint); }
tr.current td { background: var(--current-tint); }
tr.current > td:first-child { border-left: 3px solid var(--ink); }
tr.current .ticker, tr.current td { color: var(--ink); }
/* The gutter column is zero-width: the row marker sits on the rail itself,
   as a notch in the left line, and the contract never shifts. */
td.gutter, th:first-child { width: 0; padding: 0; position: relative; }
/* The marker is the rail itself pointing into the row: a solid arrowhead
   drawn in the rail's colour, flush against it, so the line reads as an
   arrow rather than a line with a character beside it. */
.here-arrow {
  position: absolute; left: 0; top: 50%; transform: translateY(-50%);
  width: 0; height: 0;
  border-left: 8px solid var(--ink);
  border-top: 7px solid transparent; border-bottom: 7px solid transparent;
}
tr[data-chain] { cursor: pointer; }
tr[data-chain] a, tr[data-chain] button { cursor: pointer; }
/* Projections -- an open position's close price and closing cash at target. */
.proj { font-style: italic; color: var(--dim); }
td.unit { font-variant-numeric: tabular-nums; }
/* Hovering a contract underlines the whole thing, not just the ticker. */
td a:hover .contract > :not(.qty) { text-decoration: underline; }
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

  // Expanding a chain or opening an action form swaps just the positions
  // table -- fetched as a bare table, and prefetched ahead of the click, so
  // the swap is instant. Links still work as plain navigation without this.
  var table = document.querySelector('table.positions');
  if (table && window.fetch && window.DOMParser) {
    var cache = {};

    var partial = function (href) {
      var parts = href.split('#');
      var url = parts[0] + (parts[0].indexOf('?') >= 0 ? '&' : '?') + 'partial=table';
      return { url: url, anchor: parts[1] || null };
    };

    var load = function (href) {
      if (cache[href]) return Promise.resolve(cache[href]);
      var target = partial(href);
      return fetch(target.url, { credentials: 'same-origin' })
        .then(function (res) { if (!res.ok) throw new Error(res.status); return res.text(); })
        .then(function (html) { cache[href] = html; return html; });
    };

    var render = function (href, html, push) {
      var current = document.querySelector('table.positions');
      if (!current) { window.location.href = href; return; }
      var holder = document.createElement('div');
      holder.innerHTML = html;
      var fresh = holder.querySelector('table.positions');
      if (!fresh) { window.location.href = href; return; }
      current.replaceWith(fresh);
      if (push) history.pushState({ bcoj: href }, '', href);
      // Bring the opened form (or the clicked row) into view, then focus
      // without a second scroll: one movement, never a jump.
      var anchor = partial(href).anchor;
      var row = anchor ? document.getElementById(anchor) : null;
      var form = fresh.querySelector('tr.action-row');
      if (form) form.scrollIntoView({ block: 'nearest' });
      else if (row) row.scrollIntoView({ block: 'nearest' });
      var focus = fresh.querySelector('[autofocus]');
      if (focus) focus.focus({ preventScroll: true });
      prefetchAll(fresh);
    };

    var swap = function (href, push) {
      // Remember the table we are leaving, so coming back is instant too.
      var here = window.location.pathname + window.location.search;
      var current = document.querySelector('table.positions');
      if (current && !cache[here]) cache[here] = current.outerHTML;
      load(href).then(function (html) { render(href, html, push); })
        .catch(function () { window.location.href = href; });
    };

    // Warm the cache for every toggle target in view, a few at a time.
    var prefetchAll = function (root) {
      var targets = [];
      root.querySelectorAll('tr[data-chain], a.legs').forEach(function (el) {
        var href = el.getAttribute('data-chain') || el.getAttribute('href');
        if (href && !cache[href] && targets.indexOf(href) < 0) targets.push(href);
      });
      var i = 0;
      var next = function () {
        if (i >= targets.length || i >= 24) return;
        load(targets[i++]).catch(function () {}).then(next);
      };
      next(); next();  // two in flight
    };
    prefetchAll(table);

    document.addEventListener('click', function (event) {
      if (event.metaKey || event.ctrlKey || event.button !== 0) return;
      var link = event.target.closest('table.positions a.legs, table.positions a.act');
      if (link) {
        event.preventDefault();
        swap(link.getAttribute('href'), true);
        return;
      }
      if (event.target.closest('a, button, input, select, label, form')) return;
      var row = event.target.closest('table.positions tr[data-chain]');
      if (!row) return;
      swap(row.getAttribute('data-chain'), true);
    });
    // Hovering a row is a strong hint it is about to be clicked.
    document.addEventListener('mouseover', function (event) {
      var row = event.target.closest('table.positions tr[data-chain]');
      if (row) load(row.getAttribute('data-chain')).catch(function () {});
      var link = event.target.closest('table.positions a.act, table.positions a.legs');
      if (link) load(link.getAttribute('href')).catch(function () {});
    });
    window.addEventListener('popstate', function (event) {
      var href = (event.state && event.state.bcoj) || (window.location.pathname + window.location.search + window.location.hash);
      load(href).then(function (html) { render(href, html, false); })
        .catch(function () { window.location.reload(); });
    });
  }

  // Selling from a specific lot: cap the quantity at what that lot holds.
  // Buy / Sell / Buy-write tabs show forms already on the page, so choosing
  // one neither reloads nor scrolls away from where the reader was.
  var tabs = document.querySelectorAll('a[data-form-tab]');
  var panels = document.querySelectorAll('.share-form[data-form]');
  if (tabs.length && panels.length) {
    tabs.forEach(function (tab) {
      tab.addEventListener('click', function (event) {
        event.preventDefault();
        var key = tab.getAttribute('data-form-tab');
        tabs.forEach(function (t) { t.classList.toggle('here', t === tab); });
        panels.forEach(function (p) { p.hidden = p.getAttribute('data-form') !== key; });
        var first = document.querySelector(
          '.share-form[data-form="' + key + '"] input:not([type=hidden]):not([value]), ' +
          '.share-form[data-form="' + key + '"] input:not([type=hidden])');
        if (first) first.focus({ preventScroll: true });
        if (window.history.replaceState) {
          window.history.replaceState(null, '', tab.getAttribute('href').replace(/#.*$/, ''));
        }
      });
    });
  }

  var lotSelect = document.getElementById('f_lot_id');
  var lotData = document.getElementById('lot-remaining');
  var qtyInput = lotSelect && lotSelect.form
    ? lotSelect.form.querySelector('input[name=quantity]') : null;
  if (lotSelect && lotData && qtyInput) {
    var remaining = {};
    try { remaining = JSON.parse(lotData.textContent || '{}'); } catch (e) {}
    var cap = function () {
      var left = remaining[lotSelect.value];
      if (left) {
        qtyInput.max = left;
        if (parseInt(qtyInput.value, 10) > left) qtyInput.value = left;
        qtyInput.title = 'That lot holds ' + left;
      } else {
        qtyInput.removeAttribute('max'); qtyInput.title = '';
      }
    };
    lotSelect.addEventListener('change', cap); cap();
  }

  // Uppercase tickers as they are typed, without moving the cursor.
  document.querySelectorAll('input[name=underlying]').forEach(function (ticker) {
    ticker.addEventListener('input', function () {
      var at = ticker.selectionStart;
      ticker.value = ticker.value.toUpperCase();
      ticker.setSelectionRange(at, at);
    });
  });
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
