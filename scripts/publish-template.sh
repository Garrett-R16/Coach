#!/usr/bin/env bash
# Export a clean copy of the harness (tracked files only, no history) for publishing as a template.
# usage: scripts/publish-template.sh <empty or new directory>
set -euo pipefail
DEST="${1:?usage: publish-template.sh <directory>}"
SRC="$(cd "$(dirname "$0")/.." && pwd)"
[ "$(realpath -m "$DEST")" = "$SRC" ] && { echo "refusing to export onto the source repo"; exit 1; }
[ -e "$DEST/.git" ] && { echo "$DEST already has a .git; use a fresh directory"; exit 1; }
mkdir -p "$DEST"
git -C "$SRC" ls-files -z | tar --null -C "$SRC" -T - -cf - | tar -C "$DEST" -xf -
echo "Exported $(git -C "$SRC" ls-files | wc -l) files to $DEST"
