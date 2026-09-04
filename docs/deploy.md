# Running, backing up, restoring

The journal is one SQLite file. Everything below is about keeping that file
safe and serving the app in front of it.

## Bare (LXC, a VM, a laptop)

```bash
python3 -m unittest discover -s tests -q      # proves the engine before you trust it
python3 -m bcoj.web --db /srv/bcoj/journal.db  # http://127.0.0.1:8000/
```

Python 3.10 or newer, standard library only. Nothing to install. To reach it
from another machine, forward the port (`ssh -L 8000:127.0.0.1:8000 host`, or an
editor's port forwarding) rather than binding a public address. If you must
bind wider, set `BCOJ_PASSWORD`.

A systemd unit for an LXC container:

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

The journal lives on the `./docker/data` volume; the port is published to
loopback only.

## Backups

Three ways, all producing a complete, consistent copy:

1. **Data page, "Take a snapshot now".** Written to a `backups/` folder next
   to the journal through SQLite's backup API, so it is consistent even if a
   write is in flight. Optional label.
2. **Export, `journal.db`.** The same copy, downloaded to the browser.
3. **Copy the file** while the server is stopped. With the server running,
   prefer 1 or 2: a plain copy can catch a write half way.

Snapshots are files; keep them somewhere the journal is not (another disk,
another machine). Nothing in the app deletes them.

## Restore

On the Data page, each snapshot has a Restore button behind a confirmation.
Restoring snapshots the journal as it is first (labelled `before-restore`),
then copies the chosen snapshot over the live journal and rebuilds the share
allocations. Undo a restore by restoring the `before-restore` snapshot.

Or, with the server stopped:

```bash
cp /srv/bcoj/backups/journal-YYYYMMDD-HHMMSS.db /srv/bcoj/journal.db
```

## Upgrading

Pull the new code, run the tests, restart the server. Schema migrations run on
start and each in its own transaction; a snapshot beforehand costs nothing.
