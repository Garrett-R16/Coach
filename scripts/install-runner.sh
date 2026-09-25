#!/usr/bin/env bash
# Run as yourself (no sudo). Installs the runner service and morning timer as user units.
set -euo pipefail
cd "$(dirname "$0")/.."
[ -d /var/lib/coach/queue ] || { echo "run 'sudo scripts/install-broker.sh' first"; exit 1; }
mkdir -p ~/.config/systemd/user
for u in systemd/user/*; do ln -sf "$PWD/$u" ~/.config/systemd/user/; done
systemctl --user daemon-reload
systemctl --user enable --now coach-runner.service coach-morning.timer
systemctl --user restart coach-runner.service
loginctl enable-linger "$USER" || echo "enable-linger failed; the runner will stop when you log out"
systemctl --user --no-pager status coach-runner.service coach-morning.timer | head -30
