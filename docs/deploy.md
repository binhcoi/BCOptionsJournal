# Running, backing up, restoring

The journal is one SQLite file. Everything here is about that file.

## From source (laptop, dev box)

```bash
python3 -m unittest discover -s tests -q
python3 -m bcoj.web --db journal.db     # http://127.0.0.1:8000/
```

Python 3.10 or newer, standard library only. Loopback by default; reach it by
forwarding the port (`ssh -L 8000:127.0.0.1:8000 host`).

## LXC from a release

Each release on GitHub carries the wheel, `bcoj.service`, and three scripts.
Debian 12 or Ubuntu 24.04 template, unprivileged, 1 vCPU, 512 MB, 4 GB disk.
The app peaks at 29 MB resident; the journal grows about 150 KB a year.

```bash
curl -fsSLO https://github.com/binhcoi/BCOptionsJournal/releases/latest/download/install.sh
sh install.sh                # or: sh install.sh 1.0.0
```

That installs Python and a venv under `/opt/bcoj`, the wheel, a `bcoj`
service user, the journal at `/srv/bcoj/journal.db`, and the systemd unit,
bound to all interfaces for the proxy below. First login: `bcoj`.

| Script | Does |
|---|---|
| `install.sh [version]` | First install. Idempotent |
| `update.sh [version]` | Snapshot to `/srv/bcoj/backups/`, install the wheel, restart. Same command moves down a version; restore the snapshot if the schema moved |
| `import.sh export.csv [estimates.json] [--force]` | Stop, reconcile, import, start. Refuses an unclean diff without `--force` |

The journal lives inside the container. A Proxmox rollback restores it too and
loses every trade entered since, so snapshot from the Options page before an
upgrade, and copy `/srv/bcoj/backups/` off the node nightly.

## Releases

Bump `__version__` in `bcoj/__init__.py`, commit, push to main. The workflow
runs the tests on 3.10 and 3.12, tags `v<version>`, builds the wheel, creates
the GitHub release with the wheel and the deploy scripts, and pushes
`ghcr.io/binhcoi/bcoj:<version>` and `:latest`. A version already tagged only
runs the tests. Pull requests and pushes to other branches run the tests only.

## Behind a reverse proxy

The app must bind an address the proxy can reach; `127.0.0.1` gives the proxy
a 502, hence `--host 0.0.0.0` in the unit above. Firewall port 8000 to the proxy node only. In Nginx Proxy Manager: scheme
`http`, the container's address, port 8000, an SSL certificate with Force SSL.
No websockets. The proxy's `X-Forwarded-Proto: https` header, which NPM sends,
makes the session cookie `Secure`.

## Password

Always required. First login is `bcoj`; the app then forces a change and
stores the hash in the journal. Change it again under Options. Forgotten:
start once with `BCOJ_PASSWORD=new-password`, which resets it. Scripts may
send the password as HTTP Basic instead of logging in:

```bash
curl -u x:PASSWORD -o journal.db http://127.0.0.1:8000/export/journal.db
```

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
docker compose -f docker/compose.yml up -d     # builds locally; tests run during the build
docker run -d -p 127.0.0.1:8000:8000 -v bcoj:/data ghcr.io/binhcoi/bcoj:latest   # or the published image
```

Journal on the `/data` volume.

## Backup

All three give a complete, consistent copy:

1. Options page, **Take a snapshot now**. Written to `backups/` beside the journal through SQLite's backup API. Optional label. Each snapshot has a **Download** link.
2. Options page, **Export**, `journal.db`. The same copy, downloaded.
3. Copy the file with the server stopped. Never while it runs: a plain copy can catch a write half way.

Keep snapshots on another disk or machine. The app never deletes them.

## Restore

Options page, **Restore** on any snapshot, behind a confirmation. The current
journal is snapshotted first as `before-restore`, then replaced and share
allocations rebuilt. To undo, restore `before-restore`.

With the server stopped:

```bash
cp /srv/bcoj/backups/journal-YYYYMMDD-HHMMSS.db /srv/bcoj/journal.db
```
