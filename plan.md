# BC Options Trade Journal — design

A personal, local options journal. Correct P/L across rolls, assignments,
covered calls **and shares**; fast manual entry; real decision support when
rolling. Single user, single account, offline.

It replaces a hand-maintained spreadsheet, and the spreadsheet's arithmetic is
worth taking seriously — five years of it reconciles to the cent. But the sheet
is the source of *history*, not the design target. §3 is the list of things it
cannot do, and those are what the app is for.

No real trade data appears in this repository. See [docs/legacy-format.md](docs/legacy-format.md)
for the import format.

---

## 1. Decisions

| Area | Decision | Why |
| --- | --- | --- |
| Language | Python 3.10+ | Best fit for money math (`Decimal`) and messy CSV parsing. |
| Dependencies | **None, anywhere** | `Decimal`, `csv`, `sqlite3` and `http.server` are all standard library. Nothing to install, so the app runs on any machine with Python and the P/L rules are testable everywhere. |
| Database | SQLite, one file, stdlib `sqlite3` | Zero admin; a few hundred rows a year. Back up by copying the file. Ten simple tables with straightforward joins do not earn an ORM. |
| Migrations | A small versioned runner over `PRAGMA user_version` | Alembic is heavy for this and needs a package manager. Each migration runs in its own transaction. |
| Frontend | stdlib `http.server` + hand-written HTML and CSS | Changed from FastAPI/Jinja2/HTMX. For one user on loopback there is nothing for a framework to do: no concurrency, no schema to publish, and validation is domain logic that already exists. What it buys is the deployment story -- `python3 -m bcoj.web` runs anywhere Python does, with nothing to install. |
| JavaScript | None third-party; hand-written script is the primary interaction layer | Expanding, opening forms and switching tabs swap in place, prefetched on hover. Server-rendered paths stay as the fallback and as what the tests drive, but they do not limit what the UI does. Nothing to vendor, audit or keep current. |
| Priorities | **Data integrity first, then snappiness** | Set 2026-09-03. Refuse impossible states at entry (future dates, selling before buying, over-selling a pinned lot); every write audited and undoable. No full page reload for expand, open, or switch. Integrity and data-health work is ordered ahead of reporting. |
| Deployment | One Docker image, one volume; also bare in an LXC | Self-contained either way. |
| Market data | None. Open positions show a **computed profit target** | Nothing to type in, nothing to fetch, works air-gapped. |
| Data in | Manual entry for daily trades; a **one-off importer** for the legacy sheet | Import is a migration; corrections happen in the app afterwards. |
| Auth | Localhost bind; optional single password via env var | A personal app, not a service. |
| Money | `Decimal` everywhere, never `float` | Floats silently corrupt accounting. |

---

## 2. The arithmetic

Every figure derives from the entered values. Nothing computed is stored, so
correcting a rule fixes all history with nothing to migrate.

```
Q = signed quantity     (positive = SHORT / sold to open, negative = LONG)

open_cash  =  Q × mult × open_price  − open_fee
close_cash = −Q × mult × close_price − close_fee
P/L        = open_cash + close_cash        (zero while open)
```

One formula covers long and short, calls and puts.

**Chain carry.** Following the roll link, `carry(p) = realized(parent) +
carry(parent)`. This is what makes a rolled position legible: per-leg P/L alone
actively misleads, because the leg being closed in a roll is usually a loss even
when the position is net ahead.

**Profit target.** The close price at which a chain would net a chosen fraction
(50% by default) of its available credit — computed, not stored, so it stays
correct as the carry moves underneath it. Clamped at zero: a chain already
underwater has no profit target, and its best case is expiring worthless.

**Break-even.** `strike − net_chain_credit / shares` for a short put, mirrored
for a call. The figure a rolled position most needs and the sheet never had:
below it at expiry, however many years of rolling the chain represents, the whole
thing is a net loss.

---

## 3. What a sheet like this cannot do

### 3.1 Share P/L is never computed

Covered calls sit as individual unlinked rows and the shares behind them have no
home. A column records share *cash flow*, but nothing accumulates it, so the
gain or loss on the stock is never worked out. A wheel — put assigned, dozens of
covered calls, shares finally called away — is dozens of disconnected rows with
no total and no duration.

### 3.2 The headline total is silently conditional

A summary that adds share cash flow to option P/L is correct **only while the
underlying is flat**, and nothing tells you whether it is. Every dollar of a
share purchase reads as a loss until those shares are sold. The same total is
therefore trustworthy for one ticker and badly wrong for the next, with no
indication which — and it understates by the entire cost of anything still held.

With real share lots the total becomes `realized option P/L + realized share
P/L`, open lots carried at basis and reported separately, plus a warning when a
ticker's share count doesn't reconcile.

### 3.3 Two correct numbers that look contradictory

Realized P/L for a ticker already contains the losses on legs since rolled, so
a ticker can show a healthy realized figure while one of its chains carries a
large loss forward. Both are right at different scopes. A sheet labels neither,
so the same ticker reads as profitable or catastrophic depending which cell you
land on. The app labels scope on every figure — leg, chain, wheel, ticker,
portfolio — and never mixes scopes in one sum.

### 3.4 A roll chain can't be read

A chain of a dozen or more legs spanning years is scattered across the sheet by
date. There is no way to see the strike walking down while the quantity grows,
or the capital at risk growing with it.

### 3.5 Derived metrics fail confusingly

Per-share figures divide by a share count. When the count is zero they show
`#DIV/0!`; worse, when the count is *negative* — which happens, see
[docs/legacy-format.md](docs/legacy-format.md) — they produce plausible-looking
numbers that are meaningless. Silent nonsense beats a visible error only in
appearance.

### 3.6 Ease of use

Dropdown-and-macro-button filtering, no notes column in a journal, no
validation, and every roll re-typed by hand.

---

## 4. Core model

### 4.1 `position` — one option position

- `id`, `underlying`, `expiry`, `strike`, `right`, `multiplier`
- `direction` (`SHORT`/`LONG`), `quantity` (**always positive**)
- `opened_on`, `open_price`, `open_fee`
- `closed_on`, `close_price`, `close_fee`
- `status`: `OPEN` | `CLOSED` | `EXPIRED` | `ROLLED` | `ASSIGNED` | `SPLIT`
- `rolled_from_id`, `split_from_id`, `split_from_quantity`
- `share_lot_id`, `spread_group_id`, `target_pct`, `notes`, `tags`

Direction plus a positive quantity replaces a signed quantity, so no arithmetic
depends on a sign convention. The entry form still accepts a negative quantity
as shorthand for long, matching the habit the sheet built.

One row per position rather than a fill ledger, because that is what keeps
manual entry to one form and one submit per trade. Partial events are handled by
§4.3 instead of by lot matching.

### 4.2 Shares — lots, disposals, explicit matching

Options need no lot matching: one row is one position. **Shares do.** Where lots
were acquired at very different prices, FIFO and LIFO can differ by tens of
thousands on a single disposal *and* leave a different lot behind. An unstated
convention is a wrong answer waiting to happen, so the share side is a proper
three-table ledger:

- **`share_lot`** — an acquisition: quantity, date, cost per share, fee, and a
  `source` of `OUTRIGHT_BUY` | `BUY_WRITE` | `PUT_ASSIGNMENT`
- **`share_disposal`** — a sale: quantity, date, proceeds per share, fee, and a
  `kind` of `SOLD` | `CALLED_AWAY`
- **`share_allocation`** — which lots a disposal consumed, and the P/L that fell
  out of it

**FIFO by default**, with a **specific-lot override per disposal**. The
allocation rows record what was actually chosen, so the answer stops depending
on a convention nobody wrote down. Changing the rule is a rebuild, not a
migration.

**Buy-write shares are earmarked.** When a buy-write's own call is exercised,
the shares bought for it are the shares delivered. Plain FIFO would reach past
them to an older, cheaper lot and report a gain that never happened, so those
disposals match their own lot specifically.

**`OUTRIGHT_BUY` is the case a sheet like this has no room for:** buy shares
now, write a call later or never. The lot stands alone. The three sources differ
only in that label — everything downstream treats them identically, which is
also what lets the importer convert disguised share rows into ordinary lots.

A short call with a `share_lot_id` is covered; without one it is naked and
flagged. The app warns when linked call contracts exceed a lot's remaining
shares, and **refuses to let a ticker's share count go negative.**

### 4.3 Split — partial closes, assignments and rolls

**Split** divides a position into two children, each inheriting the open date,
price and a pro-rata share of the opening fee. Each records `split_from_id` and
`split_from_quantity` — the parent's size at the moment of the split, which is
what lets inherited chain history be *divided* between the halves rather than
duplicated onto both. Duplicating it would double-count the history and, worse,
spread a whole chain's loss across a fraction of the contracts, making
break-even nonsense.

The parent is kept as a `SPLIT` record — a journal should not delete something
that happened — and realizes nothing thereafter, so no total moves. An
invariant test asserts exactly that: splitting changes neither premium nor
realized P/L.

One primitive covers every partial case:

- **Partial assignment** — split, assign one child (creating a share lot), roll the other
- **Partial close** — split, close one child, leave the other open
- **Partial roll** — split, roll one child, let the other run to expiry

There is exactly one definition of carry (`engine.chains.inherit`), used by
both the chain walker and the memoized index, so the two cannot drift apart —
they did once, and break-even silently disagreed with itself by a cent until a
test caught it.

### 4.4 `chain` and `wheel` (both derived)

Walking the roll and split links yields the **chain**: cumulative P/L, duration
from first open to last close, net premium, capital at risk, leg count,
break-even, profit target, and the credit still needed to recover.

The **wheel** is the unit §3.1 says is missing — a ticker's full episode, keyed
on a share lot (or a chain with no shares). It sums the option chains that fed
it, every covered call written against the lot and their chains, and the share
P/L on disposal: one number for "how did this actually go", and one duration.
`wheel_link` groups a lot with its chains, seeded on import and editable by hand.

Several successors from one predecessor are allowed, so a forked chain renders as
a tree. Merging two chains into one position is not supported.

### 4.5 `spread_group` — multi-leg positions

Verticals appear as separate rows with opposite signs and independently rolled
legs. A nullable `spread_group_id` keeps them displayed and rolled together.
Cheap, and rare enough not to shape anything else.

---

## 5. Accounting conventions

**Premium and share P/L stay separate** in storage; display also offers the
trader view, with basis reduced by net premium (§5.1). The wheel total is
identical either way — only the option/share split moves — and a test asserts
they reconcile.

**Period attribution.** Each leg realizes on its own close date, so a chain
spanning years contributes to whichever period each leg closed in. Chain and
wheel totals are reported separately from period totals: different questions,
not summable.

**Capital at risk.** `strike × mult × quantity` for a short put, share cost
basis for a covered call, the debit paid for a long option, `width × mult − net
credit` for a vertical. Naked short calls are flagged and excluded from return
averages.

**Fees.** Always net-of-fees. Stored per leg, defaulting to `quantity ×
fee_rate`, and **editable on every leg, open and close, always.** Expiry and
assignment default to a zero closing fee.

**Derived metrics with no denominator are withheld with a reason**, never shown
as an error code — and an impossible input is refused rather than computed
through.

### 5.1 Adjusted share basis

"What do I really own these at", as two figures rather than one:

```
adjusted_unit_price  = (lot_cost − acq_premium) / quantity
adjusted_after_calls = (lot_cost − acq_premium − cc_premium) / quantity
min_call_strike      = adjusted_after_calls, rounded up to a listed strike
```

Two, so the covered-call contribution is visible instead of blended in: the
first is the basis after the premium that acquired the shares, the second after
the calls written against them since. `min_call_strike` is the actionable form —
writing a call below the adjusted basis locks in a loss, so it is the floor
worth knowing before selling one.

Rules that matter:

- Chains contribute their **cumulative net**, not the last leg
- **A losing call chain raises the basis** — net P/L handles the sign, no special case
- **Only realized premium counts**; open premium is a separate projection
- Premium spreads over the **linked lot's** shares, since the lot is the unit measured
- Multiple lots report **individually and blended**, because a blend of lots
  bought at very different prices describes none of them
- Basis is **queryable at any past date**, not just today

---

## 6. Manual entry UX

Daily entry is the app's main job, and rolls dominate the volume.

**New position** — one keyboard-navigable row, no page reloads: ticker
(autocomplete from history, most recent first), expiry (picker defaulting to the
nearest Friday, plus `+7d`/`+14d`/`+1mo`), strike, right, direction, quantity,
price. Fee auto-fills and stays editable. Defaults follow the actual
distribution. Submit leaves the cursor in a fresh row.

**Roll** — one button on any open position. Pre-fills from the old row, asks
only for the closing price and the new strike/expiry/quantity/price, then in one
commit marks the old row rolled, links it, and carries the chain forward — with
the §7 decision panel visible *before* you confirm.

**Split** — one field: how many contracts to peel off. Then act on each child
independently. This is the partial-assignment workflow.

**Close / Expire / Assign** — quick actions from the positions list. Expire and
Assign prefill a zero close price and fee. Assign also creates the share lot (a
put) or disposes the linked lot (a call), so the share side cannot be forgotten
— which is exactly how a sheet loses it.

**Buy-write** — one form creating a share lot and its covered call together.

**Buy / sell shares** — creates or disposes a lot with no option involved, so
outright stock activity never needs a fake option row again.

**Open positions view** — grouped by ticker: carried P/L, capital at risk, days
held, DTE, target close price, expected P/L at target, break-even, and a coverage
flag on short calls.

**Expiry queue** — with no market data the app cannot know whether an expired
short put expired worthless or was assigned, so past-expiry open positions land
in an "action needed" list. Two clicks each. A core screen, not an edge case.

---

## 7. Beyond the sheet

**Roll decision support.** The most-used action deserves the most help. Before
confirming: carried chain P/L, credit or debit for this roll, new capital at
risk, new break-even, the new target, and **the credit required to bring the
chain back to net positive.** Also a flag when a roll increases size, since that
is how a position quietly grows several-fold one step at a time.

**Break-even and assignment exposure.** Per position, per chain, per ticker, plus
a **cash-obligation calendar**: for each expiry date, the cash required if
everything at or below strike is assigned.

**Concentration view.** Capital at risk by ticker as a share of the total, paired
with §3.3's scope labels so a healthy realized figure never hides what is still
at risk beneath it.

**Chain timeline.** One screen per chain: every leg in order with strike, expiry,
quantity, credit/debit, running total, DTE and days held, plus strike drift and
size growth.

**Wheel view.** §4.4 — option and share P/L side by side, finally.

**Target performance.** A 50% target is worth measuring against: how often
positions actually close at or past it versus getting rolled, by ticker and DTE.

**Journal notes and tags.** Free text per position plus tags, so the reason for a
trade or a roll is recorded. A journal without a notes column is the most obvious
gap of all.

**Entry validation.** Expiry after open, close after open, close after the
predecessor's open, fee plausible for the contract count, strike on a listed
increment, no negative DTE, duplicate warning. Every date error in the legacy
data would have been caught at entry.

**Real filtering and search.** Multi-select ticker, date range, status, right,
direction, tag, free text over notes; every column sortable; saved views.

**Audit trail and undo.** Every create, edit and delete recorded with a timestamp
and previous values, and revertible. The data is irreplaceable and hand-entered.

---

## 8. Import and data health

**Import is faithful.** It writes what the sheet says and changes nothing.

1. **Parse** the known layout — accounting negatives, currency strings, `-` as
   null, `0.00` distinct from blank. Row identity comes from the sheet's GUID, so
   roll links resolve directly and no detection heuristics are needed.
2. **Recompute** each row's cash flows, P/L, chain carry, share cash flow, put
   risk and profit target, then **diff all eight against the sheet's own
   columns**, plus the summary totals.
3. **Report** the diff: exact, within a cent or two of rounding, disagreeing, or
   unparseable — each with its row id and reason. Nothing is dropped silently.
4. **Flag, don't fix** — suspected disguised share rows, suspected flattened
   partials, unbalanced share counts, date errors. Flags are stored on the row.

**Data health screen** — a permanent feature, not an import wizard. Flagged rows
with one-click fixes: convert to a share transaction, correct a date, reconstruct
as a split, link a call to a lot, dismiss. Available forever, because new
mistakes will happen too.

**Reconstructed lots** live in configuration outside version control, never in
source: see `bcoj/importer/estimates.py`. Absent that config, a ticker in deficit
stays blocked, which is the safe default — a blocked ticker is visible, a guessed
one is not.

The diff is the engine's acceptance test. Five years of hand-maintained P/L is a
far better oracle than any fixture, so the engine is not trusted until it agrees
row by row, or every difference is explained.

---

## 9. Schema

```
settings          key, value        -- fee_rate, profit_target_pct, multiplier,
                                    -- share_matching_rule, strike_increment
positions         id, underlying, expiry, strike, option_right, direction,
                  quantity, multiplier, opened_on, open_price, open_fee,
                  closed_on, close_price, close_fee, status,
                  rolled_from_id, split_from_id, share_lot_id,
                  spread_group_id, target_pct, notes, import_batch_id
share_lots        id, underlying, quantity, acquired_on, cost_per_share, fee,
                  source, assigning_position_id, estimated, notes
share_disposals   id, underlying, quantity, disposed_on, proceeds_per_share,
                  fee, kind, disposing_position_id, specific_lot_ids
share_allocations disposal_id, lot_id, quantity, realized_pl, cost_basis,
                  proceeds                          -- derived, rebuildable
wheel_links       id, share_lot_id, position_id, role
spread_groups     id, label
tags / position_tags
flags             entity_type, entity_id, kind, detail, raised_at, resolved_at
audit_log         at, entity_type, entity_id, action, before, after
saved_views       id, name, filter
import_batches    id, filename, file_hash, row_count, imported_at, report
```

Money is `TEXT` holding a Decimal's exact digits — never `REAL`, which SQLite
would happily accept and which would silently corrupt accounting. Dates are
ISO-8601 `TEXT`.

---

## 10. Layout

```
bcoj/
  domain/     money, enums, types              — pure values, no I/O
  engine/     pnl, chains, targets, risk,
              shares, basis                    — pure calculation
  db/         schema, store                    — stdlib sqlite3, no ORM
  importer/   legacy_csv, reconcile, triage,
              shares, legacy_formulas, estimates
  cli.py
tests/        synthetic fixtures only
docs/         legacy-format.md
docker/
```

`domain/` and `engine/` never import `db/` or `importer/`. The engine takes
positions and returns numbers, which is what makes row-by-row reconciliation
possible at all.

---

## 11. Reporting

**Dashboard** — realized option P/L, realized share P/L, **true total**, cost
basis, capital at risk; open call/put/share counts; adjusted basis before and
after calls, and `min_call_strike`.

**No composite "unrealised P/L".** Without marks it isn't computable, so the
three things that *are* known are reported side by side and never summed:

- **Open premium collected** — cash in hand against an outstanding obligation
- **Expected P/L at target** — if every open chain closes at its target
- **Chain carry** — already realized on legs since rolled

Also: P/L by ticker (options, shares, combined), by period (day/week/month/
quarter/year, by leg close date), by chain and by wheel; the exposure calendar
and concentration view; win rate, average credit, average days held, premium
capture, return on capital at risk, annualized, target hit rate; breakdowns by
strategy, direction and ticker; CSV and JSON export plus a full SQLite backup.

---

## 12. Milestones

| # | Deliverable | State |
| --- | --- | --- |
| M1 | Engine (P/L, chains, targets, break-even, share matching, adjusted basis) + faithful importer + reconciliation + storage. No UI | **Done.** 119 tests. A hand-computed synthetic fixture reconciles exactly, and a real five-year export reconciles to the cent bar one 25-cent fee typo |
| ~~M2~~ | ~~Manual entry, positions list, validation, expiry queue, audit trail~~ | **Done.** 221 tests. Web UI over stdlib only; split divides chain history pro-rata; every mutation audited and revertible |
| ~~M3~~ | ~~Shares: lots, buy-write, outright buy/sell, covered-call linking, wheel view, true total P/L~~ | **Done.** Shares and per-ticker pages, buy/sell/buy-write forms, cover/uncover, bulk linking of imported covered calls, wheel totals, true total on the dashboard |
| ~~M4~~ | ~~Decision support: roll panel, break-even, obligation calendar, concentration~~ | **Done.** Roll form previews the chain before and after as it is typed, computed by running the real roll on a copy; Risk page with concentration by ticker and a worst-case obligation calendar; strike drift and size growth on every chain |
| ~~M5~~ | ~~Reporting, dashboard, filtering, saved views, notes and tags, export~~ | **Done.** Reports page: realized by month/quarter/year with running total, by ticker, and how short legs ended (hit rate, win rate, premium capture, by ticker and by DTE). Filter bar on the positions page that every link preserves; saved views; notes and tags editable on the position and at entry; CSV, JSON and SQLite-backup export |
| ~~M6~~ | ~~Data health screen; packaging: backup/restore, LXC notes~~ | **Done.** Data page: every record that disagrees with another or with the calendar, each with a link to where it is fixed, import flags dismissable; snapshots through SQLite's backup API next to the journal, restore behind a confirmation that snapshots first; docs/deploy.md for bare, LXC (systemd) and Docker |

M1 before any UI was deliberate: the reconciliation proves the engine against
years of real records, and every screen after it just displays what the engine
computes.

---

## 13. Testing

The public suite is synthetic and self-contained. Its fixture is fabricated but
internally consistent — every derived column hand-computed — so a clean
reconciliation means the engine agrees with arithmetic done independently of it,
rather than agreeing with itself.

Covered: one row of every shape the format produces (long and short, calls and
puts, expiry, assignment, buy-writes, multi-leg chains, an open position with a
target, a placeholder, a disguised share transaction, a flattened partial, a
ticker whose share count goes negative, and a partial disposal where FIFO and
LIFO diverge); parser edge cases; storage round-trips, rebuilds and batch
reverts; and the invariants below.

Invariants: a chain total equals the sum of its legs including split children;
split quantities and allocated fees sum to the parent's; allocation proceeds sum
exactly to the disposal's; both basis conventions reconcile to the same wheel
total; linked call contracts never exceed a lot's shares; no position closes
before it opens; a disposal never exceeds what was acquired.

A separate, unpublished suite reconciles the real export in full. It is the
authoritative check, and it is not in this repository because the data isn't.

---

## 14. Out of scope

- Live quotes, greeks, IV, mark-to-market
- Broker API sync and a generic multi-broker importer
- Multi-user, multi-account, multi-currency
- Tax lots, wash sales, 1256 contracts — a journal, not a tax tool
- Corporate actions (splits, mergers) — handled by editing rows
- Merging two roll chains into one position

---

## 15. Settled decisions

| Decision | Consequence |
| --- | --- |
| **Share lot matching: FIFO**, with a per-disposal specific-lot override | Makes the convention explicit and recorded rather than implied |
| **Buy-write shares are earmarked to their own call** | Avoids FIFO reaching past them to a cheaper lot |
| **No composite unrealised P/L** | Open premium, expected P/L and carry reported separately; unrealised P/L isn't computable without marks |
| **Profit target 50%** global default, per-position override | Computed, never stored |
| **Import faithfully, correct in the app** | Plus a permanent data-health screen |
| **Partial events import as-is**, flagged, not reconstructed | Only chain attribution is affected, not totals |
| **No dependencies below the UI** | Engine, importer and storage are stdlib only |
| **Reconstructed lots live in config, not source** | Real holdings never enter version control |
| **A split keeps the parent as a `SPLIT` record** | A journal should not delete something that happened; the tombstone realizes nothing, so no total moves |
| **Split divides chain carry pro-rata by quantity** | Duplicating it onto both halves would double-count and wreck break-even |
| **Refused actions are 400, not 500** | "Split must be between 1 and 9" is the user's mistake, not a crash |
| **Actions happen on the positions page** | Close / roll / expire / assign / split open inline beneath the row; the detail page is for reading a chain, not for acting |
| **Close and buy-back prices pre-fill with the profit target** | It is what you were aiming at; blank when the chain has nothing to aim for |
| **A roll form is two labelled trades** | "Close this leg" then "Open the new leg" -- it is two fills, and the form says so |
| **A split child links only to its split parent** | One link, never both, enforced on the type. The tombstone stays in every lineage regardless of the parent's own history, so the tree's shape no longer depends on what came before |
| **One row renderer for positions and chains** | A chain is shown as ordinary position rows -- same columns, same figures -- on the position page and when expanded in the list. Expanding reveals the chain's legs in order where the clicked row was, under a header row with the chain's total and a Hide control; legs already in the list move into it rather than appearing twice. No labels, no tree, no "family": it is a chain |
| **Position columns: open price, close price, credit, closing, realized, break-even, at risk** | Per-share prices beside the cash they produced. An open position's close price and closing cash are the profit target, shown as projections -- so no separate Target column. Carry stays on the position page, not in the table |
| **A wheel is derived, never stored** | Acquisition chain (the assigning put's whole chain), covered-call chains linked to the lot (chain heads only, so a rolled call counts once), and the share P/L matched to that lot. Total is realized only |
| **Covered-call links are proposed, not guessed** | A short call written while exactly one lot of its ticker was held is proposed for linking; several candidate lots means no proposal, because misplacing premium between lots is worse than leaving it unlinked. Imported history has no links, so this is how it gets them |
| **A sale pinned to a lot cannot exceed what that lot holds** | Refused at entry, with the lot's remaining shown in the dropdown and enforced as the input's maximum. A pinned sale draws only from its lot, so no later purchase can rescue it -- which is why it must be right when entered |
| **Share records are removable and the removal is undoable** | A mis-entered sale or purchase can block a ticker's matching; "remove" on the ticker page deletes it with a full audit snapshot, and History can restore it. A lot with calls written against it, or sales pinned to it, is refused |
| **No share record may be dated in the future** | Assignments and lot re-dates are refused past today. A lot dated after today is a recording error, and a sale dated today cannot draw from it -- the engine says so by name rather than reporting an empty lot |
| **A lot's date can be moved, and its assignment moves with it** | The one repair a future-dated assignment needs. Lot and position are updated together and each update is audited and undoable, so they never disagree about when the shares arrived |
| **Undo takes back a whole action or nothing** | Every record an action writes is audited under one group, and History undoes the group in reverse order in one transaction. Undoing one record of a split left the halves alive beside the parent. An action a later one depends on is refused until that later one is undone |
| **Health checks name problems and point at the fix; they never change data** | The entry guards stop most mistakes; the Data page catches what slipped past, was imported, or became true later (an option past expiry with no outcome). Fixing stays where the record lives, audited like any edit |
| **Snapshots are consistent copies, and a restore snapshots first** | SQLite's backup API, not a file copy that can catch a write half way. Restoring keeps the journal as it was under a `before-restore` name, so a restore is itself undoable |
| **A long leg targets a profit on its debit, 25% by default** | It has no credit to capture a fraction of. Target price is 1.25 times the price paid; expected P/L, capital at risk and the strip all follow from that instead of treating the debit as an underwater chain |
| **Reports never mix scopes in a sum** | Options are booked on the leg's close date, shares on the sale's date; each period and ticker shows the two apart before the total. Open legs appear in no report at any value |
| **"Hit" means ended at or past the target** | Closed at or under the target price, or expired worthless. A roll or an assignment is neither a hit nor a miss; the chain decides later |
| **A filter travels with every link** | Opening a form or a chain from a filtered list keeps the filter, so the view being looked at is never lost. A saved view is that query string under a name |
| **Exports carry the computed columns** | realized, carry, break-even, capital at risk ride along in positions.csv, so a spreadsheet gets the engine's answers, not just the inputs. The .db export goes through SQLite's backup API for a consistent copy |
| **The roll preview is the roll itself, run on a copy** | The panel calls the same action and chain code the roll will use and reads the resulting chain. No second formula exists to drift. Fetched from the server as the form is typed, so the figures shown are the figures recorded |
| **Risk is worst case by construction** | With no market data the calendar shows the cash owed if every short put were assigned and the shares every short call must deliver. A covered call rides on its shares and is not counted twice; a naked call is flagged, not summed, because it has no ceiling |
| **Repairs live on a per-ticker raw-data page, not beside the figures** | Removing or re-dating a record is fixing a mistake, not a daily action. The ticker page shows the figures and flags a suspect record; the fix is one link away |
| **Positions-page links carry both the expanded chain and the open action** | Opening a form no longer collapses the chain, and expanding a chain no longer closes the form. The open action's own link closes it |
| **The three share forms are all in the page; tabs only toggle** | Switching never reloads or scrolls; without script the links still work and land on the forms |
| **Assignment dates default to today, or to the expiry once it has passed** | Assignment is noticed the morning after and belongs on the expiry date; early assignment of a live contract belongs on today. Neither needs typing |
| **A blocked ticker is not "short"** | Matching can fail with a positive share count (a pinned sale its lot cannot cover). The badge says "matching blocked" and the callout names the sale; "short" is reserved for a genuinely negative count |
| **Shares are written before positions in a transaction** | A buy-write's call points at a lot created in the same action; the reverse never happens |
| **The position a page is about is tagged** | "▸ this position", its own tint and rail -- distinct from the open-leg tint, so it stands out even as a closed leg among closed legs |
| **Actions sit beneath the contract, colour-coded** | Blue closes, purple rolls, green keeps the credit, amber moves stock, grey divides |
| **Open and closed legs look different** | Status is a coloured pill; in a chain, closed legs step back and the open leg steps forward |
| **Open positions of one chain are marked** | A shared coloured rail and "1 of N open in this chain"; closed legs are history, not branches, and are never marked |
| **Put risk is not shown beside capital at risk** | Identical for a short put; the former exists only to reconcile the legacy column |
