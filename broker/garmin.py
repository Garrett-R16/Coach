"""Garmin Connect source for ride power.

Apple Health carries a Garmin ride's summary but not its power channel. When a
cycling workout arrives from the phone, this module finds the matching Garmin
activity and queues its summary, per-second streams and laps for the runner.
A slow hourly check catches anything the phone missed.

Auth: python-garminconnect with a token store under the broker's state dir.
The password is only ever typed at the one-time interactive login
(`python3 -m broker.garmin login`), never stored. If the tokens stop working
the broker tells the athlete over Signal instead of failing silently.
"""
from __future__ import annotations

import json
import logging
import queue as pyqueue
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from common import queue
from . import config

log = logging.getLogger("garmin")
TOKENS = Path(config.env("GARMIN_TOKENS", str(Path(config.env("COACH_STATE", "/var/lib/coach/state")).parent / "garmin")))
POLL_S = int(config.env("GARMIN_POLL_SECONDS", "3600"))
SEEN_FILE = config.STATE / "garmin_seen.json"
MATCH_WINDOW = timedelta(minutes=12)
RETRY_EVERY_S = 300
RETRIES = 6
CYCLING_WORDS = ("cycl", "bike", "biking", "ride")

try:
    from garminconnect import Garmin  # type: ignore
    from garminconnect import GarminConnectAuthenticationError, GarminConnectConnectionError  # type: ignore
    AVAILABLE = True
except Exception:  # library not installed: feature off, broker still runs
    AVAILABLE = False

notify = None  # set by main: callable(text) that sends a Signal message


def configured() -> bool:
    return AVAILABLE and TOKENS.exists() and any(TOKENS.iterdir())


def _load(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _save(path: Path, obj) -> None:
    path.write_text(json.dumps(obj))
    path.chmod(0o600)


def _parse_garmin_time(s: str | None, utc: bool) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.strptime(s[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
    return dt.replace(tzinfo=timezone.utc) if utc and dt.tzinfo is None else dt


def is_cycling(name: str | None) -> bool:
    n = (name or "").lower()
    return any(w in n for w in CYCLING_WORDS)


class GarminSource:
    def __init__(self) -> None:
        self._client = None
        self._lock = threading.Lock()
        self._work: "pyqueue.Queue[tuple[datetime, int]]" = pyqueue.Queue()
        self._last_alert = 0.0

    # --- auth -----------------------------------------------------------
    def client(self):
        with self._lock:
            if self._client is not None:
                return self._client
            g = Garmin()
            try:
                g.login(str(TOKENS))
            except Exception as e:
                self._alert(f"Garmin login failed ({type(e).__name__}). Ride power is off until you re-run scripts/garmin-login.sh on the server.")
                raise
            self._client = g
            log.info("Garmin session ready (%s)", getattr(g, "display_name", "?"))
            return g

    def _alert(self, text: str) -> None:
        if time.time() - self._last_alert < 24 * 3600:
            return
        self._last_alert = time.time()
        log.error(text)
        if notify:
            try:
                notify(text)
            except Exception as e:
                log.warning("notify failed: %s", e)

    def _call(self, fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except Exception as e:
            # drop the session so the next call logs in fresh from the token store
            with self._lock:
                self._client = None
            raise

    # --- fetching -------------------------------------------------------
    def _seen(self) -> set:
        return set(_load(SEEN_FILE, {"ids": []})["ids"])

    def _mark(self, activity_id) -> None:
        st = _load(SEEN_FILE, {"ids": []})
        st["ids"] = (st["ids"] + [activity_id])[-500:]
        _save(SEEN_FILE, st)

    def _recent(self, days: int = 2) -> list[dict]:
        g = self.client()
        start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        end = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
        return self._call(g.get_activities_by_date, start, end, "cycling") or []

    def _fetch_and_queue(self, a: dict) -> Path:
        g = self.client()
        aid = a["activityId"]
        detail = self._call(g.get_activity, str(aid)) or {}
        details = self._call(g.get_activity_details, str(aid), 3000, 0) or {}
        splits = self._call(g.get_activity_splits, str(aid)) or {}
        path = queue.enqueue(queue.INBOX, {"kind": "garmin", "listed": a, "activity": detail,
                                           "details": details, "splits": splits})
        self._mark(aid)
        log.info("queued Garmin %s '%s' (%s) -> %s", a.get("activityType", {}).get("typeKey"),
                 a.get("activityName"), aid, path.name)
        return path

    def _find(self, start: datetime) -> dict | None:
        for a in self._recent():
            t = _parse_garmin_time(a.get("startTimeGMT"), utc=True)
            if t and abs(t - start.astimezone(timezone.utc)) <= MATCH_WINDOW:
                return a
        return None

    # --- entry points ---------------------------------------------------
    def request(self, start: datetime) -> None:
        """Ask for the Garmin activity that started near `start` (tz-aware)."""
        self._work.put((start, 0))

    def request_for_payload(self, payload: dict) -> int:
        """Scan a Health Auto Export payload for cycling workouts and request each."""
        data = payload.get("data", payload)
        n = 0
        for w in data.get("workouts", []) or []:
            if not is_cycling(w.get("name")):
                continue
            s = w.get("start")
            try:
                start = datetime.strptime(s, "%Y-%m-%d %H:%M:%S %z")
            except (TypeError, ValueError):
                continue
            self.request(start)
            n += 1
        return n

    def worker(self) -> None:
        if not configured():
            log.warning("Garmin not configured (no tokens in %s); ride power off. Run scripts/garmin-login.sh", TOKENS)
        while True:
            start, attempt = self._work.get()
            if not configured():
                continue
            try:
                a = self._find(start)
                if a is None:
                    if attempt < RETRIES:
                        log.info("no Garmin activity yet for ride at %s (try %d); retrying in %ds", start, attempt + 1, RETRY_EVERY_S)
                        threading.Timer(RETRY_EVERY_S, lambda: self._work.put((start, attempt + 1))).start()
                    else:
                        log.warning("gave up finding a Garmin activity for ride at %s", start)
                    continue
                if a["activityId"] in self._seen():
                    log.info("Garmin %s already queued", a["activityId"])
                    continue
                self._fetch_and_queue(a)
            except Exception as e:
                log.error("fetch failed for ride at %s: %s", start, e)
                if attempt < RETRIES:
                    threading.Timer(RETRY_EVERY_S, lambda: self._work.put((start, attempt + 1))).start()

    def backup_poll(self) -> None:
        time.sleep(120)
        while True:
            if configured():
                try:
                    seen = self._seen()
                    for a in self._recent():
                        if a["activityId"] not in seen:
                            self._fetch_and_queue(a)
                except Exception as e:
                    log.error("backup poll failed: %s", e)
            time.sleep(POLL_S)


SOURCE = GarminSource()


def start(notify_fn=None) -> None:
    global notify
    notify = notify_fn
    if not AVAILABLE:
        log.warning("garminconnect not installed; ride power off (see broker/requirements.txt)")
        return
    threading.Thread(target=SOURCE.worker, daemon=True, name="garmin-worker").start()
    threading.Thread(target=SOURCE.backup_poll, daemon=True, name="garmin-poll").start()


# --- CLI: one-time login and a connectivity test --------------------------

def _cli(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if not AVAILABLE:
        print("garminconnect is not installed in this Python. Use the broker venv.", file=sys.stderr)
        return 2
    cmd = argv[0] if argv else "test"
    if cmd == "login":
        from getpass import getpass
        email = input("Garmin Connect email: ").strip()
        password = getpass("Garmin Connect password (not stored): ")
        g = Garmin(email, password, prompt_mfa=lambda: input("MFA code: ").strip())
        g.login(str(TOKENS))
        print(f"Logged in as {g.display_name}. Tokens saved under {TOKENS}. The password was not kept.")
        return 0
    if cmd == "test":
        g = Garmin()
        g.login(str(TOKENS))
        acts = g.get_activities(0, 3)
        print(f"Session OK as {g.display_name}. Last activities:")
        for a in acts:
            print(f"  {a.get('startTimeLocal')}  {a.get('activityType', {}).get('typeKey')}  {a.get('activityName')}"
                  f"  avgPower={a.get('avgPower')}")
        return 0
    print("usage: python3 -m broker.garmin login|test", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
