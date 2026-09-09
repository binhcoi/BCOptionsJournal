#!/bin/sh
# Move an installed LXC to another release. Run as root.
#   sh update.sh             latest release
#   sh update.sh 1.1.0       a specific version, up or down
# Takes a snapshot first; on a downgrade past a schema change, restore it.
set -eu
REPO=binhcoi/BCOptionsJournal
VERSION=${1:-latest}
PIP=/opt/bcoj/venv/bin/pip

if [ "$VERSION" = latest ]; then
  VERSION=$(curl -fsSL "https://api.github.com/repos/$REPO/releases/latest" \
            | sed -n 's/.*"tag_name": *"v\([^"]*\)".*/\1/p')
fi
current=$($PIP show bcoj 2>/dev/null | sed -n 's/^Version: //p')
[ "$current" = "$VERSION" ] && { echo "already on $VERSION"; exit 0; }

systemctl stop bcoj
install -d -o bcoj -g bcoj /srv/bcoj/backups
sudo -u bcoj python3 - <<PY
import sqlite3, datetime
src = sqlite3.connect("/srv/bcoj/journal.db")
name = "/srv/bcoj/backups/journal-%s-before-update.db" % datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
dst = sqlite3.connect(name); src.backup(dst); dst.close(); src.close()
print("snapshot", name)
PY
sudo -u bcoj $PIP install -q \
  "https://github.com/$REPO/releases/download/v$VERSION/bcoj-$VERSION-py3-none-any.whl"
systemctl start bcoj
echo "bcoj $current -> $VERSION"
