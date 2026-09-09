# BC Options Trade Journal: design

A local, single-user options journal. Correct P/L across rolls, assignments,
covered calls and shares; fast manual entry; decision support when rolling.

It replaces a hand-maintained spreadsheet whose five years of arithmetic
reconcile to the cent. The sheet is the source of history, not the design
target. §3 lists what it cannot do; that is what the app is for. Its format is
in [docs/legacy-format.md](docs/legacy-format.md).

---

## 1. Decisions

| Area | Decision | Why |
| --- | --- | --- |
| Language | Python 3.10+ | `Decimal` for money, `csv` for the import |
| Dependencies | None | `Decimal`, `csv`, `sqlite3`, `http.server` are standard library. Runs wherever Python is |
| Database | SQLite, one file | A few hundred rows a year. No ORM: ten tables, plain joins |
| Migrations | Versioned runner over `PRAGMA user_version` | Each migration in its own transaction |
| Frontend | stdlib `http.server`, hand-written HTML, CSS and script | One user on loopback needs no framework. Script swaps expand, open and switch in place, prefetched on hover; server-rendered paths remain and are what tests drive |
| Priorities | Integrity first, then snappiness | Set 2026-09-03. Refuse impossible states at entry; every write audited and undoable; no full reload for expand, open, switch |
| Deployment | Docker image with one volume, or bare in an LXC | See docs/deploy.md |
| Market data | None. Open positions show a computed profit target | Works air-gapped |
| Data in | Manual entry; a one-off importer for the legacy sheet | Import is a migration; corrections happen in the app |
| Auth | Loopback bind; optional password via `BCOJ_PASSWORD` | A personal app, not a service |
| Money | `Decimal` everywhere, stored as `TEXT` | `float` and `REAL` corrupt accounting silently |

---

## 2. Arithmetic

Nothing computed is stored. Correcting a rule fixes all history.

```
Q = signed quantity   (positive SHORT, negative LONG)

open_cash  =  Q × mult × open_price  − open_fee
close_cash = −Q × mult × close_price − close_fee
P/L        = open_cash + close_cash        (zero while open)
```

**Carry.** Following the roll link, `carry(p) = realized(parent) + carry(parent)`.
The leg closed in a roll is usually a loss even when the chain is ahead, so
per-leg P/L alone misleads.

**Profit target.** The close price at which a chain nets a fraction (50% by
default) of its available credit. Clamped at zero: an underwater chain has no
target, and its best case is expiring worthless. A long leg targets 25% profit
on its debit.

**Break-even.** `strike − net_chain_credit / shares` for a short put, mirrored
for a call. Below it at expiry the whole chain is a net loss.

---

## 3. What the sheet cannot do

- **Share P/L.** Covered calls are unlinked rows; shares have no home. A wheel has no total and no duration.
- **A trustworthy headline.** Adding share cash flow to option P/L is right only while the underlying is flat. Anything still held reads as a loss.
- **Scope.** Realized for a ticker already contains losses on rolled legs, so a ticker can look healthy while a chain carries a large loss. Both are right at different scopes; the sheet labels neither.
- **Read a roll chain.** Legs scattered by date; no view of the strike walking down as size grows.
- **Break-even.** Never computed.
- **Divide safely.** Per-share figures over a zero or negative share count give `#DIV/0!` or plausible nonsense.
- **Usability.** Macro-button filtering, no notes, no validation, every roll retyped.

---

## 4. Model

### 4.1 `position`

`id`, `underlying`, `expiry`, `strike`, `right`, `multiplier`; `direction`
(`SHORT`/`LONG`) and `quantity` (always positive); open and close date, price,
fee; `status` in `OPEN | CLOSED | EXPIRED | ROLLED | ASSIGNED | SPLIT`;
`rolled_from_id`, `split_from_id`, `split_from_quantity`; `target_pct`,
`notes`, `tags`.

One row per position, not a fill ledger: one form, one submit per trade. The
entry form accepts a negative quantity as shorthand for long.

### 4.2 Shares

FIFO and LIFO can differ by tens of thousands on one disposal and leave a
different lot behind, so shares are a three-table ledger:

- `share_lot`: quantity, date, cost per share, fee, `source` in `OUTRIGHT_BUY | BUY_WRITE | PUT_ASSIGNMENT`
- `share_disposal`: quantity, date, proceeds per share, fee, `kind` in `SOLD | CALLED_AWAY`
- `share_allocation`: which lots a disposal consumed and the P/L; derived, rebuilt on demand

FIFO by default, specific-lot override per disposal. A buy-write's shares are
earmarked to its own call, so FIFO cannot reach past them to a cheaper lot.
The three sources differ only in label.

Coverage is a fact about the ticker, not a link on the call: a short call is
covered by the shares the ticker holds, oldest first; assignment sells by the
matching rule as the broker does. A short call with no shares behind it is
naked and flagged. A ticker's share count may not go negative.

### 4.3 Split

Divides a position into two children with the open date and price, the fee
pro-rata, and `split_from_quantity` so inherited carry is divided, not
duplicated. The parent stays as a `SPLIT` record and realizes nothing after.
One primitive covers partial assignment, partial close and partial roll.

Carry has one definition, `engine.chains.inherit`, used by the walker and the
memoized index alike.

### 4.4 Chain and wheel, both derived

Walking roll and split links gives the chain: cumulative P/L, duration, net
credit, capital at risk, legs, break-even, target, credit still to recover.
Several successors from one predecessor render as a tree. Merging chains is
not supported.

The wheel is a ticker's episode: the assigning put's chain, the covered-call
chains on the shares, and the share P/L. Realized only.

### 4.5 `spread_group`

A nullable id keeping a vertical's legs displayed and rolled together.

---

## 5. Conventions

- **Option and share P/L are stored apart.** The trader view reduces basis by net premium; the wheel total is identical either way, and a test asserts it.
- **Period attribution.** A leg realizes on its close date. Chain and wheel totals are not summable with period totals.
- **Capital at risk.** `strike × mult × qty` for a short put; the share cost for a covered call; the debit for a long option; `width × mult − credit` for a vertical. Naked calls are flagged and excluded from averages.
- **Fees.** Always net. Per leg, default `quantity × fee_rate`, editable always. Expiry and assignment default to zero closing fee.
- **No denominator, no figure.** Withheld with a reason, never an error code. Impossible inputs are refused.

### 5.1 Adjusted basis

One figure per ticker:

```
adjusted_basis  = (cost of shares held − option P/L − share P/L) / shares held
min_call_strike = adjusted_basis rounded up to a listed strike, never below 0
```

Shares are held at what was paid, fees in. Every closed option leg on the
ticker and the P/L on every share sold lowers what the rest must fetch.
Writing a call below the adjusted basis locks in a loss. Rules: the unit is the
ticker, never the lot; realized only; lifetime, never reset; dividends out of
scope.

---

## 6. Entry

Every trade form is the leg grid with the date first. Fee auto-fills, stays
editable. Defaults follow the data.

- **New position**: one row, submit leaves the cursor in a fresh row.
- **Roll**: prefilled from the old leg; asks for the closing price and the new leg. One commit closes, links and carries. The preview (§7) is the roll itself run on a copy.
- **Split**: one field, how many to peel off.
- **Close, Expire, Assign**: inline under the row. Assign creates the lot (put) or sells shares (call).
- **Buy-write, buy, sell shares**: on the shares page; outright stock never needs a fake option row.
- **Due**: past-expiry open positions need a human to say expired or assigned; a filter on the positions table, not a page.

---

## 7. Beyond the sheet

- **Roll preview**: carry, this roll's credit or debit, capital at risk, break-even, target, credit still to recover, size growth, strike change; before and after.
- **Risk**: concentration by ticker; worst-case obligation calendar by expiry.
- **Chain view**: every leg in order as ordinary position rows, with the chain total.
- **Wheel view**: option and share P/L side by side.
- **Target performance**: how often legs end at or past the target, by ticker and DTE.
- **Notes and tags** per position.
- **Validation at entry**: dates in order, no future share dates, fee plausible, no over-selling a pinned lot, duplicate warning.
- **Filters and saved views**: ticker, date, status, right, direction, tag, text; every link keeps the filter.
- **Audit and undo**: every action recorded as a group and undone as one.

---

## 8. Import and data health

Import writes what the sheet says. It parses the layout, recomputes eight
derived figures per row, diffs them against the sheet (exact, rounding,
disagreement, unparseable), and flags rather than fixes: disguised share rows,
flattened partials, negative share counts, date errors. The diff is the
engine's acceptance test.

The Data page is permanent: every record that disagrees with another or with
the calendar, each linking to where it is fixed. Import flags can be dismissed
once seen. Reconstructed lots live in config outside version control (see
`bcoj/importer/estimates.py`); without it a ticker in deficit stays blocked,
which is visible where a guess is not.

---

## 9. Schema

```
settings           key, value
positions          id, underlying, expiry, strike, option_right, direction,
                   quantity, multiplier, opened_on, open_price, open_fee,
                   closed_on, close_price, close_fee, status,
                   rolled_from_id, split_from_id, split_from_quantity,
                   share_lot_id, spread_group_id, target_pct, notes,
                   import_batch_id
share_lots         id, underlying, quantity, acquired_on, cost_per_share, fee,
                   source, assigning_position_id, estimated, notes
share_disposals    id, underlying, quantity, disposed_on, proceeds_per_share,
                   fee, kind, disposing_position_id, specific_lot_ids
share_allocations  disposal_id, lot_id, quantity, realized_pl, cost_basis,
                   proceeds                        -- derived, rebuildable
position_covers, wheel_links, spread_groups, tags, position_tags
flags              entity_type, entity_id, kind, detail, raised_at, resolved_at
audit_log          at, entity_type, entity_id, action, before, after
saved_views        id, name, filter
import_batches     id, filename, file_hash, row_count, imported_at, report
```

Money is `TEXT` holding a Decimal's digits. Dates are ISO-8601 `TEXT`.

---

## 10. Reporting

Dashboard: realized option P/L, realized share P/L, true total, capital at
risk; per ticker, adjusted basis and `min_call_strike`.

No composite unrealized P/L. Without marks it is not computable, so three
known figures sit side by side, never summed: open premium, expected P/L at
target, chain carry.

Reports: realized by month, quarter and year with running total; by ticker;
how short legs ended (hit rate, win rate, premium capture) by ticker and by
DTE. Options book on the leg's close date, shares on the sale's date, shown
apart before the total. Exports: positions.csv and shares.csv with the
computed columns, journal.json, journal.db through the backup API.

---

## 11. Milestones

| # | Deliverable | State |
| --- | --- | --- |
| ~~M1~~ | Engine, importer, reconciliation, storage | Done. Synthetic fixture reconciles exactly; a real five-year export to the cent bar one 25-cent fee typo |
| ~~M2~~ | Manual entry, positions list, validation, audit trail | Done. Stdlib web UI; split divides chain history pro-rata; every mutation audited and revertible |
| ~~M3~~ | Shares: lots, buy-write, buy and sell, wheels, true total | Done. Shares and per-ticker pages, wheel totals, true total on the dashboard |
| ~~M4~~ | Roll preview, break-even, obligation calendar, concentration | Done. Preview runs the real roll on a copy; Risk page; strike drift and size growth on every chain |
| ~~M5~~ | Reports, filters, saved views, notes and tags, export | Done. Realized by period and ticker, how short legs ended; filter bar every link keeps; CSV, JSON, SQLite export |
| ~~M6~~ | Data health, snapshots and restore, deploy notes | Done. Data page with linked fixes and dismissable import flags; snapshots via backup API; restore snapshots first; docs/deploy.md |
| M7 | Login page, options page, versioning | Planned, about a day. Salted PBKDF2 hash in `settings`; a first-run default password that must be changed; a login page with a signed cookie, logout, redirect back; an Options page holding the password change. App version constant shown in the footer and on the Data page beside the schema version; git tag per release; refuse a journal whose `user_version` is newer than the code |
| M8 | Themes | Planned, 2 to 4 hours. Palettes move under a `data-theme` attribute set by a setting on the Options page: system, light, dark first; each further palette about half an hour |
| M9 | Snapshot by API | Planned, 1 to 2 hours. A read-only token, hashed in `settings`, shown once on the Options page; `/api/snapshot` takes a snapshot and streams it; a one-line cron for another machine in docs/deploy.md |
| M10 | Split a closed leg | Candidate, 2 to 3 hours. The importer flags a roll whose child has fewer contracts than its parent; the money is right, the shape is not, and split refuses a closed leg. Allow it, with close fields pro-rata and the child's roll link repointed. Build if the fresh import shows more than a couple |

Before M7, a fix: the roll preview still uses the old `.totals` boxes. Rebuild
it as the `_facts` strip every other page uses, keeping fetch-as-you-type.

The engine came before any UI so the reconciliation could prove it. 353 tests.

---

## 12. Testing

The public suite is synthetic: a hand-computed fixture with one row of every
shape the format produces, so a clean reconciliation means the engine agrees
with independent arithmetic. Invariants: a chain total equals the sum of its
legs including split children; split quantities and fees sum to the parent's;
allocation proceeds sum to the disposal's; both basis conventions give the same
wheel total; no position closes before it opens; a disposal never exceeds what
was acquired.

A separate, unpublished suite reconciles the real export in full.

---

## 13. Out of scope

Live quotes, greeks, marks. Broker sync, multi-broker import. Multi-user,
multi-account, multi-currency. Tax lots, wash sales, 1256. Corporate actions
(edit rows). Merging chains.

---

## 14. Settled decisions

| Decision | Why |
| --- | --- |
| FIFO matching, specific-lot override per disposal | The convention is explicit and recorded |
| Buy-write shares earmarked to their own call | FIFO would reach past them to a cheaper lot |
| No composite unrealized P/L | Not computable without marks |
| Profit target 50% default, per-position override; long legs 25% on the debit | Computed, never stored |
| Import faithfully, correct in the app | Plus a permanent Data page |
| Partial events import as-is, flagged | Only chain attribution is affected |
| No dependencies | Engine, importer, storage and UI are stdlib |
| Reconstructed lots in config, not source | Real holdings never enter version control |
| Split keeps the parent as a `SPLIT` record | A journal does not delete what happened; the tombstone realizes nothing |
| Split divides carry pro-rata by quantity | Duplicating it double-counts and wrecks break-even |
| A split child links only to its split parent | One link, enforced on the type |
| Refused actions are 400 | The user's mistake, not a crash |
| Actions happen on the positions page, inline under the row | The detail page is for reading a chain |
| Close prices prefill with the profit target | Blank when the chain has nothing to aim for |
| A roll form is two labelled trades | It is two fills |
| One row renderer for positions and chains | A chain expands in place as ordinary rows under a total; no tree, no labels |
| Position columns: open price, close price, credit, closing, realized, break-even, at risk | An open leg's close price and closing cash are the target. Carry stays on the position page |
| A wheel is derived, never stored | Realized only |
| Coverage is a fact about the ticker, not a link on the call (2026-09-04) | Oldest lots cover first; assignment sells by the matching rule as the broker does. The cover-with-lot forms and unlinked-calls queue are gone |
| Adjusted basis is a ticker figure: cost held less everything paid back (2026-09-04) | Replaced the per-lot model, which left out puts that never assigned and spread old calls over unrelated lots |
| A pinned sale cannot exceed its lot | Refused at entry; a pinned sale draws only from its lot |
| Share records are removable, undoably | A mis-entered record can block matching. Refused if calls or pinned sales depend on it |
| No share record dated in the future | A lot dated after today is a recording error |
| A lot's date can move, and its assignment moves with it | Updated together, audited, undoable |
| One function, one form; one kind of list, one table | The leg grid serves every trade form; Due is a filter, not a page |
| Undo takes back a whole action or nothing | One audit group per action, undone in reverse in one transaction; a dependent later action must be undone first |
| Health checks name problems and point at the fix; they never change data | Fixing happens where the record lives |
| Snapshots through SQLite's backup API; restore snapshots first | A restore is itself undoable |
| Reports never mix scopes in a sum | Options by close date, shares by sale date; open legs appear nowhere |
| "Hit" means ended at or past the target | A roll or assignment is neither; the chain decides later |
| A filter travels with every link | A saved view is a query string under a name |
| Exports carry the computed columns | A spreadsheet gets the engine's answers |
| The roll preview is the roll itself, run on a copy | No second formula to drift; fetched from the server as typed |
| Risk is worst case by construction | Every short put assigned, every short call delivered; a naked call is flagged, not summed |
| Repairs live on a per-ticker raw-data page | Removing or re-dating is not a daily action |
| Links carry both the expanded chain and the open action | Opening a form never collapses the chain |
| Share forms are all in the page; tabs only toggle | No reload, no scroll |
| Assignment dates default to today, or the expiry once passed | Neither needs typing |
| A blocked ticker is not "short" | Matching can fail with a positive count; "short" means negative |
| Shares are written before positions in a transaction | A buy-write's call points at a lot created in the same action |
| Visual: the page's own position is tagged; actions colour-coded (blue close, purple roll, green keep, amber stock, grey split); open and closed legs look different; a chain's open legs share a rail | |
| Put risk is not shown beside capital at risk | Identical for a short put |
