#!/usr/bin/env bash
# Check the broker's Garmin session: lists the last three activities with average power.
set -euo pipefail
cd /opt/coach
exec sudo -u coach-svc -H env HOME=/var/lib/coach GARMIN_TOKENS=/var/lib/coach/garmin COACH_STATE=/var/lib/coach/state \
  /opt/coach/venv/bin/python3 -m broker.garmin test
