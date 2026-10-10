"""Intervals.icu source: completed rides with power, planned workouts, fitness, and writing workouts.

Official API, personal API key (HTTP basic, user "API_KEY"). The broker holds the key; the runner
sees only what lands in the queue. Intervals.icu receives rides from Garmin Connect and MyWhoosh,
so this replaces the direct Garmin fetch for ride power. The direct Garmin login stays only for the
calendar, which Garmin does not let anyone read through partners.

Queue items this module produces (kind):
  intervals_activity  a completed ride: activity summary, streams, intervals
  planned_intervals   planned workouts for today and the next two days (with resolved targets)
  wellness            fitness/fatigue/form and whatever else Intervals.icu holds, last 7 days
  scheduled           the answer to a "schedule_workout" request from the coach
Outbox kinds it consumes: schedule_workout, refresh_planned (alongside the Garmin calendar).
"""
from __future__ import annotations

import base64
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from common import queue
from . import config

log = logging.getLogger("intervals")
API = "https://intervals.icu/api/v1"
def _find_env(*must: str, exclude: tuple[str, ...] = ()) -> str | None:
    """First environment variable whose name contains all of `must` (case-insensitive) and none of
    `exclude`. Lets the secrets be named freely in the vault (INTERVALS_API_KEY, intervals_key, ...)."""
    import os
    for name, val in sorted(os.environ.items()):
        n = name.lower()
        if val and all(m in n for m in must) and not any(x in n for x in exclude):
            return val
    return None


KEY = _find_env("intervals", "key", exclude=("athlete", "_id"))
ATHLETE = _find_env("intervals", "athlete") or _find_env("intervals", "id", exclude=("key",))
POLL_S = int(config.env("INTERVALS_POLL_SECONDS", "600"))
PLANNED_DAYS = int(config.env("INTERVALS_PLANNED_DAYS", "3"))
LOOKBACK_DAYS = int(config.env("INTERVALS_LOOKBACK_DAYS", "3"))
FIRST_LOOKBACK_DAYS = int(config.env("INTERVALS_FIRST_LOOKBACK_DAYS", "7"))  # one-time backfill on the first poll
RIDE_TYPES = set((config.env("INTERVALS_TYPES") or "Ride,VirtualRide,GravelRide,MountainBikeRide,EBikeRide,TrackRide").split(","))
STREAMS = "time,watts,heartrate,cadence,velocity_smooth,altitude,distance"
USER_AGENT = config.env("INTERVALS_USER_AGENT", "coach-harness/1.0 (+https://github.com/Garrett-R16/Coach)")
SEEN_FILE = config.STATE / "intervals_seen.json"
notify = None


def configured() -> bool:
    return bool(KEY and ATHLETE)


def _load(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _save(path: Path, obj) -> None:
    path.write_text(json.dumps(obj))
    path.chmod(0o600)


class Intervals:
    def __init__(self) -> None:
        self._auth = "Basic " + base64.b64encode(f"API_KEY:{KEY}".encode()).decode() if KEY else ""
        self._last_alert = 0.0

    # --- http -----------------------------------------------------------
    def _req(self, method: str, path: str, body=None, **q):
        url = f"{API}{path}" + (f"?{urllib.parse.urlencode(q)}" if q else "")
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Authorization": self._auth, "Content-Type": "application/json",
                                              "Accept": "application/json", "User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            blocked = "cloudflare" in detail.lower() or "<html" in detail.lower()
            if e.code in (401, 403) and not blocked:
                self._alert(f"Intervals.icu rejected the API key ({e.code}). Ride power and planned workouts from Intervals are off until it is fixed in Infisical.")
            elif e.code == 403 and blocked:
                self._alert("Intervals.icu's edge (Cloudflare) is refusing the broker's requests; this is a blocking rule, not the key. Check the User-Agent and try again later.")
            raise RuntimeError(f"Intervals.icu {method} {path} -> {e.code}: {detail}") from None

    def _get(self, path: str, **q):
        return self._req("GET", path, **q)

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

    # --- activities -----------------------------------------------------
    def _seen(self) -> set:
        return set(_load(SEEN_FILE, {"ids": []})["ids"])

    def _mark(self, aid) -> None:
        st = _load(SEEN_FILE, {"ids": []})
        st["ids"] = (st["ids"] + [aid])[-500:]
        _save(SEEN_FILE, st)

    def recent(self, days: int = 3) -> list[dict]:
        oldest = (date.today() - timedelta(days=days)).isoformat()
        newest = (date.today() + timedelta(days=1)).isoformat()
        return self._get(f"/athlete/{ATHLETE}/activities", oldest=oldest, newest=newest) or []

    def fetch_full(self, aid: str) -> dict:
        activity = self._get(f"/activity/{aid}", intervals="true") or {}
        streams = self._get(f"/activity/{aid}/streams", types=STREAMS) or []
        return {"activity": activity,
                "streams": {s.get("type"): s.get("data") for s in streams if isinstance(s, dict) and s.get("data")},
                "intervals": activity.get("icu_intervals") or []}

    def poll_activities(self) -> int:
        n = 0
        first = not SEEN_FILE.exists()
        seen = self._seen()
        acts = self.recent(FIRST_LOOKBACK_DAYS if first else LOOKBACK_DAYS)
        rides = [a for a in acts if a.get("type") in RIDE_TYPES]
        log.info("activities in window: %d (%d rides, %d already seen)%s", len(acts), len(rides),
                 sum(1 for a in rides if a.get("id") in seen), " [first poll, backfill]" if first else "")
        if first and not rides:
            _save(SEEN_FILE, {"ids": []})
        for a in sorted(rides, key=lambda x: x.get("start_date_local") or ""):
            if a.get("id") in seen:
                continue
            full = self.fetch_full(a["id"])
            path = queue.enqueue(queue.INBOX, {"kind": "intervals_activity", **full})
            self._mark(a["id"])
            n += 1
            log.info("queued %s '%s' (%s, from %s) -> %s", a.get("type"), a.get("name"), a["id"],
                     a.get("source") or a.get("device_name"), path.name)
        return n

    # --- planned workouts -------------------------------------------------
    def planned(self, days: int = PLANNED_DAYS) -> tuple[list[str], list[dict]]:
        dates = [(date.today() + timedelta(days=i)).isoformat() for i in range(days)]
        events = self._get(f"/athlete/{ATHLETE}/events", oldest=dates[0], newest=dates[-1],
                           category="WORKOUT", resolve="true") or []
        return dates, [e for e in events if isinstance(e, dict) and (e.get("start_date_local") or "")[:10] in dates]

    def fetch_planned_and_queue(self, request: str | None = None) -> Path:
        item: dict = {"kind": "planned_intervals", "request": request,
                      "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds")}
        try:
            item["dates"], item["items"] = self.planned()
        except Exception as e:
            item["dates"] = [(date.today() + timedelta(days=i)).isoformat() for i in range(PLANNED_DAYS)]
            item["error"] = f"Intervals.icu calendar read failed: {e}"
            log.error(item["error"])
        path = queue.enqueue(queue.INBOX, item)
        log.info("queued %s Intervals planned workout(s) -> %s", len(item.get("items") or []), path.name)
        return path

    # --- wellness / fitness -------------------------------------------------
    def fetch_wellness_and_queue(self) -> Path | None:
        try:
            oldest = (date.today() - timedelta(days=7)).isoformat()
            recs = self._get(f"/athlete/{ATHLETE}/wellness", oldest=oldest, newest=date.today().isoformat()) or []
            keep = ("id", "ctl", "atl", "rampRate", "ctlLoad", "atlLoad", "restingHR", "hrv", "hrvSDNN",
                    "sleepSecs", "sleepScore", "readiness", "weight", "eftp", "sportInfo")
            slim = [{k: r.get(k) for k in keep if r.get(k) is not None} for r in recs if isinstance(r, dict)]
            return queue.enqueue(queue.INBOX, {"kind": "wellness", "source": "intervals", "records": slim,
                                               "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds")})
        except Exception as e:
            log.error("wellness fetch failed: %s", e)
            return None

    # --- writing a workout --------------------------------------------------
    def schedule(self, req: dict) -> Path:
        """Create a planned workout on the athlete's Intervals.icu calendar. Intervals parses the
        description (its workout text syntax) into steps and pushes it to Garmin and MyWhoosh."""
        rid = req.get("request")
        out: dict = {"kind": "scheduled", "request": rid}
        try:
            day = req["date"]
            datetime.strptime(day, "%Y-%m-%d")
            body = {"category": "WORKOUT", "start_date_local": f"{day}T00:00:00", "type": req.get("sport") or "Ride",
                    "name": (req.get("name") or "Workout")[:100], "description": req.get("description") or "",
                    "external_id": f"coach-{rid or int(time.time())}"}
            if req.get("indoor") is not None:
                body["indoor"] = bool(req["indoor"])
            ev = self._req("POST", f"/athlete/{ATHLETE}/events", body=body, upsertOnUid="false") or {}
            out["ok"] = True
            out["event"] = {k: ev.get(k) for k in ("id", "name", "type", "start_date_local", "moving_time",
                                                     "icu_training_load", "icu_intensity", "description", "push_errors")}
            out["event"]["steps"] = (ev.get("workout_doc") or {}).get("steps")
            log.info("scheduled '%s' on %s (event %s)", body["name"], day, ev.get("id"))
        except Exception as e:
            out["ok"] = False
            out["error"] = str(e)
            log.error("schedule failed: %s", e)
        return queue.enqueue(queue.INBOX, out)

    # --- loop ---------------------------------------------------------------
    def loop(self) -> None:
        last_planned = last_wellness = 0.0
        while True:
            try:
                self.poll_activities()
                if time.time() - last_planned > 1800:
                    self.fetch_planned_and_queue(); last_planned = time.time()
                if time.time() - last_wellness > 3600:
                    self.fetch_wellness_and_queue(); last_wellness = time.time()
            except Exception as e:
                log.error("poll failed: %s", e)
            time.sleep(POLL_S)


SOURCE = Intervals()


def start(notify_fn=None) -> None:
    global notify
    notify = notify_fn
    if not configured():
        log.warning("Intervals.icu not configured (INTERVALS_API_KEY / INTERVALS_ATHLETE_ID); ride power via Intervals off")
        return
    threading.Thread(target=SOURCE.loop, daemon=True, name="intervals").start()
