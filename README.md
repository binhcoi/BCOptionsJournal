# BC Options Trade Journal

A personal, local options journal. Correct P/L across rolls, assignments,
covered calls **and shares** — the last of which the spreadsheet it replaces
never computed at all.

Offline, single user, one SQLite file. No dependencies below the UI.

See [plan.md](plan.md) for the design and [docs/legacy-format.md](docs/legacy-format.md)
for the import format.

> **Status: complete through M6.** Engine, importer and reconciliation, entry
> UI, shares and wheels, decision support, reporting with filters and saved
> views, and a Data page with health checks, snapshots and restore. See
> [docs/deploy.md](docs/deploy.md) to run it and keep the journal safe.

---

## Why

A spreadsheet can track option premium well enough. What it cannot do:

- **Compute share P/L.** Covered calls sit as unlinked rows and the shares
  behind them have no home, so a wheel — put assigned, dozens of calls, shares
  called away — has no total and no duration.
- **Give a trustworthy headline.** Adding share cash flow to option P/L is
  correct only while the underlying is flat, and nothing tells you whether it
  is. Anything still held reads as a loss.
- **Show a roll chain.** A dozen legs over several years, scattered by date,
  with no view of the strike walking down as the size grows.
- **Tell you break-even.** The one number a rolled position most needs: the
  price below which, however long you have been rolling, the whole chain is a
  net loss.
- **Match share lots.** FIFO and LIFO can differ by tens of thousands on a
  single disposal, and leave a different lot behind. An unstated convention is a
  wrong answer waiting to happen.

---

## Quickstart

Python 3.10+ and the standard library. Nothing to install.

```bash
python3 -m unittest discover -s tests -q          # 221 tests
python3 -m bcoj.web --db journal.db               # then open the URL it prints
```

The UI binds to loopback and needs no install. Set `BCOJ_PASSWORD` to require
a password; forms carry CSRF tokens, because "localhost" is not a security
boundary — any page in the browser can POST to it.

Point it at your own export (see [docs/legacy-format.md](docs/legacy-format.md)):

```bash
mkdir -p data && cp /path/to/export.csv data/

python3 -m bcoj.cli reconcile data/export.csv          # recompute and diff
python3 -m bcoj.cli flags     data/export.csv          # what needs a human
python3 -m bcoj.cli panel     data/export.csv --symbol TICKER
python3 -m bcoj.cli shares    data/export.csv
python3 -m bcoj.cli chains    data/export.csv --top 5
python3 -m bcoj.cli import    data/export.csv --db journal.db
```

`import` refuses to write if anything disagrees with the sheet; `--force`
overrides. Re-importing the same file is a no-op, and any batch is revertible
whole.

### Docker

```bash
docker compose -f docker/compose.yml build     # runs the tests during build
docker compose -f docker/compose.yml run --rm bcoj reconcile /import/export.csv
```

### LXC / bare

Copy the repo, ensure `python3` is present, run the commands above. The journal
is one SQLite file; the Data page takes consistent snapshots of it and can
restore one. Details in [docs/deploy.md](docs/deploy.md).

---

## Commands

| Command | What it does |
| --- | --- |
| `reconcile` | Recomputes eight derived figures per row and diffs each against the sheet's own columns |
| `import` | Reconciles, then persists positions, share lots, disposals and flags |
| `panel` | Reproduces the sheet's summary panel, bugs included, and names them |
| `shares` | Share lots, disposals and realized P/L per ticker |
| `flags` | Rows needing a human: disguised share rows, date errors, flattened partials |
| `chains` | Roll chains with carry, net credit, break-even and the profit target |
| `serve` | Run the web UI (same as `python3 -m bcoj.web`) |

---

## How it's built

```
bcoj/
  domain/     money, enums, types              — pure values, no I/O
  engine/     pnl, chains, targets, risk,
              shares, basis                    — pure calculation
  db/         schema, store                    — stdlib sqlite3, no ORM
  importer/   legacy_csv, reconcile, triage,
              shares, legacy_formulas, estimates
  web/        server, routes, render, static  — stdlib http.server, no deps
  cli.py
```

`domain/` and `engine/` never import `db/` or `importer/`. The engine takes
positions and returns numbers, which is what makes row-by-row reconciliation
against years of hand-kept records possible at all.

**Only entered values are stored.** Cash flows, P/L, chain carry, profit
targets, break-even, capital at risk and adjusted basis are computed on read, so
correcting a rule fixes every historical row with nothing to migrate.

**Money is `Decimal` everywhere**, stored as `TEXT`. Never `float`, and never a
`REAL` column — SQLite would accept one and silently corrupt the accounting.

---

## Privacy

This repository contains **no real trade data**. Test fixtures are synthetic and
hand-computed.

If you use it with your own records, they stay out of version control:

- `/private/` is gitignored wholesale — put your export, journal and notes there
- `*.db` and `*.csv` are ignored anywhere else, with test fixtures the only
  explicit exception
- Reconstructed share lots live in a config file (`bcoj/importer/estimates.py`
  describes the format), never in source

Check with `git status --ignored` before your first push.

---

## Known gaps

- **Share pages and reporting.** M3-M5. Assignment already creates and
  disposes share lots correctly; there is just no page to browse them yet.
- **Reconstructed lots are approximations.** A ticker whose recorded
  acquisitions fall short of its disposals stays blocked until a lot is
  configured; the lot is flagged wherever it contributes.
- **Flattened partials import as-is** and are flagged. Where part of a position
  was assigned and the rest rolled, a sheet reshapes the rows by hand and the
  detail is gone. Only chain attribution is affected, not totals.

## License

MIT
