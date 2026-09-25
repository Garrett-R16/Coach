"""Build prompts for each trigger and hand them to the coach.

CLI:  python3 -m runner.triggers morning [--force]|chat "<text>" [--attach <image>]...|workout <file>|build "<spec>"
"""
from __future__ import annotations

import json
import sys
from datetime import timedelta
from pathlib import Path

from common import queue
from . import coach, config, health
from .util import now, read_json, setup_logging, today, write_json

log = setup_logging("triggers")


def _recent_days(n: int) -> list[dict]:
    """Today first (overnight sleep and this morning's readings land on today's date), then back n days."""
    out = []
    for i in range(0, n + 1):
        d = (today() - timedelta(days=i)).isoformat()
        c = health.daily_compact(d)
        if c:
            out.append(c)
    return out


def _recent_workouts(days: int) -> list[str]:
    cutoff = (today() - timedelta(days=days)).isoformat()
    return sorted(p.name for p in config.WORKOUTS.glob("*.json") if p.name[:10] >= cutoff)


def morning_prompt() -> str:
    days = _recent_days(config.MORNING_LOOKBACK_DAYS)
    yesterday = (today() - timedelta(days=1)).isoformat()
    return (
        "Morning report.\n\n"
        f"Today is {today().isoformat()}. Yesterday was {yesterday}.\n\n"
        f"Health data for today and the previous {config.MORNING_LOOKBACK_DAYS} days (most recent first), "
        "normalised from Apple Health. Last night's sleep and this morning's resting HR and HRV are on today's date. "
        "Missing days mean no export arrived:\n"
        f"{json.dumps(days, indent=1)}\n\n"
        f"Workout files from the last 7 days in data/workouts/: {_recent_workouts(7)}\n\n"
        "Follow the morning-report format in your instructions."
    )


def workout_prompt(path: Path) -> str:
    w = read_json(path, {})
    src = "Garmin (with power)" if w.get("garmin") and w.get("source") == "garmin" else \
          "Apple Health plus Garmin power data" if w.get("garmin") else "Apple Health"
    return (
        f"A workout just arrived from {src}.\n\n"
        f"File: {path.relative_to(config.DATA_ROOT)} (full samples are in the file if you need them)\n"
        f"Summary:\n{json.dumps(health.workout_summary(w), indent=1, default=str)}\n\n"
        "Follow the post-workout format in your instructions."
    )


def chat_prompt(text: str, attachments: list[Path] | None = None, failed_attachments: int = 0) -> str:
    prompt = f"Message from the athlete:\n\n{text or '(no text, only attachments)'}"
    if attachments:
        prompt += "\n\nThe athlete attached these files (images). Read each one with the Read tool before answering:\n"
        prompt += "\n".join(f"- {p.relative_to(config.DATA_ROOT)}" for p in attachments)
    if failed_attachments:
        n = failed_attachments
        prompt += (f"\n\n{n} attachment{'s were' if n != 1 else ' was'} sent but could not be retrieved. "
                   "Tell the athlete you could not see it and ask them to resend or describe it; do not guess its contents.")
    return prompt


MORNING_SENT = config.STATE / "morning_sent.json"
MORNING_EARLIEST_HOUR = 5


def morning_sent_today() -> bool:
    return read_json(MORNING_SENT, {}).get("date") == today().isoformat()


def morning(send: bool = True, force: bool = False) -> coach.Result | None:
    """Morning report. Runs once per day: the first health export after 05:00 triggers it,
    the timer is only a fallback. `force` (the /morning command) always runs it."""
    if morning_sent_today() and not force:
        log.info("morning report already sent today; skipping")
        return None
    coach.reset_session()  # a new day starts a fresh conversation
    r = coach.run_coach("morning", morning_prompt(), resume=False)
    if send:
        queue.send_text(r.text)
    if r.ok:
        write_json(MORNING_SENT, {"date": today().isoformat(), "at": now().isoformat(timespec="seconds")})
    return r


def morning_due_from_export(payload: dict) -> bool:
    """True when this export should trigger today's report: has metrics, it is after 05:00,
    and the report has not gone out yet."""
    data = payload.get("data", payload)
    if not (data.get("metrics") or []):
        return False
    return now().hour >= MORNING_EARLIEST_HOUR and not morning_sent_today()


def workout(path: Path, send: bool = True) -> coach.Result:
    r = coach.run_coach("workout", workout_prompt(path))
    if send:
        queue.send_text(r.text)
    return r


def chat(text: str, send: bool = True, attachments: list[Path] | None = None,
         failed_attachments: int = 0) -> coach.Result:
    r = coach.run_coach("chat", chat_prompt(text, attachments, failed_attachments))
    if send:
        queue.send_text(r.text)
    if r.build_request:
        if send:
            queue.send_text(f"Starting builder: {r.build_request}")
        b = coach.run_builder(r.build_request)
        if send:
            queue.send_text(b.text)
    return r


def build(spec: str, send: bool = True) -> coach.Result:
    r = coach.run_builder(spec)
    if send:
        queue.send_text(r.text)
    return r


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    cmd, args = argv[0], argv[1:]
    send = "--no-send" not in args
    args = [a for a in args if a != "--no-send"]
    if cmd == "morning":
        r = morning(send, force="--force" in args)
        if r is None:
            print("morning report already sent today (use --force to resend)")
            return 0
    elif cmd == "chat":
        attach: list[Path] = []
        while "--attach" in args:
            i = args.index("--attach")
            attach.append(Path(args[i + 1]).resolve())
            del args[i:i + 2]
        r = chat(" ".join(args), send, attachments=attach or None)
    elif cmd == "workout":
        r = workout(Path(args[0]).resolve(), send)
    elif cmd == "build":
        r = build(" ".join(args), send)
    else:
        print(__doc__)
        return 2
    print(r.text)
    return 0 if r.ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
