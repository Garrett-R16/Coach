#!/usr/bin/env bash
# Give the coach runs their own long-lived Claude login so they stop depending on the interactive
# session's token (which rotates on refresh and expires when two clients share it).
# Step 1 runs `claude setup-token` in plain view: over SSH it prints a URL to open on any device
# and asks for the code it gives you, then prints the token. Step 2 asks you to paste that token
# (not echoed) and stores it in ~/.config/coach/runner.env (mode 600), then restarts the runner.
set -euo pipefail
ENVF="$HOME/.config/coach/runner.env"
mkdir -p "$(dirname "$ENVF")"
echo "== Claude login. Open the URL it prints on any device, approve, paste the code back here."
echo "   The token it prints at the end is captured automatically."
CAP="$(mktemp)"; trap 'rm -f "$CAP"' EXIT
claude setup-token 2>&1 | tee "$CAP" || true
TOKEN="$(tr -d '\r\n ' < "$CAP" | grep -oE 'sk-ant-oat[A-Za-z0-9_-]+' | tail -1 || true)"
if [ -z "$TOKEN" ]; then
  echo; echo "== Could not capture it. Paste the token (starts with sk-ant-oat); input is hidden."
  read -r -s -p "token: " TOKEN; echo
fi
TOKEN="${TOKEN//[[:space:]]/}"
case "$TOKEN" in sk-ant-oat*) ;; *) echo "that does not look like a setup token; nothing changed"; exit 1;; esac
touch "$ENVF"; chmod 600 "$ENVF"
grep -v '^CLAUDE_CODE_OAUTH_TOKEN=' "$ENVF" > "$ENVF.tmp" || true
echo "CLAUDE_CODE_OAUTH_TOKEN=$TOKEN" >> "$ENVF.tmp"
mv "$ENVF.tmp" "$ENVF"; chmod 600 "$ENVF"
systemctl --user restart coach-runner.service
echo "Stored (${#TOKEN} chars) and runner restarted. Test: python3 -m runner.triggers chat 'auth check' --no-send"
