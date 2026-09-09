# Running, backing up, restoring

The journal is one SQLite file. Everything here is about that file.

## Bare (LXC, VM, laptop)

```bash
python3 -m unittest discover -s tests -q
python3 -m bcoj.web --db /srv/bcoj/journal.db     # http://127.0.0.1:8000/
```

Python 3.10 or newer, standard library only. Reach it from another machine by
forwarding the port (`ssh -L 8000:127.0.0.1:8000 host`), not by binding a
public address. If you must bind wider, set `BCOJ_PASSWORD`.

### LXC sizing

One process, one file, nothing running between requests. A four-year journal
(650 KB) peaks at 29 MB resident; a page renders in 10 to 25 ms.

| Resource | Size | Note |
|---|---|---|
| CPU | 1 vCPU | |
| RAM | 256 MB | 512 MB if the container also runs `unattended-upgrades` or a working shell |
| Root disk | 2 GB | OS plus Python; the code is 1 MB |
| Data disk | 1 GB at `/srv/bcoj` | Journal grows about 150 KB a year; a year of daily snapshots is under 300 MB |
| Swap | none | |

Unprivileged, no nesting, no devices.

Keep `/srv/bcoj` on a bind mount or a separate volume, outside the container's
root. Rolling the container back after a bad upgrade must not roll the journal
back with it. Container snapshots are crash-consistent for SQLite (the journal
and its WAL share a directory), but they sit on the same disk as the container,
so also copy the app's `backups/` folder off the host nightly.

systemd unit:

```ini
[Unit]
Description=BC Options Journal
After=network.target

[Service]
User=bcoj
WorkingDirectory=/opt/bcoj
ExecStart=/usr/bin/python3 -m bcoj.web --db /srv/bcoj/journal.db
Restart=on-failure
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

## Docker

```bash
docker compose -f docker/compose.yml up -d     # tests run during the build
```

Journal on the `./docker/data` volume; port published to loopback only.

## Backup

All three give a complete, consistent copy:

1. Data page, **Take a snapshot now**. Written to `backups/` beside the journal through SQLite's backup API. Optional label.
2. Data page, **Export**, `journal.db`. The same copy, downloaded.
3. Copy the file with the server stopped. Never while it runs: a plain copy can catch a write half way.

Keep snapshots on another disk or machine. The app never deletes them.

## Restore

Data page, **Restore** on any snapshot, behind a confirmation. The current
journal is snapshotted first as `before-restore`, then replaced and share
allocations rebuilt. To undo, restore `before-restore`.

With the server stopped:

```bash
cp /srv/bcoj/backups/journal-YYYYMMDD-HHMMSS.db /srv/bcoj/journal.db
```

## Upgrade

Pull, run the tests, restart. Migrations run on start, each in its own
transaction. Snapshot first.
