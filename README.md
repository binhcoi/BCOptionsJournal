# BC Options Trade Journal

A local, single-user options journal. Correct P/L across rolls, assignments,
covered calls and shares. One SQLite file, Python standard library only.

- [plan.md](plan.md): design and decisions
- [docs/legacy-format.md](docs/legacy-format.md): the spreadsheet the importer reads
- [docs/deploy.md](docs/deploy.md): running, backup, restore

## Quickstart

Python 3.10 or newer. Nothing to install.

```bash
python3 -m unittest discover -s tests -q
python3 -m bcoj.web --db journal.db        # http://127.0.0.1:8000/
```

Import a legacy export:

```bash
python3 -m bcoj.cli reconcile data/export.csv              # recompute and diff
python3 -m bcoj.cli flags     data/export.csv              # rows needing a human
python3 -m bcoj.cli import    data/export.csv --db journal.db
```

`import` refuses to write if the diff disagrees with the sheet; `--force`
overrides. Re-importing the same file is a no-op. A batch reverts whole.

## Commands

| Command | Does |
| --- | --- |
| `reconcile` | Recomputes eight derived figures per row and diffs them against the sheet |
| `import` | Reconciles, then writes positions, share lots, disposals and flags |
| `flags` | Rows needing a human: disguised share rows, date errors, flattened partials |
| `panel` | Reproduces the sheet's summary panel, bugs included, and names them |
| `shares` | Lots, disposals and realized P/L per ticker |
| `chains` | Roll chains with carry, net credit, break-even and target |
| `serve` | The web UI; same as `python3 -m bcoj.web` |

## Layout

```
bcoj/
  domain/    money, enums, types            pure values
  engine/    pnl, chains, targets, risk,
             shares, basis, health, ...     pure calculation
  db/        schema, store                  stdlib sqlite3
  importer/  legacy_csv, reconcile, triage,
             shares, estimates
  web/       server, routes, render, static stdlib http.server
  cli.py
tests/       synthetic fixtures only
```

`domain/` and `engine/` never import `db/` or `importer/`. Only entered values
are stored; every figure is computed on read. Money is `Decimal`, stored as
`TEXT`.

## Privacy

No real trade data is in this repository. `private/` is gitignored whole;
`*.db` and `*.csv` are ignored everywhere except `tests/fixtures/`.
Reconstructed share lots live in a config file (see
`bcoj/importer/estimates.py`), never in source.

## License

MIT
