"""Runner: watches the broker's inbox, runs the coach, writes replies to the outbox.

Runs as the athlete's own user. Holds no secrets. (systemd user unit coach-runner.service)
"""
from __future__ import annotations

import base64
import os
import time
import traceback
from pathlib import Path

from common import queue
from . import coach, config, health, triggers
from .util import now, read_json, setup_logging

log = setup_logging("runner")

HELP = """Commands:
/new - start a fresh conversation (the coach forgets today's chat, not the files)
/morning - run the morning report now
/workout - analyse the most recent workout file now
/build <request> - ask the builder agent to add or change functionality
/status - last health export, session, recent runs
/help - this list
Anything else goes to the coach."""


def _status() -> str:
    raws = sorted(config.HEALTH_RAW.glob("*.json"))
    sess = read_json(coach.SESSION_FILE, {})
    try:
        runs = coach.RUN_LOG.read_text().splitlines()[-5:]
    except FileNotFoundError:
        runs = []
    workouts = sorted(config.WORKOUTS.glob("*.json"))
    return (
        f"Last health export: {raws[-1].stem if raws else 'never'}\n"
        f"Daily files: {len(list(config.DAILY.glob('*.json')))}, workouts: {len(workouts)}"
        + (f" (latest {workouts[-1].name})" if workouts else "") + "\n"
        f"Session: {sess.get('session_id', 'none')} ({sess.get('date', '-')})\n"
        "Recent runs:\n" + ("\n".join(runs) if runs else "none")
    )


_EXT = {"image/jpeg": "jpg", "image/png": "png", "image/gif": "gif", "image/webp": "webp",
        "image/heic": "heic", "image/heif": "heif", "application/pdf": "pdf"}


def save_attachments(attachments: list[dict]) -> tuple[list[Path], int]:
    """Write each attachment from an inbox item to data/attachments/YYYY-MM-DD_HHMM_<n>.<ext>.
    Returns (saved paths, number that could not be retrieved or saved)."""
    saved: list[Path] = []
    failed = 0
    stamp = now().strftime("%Y-%m-%d_%H%M")
    for n, att in enumerate(attachments, 1):
        data = att.get("data") if isinstance(att, dict) else None
        if not data:
            failed += 1
            log.warning("attachment %d not delivered: %s", n, (att or {}).get("error", "no data"))
            continue
        ctype = (att.get("contentType") or "").lower()
        ext = _EXT.get(ctype) or Path(att.get("filename") or "").suffix.lstrip(".").lower() \
            or ctype.split("/")[-1] or "bin"
        path = config.ATTACHMENTS / f"{stamp}_{n}.{ext}"
        try:
            path.write_bytes(base64.b64decode(data))
            saved.append(path)
        except (ValueError, OSError) as e:
            failed += 1
            log.error("could not save attachment %d: %s", n, e)
    return saved, failed


def handle_message(text: str, attachments: list[dict] | None = None) -> None:
    text = text.strip()
    cmd, _, rest = text.partition(" ")
    cmd = cmd.lower()
    stamp = now().strftime("%Y-%m-%dT%H-%M-%S")
    (config.INBOX / f"{stamp}.txt").write_text(text)
    saved, failed = save_attachments(attachments or [])
    if saved or failed:
        # images always go to the coach, whatever the caption says
        triggers.chat(text, send=True, attachments=saved, failed_attachments=failed)
        return
    if cmd in ("/start", "/help"):
        queue.send_text(HELP)
    elif cmd == "/status":
        queue.send_text(_status())
    elif cmd == "/new":
        coach.reset_session()
        queue.send_text("Fresh session. Files and history are untouched.")
    elif cmd == "/morning":
        triggers.morning(send=True, force=True)
    elif cmd == "/workout":
        files = sorted(config.WORKOUTS.glob("*.json"))
        if not files:
            queue.send_text("No workout files yet.")
        else:
            triggers.workout(files[-1], send=True)
    elif cmd == "/build":
        if not rest.strip():
            queue.send_text("Usage: /build <what you want added or changed>")
        else:
            queue.send_text("Builder started. This can take a few minutes.")
            triggers.build(rest, send=True)
    else:
        triggers.chat(text, send=True)


def handle_health(payload: dict) -> None:
    result = health.ingest(payload)
    if triggers.morning_due_from_export(payload):
        log.info("first export of the day -> morning report")
        triggers.morning(send=True)
    for path, w in result["workouts"]:
        if health.is_cycling(w.get("name")):
            # rides wait up to PENDING_WAIT_S for their Garmin power data
            log.info("new ride %s -> waiting for Garmin data", path.name)
            health.add_pending(path)
        else:
            log.info("new workout %s -> coach", path.name)
            triggers.workout(path, send=True)


def handle_garmin(item: dict) -> None:
    path, created = health.ingest_garmin(item)
    if health.pop_pending(path) or created:
        log.info("Garmin data for %s -> coach", path.name)
        triggers.workout(path, send=True)
    else:
        log.info("Garmin data merged into %s (already analysed, no re-run)", path.name)


def flush_pending() -> None:
    for path in health.expired_pending():
        if path.exists():
            log.info("no Garmin data for %s after wait -> coach without power", path.name)
            triggers.workout(path, send=True)


def handle(item: dict) -> None:
    kind = item.get("kind")
    if kind == "message":
        handle_message(item.get("text") or "", item.get("attachments") or [])
    elif kind == "health":
        handle_health(item.get("payload") or {})
    elif kind == "garmin":
        handle_garmin(item)
    else:
        log.warning("unknown inbox item kind %r", kind)


def main() -> None:
    os.umask(0o007)
    log.info("watching %s", queue.INBOX)
    while True:
        for path in queue.pending(queue.INBOX):
            item = queue.load(path)
            if not item:
                queue.finish(path, ok=False)
                continue
            try:
                handle(item)
                queue.finish(path)
            except Exception as e:
                log.error("failed on %s: %s\n%s", path.name, e, traceback.format_exc())
                queue.finish(path, ok=False)
                try:
                    queue.send_text(f"Error: {e}")
                except Exception:
                    pass
        try:
            flush_pending()
        except Exception as e:
            log.error("pending flush failed: %s", e)
        time.sleep(1)


if __name__ == "__main__":
    main()
