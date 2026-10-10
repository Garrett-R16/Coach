"""Build prompts for each trigger and hand them to the coach.

CLI:  python3 -m runner.triggers morning [--force]|chat "<text>" [--attach <image>]...|workout <file>|build "<spec>"
"""
from __future__ import annotations

import json
import sys
from datetime import timedelta
from pathlib import Path

from common import queue
from . import coach, config, health, planned
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


def _wellness_section() -> str:
    w = read_json(config.DATA / "intervals" / "wellness.json", None)
    if not w or not w.get("records"):
        return ""
    recs = w["records"][-7:]
    return ("Fitness from Intervals.icu (ctl = fitness, atl = fatigue, form = ctl - atl; rampRate = weekly ctl change), "
            f"fetched {str(w.get('fetched_at', ''))[:16]}:\n{json.dumps(recs)}\n\n")


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
        f"{_wellness_section()}"
        f"{planned.prompt_section()}\n\n"
        "Follow the morning-report format in your instructions."
    )


def workout_prompt(path: Path) -> str:
    w = read_json(path, {})
    power = "intervals" if w.get("intervals") else "garmin" if w.get("garmin") else None
    label = {"intervals": "Intervals.icu (Garmin or MyWhoosh ride, with power)", "garmin": "Garmin (with power)"}.get(power)
    src = label if w.get("source") in ("garmin", "intervals") else \
          f"Apple Health plus power data from {label.split(' (')[0]}" if power else "Apple Health"
    return (
        f"A workout just arrived from {src}.\n\n"
        f"File: {path.relative_to(config.DATA_ROOT)} (full samples are in the file if you need them)\n"
        f"Summary:\n{json.dumps(health.workout_summary(w), indent=1, default=str)}\n\n"
        f"{planned.prompt_section()}\n\n"
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
    prompt += "\n\n" + planned.prompt_section()
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
    _refresh_planned("morning")
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


PLANNED_FRESH_S = int(config.env("PLANNED_FRESH_MINUTES", "60")) * 60


def _refresh_planned(trigger: str) -> None:
    """Fresh Garmin calendar files before the prompt is built. Never blocks a coach run on failure.
    The morning report always refreshes; chats and workout analyses reuse a copy fetched within
    the last hour (the broker also polls hourly), so a reply is not held up by a Garmin round trip."""
    try:
        if trigger != "morning":
            st = read_json(planned.FETCHED_FILE, {})
            last = planned._ts(st.get("at"))
            if last and (now() - last).total_seconds() < PLANNED_FRESH_S:
                log.info("planned workouts before %s: stored copy from %s is recent, not refreshing", trigger, st.get("at"))
                return
        r = planned.refresh(timeout_s=12 if trigger == "chat" else planned.REFRESH_TIMEOUT_S)
        log.info("planned workouts before %s: written=%s unchanged=%d removed=%s error=%s",
                 trigger, r["written"], len(r["unchanged"]), r["removed"], r.get("error"))
    except Exception as e:
        log.error("planned-workout refresh before %s failed: %s", trigger, e)


def workout(path: Path, send: bool = True) -> coach.Result:
    _refresh_planned("workout")
    r = coach.run_coach("workout", workout_prompt(path))
    if send:
        queue.send_text(r.text)
    return r


def chat(text: str, send: bool = True, attachments: list[Path] | None = None,
         failed_attachments: int = 0) -> coach.Result:
    _refresh_planned("chat")
    r = coach.run_coach("chat", chat_prompt(text, attachments, failed_attachments))
    if send:
        queue.send_text(r.text)
    if r.build_request:
        # only the coach's reply reaches the phone; the spec it wrote is logged, not sent
        log.info("coach requested a build: %s", r.build_request)
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
