#!/bin/sh
# Import the legacy export into the live journal. Run as root.
#   sh import.sh export.csv [estimates.json] [--force]
# Reconciles first and stops if the diff is not clean; --force imports anyway.
set -eu
CSV=${1:?usage: import.sh export.csv [estimates.json] [--force]}
BCOJ=/opt/bcoj/venv/bin/bcoj
DB=/srv/bcoj/journal.db
EST=""; FORCE=""; shift
for arg in "$@"; do
  case "$arg" in --force) FORCE=--force ;; *) EST=$arg ;; esac
done

work=$(mktemp -d); trap 'rm -rf "$work"' EXIT
cp "$CSV" "$work/export.csv"
[ -n "$EST" ] && cp "$EST" "$work/estimates.json"
chown -R bcoj:bcoj "$work"
env=""; flag=""
[ -n "$EST" ] && { env="BCOJ_ESTIMATES=$work/estimates.json"; flag="--estimates"; }

systemctl stop bcoj
trap 'systemctl start bcoj; rm -rf "$work"' EXIT
sudo -u bcoj env $env $BCOJ reconcile "$work/export.csv"
sudo -u bcoj env $env $BCOJ import "$work/export.csv" --db "$DB" $flag $FORCE
