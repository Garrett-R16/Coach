#!/usr/bin/env bash
# One-time Garmin Connect login for the broker. Prompts for email, password and MFA code;
# stores only the resulting tokens under /var/lib/coach/garmin (owned by coach-svc, 0700).
# The password is not written anywhere. Re-run if the broker reports the Garmin login expired.
set -euo pipefail
[ -x /opt/coach/venv/bin/python3 ] || { echo "broker not installed yet: run sudo scripts/install-broker.sh first"; exit 1; }
sudo install -d -o coach-svc -g coach-svc -m 700 /var/lib/coach/garmin
cd /opt/coach
sudo -u coach-svc -H env HOME=/var/lib/coach GARMIN_TOKENS=/var/lib/coach/garmin COACH_STATE=/var/lib/coach/state \
  /opt/coach/venv/bin/python3 -m broker.garmin login
sudo systemctl restart coach-broker.service
echo "Broker restarted. Test with: scripts/garmin-test.sh"
