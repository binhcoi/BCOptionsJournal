# Deployment and operations

The application stores its journal, settings, password hash, and audit trail
in one SQLite database. Keep that data file separate from application code.

For the basic source checkout and startup command, see the
[README](../README.md#run-locally).

## Network access

The source server binds to `127.0.0.1:8000` by default. For remote access
through an SSH tunnel:

```bash
ssh -L 8000:127.0.0.1:8000 user@example-host
```

To listen on network interfaces directly:

```bash
python3 -m bcoj.web --db journal.db --host 0.0.0.0 --port 8000
```

For access over an untrusted network, use an HTTPS reverse proxy and restrict
the application port to the proxy. A proxy on another host or in another
container needs an address it can reach; a proxy on the same host can use
loopback. Forward `X-Forwarded-Proto: https` so the app marks session cookies
`Secure`. WebSocket support is not required.

## Release installation with systemd

[GitHub releases](https://github.com/binhcoi/BCOptionsJournal/releases) contain
a Python wheel, a systemd unit, and install, update, and import scripts.

Run the scripts as root on a Debian or Ubuntu host with `sudo` available.
Review the downloaded script before running it:

```bash
curl -fsSLO https://github.com/binhcoi/BCOptionsJournal/releases/latest/download/install.sh
sh install.sh
```

The installer creates a `bcoj` service account, installs the wheel into
`/opt/bcoj/venv`, and stores the journal at `/srv/bcoj/journal.db`. The
[packaged systemd unit](../deploy/bcoj.service) binds to all interfaces for
network or reverse-proxy access.

| Script | Purpose |
| --- | --- |
| [install.sh](../deploy/install.sh) `[version]` | Install a specified release, or the latest release by default. |
| [update.sh](../deploy/update.sh) `[version]` | Stop the service, snapshot the journal, install the wheel, and restart. |
| [import.sh](../deploy/import.sh) `export.csv [estimates.json] [--force]` | Stop the service, import a legacy export, then restart. See the [import reference](legacy-format.md). |

Service management:

```bash
systemctl status bcoj
journalctl -u bcoj
```

For an LXC deployment, keep `/srv/bcoj` on a separate volume or bind mount so
rolling back the container's root filesystem does not also roll back the
journal. Keep independent backups outside the container host.

## Docker

Build from the checkout:

```bash
docker compose -f docker/compose.yml up -d
```

The [Compose configuration](../docker/compose.yml) publishes port 8000 to
host loopback and stores the journal in `docker/data/`. The bind-mounted data
directory must be writable by the container's user, UID 1000.

Alternatively, use the published image with a named volume:

```bash
docker run -d --name bcoj -p 127.0.0.1:8000:8000 -v bcoj:/data ghcr.io/binhcoi/bcoj:latest
```

The journal is `/data/journal.db` inside the container. The
[Dockerfile](../docker/Dockerfile) runs the test suite during image build and
starts the app as a non-root user.

## Authentication and settings

The initial password is `bcoj`. First login requires choosing a replacement.
Change it again on the Options page; changing the password signs out existing
sessions. The same page holds the theme, snapshots, and exports.

To reset a forgotten password, stop the current server and start it once with
`BCOJ_PASSWORD` set:

```bash
BCOJ_PASSWORD='replace-with-new-password' python3 -m bcoj.web --db journal.db
```

For a service or container, supply that variable to its process for one
startup, then remove it from the service or container configuration so later
restarts do not reset the password again.

Scripts can use HTTP Basic. This command prompts for the password instead of
including it in the command line:

```bash
curl --user bcoj --output journal-backup.db http://127.0.0.1:8000/export/journal.db
```

## Backup and restore

Use either **Take a snapshot now** or the `journal.db` export on the Options
page. Both use SQLite's backup API to create a consistent copy while the app
is running. Snapshots are stored in `backups/` beside the database and can be
downloaded from that page.

Do not copy only the live `.db` file while the server is writing: SQLite uses
a write-ahead log. Use the snapshot or export operation instead. Keep copies
on another disk or machine; the app does not automatically delete old
snapshots.

To restore, choose a snapshot on the Options page and confirm. The current
journal is first saved as a `before-restore` snapshot. The selected snapshot
is then restored and share allocations are rebuilt. To reverse the restore,
restore the saved `before-restore` snapshot. Files from a newer schema version
are refused.

An update script also takes a snapshot before installing a release. A
package downgrade does not downgrade the schema; a compatible earlier
snapshot may be needed.

## Release process

The version is defined in [bcoj/__init__.py](../bcoj/__init__.py). The
[CI workflow](../.github/workflows/ci.yml) tests pushes and pull requests on
Python 3.10 and 3.12. A push to `main` with a version that has no existing tag
creates `v<version>`, builds the wheel, publishes the GitHub release and
scripts, and pushes `ghcr.io/binhcoi/bcoj:<version>` and `:latest`.

An already tagged version runs tests without publishing another release.
