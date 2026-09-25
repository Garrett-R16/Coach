#!/usr/bin/env bash
# Create a new athlete data directory from the templates and make it a git repo.
# usage: scripts/init-data.sh ~/Projects/coach-data
set -euo pipefail
DEST="${1:?usage: init-data.sh <directory>}"
SRC="$(cd "$(dirname "$0")/.." && pwd)/templates"
[ -e "$DEST/context/profile.md" ] && { echo "$DEST already has a profile; refusing to overwrite"; exit 1; }
mkdir -p "$DEST"
cp -rn "$SRC"/. "$DEST"/
mkdir -p "$DEST"/log "$DEST"/data/daily "$DEST"/data/workouts "$DEST"/data/health/raw "$DEST"/inbox
if [ ! -d "$DEST/.git" ]; then
  git -C "$DEST" init -q
  git -C "$DEST" add -A
  git -C "$DEST" -c user.name="coach" -c user.email="coach@localhost" commit -q -m "Athlete data directory from templates"
fi
mkdir -p ~/.config/coach
grep -q '^COACH_DATA=' ~/.config/coach/runner.env 2>/dev/null && sed -i "s|^COACH_DATA=.*|COACH_DATA=$DEST|" ~/.config/coach/runner.env || echo "COACH_DATA=$DEST" >> ~/.config/coach/runner.env
echo "Data directory ready at $DEST and set in ~/.config/coach/runner.env."
echo "Fill in $DEST/context/profile.md and schedule.md (or tell the coach in chat), then: systemctl --user restart coach-runner"
