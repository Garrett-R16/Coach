"""Paths, .env loading, shared settings. Stdlib only."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # the harness (this repo, public)
CONTEXT = ROOT / "context"                              # instructions.md, builder.md: how the coach works
TEMPLATES = ROOT / "templates"                          # starter files for a new athlete data directory
STATE = ROOT / "state"                                  # session ids, locks, run log (gitignored)


def env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


# Everything the coach knows and writes about the athlete lives under COACH_DATA (a private repo).
# Defaults to this repo so a fresh clone works as a single tree.
DATA_ROOT = Path(env("COACH_DATA", str(ROOT))).expanduser().resolve()
ATHLETE = DATA_ROOT / "context"                         # persona.md, thresholds.md, profile.md, schedule.md
PLAN = DATA_ROOT / "plan"
LOG = DATA_ROOT / "log"
DATA = DATA_ROOT / "data"
HEALTH_RAW = DATA / "health" / "raw"
DAILY = DATA / "daily"
WORKOUTS = DATA / "workouts"
ATTACHMENTS = DATA / "attachments"  # images the athlete sends in chat
INBOX = DATA_ROOT / "inbox"
KNOWLEDGE = DATA_ROOT / "knowledge"

for _d in (HEALTH_RAW, DAILY, WORKOUTS, ATTACHMENTS, INBOX, STATE, LOG, PLAN, ATHLETE, KNOWLEDGE):
    _d.mkdir(parents=True, exist_ok=True)


def env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


TELEGRAM_BOT_TOKEN = env("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = env("TELEGRAM_CHAT_ID")
HEALTH_WEBHOOK_TOKEN = env("HEALTH_WEBHOOK_TOKEN")
HEALTH_WEBHOOK_PORT = int(env("HEALTH_WEBHOOK_PORT", "8787"))
HEALTH_WEBHOOK_BIND = env("HEALTH_WEBHOOK_BIND", "0.0.0.0")
COACH_MODEL = env("COACH_MODEL")
COACH_TZ = env("COACH_TZ")  # None -> system local time
MORNING_LOOKBACK_DAYS = int(env("MORNING_LOOKBACK_DAYS", "7"))

CLAUDE_BIN = env("CLAUDE_BIN") or shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude")
