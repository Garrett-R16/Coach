"""Broker settings. Secrets arrive as environment variables from Infisical."""
from __future__ import annotations

import os
from pathlib import Path


def env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


STATE = Path(env("COACH_STATE", "/var/lib/coach/state"))
STATE.mkdir(parents=True, exist_ok=True)

SIGNAL_CLI = env("SIGNAL_CLI", "/usr/local/bin/signal-cli")
SIGNAL_CONFIG = env("SIGNAL_CONFIG", "/var/lib/coach/signal")
# Infisical names: host_number = the coach's Signal account, dest_number = the athlete
SIGNAL_NUMBER = env("host_number") or env("HOST_NUMBER")
SIGNAL_ATHLETE_NUMBER = env("dest_number") or env("DEST_NUMBER")  # learned from first sender if unset

HEALTH_WEBHOOK_TOKEN = env("HEALTH_WEBHOOK_TOKEN")
HEALTH_WEBHOOK_PORT = int(env("HEALTH_WEBHOOK_PORT", "8787"))
HEALTH_WEBHOOK_BIND = env("HEALTH_WEBHOOK_BIND", "0.0.0.0")
