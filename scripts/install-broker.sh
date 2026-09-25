#!/usr/bin/env bash
# Run with sudo. Creates the coach-svc user, deploys broker code to /opt/coach,
# moves Signal registration data out of your home, and starts the system service.
# Re-run after any broker/common code change: it re-copies and restarts.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
REPO="$(cd "$(dirname "$0")/.." && pwd)"
OWNER="${SUDO_USER:?run via sudo from your own user}"
SVC=coach-svc

id -u $SVC >/dev/null 2>&1 || useradd --system --home-dir /var/lib/coach --create-home --shell /usr/sbin/nologin $SVC
install -d -o $SVC -g $SVC -m 750 /var/lib/coach /var/lib/coach/state /var/lib/coach/signal
install -d -o $SVC -g $SVC -m 770 /var/lib/coach/queue /var/lib/coach/queue/inbox /var/lib/coach/queue/outbox /var/lib/coach/queue/done
# the runner (your user) reads/writes the queue; ACLs so it works without re-login
setfacl -R -m u:$OWNER:rwx -m d:u:$OWNER:rwx -m d:u:$SVC:rwx /var/lib/coach/queue
setfacl -m u:$OWNER:x /var/lib/coach

# code: copy, never symlink, so the broker runs what you deployed, not the working tree
install -d -m 755 /opt/coach /opt/coach/scripts
rm -rf /opt/coach/broker /opt/coach/common
cp -r "$REPO/broker" "$REPO/common" /opt/coach/
install -m 755 "$REPO/scripts/with-secrets.sh" /opt/coach/scripts/with-secrets.sh
find /opt/coach -name __pycache__ -prune -exec rm -rf {} +

# python deps for the broker (garminconnect) in a venv it can read but not write
[ -x /opt/coach/venv/bin/python3 ] || python3 -m venv /opt/coach/venv
/opt/coach/venv/bin/pip install -q --upgrade pip >/dev/null
/opt/coach/venv/bin/pip install -q -r /opt/coach/broker/requirements.txt
install -d -o $SVC -g $SVC -m 700 /var/lib/coach/garmin

# signal-cli binary and any registration done from your user
if [ ! -x /usr/local/bin/signal-cli ] && [ -x "/home/$OWNER/.local/opt/signal-cli" ]; then
  install -m 755 "/home/$OWNER/.local/opt/signal-cli" /usr/local/bin/signal-cli
fi
if [ -d "/home/$OWNER/.local/share/signal-cli/data" ] && [ ! -d /var/lib/coach/signal/data ]; then
  mv "/home/$OWNER/.local/share/signal-cli/data" /var/lib/coach/signal/
  chown -R $SVC:$SVC /var/lib/coach/signal
  echo "moved Signal registration from your home to /var/lib/coach/signal"
fi

# config
install -d -m 750 -o root -g $SVC /etc/coach
if [ ! -f /etc/coach/broker.env ]; then
  PID="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["workspaceId"])' "$REPO/.infisical.json")"
  cat > /etc/coach/broker.env <<CFG
INFISICAL_PROJECT_ID=$PID
INFISICAL_ENV=dev
COACH_QUEUE=/var/lib/coach/queue
COACH_STATE=/var/lib/coach/state
SIGNAL_CONFIG=/var/lib/coach/signal
HEALTH_WEBHOOK_PORT=8787
GARMIN_TOKENS=/var/lib/coach/garmin
CFG
  chmod 640 /etc/coach/broker.env; chown root:$SVC /etc/coach/broker.env
fi
grep -q '^GARMIN_TOKENS=' /etc/coach/broker.env || echo 'GARMIN_TOKENS=/var/lib/coach/garmin' >> /etc/coach/broker.env
if [ ! -f /etc/coach/infisical.env ]; then
  cat > /etc/coach/infisical.env <<CFG
INFISICAL_UNIVERSAL_AUTH_CLIENT_ID=
INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET=
CFG
  chmod 640 /etc/coach/infisical.env; chown root:$SVC /etc/coach/infisical.env
  echo "*** fill in /etc/coach/infisical.env (machine identity), then re-run this script"
  exit 0
fi

install -m 644 "$REPO/systemd/system/coach-broker.service" /etc/systemd/system/coach-broker.service
systemctl daemon-reload
systemctl enable --now coach-broker.service
systemctl restart coach-broker.service
sleep 3
systemctl --no-pager --lines=15 status coach-broker.service || true
