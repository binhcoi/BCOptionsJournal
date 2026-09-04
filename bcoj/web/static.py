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
  --closed: #be185d; --closed-tint: #fce7f3;
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
    --closed: #f472b6; --closed-tint: #3b1a2b;
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
th abbr { text-decoration: none; cursor: help; border-bottom: 1px dotted var(--line); }
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
.totals small { display: block; font-size: .7rem; font-weight: 400; color: var(--dim); }
.totals .pos { color: var(--pos); } .totals .neg { color: var(--neg); }
/* The scorecard wears the same tinted cards as every other strip; only
   the figure is larger, since it is the row read first. */
.totals.score > div { flex: 1 1 160px; }
.totals.score b { font-size: 1.4rem; }
.chips.facts { margin: -.4rem 0 1rem; }
.chips.facts .chip.pos { border-color: var(--pos); background: var(--pos-tint); color: var(--pos); }
.chips.facts .chip.dim { color: var(--dim); border-style: dashed; }
.chips.facts .chip { font-size: .78rem; padding: .1rem .6rem; }
.chips.facts .chip.neg { border-color: var(--neg); background: var(--neg-tint); color: var(--neg); }
.totals > div.good { background: var(--pos-tint); border-color: var(--pos); }
.totals b small { font-weight: 400; margin-top: .15rem; }
.totals > div.bad { background: var(--neg-tint); border-color: var(--neg); }
.grow { flex: 1; }
a.btn.new { padding: .45rem .9rem; font-size: inherit; font-weight: 600;
  color: var(--accent); border-color: var(--accent); background: var(--panel); }
a.btn.new:hover, a.btn.new.here { background: var(--accent); color: #fff; }
table[data-fixed] tr[data-chain] { cursor: default; }
table[data-fixed] tr.chain-head td { padding-top: .3rem; padding-bottom: .3rem; }
.tabs.quick { margin: 0 0 .4rem; }
.tabs.quick a { font-size: .8rem; padding: .15rem .6rem; }
.act-open { color: var(--accent); background: var(--accent-tint); border-color: var(--accent); }
button.btn-open { background: var(--accent); }
details.report { margin: 1.2rem 0; }
details.raw > summary { font-size: .95rem; color: var(--dim); }
details.raw form.compact { margin-bottom: .6rem; }
.raw-actions { display: flex; gap: 1rem; align-items: center; flex-wrap: wrap; margin-top: .8rem; }
form.inline.reopen button { background: var(--panel); color: var(--accent);
  border: 1px solid var(--accent); border-radius: 6px; padding: .35rem .8rem; font-weight: 600;
  text-decoration: none; }
form.inline.reopen button:hover { background: var(--accent-tint); }
form.convert { display: inline-flex; align-items: center; gap: .5rem; }
form.convert label.check { flex-direction: row; align-items: center; gap: .3rem; font-size: .8rem;
  color: var(--dim); }
form.inline.convert button { background: var(--panel); color: var(--amber);
  border: 1px solid var(--amber); border-radius: 6px; padding: .35rem .8rem; font-weight: 600;
  text-decoration: none; }
details.report summary { cursor: pointer; font-weight: 600; font-size: 1.15rem; margin: 0 0 .5rem; }
details.report summary:hover { color: var(--accent); }
.preview { grid-column: 1 / -1; }
.preview .totals { margin: .4rem 0 0; }
.preview .callout { margin: .5rem 0 0; }
.share { display: inline-flex; align-items: center; gap: .4rem; min-width: 9rem;
  font-variant-numeric: tabular-nums; }
.bar { display: inline-block; height: .55rem; min-width: 2px; max-width: 6rem;
  background: var(--accent); border-radius: 3px; flex: 0 0 auto; }
table.calendar td:last-child { line-height: 1.5; }

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
/* A confirm button wears its action's colour, the same one its pill wears. */
button.btn-close  { background: var(--closed); }
button.btn-roll   { background: var(--roll); }
button.btn-expire { background: var(--pos); }
button.btn-assign { background: var(--amber); }
button.btn-split  { background: var(--dim); }
/* The x that closes an open form, top right of its panel. */
.close-form { float: right; text-decoration: none; color: var(--dim); font-size: 1.25rem;
  line-height: 1; padding: .1rem .45rem; border-radius: 6px; }
.close-form:hover { color: var(--ink); background: var(--bg); }
[data-form] { position: relative; }
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
/* One button per row, in a column of its own, always visible. */
td.acts { width: 0; padding-left: .2rem; padding-right: .2rem; }
a.act {
  font-size: .76rem; padding: .05rem .5rem; border-radius: 999px;
  text-decoration: none; font-weight: 600; border: 1px solid transparent;
}
.act-close  { color: var(--closed); background: var(--closed-tint); }
.act-roll   { color: var(--roll);   background: var(--roll-tint); }
.act-expire { color: var(--pos);    background: var(--pos-tint); }
.act-assign { color: var(--amber);  background: var(--amber-tint); }
.act-split  { color: var(--dim);    background: var(--bg); border-color: var(--line); }
a.act:hover, a.act.here { border-color: currentColor; }
/* Tabs of equal width, confirm buttons of equal width, so switching forms
   moves nothing. */
.tabs.even a { min-width: 5.4rem; text-align: center; }
/* Filter bar: segments for the two filters used most, the rest folded. */
.titlebar { display: flex; align-items: center; justify-content: space-between; gap: 1rem; }
.callout.ok { background: var(--ok-bg); border-color: var(--pos); }
table.health td:nth-child(5) { white-space: normal; text-align: left; }
table.rawdata td:nth-child(8) { white-space: normal; text-align: left; max-width: 22rem; }
table.rawdata small.note { display: block; line-height: 1.35; margin-top: .15rem; }
table.rawdata .rowtools { display: inline-flex; align-items: baseline; gap: .6rem;
  white-space: nowrap; }
table.rawdata .rowtools form.inline, table.rawdata .rowtools form.inline button,
table.rawdata .rowtools .inuse { font-size: .85rem; line-height: 1.2; }
table.rawdata details.edit { display: inline-block; vertical-align: baseline; }
table.rawdata form.unlink { display: inline-flex; align-items: center; gap: .4rem; flex-wrap: wrap; }
table.rawdata form.unlink label.check { flex-direction: row; align-items: center; gap: .3rem;
  font-size: .8rem; color: var(--neg); }
table.rawdata details.edit > summary { cursor: pointer; list-style: none; color: var(--accent);
  text-decoration: underline; font-size: .85rem; display: inline; }
table.rawdata details.edit > summary::-webkit-details-marker { display: none; }
table.rawdata details.edit[open] { display: block; }
table.rawdata details.edit .bubble { margin: .5rem 0; text-align: left; white-space: normal;
  min-width: 34rem; }
table.rawdata details.edit label.check { flex-direction: row; gap: .35rem; align-items: center;
  font-size: .85rem; }
form.restore { display: inline-flex; align-items: center; gap: .5rem; }
form.restore label.check { flex-direction: row; align-items: center; gap: .3rem; font-size: .8rem;
  color: var(--dim); }
/* Real buttons where a click has consequences: restore is red on white
   text, a snapshot is the accent, whatever the inline-form default says. */
form.restore button, form.save-view button {
  background: var(--accent); color: #fff; border: 1px solid transparent; border-radius: 6px;
  padding: .35rem .8rem; font-weight: 600; text-decoration: none;
}
form.restore button { background: var(--neg); }
form.restore button:hover, form.save-view button:hover { filter: brightness(1.08); }
.titlebar h1 { margin-right: auto; }
.filterbar { margin: 0 0 .8rem; }
.chips.active { margin: .5rem 0 0; }
.chips .lbl { font-size: .75rem; text-transform: uppercase; letter-spacing: .04em;
  font-weight: 600; margin-right: .2rem; }
.chip.view { background: var(--panel); }
details.save { display: inline-block; }
details.save > summary { cursor: pointer; list-style: none; font-size: .8rem; color: var(--accent);
  padding: .1rem .4rem; }
details.save > summary::-webkit-details-marker { display: none; }
details.save[open] > summary { color: var(--dim); }
details.save form.save-view { margin-left: .3rem; }
.filterbar .fbar { display: flex; flex-wrap: wrap; gap: .6rem; align-items: center; }
.seg { display: inline-flex; border: 1px solid var(--line); border-radius: 8px; overflow: hidden;
  background: var(--panel); }
.seg a { padding: .35rem .75rem; text-decoration: none; color: var(--dim); font-size: .85rem;
  font-weight: 600; border-right: 1px solid var(--line); }
.seg a:last-child { border-right: 0; }
.seg a:hover { background: var(--bg); color: var(--ink); }
.seg a.here { background: var(--accent); color: #fff; }
form.filters { display: flex; flex-wrap: wrap; gap: .6rem; align-items: center; margin: 0; }
form.filters label { width: auto; flex-direction: row; align-items: center; gap: .35rem; }
form.filters label > span { font-size: .8rem; }
form.filters input, form.filters select { padding: .3rem .45rem; }
form.filters input[name=q] { width: 11rem; }
form.filters button { padding: .35rem .9rem; }
/* The views are one stacked control: rows in a single band, divided by
   hairlines, so they read together and apart from the filter row below. */
.viewstack { display: inline-flex; flex-direction: column; align-items: stretch; margin: 0 0 1rem;
  border: 1px solid var(--line); border-radius: 10px; background: var(--bg); overflow: hidden;
  max-width: 100%; }
.chips.views { display: flex; flex-wrap: wrap; gap: 0; margin: 0; padding: 0; border: 0;
  border-radius: 0; background: none; }
.chips.views.sub { border-top: 1px solid var(--line); }
.chips.views .chip, .chips.views a.views-label, .chips.views .lbl { border: 0; border-radius: 0;
  background: none; margin: 0; padding: .4rem .9rem; font-size: .9rem;
  border-right: 1px solid var(--line); }
.chips.views > :last-child { border-right: 0; }
/* Every row starts with the same cell: one width, one style; the top one is
   the toggle and is darker for it. */
.chips.views .lbl, .chips.views a.views-label { font-size: .74rem; text-transform: uppercase;
  letter-spacing: .04em; color: var(--dim); font-weight: 700; background: var(--panel);
  display: inline-flex; align-items: center; flex: 0 0 6.5rem; box-sizing: border-box; }
.chips.views .chip.view { color: var(--accent); }
.chips.views .chip.view:hover { background: var(--panel); }
.chips.views .chip.view.here { background: var(--accent); color: #fff; }
.chips.views .chip.view.saved { display: inline-flex; align-items: center; gap: .2rem; }
.chips.views .chip.view.saved.here a { color: #fff; }
.chips.views details.save { padding: .2rem .9rem; }
/* The line's label is also the toggle for the rest of the views. */
.chips.views a.views-label { color: var(--ink); text-decoration: none; white-space: nowrap; }
.chips.views a.views-label:hover, .chips.views a.views-label.here { color: var(--accent); }
a.views-label .caret { display: inline-block; width: 0; height: 0; vertical-align: middle;
  border-left: 5px solid transparent; border-right: 5px solid transparent;
  border-top: 6px solid currentColor; margin-left: .25rem; }
a.views-label.here .caret { border-top: 0; border-bottom: 6px solid currentColor; }
/* hidden must win over any display rule an element's class sets. */
[hidden] { display: none !important; }
details.more > summary { white-space: nowrap; }
form.filters .seg { margin-left: .2rem; }
.chip.view { text-decoration: none; }
a.chip.view { padding: .1rem .7rem; }
.chip.view.here { background: var(--accent); color: #fff; border-color: var(--accent); }
.chip.view.here a { color: #fff; }
.chip.view.saved { background: var(--panel); }
details.more { position: relative; }
details.more > summary { cursor: pointer; list-style: none; padding: .35rem .75rem;
  border: 1px solid var(--line); border-radius: 8px; font-size: .85rem; font-weight: 600;
  color: var(--dim); background: var(--panel); }
details.more > summary::-webkit-details-marker { display: none; }
details.more[open] > summary { color: var(--ink); border-color: var(--accent); }
details.more .more-fields { position: absolute; z-index: 5; top: 110%; left: 0;
  display: flex; flex-wrap: wrap; gap: .6rem; align-items: flex-end; padding: .7rem .8rem;
  background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
  box-shadow: 0 6px 18px rgba(0,0,0,.12); min-width: 34rem; }
details.more .more-fields label { flex-direction: column; align-items: stretch; }
details.more .more-fields .actions { grid-column: auto; }
.chip.on { background: var(--accent-tint); border-color: var(--accent); }
.chip a.x { padding: 0 .35rem; color: var(--dim); font-size: 1rem; line-height: 1; }
.chip a.x:hover { color: var(--neg); }
a.flt.clear { font-size: .8rem; color: var(--dim); }
.chips { display: flex; flex-wrap: wrap; gap: .4rem; align-items: center; margin: 0 0 .8rem; }
.chip { display: inline-flex; align-items: center; gap: .2rem; border: 1px solid var(--line);
  border-radius: 999px; padding: .05rem .2rem .05rem .6rem; font-size: .8rem; }
.chip a { text-decoration: none; }
.chip.here { border-color: var(--accent); background: var(--accent-tint); }
.chip form.inline button { padding: 0 .4rem; background: none; color: var(--dim);
  border: 0; font-size: .9rem; }
.chip form.inline button:hover { color: var(--neg); filter: none; }
form.save-view { display: inline-flex; align-items: flex-end; gap: .4rem; }
form.save-view label { width: auto; }
form.save-view input { padding: .3rem .45rem; width: 10rem; }
form.save-view button { padding: .35rem .8rem; }
.tag { display: inline-block; font-size: .68rem; color: var(--dim); background: var(--bg);
  border: 1px solid var(--line); border-radius: 4px; padding: 0 .3rem; margin-left: .3rem;
  vertical-align: middle; }
textarea { font: inherit; padding: .45rem .55rem; border: 1px solid var(--line);
  border-radius: 6px; background: var(--bg); color: var(--ink); resize: vertical; }
textarea:focus { outline: 2px solid var(--accent); }
/* Form tabs wear their action's colour: text when idle, fill when chosen. */
.tabs a.tab-close  { color: var(--closed); }
.tabs a.tab-roll   { color: var(--roll); }
.tabs a.tab-expire { color: var(--pos); }
.tabs a.tab-assign { color: var(--amber); }
.tabs a.tab-split  { color: var(--dim); }
.tabs a.tab-close:hover  { background: var(--closed-tint); color: var(--closed); }
.tabs a.tab-roll:hover   { background: var(--roll-tint);   color: var(--roll); }
.tabs a.tab-expire:hover { background: var(--pos-tint);    color: var(--pos); }
.tabs a.tab-assign:hover { background: var(--amber-tint);  color: var(--amber); }
.tabs a.tab-split:hover  { background: var(--bg);          color: var(--ink); }
.tabs a.tab-close.here,  .tabs a.tab-close.here:hover  { background: var(--closed); color: #fff; }
.tabs a.tab-roll.here,   .tabs a.tab-roll.here:hover   { background: var(--roll);   color: #fff; }
.tabs a.tab-expire.here, .tabs a.tab-expire.here:hover { background: var(--pos);    color: #fff; }
.tabs a.tab-assign.here, .tabs a.tab-assign.here:hover { background: var(--amber);  color: #fff; }
.tabs a.tab-split.here,  .tabs a.tab-split.here:hover  { background: var(--dim);    color: #fff; }
.form-box .actions button { min-width: 11rem; }
.form-box:not(#new-box) > [data-form] { min-height: 17.5rem; }
.form-box:not(#new-box) > [data-form] > form { min-height: 100%; }
/* Compact field rows: plain rows of small fields, no panel of their own,
   so they read as part of the form they sit in. */
.grid.compact { display: flex; flex-wrap: wrap; gap: .5rem .9rem; align-items: flex-end;
  padding: 0; background: none; border: 0; }
.grid.compact label { width: auto; min-width: 8rem; }
.grid.compact input, .grid.compact select { padding: .3rem .45rem; }
.grid.compact .when-over, .grid.compact .when-shares { display: contents; }
.grid.compact .when-over.off, .grid.compact .when-shares.off { display: none; }
.action-inline .tabs { margin: .2rem 0 .7rem; }

/* Status pills. Open is the one that matters; everything else is history. */
.badge {
  display: inline-block; padding: .05rem .5rem; border-radius: 999px;
  font-size: .76rem; font-weight: 600; letter-spacing: .01em;
}
.st-open     { color: var(--accent); background: var(--accent-tint); }
.st-rolled   { color: var(--roll);   background: var(--roll-tint); }
.st-closed   { color: var(--closed); background: var(--closed-tint); }
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
.fam-note { display: inline; font-size: .72rem; color: var(--dim); margin-left: .5rem;
  cursor: help; }
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
tr.action-row > td { padding: .5rem .7rem .8rem; background: var(--bg); white-space: normal; }
tr.action-row:hover { background: var(--bg); }
/* A form is a bubble: rounded, bordered, lifted off the table. */
.action-inline, #new-box > [data-form], .bubble {
  padding: .9rem 1rem 1rem; border: 1px solid var(--line); border-radius: 12px;
  background: var(--panel); box-shadow: 0 2px 10px rgba(0,0,0,.08);
}
#new-box > [data-form] { margin: 0 0 1rem; }
.action-inline, #new-box > [data-form], .bubble { border-left: 4px solid var(--accent); }
.bubble { border-left-color: var(--dim); }
.bubble h3, #new-box h3 { margin: 0 0 .6rem; font-size: .95rem; }
.bubble h3 small { font-weight: 400; margin-left: .4rem; }
.bubble p.hint { margin-bottom: 0; }
.action-inline h3 { margin: 0 0 .6rem; font-size: .95rem; }
.action-inline .callout, .action-inline p.hint { margin-top: 0; }

/* A roll is two trades. Show it as two. */
form.two-part { display: grid; gap: .8rem; justify-items: start; }
form.two-part > label { width: 13rem; }
form.two-part > .leg-grid, form.two-part > .preview, form.two-part > .actions,
form.two-part > p.hint { justify-self: stretch; }
form.two-part > p.hint { margin: -.4rem 0 0; }
/* The roll as two legs with matching columns. */
.leg-grid { display: grid;
  grid-template-columns: 4.8rem 6.5rem 9.5rem 6.5rem 5rem 4.5rem 6rem 5.5rem;
  gap: .35rem .5rem; align-items: center; overflow-x: auto; padding-bottom: .2rem; }
.leg-grid .rg-head, .leg-grid .rg-row { display: contents; }
.leg-grid .rg-head span { font-size: .72rem; text-transform: uppercase; letter-spacing: .04em;
  color: var(--dim); font-weight: 600; }
.leg-grid .fixed { color: var(--dim); font-variant-numeric: tabular-nums;
  display: inline-flex; align-items: center; min-height: 2.45rem;
  padding: 0 calc(.45rem + 1px); box-sizing: border-box; }
.leg-grid .leg-tag { display: inline-flex; align-items: center; justify-content: center;
  min-height: 1.6rem; }
.leg-grid input, .leg-grid select { width: 100%; min-width: 0; padding-left: .45rem;
  padding-right: .3rem; }
a.btn.cancel { padding: .5rem 1rem; font-size: inherit; color: var(--dim); }
a.btn.cancel:hover { background: var(--bg); color: var(--ink); }
.leg-tag { font-size: .72rem; font-weight: 700; letter-spacing: .04em; padding: .15rem .4rem;
  border-radius: 4px; text-align: center; }
/* One colour per outcome, everywhere it appears: the status pill, the row
   button, the form tab, the confirm button and the leg tag all agree.
   Closed teal, rolled purple, expired green, assigned amber, split grey; an
   opening leg wears the blue of an open position. */
.leg-tag.close  { color: var(--closed); background: var(--closed-tint); }
.leg-tag.open   { color: var(--accent); background: var(--accent-tint); }
.leg-tag.expire { color: var(--pos);    background: var(--pos-tint); }
.leg-tag.assign { color: var(--amber);  background: var(--amber-tint); }
.leg-tag.split  { color: var(--dim);    background: var(--bg); border: 1px dashed var(--line); }
.leg-tag.shares { color: var(--ink);    background: var(--bg); border: 1px solid var(--line); }
.preview .totals b .pos { color: var(--pos); }
.preview .totals b .neg { color: var(--neg); }
.preview .totals small.pos { color: var(--pos); }
.preview .totals small.neg { color: var(--neg); }
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
    [document.getElementById('f_open_fee'), document.getElementById('f_close_fee')]
      .forEach(function (fee) {
        if (!qty || !fee || isNaN(rate)) return;
        var touched = false;
        fee.addEventListener('input', function () { touched = true; });
        qty.addEventListener('input', function () {
          if (touched) return;
          var n = Math.abs(parseInt(qty.value, 10));
          if (n > 0) fee.value = (rate * n).toFixed(2);
        });
      });
  }

  // A link to a folded section opens it: the Fix links on the Data page
  // land on the raw-data section of a position.
  var reveal = function () {
    if (!window.location.hash) return;
    var target = document.getElementById(window.location.hash.slice(1));
    if (target && target.tagName === 'DETAILS') {
      target.open = true;
      target.scrollIntoView({ block: 'start' });
    }
  };
  window.addEventListener('hashchange', reveal); reveal();

  // The More menu is transient: a click anywhere else, or Escape, closes it.
  // The views bubble is not: it closes only from its own summary.
  document.addEventListener('click', function (event) {
    document.querySelectorAll('details.more[open]').forEach(function (d) {
      if (!d.contains(event.target)) d.open = false;
    });
    var toggle = event.target.closest('a[data-toggle]');
    if (toggle) {
      event.preventDefault();
      var box = document.getElementById(toggle.getAttribute('data-toggle'));
      if (box) {
        box.hidden = !box.hidden;
        box.setAttribute('data-user', '1');
        toggle.classList.toggle('here', !box.hidden);
        var due = document.getElementById('duerow');     // one or the other, never both
        if (due) due.hidden = !box.hidden;
      }
    }
  });
  document.addEventListener('keydown', function (event) {
    if (event.key !== 'Escape') return;
    document.querySelectorAll('details.more[open]').forEach(function (d) { d.open = false; });
  });

  // Cancel inside a folded edit form folds it again.
  document.addEventListener('click', function (event) {
    var x = event.target.closest('[data-close-details]');
    if (!x) return;
    var box = x.closest('details');
    if (!box) return;
    event.preventDefault();
    box.open = false;
  });

  // The entry form's closing fields matter only for a trade already over,
  // its share fields only for a buy-write.
  [['f_outcome', '.when-over', 'OPEN'], ['f_with_shares', '.when-shares', 'NONE']]
    .forEach(function (spec) {
      var control = document.getElementById(spec[0]);
      if (!control) return;
      var span = control.closest('form').querySelector(spec[1]);
      var reflect = function () { if (span) span.classList.toggle('off', control.value === spec[2]); };
      control.addEventListener('change', reflect); reflect();
    });

  // "+7" in a date box means seven days from today. Faster than a picker for
  // the weekly cadence most of these trades follow.
  document.addEventListener('keydown', function (event) {
      var input = event.target;
      if (!input || input.type !== 'date') return;
      if (event.key !== 'Enter') return;
      var typed = input.value.trim();
      var relative = /^\\+(\\d+)$/.exec(typed);
      if (!relative) return;
      event.preventDefault();
      var when = new Date();
      when.setDate(when.getDate() + parseInt(relative[1], 10));
      input.value = when.toISOString().slice(0, 10);
  });

  // The positions table updates in place. Opening a form fetches the row and
  // its form; expanding a chain fetches that chain's rows; closing a form
  // fetches nothing at all. Only when the whole list may have changed is the
  // full table fetched, and then rows are diffed rather than replaced. Nothing
  // is fetched speculatively: over a forwarded port a 500-row table is slow,
  // and a prefetch of it would queue ahead of the click that matters.
  var table = document.querySelector('table.positions');
  if (table && window.fetch) {
    var cache = {}, order = [], pending = {};
    // One key per table state, however the URL was spelled: "/" and
    // "/?show=open" are the same table, and so are two orderings of the same
    // filters. Otherwise a visited view is fetched again.
    var canon = function (href) {
      var qs = (href.split('#')[0].split('?')[1] || '');
      var p = new URLSearchParams(qs);
      p.delete('partial');
      if (!p.get('show')) p.set('show', 'open');
      var keys = Array.from(p.keys()).filter(function (k, i, a) { return a.indexOf(k) === i; });
      keys.sort();
      return keys.map(function (k) { return k + '=' + p.get(k); }).join('&');
    };
    // Bounded by memory, not by count: a filtered view is a few KB, the
    // whole closed list about half a megabyte. Eight megabytes holds a long
    // session of browsing, so a view is fetched once and then never again
    // until the page itself reloads.
    var LIMIT = 8 * 1024 * 1024, held = 0;
    var remember = function (href, html) {
      var key = canon(href);
      if (cache[key]) held -= cache[key].length;
      else order.push(key);
      cache[key] = html;
      held += html.length;
      while (held > LIMIT && order.length > 1) {
        var old = order.shift();
        held -= cache[old].length;
        delete cache[old];
      }
    };
    // What a swap needs of the current page: strip, filter bar and table.
    var snapshot = function () {
      var t = document.querySelector('.totals'), b = document.querySelector('.filterbar'),
          c = current();
      return (t ? t.outerHTML : '') + (b ? b.outerHTML : '') + (c ? c.outerHTML : '');
    };
    var current = function () { return document.querySelector('table.positions'); };
    var here = function () { return window.location.pathname + window.location.search; };
    var param = function (href, name) {
      var m = new RegExp('[?&]' + name + '=([^&#]*)').exec(href);
      return m ? decodeURIComponent(m[1]) : null;
    };
    var get = function (href, partial) {
      if (partial === 'table' && cache[canon(href)]) return Promise.resolve(cache[canon(href)]);
      var key = partial + ' ' + href;
      if (pending[key]) return pending[key];
      var base = href.split('#')[0];
      var url = base + (base.indexOf('?') >= 0 ? '&' : '?') + 'partial=' + partial;
      pending[key] = fetch(url, { credentials: 'same-origin' })
        .then(function (res) { if (!res.ok) throw new Error(res.status); return res.text(); })
        .then(function (html) {
          if (partial === 'table') remember(href, html);
          delete pending[key]; return html;
        })
        .catch(function (err) { delete pending[key]; throw err; });
      return pending[key];
    };
    var rowsOf = function (html) {
      var t = document.createElement('table');
      t.innerHTML = '<tbody>' + html + '</tbody>';
      return Array.prototype.slice.call(t.tBodies[0].children);
    };
    var settle = function (href, push) {
      if (push) history.pushState({ bcoj: href }, '', href);
      var body = current();
      var form = body.querySelector('tr.action-row');
      var anchor = href.split('#')[1];
      var row = anchor ? document.getElementById(anchor) : null;
      if (form) form.scrollIntoView({ block: 'nearest' });
      else if (row) row.scrollIntoView({ block: 'nearest' });
      var focus = form && form.querySelector('[autofocus]');
      if (focus) focus.focus({ preventScroll: true });
    };

    // Bring the rows on screen in line with a freshly fetched table, keeping
    // every row whose markup is unchanged.
    var patchRows = function (cur, fresh) {
      var a = cur.tBodies[0], b = fresh.tBodies[0];
      if (!a || !b) return false;
      var have = a.children, want = Array.prototype.slice.call(b.children);
      var i = 0;
      for (var j = 0; j < want.length; j++) {
        var w = want[j], found = -1;
        for (var k = i; k < have.length && k < i + 40; k++) {
          if (have[k].outerHTML === w.outerHTML) { found = k; break; }
        }
        if (found < 0) { a.insertBefore(w, have[i] || null); i++; }
        else { var gone = found - i; while (gone-- > 0) a.removeChild(have[i]); i++; }
      }
      while (have.length > i) a.removeChild(have[have.length - 1]);
      return true;
    };

    // Two quick clicks are two requests; whichever answers last must not
    // overwrite the one asked for last. Only the newest request may render.
    var seq = 0;
    var full = function (href, push) {
      var cur = current();
      if (cur && !cache[canon(here())]) remember(here(), snapshot());
      var mine = ++seq;
      return get(href, 'table').then(function (html) {
        if (mine !== seq) return;
        var holder = document.createElement('div');
        holder.innerHTML = html;
        var fresh = holder.querySelector('table.positions');
        if (!fresh) { window.location.href = href; return; }
        if (!patchRows(cur, fresh)) cur.replaceWith(fresh);
        // The strip and the filter bar describe the table: they travel with it.
        var freshTotals = holder.querySelector('.totals');
        var curTotals = document.querySelector('.totals');
        if (freshTotals && curTotals) curTotals.replaceWith(freshTotals);
        var freshBar = holder.querySelector('.filterbar');
        var curBar = document.querySelector('.filterbar');
        if (freshBar && curBar) {
          // The views bubble keeps whatever the reader chose: closed stays
          // closed even inside a year, open stays open.
          // The year rows and the Due row never show together: choosing
          // Expiring closes the years, whatever the reader had open.
          var curViews = curBar.querySelector('#allviews');
          var freshViews = freshBar.querySelector('#allviews');
          if (curViews && freshViews && curViews.hasAttribute('data-user')
              && !freshBar.querySelector('#duerow')) {
            freshViews.hidden = curViews.hidden;
            freshViews.setAttribute('data-user', '1');
            var t = freshBar.querySelector('a[data-toggle=allviews]');
            if (t) t.classList.toggle('here', !freshViews.hidden);
          }
          var wasOpen = curBar.querySelector('details.more[open]');
          var q = curBar.querySelector('#f_q');
          var typing = q && document.activeElement === q ? [q.selectionStart, q.selectionEnd] : null;
          if (wasOpen) { var m = freshBar.querySelector('details.more'); if (m) m.open = true; }
          curBar.replaceWith(freshBar);
          if (typing) {
            var nq = freshBar.querySelector('#f_q');
            if (nq) { nq.focus({ preventScroll: true }); nq.setSelectionRange(typing[0], typing[1]); }
          }
        }
        settle(href, push);
      }).catch(function () { window.location.href = href; });
    };

    // Drop any open form and return its row's button to "open my form".
    var closeForms = function () {
      var tb = current().tBodies[0];
      tb.querySelectorAll('tr.action-row').forEach(function (r) { r.remove(); });
      tb.querySelectorAll('a.act.here').forEach(function (a) {
        a.classList.remove('here');
        var id = a.closest('tr').id.replace(/^row-/, '');
        var base = a.getAttribute('href').split('#')[0];
        a.setAttribute('href', base + '&act=' + id + '&do=close#row-' + id);
      });
    };

    // Show one of the forms a row shipped with, no request involved.
    var showTab = function (row, key) {
      row.querySelectorAll('a[data-form-tab]').forEach(function (t) {
        t.classList.toggle('here', t.getAttribute('data-form-tab') === key);
      });
      row.querySelectorAll('.form-box > [data-form]').forEach(function (p) {
        p.hidden = p.getAttribute('data-form') !== key;
      });
    };
    var openLocal = function (href, target) {
      var tpl = current().querySelector('template.acts[data-for="' + param(href, 'act') + '"]');
      if (!tpl || !tpl.content || !tpl.content.firstElementChild) return false;
      closeForms();
      var row = tpl.content.firstElementChild.cloneNode(true);
      target.after(row);
      showTab(row, param(href, 'do') || 'close');
      var pill = target.querySelector('a.act');
      if (pill) {
        pill.classList.add('here');
        pill.setAttribute('href', href.replace(/&act=[^&#]*&do=[^&#]*/, ''));
      }
      return true;
    };

    var openForm = function (href, push) {
      var target = document.getElementById('row-' + param(href, 'act'));
      if (!target) return full(href, push);
      var cur = current();
      if (!cache[canon(here())]) remember(here(), snapshot());
      if (openLocal(href, target)) { settle(href, push); return Promise.resolve(); }
      var mine = ++seq;
      return get(href, 'action').then(function (html) {
        if (mine !== seq) return;
        var rows = rowsOf(html);
        if (rows.length < 2) return full(href, push);
        closeForms();
        target.replaceWith(rows[0]);
        rows[0].after(rows[1]);
        settle(href, push);
      }).catch(function () { return full(href, push); });
    };

    var expand = function (href, push) {
      var target = document.getElementById('row-' + param(href, 'chain'));
      if (!target) return full(href, push);
      var cur = current();
      if (!cache[canon(here())]) remember(here(), snapshot());
      var mine = ++seq;
      return get(href, 'block').then(function (html) {
        if (mine !== seq) return;
        var rows = rowsOf(html);
        if (!rows.length) return full(href, push);
        var ids = {};
        rows.forEach(function (r) { if (r.id) ids[r.id] = true; });
        var tb = cur.tBodies[0];
        var marker = document.createElement('tr');
        target.before(marker);
        Array.prototype.slice.call(tb.children).forEach(function (r) {
          if (r.id && ids[r.id]) r.remove();     // legs listed on their own
        });
        rows.forEach(function (r) { marker.before(r); });
        marker.remove();
        settle(href, push);
      }).catch(function () { return full(href, push); });
    };

    var swap = function (href, push) {
      var tb = current().tBodies[0];
      var chainOpen = !!tb.querySelector('tr.chain-head');
      var formOpen = !!tb.querySelector('tr.action-row');
      var wantsChain = param(href, 'chain') !== null;
      // A position page shows one chain, always; only forms come and go.
      if (current().hasAttribute('data-fixed')) chainOpen = wantsChain;
      var wantsForm = param(href, 'act') !== null && param(href, 'do') !== null;
      if (wantsChain === chainOpen) {
        if (wantsForm) return openForm(href, push);            // a form, chain as is
        if (formOpen) { closeForms(); settle(href, push); return; }   // just close it
      }
      if (wantsChain && !chainOpen && !formOpen) return expand(href, push);
      return full(href, push);                                  // collapse and the rest
    };

    document.addEventListener('click', function (event) {
      if (event.metaKey || event.ctrlKey || event.button !== 0) return;
      var link = event.target.closest(
        'table.positions a.legs, table.positions a.act, table.positions a.close-form, ' +
        'table.positions a.cancel');
      if (link) {
        var path = link.getAttribute('href').split('?')[0];
        if (path !== window.location.pathname) return;               // another page: go there
        if (link.hasAttribute('data-form-tab')) return;               // handled as a tab
        if (link.classList.contains('legs') && link.closest('table[data-fixed]')) return;
        event.preventDefault();
        swap(link.getAttribute('href'), true);
        return;
      }
      if (event.target.closest('a, button, input, select, label, form')) return;
      var row = event.target.closest('table.positions tr[data-chain]');
      if (!row || row.closest('table[data-fixed]')) return;
      swap(row.getAttribute('data-chain'), true);
    });
    window.addEventListener('popstate', function (event) {
      var href = (event.state && event.state.bcoj) || (here() + window.location.hash);
      full(href, false).catch(function () { window.location.reload(); });
    });
    // Filtering is a table change too: no page load, rows diffed in place.
    // Every control applies itself: segments and chips are links, selects
    // and dates apply on change, the search box as you type.
    document.addEventListener('click', function (event) {
      if (event.metaKey || event.ctrlKey || event.button !== 0) return;
      var flt = event.target.closest('.filterbar a.flt');
      if (!flt) return;
      event.preventDefault();
      full(flt.getAttribute('href'), true);
    });
    document.addEventListener('change', function (event) {
      var form = event.target.closest('form.filters, form.pick');
      if (form && (event.target.tagName === 'SELECT' || event.target.type === 'date')) {
        applyFilters(form);
      }
    });
    var typeTimer = null;
    document.addEventListener('input', function (event) {
      var form = event.target.closest('form.filters');
      if (!form || event.target.name !== 'q') return;
      clearTimeout(typeTimer);
      typeTimer = setTimeout(function () { applyFilters(form); }, 300);
    });
    document.addEventListener('submit', function (event) {
      var form = event.target.closest('form.filters');
      if (!form) return;
      event.preventDefault();
      applyFilters(form);
    });
    var applyFilters = function (form) {
      var params = new URLSearchParams(new FormData(form));
      Array.from(params.keys()).forEach(function (k) {
        if (!params.get(k)) params.delete(k);
      });
      var filterField = document.querySelector('form.save-view input[name=filter]');
      if (filterField) filterField.value = params.toString();
      full('/?' + params.toString(), true);
    };
  }

  // Delegated, so tab strips inside a freshly swapped-in row work too.
  document.addEventListener('click', function (event) {
    if (event.metaKey || event.ctrlKey || event.button !== 0) return;
    var tab = event.target.closest('a[data-form-tab]');
    var closer = tab ? null : event.target.closest('[data-form-close]');
    if (!tab && !closer) return;
    var strip, box;
    if (tab) {
      var owner = tab.hasAttribute('data-tabs-for') ? tab : tab.closest('.tabs[data-tabs-for]');
      box = owner && document.getElementById(owner.getAttribute('data-tabs-for'));
    } else {
      box = closer.closest('.form-box');
    }
    strip = box && (document.querySelector('.tabs[data-tabs-for="' + box.id + '"]') || box);
    if (!box || !strip) return;
    // Inside the positions table the header x closes the form via the table
    // swap; a re-click on the open tab there is simply nothing to do.
    var inTable = !!box.closest('table.positions');
    var closing = tab ? tab.classList.contains('here') : true;
    if (closing && inTable) { event.preventDefault(); return; }
    event.preventDefault();
    var key = tab ? tab.getAttribute('data-form-tab') : null;
    var tabs = Array.prototype.slice.call(strip.querySelectorAll('a[data-form-tab]'));
    if (tab && tabs.indexOf(tab) < 0) tabs = [tab];   // a lone button is its own strip
    var panels = box.querySelectorAll(':scope > [data-form]');
    tabs.forEach(function (t) {
      t.classList.toggle('here', !closing && t.getAttribute('data-form-tab') === key);
    });
    panels.forEach(function (p) { p.hidden = closing || p.getAttribute('data-form') !== key; });
    if (!closing) {
      var shown = box.querySelector(':scope > [data-form="' + key + '"]');
      var first = shown.querySelector('input:not([type=hidden]):not([value]), select') ||
                  shown.querySelector('input:not([type=hidden])');
      if (first) first.focus({ preventScroll: true });
    }
    if (window.history.replaceState) {
      var href = (tab || closer).getAttribute('href').replace(/#.*$/, '');
      if (closing && tab) href = href.replace(/[?&](do|form|kind)=[^&]*/, '').replace(/\?$/, '');
      window.history.replaceState(null, '', href);
    }
  });

  // A form with a preview box shows what it would do as it is typed. The
  // figures come from the server -- the same arithmetic the action itself
  // uses -- so the preview and the record can never disagree. Delegated, so
  // forms swapped in later are covered too.
  document.addEventListener('input', function (event) {
    var form = event.target.closest('form');
    var box = form && form.querySelector('[data-preview]');
    if (!box || !window.fetch) return;
    clearTimeout(box._previewTimer);
    box._previewTimer = setTimeout(function () {
      var params = new URLSearchParams(new FormData(form));
      params.delete('csrf'); params.delete('next');
      fetch(box.getAttribute('data-preview') + '?' + params.toString(),
            { credentials: 'same-origin' })
        .then(function (res) { return res.ok ? res.text() : ''; })
        .then(function (html) { if (html) box.innerHTML = html; })
        .catch(function () {});
    }, 120);
  });

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
