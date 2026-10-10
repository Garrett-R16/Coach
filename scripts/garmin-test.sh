#!/usr/bin/env bash
# Check the broker's Garmin session: lists the last three activities with average power.
#   scripts/garmin-test.sh planned           scheduled workouts on the calendar, today + 7 days
#   scripts/garmin-test.sh planned --queue   same, and hand them to the runner (data/planned/)
set -euo pipefail
cd /opt/coach
exec sudo -u coach-svc -H env HOME=/var/lib/coach GARMIN_TOKENS=/var/lib/coach/garmin COACH_STATE=/var/lib/coach/state \
  COACH_QUEUE=/var/lib/coach/queue /opt/coach/venv/bin/python3 -m broker.garmin "${@:-test}"
