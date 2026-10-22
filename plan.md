# Design

BC Options Trade Journal is a single-user journal for options and shares.
Its central requirement is consistent accounting across rolls, partial
assignments, covered calls, and share disposals.

This document describes the model and design constraints. See the
[README](README.md) for the product overview, [deployment guide](docs/deploy.md)
for operations, and [import reference](docs/legacy-format.md) for the legacy
CSV format and command-line workflow.

## Architecture

| Layer | Responsibility | Source |
| --- | --- | --- |
| Domain | Typed records, enums, and monetary values | [bcoj/domain](bcoj/domain) |
| Engine | Cash flows, chains, share matching, targets, risk, and reports | [bcoj/engine](bcoj/engine) |
| Storage | SQLite schema, migrations, transactions, and audit history | [bcoj/db](bcoj/db) |
| Importer | CSV parsing, reconciliation, and data-quality flags | [bcoj/importer](bcoj/importer) |
| Web | HTTP routing, HTML rendering, CSS, and progressive JavaScript | [bcoj/web](bcoj/web) |
| CLI | Import, inspection, and server entry points | [bcoj/cli.py](bcoj/cli.py) |

The domain and engine do not depend on the database, web server, or importer.
The CLI and web layer call the same calculations. Trade actions return the
records to create or update; the storage layer persists them.

### Technology decisions

- **Python standard library.** `Decimal`, `sqlite3`, `csv`, and `http.server`
  cover the requirements without third-party runtime dependencies. This is
  intended for one journal owner, rather than a multi-tenant service.
- **SQLite.** One file contains the journal, settings, and audit trail.
  Foreign keys and constraints protect relationships; migrations advance
  `PRAGMA user_version` transactionally.
- **Explicit monetary precision.** Money uses `Decimal`, stored as `TEXT`.
  Prices, fees, and accounting outputs are rounded explicitly. Dates use
  ISO-8601 `TEXT`.
- **Derived reporting.** Cash flows, P/L, chain carry, targets, and adjusted
  basis are calculated from entered facts. Share allocations are a derived,
  rebuildable table rather than an independent source of truth.
- **Progressive enhancement.** The server renders complete pages and accepts
  ordinary form submissions. JavaScript adds inline panels, prefetched
  fragments, live previews, and submissions that retain the entry form.

The authoritative schema is [bcoj/db/schema.py](bcoj/db/schema.py); field lists
and SQL definitions are maintained there.

## Accounting model

### Positions and cash flow

A position records a contract, direction, positive quantity, opening fill,
fees, dates, and status. It can be open, closed, expired, rolled, assigned, or
superseded by a split. Roll and split links identify its predecessor; a
position cannot have both kinds of parent.

For arithmetic, quantity is signed: positive for short, negative for long.

```text
open_cash  =  signed_quantity * multiplier * open_price - open_fee
close_cash = -signed_quantity * multiplier * close_price - close_fee
realized   = open_cash + close_cash
```

An open position has no realized P/L. A split parent contributes no further
P/L because its children represent the continuing position. Fees are included
in cash flows, with defaults that remain editable at entry.

### Chains, rolls, and splits

A chain follows the predecessor links. Its carry is realized P/L inherited
from earlier legs, distinct from the current leg's opening premium:

```text
carry(next_leg) = realized(previous_leg) + carry(previous_leg)
net_credit     = carry(current_leg) + open_cash(current_leg)
```

A roll closes a leg and opens its successor as one action. Quantity can
change explicitly. The preview runs that same action on copies of the input
records, then reads the resulting chain; it has no separate accounting
formula.

A split retains its parent as a `SPLIT` record and creates two children.
Quantities and opening fees sum to the parent's values. Each child inherits
carry in proportion to its quantity through
[`engine.chains.inherit`](bcoj/engine/chains.py). Chain links are checked for
cycles. The UI presents related legs as ordinary position rows, with the
chain totals kept separate from individual-leg results.

### Shares, coverage, and wheel totals

The share ledger has acquisitions, disposals, and allocations linking each
disposal to the lots it consumed. Acquisitions can come from an outright buy,
a buy-write, put assignment, or long-call exercise. A disposal is a sale or
shares called away.

FIFO is the default matching rule. The engine also supports LIFO and explicit
lot selection on a disposal. A pinned sale can consume only its selected
lots. Shares cannot be sold before acquisition or beyond the available
quantity. Import inconsistencies remain visible as blocked matching rather
than producing a result from an invalid balance.

Coverage is derived at ticker level from the shares held and open short
calls, allocated across lots oldest first. It does not depend on a manually
maintained call-to-lot link. Excess calls are flagged as uncovered.

**Cost basis** is the cost of shares still held, including acquisition fees
pro-rata. Unit price is the original per-share fill. Option P/L and realized
share P/L are separate; the ticker's wheel total is their lifetime sum.

### Adjusted basis and targets

Adjusted basis is a ticker-level, realized-only measure:

```text
adjusted_basis = (cost_basis - realized_option_pl - realized_share_pl) / shares_held
```

It is undefined when no shares remain or share matching is blocked. A
negative value means realized returns exceed the cost of the remaining
shares. Dividends are outside the model.

For a short option, the default closing target captures 50% of the chain's
available credit. An underwater chain has no positive credit target. Long
options instead target a 25% increase in opening price by default. Both
percentages can be overridden per position.

Break-even is calculated for a short chain from its current strike and net
credit per share: subtract the credit for a put, add it for a call. Figures
without a valid denominator are withheld.

## Integrity and correction

Related position and share changes are stored together, with grouped audit
entries. Undo reverses a whole action or refuses it when a later action
depends on the records. Redo reapplies an undone action. Neither operation
removes the audit history.

Entry validation checks dates, quantities, prices, fees, share availability,
and duplicate records. Refused entries return HTTP 400 with an explanation.
Corrections to share records live on the ticker's raw-data page; health
checks identify problems and link to their records without modifying them.

Authentication uses a single password, a PBKDF2 hash, signed session cookies,
and CSRF tokens on forms. Changing the password rotates the session secret.
HTTP Basic is available for scripts. Configuration and recovery instructions
belong in the [deployment guide](docs/deploy.md).

Snapshots and database exports use SQLite's backup API. Restore first takes
a snapshot of the current journal, then replaces it and rebuilds share
allocations. A snapshot with a newer schema is refused.

## Reporting boundaries

- **Period reports** attribute option P/L to the close date and share P/L to
  the disposal date. Chain totals and ticker lifetime totals are different
  scopes and are not added to period totals.
- **Open-position figures** distinguish premium received, expected P/L at a
  target, and inherited carry. There is no composite unrealized P/L without
  market prices.
- **Risk** groups short-put assignment obligations by expiry and shows
  concentration by ticker. Covered-call exposure is represented by its
  shares; uncovered calls are flagged rather than assigned a finite risk.
- **Performance reports** group closed results by period and ticker, and
  examine short-option outcomes and target performance.

Exports include entered records and supported computed columns in CSV,
JSON, and SQLite formats.

## Verification

The public tests use synthetic, independently calculated examples.
Accounting invariants include conservation of quantities and fees through a
split, proportional inheritance of carry, matching allocation proceeds to a
disposal, and consistent wheel totals under both basis conventions.

Storage tests cover migrations, auditing, undo, and restore. HTTP tests run
a real local server against a temporary database to exercise routing,
authentication, form parsing, CSRF validation, and persistence together. Test
commands are in the [README](README.md#verification).

## Scope and planned work

The implemented workflow includes manual entry, inline close/roll/assignment
and split actions, shares, chain views, risk and performance reports, filters,
saved views, notes and tags, data health, snapshots, exports, and themes.

Planned: a read-only token for retrieving a consistent snapshot through an
API, with token management on the Options page. This endpoint is not yet
implemented.

Live quotes, broker synchronization, multi-user and multi-account operation,
multi-currency accounting, dividends, tax reporting, automated corporate
actions, and chain merging are outside the current scope. Spread metadata
exists in the schema; grouped spread entry, rolling, and risk calculation
are not implemented.
