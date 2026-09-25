"""Store and normalise Health Auto Export payloads.

Raw payloads are kept verbatim under data/health/raw/. A tolerant normaliser
folds metrics into one JSON per day (data/daily/YYYY-MM-DD.json) and writes one
JSON per workout (data/workouts/). Field names in HAE exports vary by version,
so anything unrecognised is preserved rather than dropped.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean

from . import config
from .util import now, read_json, write_json

log = logging.getLogger("health")
SEEN_FILE = config.STATE / "workouts_seen.json"
PENDING_FILE = config.STATE / "pending_workouts.json"
PENDING_WAIT_S = 600  # how long a ride waits for its Garmin data before analysis runs anyway
CYCLING_WORDS = ("cycl", "bike", "biking", "ride")

# metrics whose daily value is a total, everything else is averaged
SUM_METRICS = {
    "step_count", "active_energy", "basal_energy_burned", "apple_exercise_time",
    "apple_stand_time", "apple_stand_hour", "walking_running_distance", "cycling_distance",
    "swimming_distance", "flights_climbed", "dietary_energy", "dietary_water",
    "swimming_stroke_count", "time_in_daylight",
}
SLEEP_METRICS = {"sleep_analysis"}


def parse_dt(s: str) -> datetime | None:
    if not isinstance(s, str):
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _day(s: str) -> str | None:
    dt = parse_dt(s)
    return dt.date().isoformat() if dt else None


def store_raw(payload: dict) -> Path:
    path = config.HEALTH_RAW / f"{now().strftime('%Y-%m-%dT%H-%M-%S')}.json"
    path.write_text(json.dumps(payload))
    return path


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _agg(name: str, units: str | None, points: list[dict]) -> dict:
    qtys = [q for q in (_num(p.get("qty")) for p in points) if q is not None]
    avgs = [q for q in (_num(p.get("Avg")) for p in points) if q is not None]
    mins = [q for q in (_num(p.get("Min")) for p in points) if q is not None]
    maxs = [q for q in (_num(p.get("Max")) for p in points) if q is not None]
    out: dict = {"units": units, "samples": len(points)}
    if qtys:
        out["value"] = round(sum(qtys) if name in SUM_METRICS else mean(qtys), 2)
    if avgs:
        out["avg"] = round(mean(avgs), 2)
    if mins:
        out["min"] = round(min(mins), 2)
    if maxs:
        out["max"] = round(max(maxs), 2)
    if not (qtys or avgs or mins or maxs):
        # unknown shape: keep the last point so nothing is lost
        out["raw_last"] = points[-1] if points else None
    return out


def normalise_metrics(payload: dict) -> dict[str, dict]:
    """Return {date: {metric_name: aggregate}} for every metric in the payload."""
    data = payload.get("data", payload)
    days: dict[str, dict] = {}
    for metric in data.get("metrics", []) or []:
        name = metric.get("name")
        units = metric.get("units")
        by_day: dict[str, list] = {}
        for p in metric.get("data", []) or []:
            d = _day(p.get("date") or p.get("startDate") or p.get("start") or "")
            if d:
                by_day.setdefault(d, []).append(p)
        for d, points in by_day.items():
            slot = days.setdefault(d, {})
            slot[name] = {"units": units, "points": points}
    return days


POINT_LIMIT = 2000  # per metric per day; hourly data is ~24, HR at minute grouping would be ~1440


def _rebuild(name: str, entry: dict) -> dict:
    """Aggregate a metric's stored points into the summary fields the coach reads."""
    points = list(entry.get("points", {}).values()) if isinstance(entry.get("points"), dict) else entry.get("points", [])
    units = entry.get("units")
    if name in SLEEP_METRICS:
        return {"units": units, "records": points, "points": entry.get("points")}
    out = _agg(name, units, points)
    out["points"] = entry.get("points")
    if name in ("heart_rate_variability", "resting_heart_rate", "respiratory_rate", "blood_oxygen_saturation", "vo2_max"):
        # few readings a day and the timing matters (overnight vs evening): expose each one
        out["readings"] = [{"time": p.get("date", "")[11:16], "value": round(_num(p.get("qty")), 1)}
                           for p in sorted(points, key=lambda p: p.get("date", "")) if _num(p.get("qty")) is not None]
    return out


def merge_daily(days: dict[str, dict]) -> list[str]:
    """Fold new points into each day's file. Points are keyed by timestamp so repeated or
    overlapping exports never duplicate or overwrite earlier readings."""
    changed = []
    for d, metrics in days.items():
        path = config.DAILY / f"{d}.json"
        cur = read_json(path, {"date": d, "metrics": {}})
        store = cur.setdefault("metrics", {})
        for name, entry in metrics.items():
            existing = store.get(name) or {}
            pts = existing.get("points")
            if not isinstance(pts, dict):  # legacy file written before points were kept
                pts = {}
                for p in existing.get("records", []) or []:
                    pts[p.get("date", "")] = p
            for p in entry.get("points", []):
                pts[p.get("date", "")] = p
            if len(pts) > POINT_LIMIT:
                pts = dict(sorted(pts.items())[-POINT_LIMIT:])
            store[name] = _rebuild(name, {"units": entry.get("units") or existing.get("units"), "points": pts})
        cur["updated"] = now().isoformat(timespec="seconds")
        write_json(path, cur)
        changed.append(d)
    return changed


def rebuild_daily_from_raw() -> int:
    """Recompute every daily file from the raw exports on disk (after a normaliser fix)."""
    for p in config.DAILY.glob("*.json"):
        p.unlink()
    n = 0
    for raw in sorted(config.HEALTH_RAW.glob("*.json")):
        try:
            merge_daily(normalise_metrics(read_json(raw, {})))
            n += 1
        except Exception as e:
            log.warning("rebuild skipped %s: %s", raw.name, e)
    return n


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "workout").lower()).strip("-")


def _hr_summary(w: dict) -> dict:
    """Compact HR summary from whatever per-sample arrays HAE included."""
    series = w.get("heartRateData") or w.get("heartRate") or []
    vals = []
    for p in series:
        if isinstance(p, dict):
            for k in ("qty", "Avg", "avg", "value"):
                v = _num(p.get(k))
                if v is not None:
                    vals.append(v)
                    break
        else:
            v = _num(p)
            if v is not None:
                vals.append(v)
    if not vals:
        return {}
    return {"avg": round(mean(vals)), "max": round(max(vals)), "min": round(min(vals)), "samples": len(vals)}


def _series_stats(items: list) -> dict | None:
    """Stats for a list of {qty}/{Avg,Min,Max} samples; None if it is not numeric."""
    vals, mins, maxs = [], [], []
    for p in items:
        if not isinstance(p, dict):
            v = _num(p)
            if v is not None:
                vals.append(v)
            continue
        q = _num(p.get("qty"))
        if q is None:
            q = _num(p.get("Avg"))
        if q is not None:
            vals.append(q)
        if _num(p.get("Min")) is not None:
            mins.append(_num(p.get("Min")))
        if _num(p.get("Max")) is not None:
            maxs.append(_num(p.get("Max")))
    if not vals:
        return None
    out = {"n": len(items), "avg": round(mean(vals), 1), "min": round(min(mins or vals), 1),
           "max": round(max(maxs or vals), 1), "sum": round(sum(vals), 1)}
    units = next((p.get("units") for p in items if isinstance(p, dict) and p.get("units")), None)
    if units:
        out["units"] = units
    return out


_SCALAR_KEYS = ("activeDuration", "duration", "distance", "start", "end", "boundaryKind", "activityType",
                "name", "avgHeartRate", "maxHeartRate", "speed", "pace", "activeEnergyBurned")


def _compact_items(items: list, limit: int) -> list:
    """Laps/segments/splits: keep a few scalar fields per item, first `limit` items."""
    out = []
    for it in items[:limit]:
        if not isinstance(it, dict):
            continue
        row = {}
        for k in _SCALAR_KEYS:
            v = it.get(k)
            if isinstance(v, dict) and "qty" in v:
                row[k] = round(_num(v["qty"]) or 0, 1)
            elif isinstance(v, (int, float)):
                row[k] = round(v, 1)
            elif isinstance(v, str) and k in ("start", "end"):
                row[k] = v[11:19]  # HH:MM:SS
            elif isinstance(v, str):
                row[k] = v
        if row:
            out.append(row)
    return out


def workout_summary(w: dict, budget: int = 12000) -> dict:
    """Compact dict safe to paste into a prompt. Sample arrays become stats, laps and
    segments become short rows, and the result is trimmed to roughly `budget` chars."""
    out: dict = {}
    lists: dict[str, list] = {}
    for k, v in w.items():
        if k == "garmin" and isinstance(v, dict):
            out["garmin"] = garmin_summary(v)
        elif isinstance(v, list):
            lists[k] = v
        elif k == "metadata" and not v:
            continue
        else:
            out[k] = v
    for k, v in lists.items():
        if not v:
            continue
        stats = _series_stats(v) if k not in ("laps", "segments", "splits", "events", "activities") else None
        if stats:
            out[k] = stats
        elif k in ("laps", "segments", "activities"):
            out[k] = {"n": len(v), "items": _compact_items(v, 80)}
        elif k == "splits":
            rows = []
            for sp in v:
                for it in (sp.get("items") or []) if isinstance(sp, dict) else []:
                    rows.append(it)
            out[k] = {"n": len(rows), "items": _compact_items(rows, 40)}
        else:
            out[k] = {"n": len(v)}
    # trim: drop the biggest list-derived fields until under budget
    def size() -> int:
        return len(json.dumps(out, default=str))
    while size() > budget:
        big = max((k for k in out if isinstance(out[k], dict) and "items" in out[k]),
                  key=lambda k: len(json.dumps(out[k], default=str)), default=None)
        if big is None:
            break
        items = out[big]["items"]
        if len(items) <= 10:
            out[big] = {"n": out[big]["n"], "note": "detail omitted, see file"}
        else:
            out[big]["items"] = items[: len(items) // 2]
            out[big]["note"] = "truncated, see file"
    return out


def _workout_key(w: dict) -> str:
    return w.get("id") or f"{w.get('start')}|{w.get('name')}"


GPS_KEYS = ("route", "startLatitude", "startLongitude", "endLatitude", "endLongitude", "latitude", "longitude", "polyline")


def strip_gps(w: dict) -> dict:
    """The coach never needs where a workout happened. Drop GPS before anything is stored."""
    return {k: v for k, v in w.items() if k not in GPS_KEYS}


def extract_workouts(payload: dict) -> list[tuple[Path, dict]]:
    """Write each new workout to data/workouts/. Returns [(path, workout)] for new ones."""
    data = payload.get("data", payload)
    seen = read_json(SEEN_FILE, {"keys": []})
    seen_keys = set(seen["keys"])
    new = []
    for w in data.get("workouts", []) or []:
        key = _workout_key(w)
        if key in seen_keys:
            continue
        start = parse_dt(w.get("start") or "") or now()
        path = config.WORKOUTS / f"{start.strftime('%Y-%m-%d_%H%M')}_{_slug(w.get('name'))}.json"
        w = strip_gps(w)
        write_json(path, w)
        seen_keys.add(key)
        new.append((path, w))
    if new:
        seen["keys"] = sorted(seen_keys)[-2000:]
        write_json(SEEN_FILE, seen)
    return new


def describe_payload(payload: dict) -> str:
    data = payload.get("data", payload)
    names = [m.get("name") for m in data.get("metrics", []) or []]
    return f"metrics={names} workouts={len(data.get('workouts', []) or [])}"


def ingest(payload: dict) -> dict:
    """Full pipeline. Returns what changed."""
    raw = store_raw(payload)
    days = merge_daily(normalise_metrics(payload))
    new_workouts = extract_workouts(payload)
    log.info("ingested %s: %s -> days=%s new_workouts=%d", raw.name, describe_payload(payload), days, len(new_workouts))
    return {"raw": raw, "days": days, "workouts": new_workouts}


def daily_compact(day: str) -> dict | None:
    """Daily file with sleep records collapsed to totals, for prompts."""
    d = read_json(config.DAILY / f"{day}.json", None)
    if not d:
        return None
    out = {"date": day, "metrics": {}}
    for name, m in d.get("metrics", {}).items():
        if name in SLEEP_METRICS:
            totals: dict[str, float] = {}
            recs = m.get("records") or (list(m["points"].values()) if isinstance(m.get("points"), dict) else [])
            for r in recs:
                for k, v in r.items():
                    n = _num(v)
                    if n is not None and k not in ("date",):
                        totals[k] = round(totals.get(k, 0) + n, 2)
            out["metrics"][name] = {"units": m.get("units"), **totals}
        else:
            out["metrics"][name] = {k: v for k, v in m.items() if k not in ("raw_last", "points")}
    return out


# --- Garmin -----------------------------------------------------------------

def is_cycling(name: str | None) -> bool:
    n = (name or "").lower()
    return any(w in n for w in CYCLING_WORDS)


def _garmin_streams(details: dict) -> dict:
    """Turn Garmin's metricDescriptors + activityDetailMetrics into named lists."""
    desc = details.get("metricDescriptors") or []
    rows = details.get("activityDetailMetrics") or []
    idx = {d.get("key"): d.get("metricsIndex") for d in desc if isinstance(d, dict)}
    wanted = {"time": "sumDuration", "elapsed": "sumElapsedDuration", "power": "directPower",
              "hr": "directHeartRate", "cadence": "directBikeCadence", "speed": "directSpeed",
              "elevation": "directElevation", "distance": "sumDistance", "temp": "directAirTemperature"}
    out: dict[str, list] = {}
    for name, key in wanted.items():
        i = idx.get(key)
        if i is None:
            continue
        vals = []
        for r in rows:
            m = r.get("metrics") if isinstance(r, dict) else None
            vals.append(m[i] if m and i < len(m) else None)
        if any(v is not None for v in vals):
            out[name] = vals
    return out


def _garmin_laps(splits: dict) -> list[dict]:
    laps = []
    for l in splits.get("lapDTOs") or []:
        if not isinstance(l, dict):
            continue
        laps.append({k: (round(v, 1) if isinstance(v, float) else v) for k, v in l.items()
                     if k in ("lapIndex", "duration", "movingDuration", "distance", "averagePower", "maxPower",
                              "normalizedPower", "averageHR", "maxHR", "averageBikeCadence", "averageSpeed",
                              "elevationGain", "startTimeGMT", "intensityType")})
    return laps


def normalise_garmin(item: dict) -> dict:
    a = item.get("activity") or {}
    listed = item.get("listed") or {}
    sm = a.get("summaryDTO") or {}
    typ = ((a.get("activityTypeDTO") or {}).get("typeKey")) or ((listed.get("activityType") or {}).get("typeKey"))
    g = {
        "activityId": a.get("activityId") or listed.get("activityId"),
        "name": a.get("activityName") or listed.get("activityName"),
        "type": typ,
        "startTimeGMT": sm.get("startTimeGMT") or listed.get("startTimeGMT"),
        "startTimeLocal": sm.get("startTimeLocal") or listed.get("startTimeLocal"),
        "duration_s": sm.get("duration") or listed.get("duration"),
        "moving_s": sm.get("movingDuration") or listed.get("movingDuration"),
        "distance_m": sm.get("distance") or listed.get("distance"),
        "avg_power": sm.get("averagePower") or listed.get("avgPower"),
        "max_power": sm.get("maxPower") or listed.get("maxPower"),
        "norm_power": sm.get("normalizedPower") or listed.get("normPower"),
        "avg_cadence": sm.get("averageBikingCadenceInRevPerMinute") or listed.get("averageBikingCadenceInRevPerMinute"),
        "avg_hr": sm.get("averageHR") or listed.get("averageHR"),
        "max_hr": sm.get("maxHR") or listed.get("maxHR"),
        "elevation_gain_m": sm.get("elevationGain") or listed.get("elevationGain"),
        "avg_speed_mps": sm.get("averageSpeed") or listed.get("averageSpeed"),
        "training_stress_score": sm.get("trainingStressScore") or listed.get("trainingStressScore"),
        "intensity_factor": sm.get("intensityFactor") or listed.get("intensityFactor"),
        "device": (a.get("metadataDTO") or {}).get("deviceApplicationInstallationId") and "garmin",
        "laps": _garmin_laps(item.get("splits") or {}),
        "streams": _garmin_streams(item.get("details") or {}),
    }
    return {k: v for k, v in g.items() if v not in (None, [], {}, "")}


def _garmin_start(g: dict) -> datetime | None:
    """Garmin writes '2026-09-17 19:30:00' in lists and '2026-09-17T19:30:00.0' in summaries."""
    s = g.get("startTimeGMT")
    if not s:
        return None
    try:
        return datetime.strptime(s[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _file_start(path: Path) -> datetime | None:
    w = read_json(path, {})
    dt = parse_dt(w.get("start") or "")
    if dt is None and w.get("garmin"):
        dt = _garmin_start(w["garmin"])
    if dt is not None and dt.tzinfo is None:
        dt = dt.replace(tzinfo=now().tzinfo)
    return dt


def find_workout_near(start: datetime, window_min: int = 12) -> Path | None:
    best, best_d = None, None
    for p in config.WORKOUTS.glob("*.json"):
        fs = _file_start(p)
        if fs is None:
            continue
        d = abs((fs - start).total_seconds())
        if d <= window_min * 60 and (best_d is None or d < best_d):
            best, best_d = p, d
    return best


def ingest_garmin(item: dict) -> tuple[Path, bool]:
    """Merge a Garmin activity into the matching workout file, or create one.
    Returns (path, created_new)."""
    g = normalise_garmin(item)
    start = _garmin_start(g) or now()
    path = find_workout_near(start)
    if path:
        w = read_json(path, {})
        w["garmin"] = g
        write_json(path, w)
        log.info("merged Garmin %s into %s", g.get("activityId"), path.name)
        return path, False
    local = start.astimezone(now().tzinfo)
    path = config.WORKOUTS / f"{local.strftime('%Y-%m-%d_%H%M')}_garmin-{_slug(g.get('name') or g.get('type'))}.json"
    write_json(path, {"name": g.get("name"), "start": local.strftime("%Y-%m-%d %H:%M:%S %z"),
                      "source": "garmin", "garmin": g})
    log.info("created %s from Garmin %s", path.name, g.get("activityId"))
    return path, True


def _rolling_best(vals: list, window: int) -> float | None:
    xs = [v if isinstance(v, (int, float)) else 0.0 for v in vals]
    if len(xs) < window or window <= 0:
        return None
    s = sum(xs[:window]); best = s
    for i in range(window, len(xs)):
        s += xs[i] - xs[i - window]
        best = max(best, s)
    return best / window


def garmin_summary(g: dict) -> dict:
    """Compact, prompt-safe view of a Garmin block: key numbers plus stream-derived stats."""
    out = {k: v for k, v in g.items() if k not in ("streams", "laps")}
    for k in ("duration_s", "moving_s", "distance_m", "avg_power", "max_power", "norm_power", "avg_cadence",
              "avg_hr", "max_hr", "elevation_gain_m", "avg_speed_mps", "training_stress_score", "intensity_factor"):
        if isinstance(out.get(k), float):
            out[k] = round(out[k], 1)
    st = g.get("streams") or {}
    t = st.get("time") or st.get("elapsed")
    p = st.get("power")
    if p and t:
        # sample interval from the time stream; Garmin details are usually 1 s or a few s
        ts = [x for x in t if isinstance(x, (int, float))]
        step = max(1.0, (ts[-1] - ts[0]) / max(1, len(ts) - 1)) if len(ts) > 1 else 1.0
        n = len(p)
        q = max(1, n // 4)
        pw = [x if isinstance(x, (int, float)) else 0.0 for x in p]
        hr = [x for x in (st.get("hr") or [None] * n)]
        out["power_by_quarter"] = [round(sum(pw[i*q:(i+1)*q]) / max(1, len(pw[i*q:(i+1)*q]))) for i in range(4)]
        hq = []
        for i in range(4):
            seg = [x for x in hr[i*q:(i+1)*q] if isinstance(x, (int, float))]
            hq.append(round(sum(seg) / len(seg)) if seg else None)
        out["hr_by_quarter"] = hq
        for label, secs in (("best_5min_power", 300), ("best_20min_power", 1200)):
            b = _rolling_best(pw, int(secs / step))
            if b is not None:
                out[label] = round(b)
        bins = {}
        for x in pw:
            b = int(x // 25) * 25
            bins[f"{b}-{b+24}W"] = bins.get(f"{b}-{b+24}W", 0) + 1
        total = sum(bins.values()) or 1
        top = sorted(bins.items(), key=lambda kv: -kv[1])[:8]
        out["power_distribution_pct"] = {k: round(100 * v / total) for k, v in sorted(top, key=lambda kv: int(kv[0].split("-")[0]))}
        zero = sum(1 for x in pw if x == 0)
        out["coasting_pct"] = round(100 * zero / n)
        out["sample_seconds"] = round(step, 1)
    laps = g.get("laps") or []
    if laps:
        out["laps"] = [{k: l.get(k) for k in ("lapIndex", "duration", "distance", "averagePower", "normalizedPower", "averageHR", "averageBikeCadence") if l.get(k) is not None} for l in laps[:40]]
        if len(laps) > 40:
            out["laps_note"] = f"{len(laps)} laps, first 40 shown"
    return out


# --- pending rides: wait briefly for Garmin data before analysing ------------

def add_pending(path: Path) -> None:
    st = read_json(PENDING_FILE, {})
    st[str(path)] = now().timestamp()
    write_json(PENDING_FILE, st)


def pop_pending(path: Path) -> bool:
    st = read_json(PENDING_FILE, {})
    if str(path) in st:
        del st[str(path)]
        write_json(PENDING_FILE, st)
        return True
    return False


def expired_pending() -> list[Path]:
    st = read_json(PENDING_FILE, {})
    cutoff = now().timestamp() - PENDING_WAIT_S
    due = [Path(p) for p, ts in st.items() if ts <= cutoff]
    if due:
        for p in due:
            st.pop(str(p), None)
        write_json(PENDING_FILE, st)
    return due
