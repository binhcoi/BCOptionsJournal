#!/bin/sh
# First install on a fresh Debian or Ubuntu LXC. Run as root.
#   sh install.sh            latest release
#   sh install.sh 1.0.0      a specific version
set -eu
REPO=binhcoi/BCOptionsJournal
VERSION=${1:-latest}

apt-get update -q && apt-get install -y -q python3 python3-venv curl
id bcoj >/dev/null 2>&1 || useradd --system --home /opt/bcoj --create-home bcoj
mkdir -p /srv/bcoj && chown bcoj:bcoj /srv/bcoj
[ -d /opt/bcoj/venv ] || sudo -u bcoj python3 -m venv /opt/bcoj/venv

if [ "$VERSION" = latest ]; then
  VERSION=$(curl -fsSL "https://api.github.com/repos/$REPO/releases/latest" \
            | sed -n 's/.*"tag_name": *"v\([^"]*\)".*/\1/p')
fi
WHEEL="https://github.com/$REPO/releases/download/v$VERSION/bcoj-$VERSION-py3-none-any.whl"
sudo -u bcoj /opt/bcoj/venv/bin/pip install -q "$WHEEL"

curl -fsSL "https://github.com/$REPO/releases/download/v$VERSION/bcoj.service" \
  -o /etc/systemd/system/bcoj.service
systemctl daemon-reload
systemctl enable --now bcoj
echo "bcoj $VERSION running on port 8000. First login: bcoj"
