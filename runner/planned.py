"""Planned (scheduled) workouts from the Garmin Connect calendar.

The broker queues {"kind": "planned", "dates": [...], "items": [{"listed", "workout"}]}
for today and the next seven days, or {"kind": "planned", "dates": [...], "error": "..."}
when the calendar could not be read. Each workout is normalised to a readable step
list and stored as data/planned/YYYY-MM-DD_<slug>.json. Swim distances are in yards,
everything else in metres; durations in seconds; pace as text.

The runner has no Garmin access, so `refresh()` asks the broker (outbox
"refresh_planned") and waits for its answer before a coach run; the hourly poll
on the broker side keeps the files fresh in between.

CLI:  python3 -m runner.planned [YYYY-MM-DD]   render what is stored for a day (default today)
      python3 -m runner.planned refresh        ask the broker now, wait, then render the prompt section
"""
from __future__ import annotations

import json
import re
import sys
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from common import queue
from . import config
from .util import now, read_json, today, write_json

FETCHED_FILE = config.STATE / "planned_fetched.json"
REFRESH_TIMEOUT_S = float(config.env("PLANNED_REFRESH_TIMEOUT", "30"))
WINDOW_DAYS = 8  # today plus the next seven; the broker fetches the same window (GARMIN_PLANNED_DAYS)
YD_PER_M = 1.0936133
SWIM_WORDS = ("swim", "lap_swimming", "open_water")
STEP_LABELS = {"warmup": "warm-up", "cooldown": "cool-down", "interval": "work", "recovery": "recovery",
               "rest": "rest", "other": "step", "main": "main"}


def _slug(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "workout").lower()).strip("-")[:60] or "workout"


def _key(d: dict | None, *names: str):
    for n in names:
        v = (d or {}).get(n)
        if v not in (None, ""):
            return v
    return None


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _is_swim(sport: str | None) -> bool:
    return any(w in (sport or "").lower() for w in SWIM_WORDS)


def _fmt_secs(s: float) -> str:
    s = int(round(s))
    if s >= 3600:
        return f"{s // 3600}h{(s % 3600) // 60:02d}"
    return f"{s // 60}:{s % 60:02d}" if s >= 60 else f"{s}s"


def _pace(mps: float | None, sport: str) -> str | None:
    """m/s -> per 100 yd for swims, per mile for runs, km/h for rides."""
    if not mps or mps <= 0:
        return None
    if _is_swim(sport):
        return _fmt_secs(91.44 / mps) + "/100yd"
    if "cycl" in sport or "bik" in sport:
        return f"{mps * 3.6:.1f}km/h"
    return _fmt_secs(1609.344 / mps) + "/mi"


def _target(step: dict, sport: str) -> str | None:
    t = (_key(step.get("targetType"), "workoutTargetTypeKey") or "no.target").lower()
    lo, hi, zone = _num(step.get("targetValueOne")), _num(step.get("targetValueTwo")), step.get("zoneNumber")
    if t == "no.target":
        return None
    if t.startswith("pace") or t.startswith("speed"):
        if lo and hi:
            a, b = sorted([lo, hi])
            fast, slow = _pace(b, sport), _pace(a, sport)
            return f"pace {fast.split('/')[0]}-{slow}"
        if zone:
            return f"pace zone {zone}"
    if t.startswith("heart.rate"):
        if lo and hi:
            return f"HR {int(lo)}-{int(hi)}bpm"
        if zone:
            return f"HR zone {zone}"
    if t.startswith("power"):
        if lo and hi:
            return f"power {int(lo)}-{int(hi)}W"
        if zone:
            return f"power zone {zone}"
    if t.startswith("cadence"):
        if lo and hi:
            return f"cadence {int(lo)}-{int(hi)}"
    if t.startswith("swim.css") or t.startswith("swim"):
        if lo is not None or hi is not None:
            return f"CSS offset {lo if lo is not None else hi:+.0f}s"
        return "CSS pace"
    parts = [t.replace(".", " ")]
    if lo is not None:
        parts.append(f"{lo:g}")
    if hi is not None:
        parts.append(f"{hi:g}")
    return " ".join(parts)


def _end(step: dict, sport: str, swim_unit: str = "m") -> dict:
    """What ends the step: distance, duration (s), or a text condition.
    Garmin gives a swim step's distance in its preferredEndConditionUnit when one is set
    (a 200 with unit yard is 200 yards), and in metres otherwise; `swim_unit` is the
    workout's pool unit, used to present metre values in the athlete's pool unit."""
    cond = (_key(step.get("endCondition"), "conditionTypeKey") or "").lower()
    val = _num(step.get("endConditionValue"))
    out: dict = {}
    if cond == "distance" and val is not None:
        if _is_swim(sport):
            unit = (_key(step.get("preferredEndConditionUnit"), "unitKey") or "").lower()
            if unit.startswith("yard"):
                out["distance_yd"] = round(val)
            elif unit.startswith("meter") or swim_unit == "m":
                out["distance_m"] = round(val)
            else:
                out["distance_yd"] = round(val * YD_PER_M)
        else:
            out["distance_m"] = round(val)
    elif cond in ("time", "fixed.rest") and val is not None:
        out["duration_s"] = round(val)
    elif cond == "lap.button":
        out["until"] = "lap button"
    elif cond in ("iterations", "reps", "fixed.repetition") and val is not None:
        out["reps"] = int(val)
    elif cond:
        out["until"] = cond.replace(".", " ") + (f" {val:g}" if val is not None else "")
    return out


_RPE_RE = re.compile(r"rpe\s*(\d+)\s*(?:-\s*(\d+))?", re.I)


def _looks_like_rest(step: dict, note: str | None) -> bool:
    """Coaches often build swim rests as timed 'interval' steps at RPE 1. Treat a timed step
    with no distance, no target, and an RPE ceiling of 2 or less (or 60 s or shorter with no RPE) as rest."""
    if "duration_s" not in step or "distance_yd" in step or "distance_m" in step or step.get("target"):
        return False
    m = _RPE_RE.search(note or "")
    if m:
        hi = int(m.group(2) or m.group(1))
        return hi <= 2
    return step["duration_s"] <= 60


def _steps(raw: list, sport: str, swim_unit: str = "m") -> list[dict]:
    out = []
    for s in raw or []:
        if not isinstance(s, dict):
            continue
        kind = (_key(s.get("stepType"), "stepTypeKey") or "").lower()
        if s.get("type") == "RepeatGroupDTO" or kind == "repeat" or s.get("workoutSteps"):
            n = s.get("numberOfIterations") or _num(s.get("endConditionValue")) or 1
            out.append({"type": "repeat", "reps": int(n), "steps": _steps(s.get("workoutSteps") or [], sport, swim_unit)})
            continue
        step: dict = {"type": STEP_LABELS.get(kind, kind or "step")}
        step.update(_end(s, sport, swim_unit))
        if kind == "interval" and _looks_like_rest(step, s.get("description")):
            step["type"] = "rest"
        tgt = _target(s, sport)
        if tgt:
            step["target"] = tgt
        stroke = (_key(s.get("strokeType"), "strokeTypeKey") or "").lower()
        if stroke and stroke not in ("any_stroke", "any", "none"):
            step["stroke"] = stroke
        equip = (_key(s.get("equipmentType"), "equipmentTypeKey") or "").lower()
        if equip and equip != "none":
            step["equipment"] = equip
        if s.get("description"):
            step["note"] = s["description"]
        if s.get("exerciseName"):
            step["exercise"] = s["exerciseName"]
        out.append(step)
    # fold a rest step that directly follows a work step into that step
    folded: list[dict] = []
    for step in out:
        if step.get("type") == "rest" and folded and folded[-1].get("type") not in ("rest", "repeat") \
                and "rest" not in folded[-1] and set(step) <= {"type", "duration_s", "until", "distance_yd", "distance_m", "note"}:
            folded[-1]["rest"] = step.get("duration_s") if "duration_s" in step else step.get("until") or \
                f"{step.get('distance_yd') or step.get('distance_m')}"
            continue
        folded.append(step)
    return folded


def _any_yard_steps(w: dict) -> bool:
    def walk(steps):
        for st in steps or []:
            if isinstance(st, dict):
                if (_key(st.get("preferredEndConditionUnit"), "unitKey") or "").lower().startswith("yard"):
                    return True
                if walk(st.get("workoutSteps")):
                    return True
        return False
    return any(walk((seg or {}).get("workoutSteps")) for seg in w.get("workoutSegments") or [])


def normalise(item: dict) -> dict:
    listed, w = item.get("listed") or {}, item.get("workout") or {}
    sport = (listed.get("sportTypeKey") or _key(w.get("sportType"), "sportTypeKey") or
             listed.get("activityTypeKey") or "unknown").lower()
    pool = _num(w.get("poolLength"))  # in poolLengthUnit (25 with unit yard is a 25-yard pool)
    pool_unit = (_key(w.get("poolLengthUnit"), "unitKey") or "").lower()
    pool_unit = "yd" if pool_unit.startswith("yard") else ("m" if pool_unit.startswith("meter") else "")
    swim_unit = pool_unit or ("yd" if _any_yard_steps(w) else "m")
    steps: list[dict] = []
    for seg in w.get("workoutSegments") or []:
        steps += _steps((seg or {}).get("workoutSteps") or [], sport, swim_unit)
    out = {
        "date": listed.get("date"),
        "name": listed.get("title") or w.get("workoutName"),
        "sport": sport,
        "source": "garmin-calendar",
        "scheduled_id": listed.get("id"),
        "workout_id": listed.get("workoutId") or w.get("workoutId"),
        "description": w.get("description") or listed.get("description"),
        "estimated_duration_s": w.get("estimatedDurationInSecs"),
        "estimated_distance": (round(_num(w["estimatedDistanceInMeters"]) * YD_PER_M) if _is_swim(sport) and swim_unit == "yd"
                               else round(_num(w["estimatedDistanceInMeters"]))) if _num(w.get("estimatedDistanceInMeters")) else None,
        "distance_unit": swim_unit if _is_swim(sport) else "m",
        "pool_length": f"{pool:g} {pool_unit or 'm'}" if pool else None,
        "steps": steps,
        "detail_error": item.get("error"),  # the calendar listed it but the step detail could not be fetched
        "fetched_at": item.get("fetched_at"),
        "garmin": {"listed": listed, "workout_updated": w.get("updatedDate") or w.get("updateDate")},
    }
    return {k: v for k, v in out.items() if v not in (None, "", [], {})}


def ingest(item: dict) -> dict:
    """Write the window's planned workouts to data/planned/, remove files for dates in the
    window that no longer have a workout, and remember when the calendar was last checked.
    An item carrying "error" leaves the files alone and records the failure instead."""
    dates = item.get("dates") or []
    stamp = item.get("fetched_at") or now().isoformat(timespec="seconds")
    st = read_json(FETCHED_FILE, {})
    if item.get("error"):
        st["last_error"] = {"at": stamp, "dates": dates, "error": str(item["error"]), "request": item.get("request")}
        write_json(FETCHED_FILE, st)
        return {"written": [], "unchanged": [], "removed": [], "error": str(item["error"])}
    keep: set[Path] = set()
    written, same = [], []
    for raw in item.get("items") or []:
        p = normalise(raw)
        if not p.get("date"):
            continue
        path = config.PLANNED / f"{p['date']}_{_slug(p.get('name'))}.json"
        prev = read_json(path, None)
        if prev is not None:
            p["fetched_at"] = prev.get("fetched_at")  # compare content, not the fetch time
        if prev == p:
            same.append(path.name)
        else:
            p["fetched_at"] = stamp
            write_json(path, p)
            written.append(path.name)
        keep.add(path)
    removed = []
    for path in config.PLANNED.glob("*.json"):
        if path.name[:10] in dates and path not in keep:
            path.unlink()
            removed.append(path.name)
    write_json(FETCHED_FILE, {"at": stamp, "dates": dates, "request": item.get("request")})  # a success clears any earlier failure
    return {"written": written, "unchanged": same, "removed": removed}


# --- Intervals.icu planned workouts (events with workout_doc) -------------------

def _icu_target(step: dict) -> str | None:
    bits = []
    pw = step.get("power")
    if isinstance(pw, dict):
        u = (pw.get("units") or "").replace("%ftp", "% FTP").replace("w", " W") if pw.get("units") else ""
        if pw.get("start") is not None and pw.get("end") is not None:
            bits.append(f"ramp {pw['start']:g}-{pw['end']:g}{u}")
        elif pw.get("value") is not None:
            bits.append(f"{pw['value']:g}{u}")
        if pw.get("watts") is not None:
            bits.append(f"({pw['watts']:g} W)")
    hr = step.get("hr")
    if isinstance(hr, dict) and hr.get("value") is not None:
        bits.append(f"HR {hr['value']:g}{hr.get('units', '')}")
    pace = step.get("pace")
    if isinstance(pace, dict) and pace.get("value") is not None:
        bits.append(f"pace {pace['value']:g}{pace.get('units', '')}")
    cad = step.get("cadence")
    if isinstance(cad, dict) and cad.get("value") is not None:
        bits.append(f"{cad['value']:g} rpm")
    return " ".join(bits) or None


def _icu_steps(raw: list) -> list[dict]:
    out = []
    for st in raw or []:
        if not isinstance(st, dict):
            continue
        if st.get("reps") and st.get("steps"):
            out.append({"type": "repeat", "reps": int(st["reps"]), "steps": _icu_steps(st["steps"])})
            continue
        kind = "warm-up" if st.get("warmup") else "cool-down" if st.get("cooldown") else "rest" if st.get("rest") or (
            isinstance(st.get("power"), dict) and (st["power"].get("value") or 100) <= 55 and not st.get("text")) else "work"
        step: dict = {"type": kind}
        if st.get("duration") is not None:
            step["duration_s"] = round(float(st["duration"]))
        if st.get("distance") is not None:
            step["distance_m"] = round(float(st["distance"]))
        tgt = _icu_target(st)
        if tgt:
            step["target"] = tgt
        if st.get("text"):
            step["note"] = str(st["text"])[:120]
        if st.get("freeride") or st.get("free"):
            step["note"] = (step.get("note", "") + " free ride").strip()
        out.append(step)
    return out


def normalise_icu_event(e: dict, fetched_at: str | None) -> dict:
    doc = e.get("workout_doc") or {}
    out = {
        "date": (e.get("start_date_local") or "")[:10],
        "name": e.get("name"),
        "sport": (e.get("type") or "unknown").lower(),
        "source": "intervals",
        "event_id": e.get("id"),
        "external_id": e.get("external_id"),
        "description": (doc.get("description") or e.get("description") or "")[:600] or None,
        "estimated_duration_s": e.get("moving_time") or doc.get("duration"),
        "training_load": e.get("icu_training_load"),
        "intensity": e.get("icu_intensity"),
        "indoor": e.get("indoor"),
        "distance_unit": "m",
        "steps": _icu_steps(doc.get("steps") or []),
        "fetched_at": fetched_at,
    }
    return {k: v for k, v in out.items() if v not in (None, "", [], {})}


def ingest_intervals(item: dict) -> dict:
    """Write Intervals.icu planned workouts to data/planned/<date>_icu-<slug>.json and drop stale ones."""
    dates = item.get("dates") or []
    stamp = item.get("fetched_at") or now().isoformat(timespec="seconds")
    if item.get("error"):
        return {"written": [], "unchanged": [], "removed": [], "error": str(item["error"])}
    keep: set[Path] = set(); written, same = [], []
    for e in item.get("items") or []:
        p = normalise_icu_event(e, stamp)
        if not p.get("date"):
            continue
        path = config.PLANNED / f"{p['date']}_icu-{_slug(p.get('name'))}.json"
        prev = read_json(path, None)
        if prev is not None:
            p["fetched_at"] = prev.get("fetched_at")
        if prev == p:
            same.append(path.name)
        else:
            p["fetched_at"] = stamp
            write_json(path, p)
            written.append(path.name)
        keep.add(path)
    removed = []
    for path in config.PLANNED.glob("*_icu-*.json"):
        if path.name[:10] in dates and path not in keep:
            path.unlink(); removed.append(path.name)
    return {"written": written, "unchanged": same, "removed": removed}


def _ts(s: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(s) if s else None
    except ValueError:
        return None


def refresh(timeout_s: float = REFRESH_TIMEOUT_S) -> dict:
    """Ask the broker to read the calendar now and wait for its answer, ingesting it here so the
    prompt built next sees fresh files. Only the "planned" inbox items are touched; a reply older
    than the request (the hourly poll) is ingested but not accepted as the answer. If nothing
    arrives in time the failure is recorded so the prompt can say so."""
    asked = now().isoformat(timespec="seconds")
    if not queue.broker_supports("refresh_planned"):
        # an older deployed broker would send the request to the phone as a message; do not ask
        return ingest({"kind": "planned", "dates": [], "fetched_at": asked,
                       "error": "the broker is running code without calendar support; run sudo scripts/install-broker.sh"})
    req = uuid.uuid4().hex[:8]
    queue.enqueue(queue.OUTBOX, {"kind": "refresh_planned", "at": asked, "request": req})
    deadline = time.monotonic() + timeout_s
    while True:
        for p in queue.pending(queue.INBOX):
            if "_planned_" not in p.name:
                continue
            item = queue.load(p)
            if not item:
                queue.finish(p, ok=False)
                continue
            r = ingest(item)  # the broker echoes "request"; the hourly poll carries none
            queue.finish(p)
            if item.get("request") == req:
                return r
        # another runner process (the CLI next to the service) may have taken our answer
        st = read_json(FETCHED_FILE, {})
        if st.get("request") == req:
            return {"written": [], "unchanged": [], "removed": []}
        if (st.get("last_error") or {}).get("request") == req:
            return {"written": [], "unchanged": [], "removed": [], "error": st["last_error"].get("error")}
        if time.monotonic() >= deadline:
            break
        time.sleep(0.5)
    err = (f"no answer from the broker within {int(timeout_s)}s; it may be down or running code without "
           f"calendar support (sudo scripts/install-broker.sh)")
    return ingest({"kind": "planned", "dates": [], "error": err, "fetched_at": now().isoformat(timespec="seconds")})


def files_for(day: str) -> list[Path]:
    files = sorted(config.PLANNED.glob(f"{day}_*.json"))
    icu_names = {_slug(read_json(f, {}).get("name")) for f in files if "_icu-" in f.name}
    out = []
    for f in files:
        if "_icu-" not in f.name and _slug(read_json(f, {}).get("name")) in icu_names:
            continue  # Garmin's copy of a workout Intervals.icu pushed there
        out.append(f)
    return out


def _step_text(step: dict, unit: str, indent: str = "") -> list[str]:
    if step.get("type") == "repeat":
        lines = [f"{indent}{step.get('reps')}x:"]
        for s in step.get("steps") or []:
            lines += _step_text(s, unit, indent + "  ")
        return lines
    bits = []
    if "distance_yd" in step:
        bits.append(f"{step['distance_yd']}{step.get('distance_unit', 'yd')}")
    elif "distance_m" in step:
        bits.append(f"{step['distance_m']}m")
    if "duration_s" in step:
        bits.append(_fmt_secs(step["duration_s"]))
    if "until" in step:
        bits.append(f"until {step['until']}")
    if "reps" in step:
        bits.append(f"{step['reps']} reps")
    for k in ("stroke", "equipment", "exercise", "target"):
        if step.get(k):
            bits.append(str(step[k]))
    if "rest" in step:
        r = step["rest"]
        bits.append(f"rest {_fmt_secs(r) if isinstance(r, (int, float)) else r}")
    if step.get("note"):
        bits.append(f"({step['note']})")
    return [f"{indent}{step.get('type')}: " + " ".join(bits)]


def _head(p: dict) -> str:
    head = f"{p.get('date')} {p.get('sport')}: {p.get('name')}"
    extras = []
    if p.get("estimated_distance"):
        extras.append(f"{p['estimated_distance']}{p.get('distance_unit', '')} total")
    if p.get("estimated_duration_s"):
        extras.append(f"~{_fmt_secs(p['estimated_duration_s'])}")
    if p.get("pool_length"):
        extras.append(f"pool {p['pool_length']}")
    if extras:
        head += " (" + ", ".join(extras) + ")"
    return head


def render(p: dict) -> str:
    lines = [_head(p)]
    if p.get("description"):
        lines.append(f"  note: {p['description']}")
    for s in p.get("steps") or []:
        lines += _step_text(s, p.get("distance_unit", "m"), "  ")
    if not p.get("steps"):
        lines.append(f"  (step detail could not be fetched from Garmin: {p['detail_error']})" if p.get("detail_error")
                     else "  (no step detail)")
    return "\n".join(lines)


def _status() -> tuple[dict, dict | None]:
    """(last successful fetch {at, dates}, last failure {at, error} if it is newer than the success)."""
    st = read_json(FETCHED_FILE, {})
    err = st.get("last_error")
    ok_at, err_at = _ts(st.get("at")), _ts((err or {}).get("at"))
    if err and (ok_at is None or (err_at and err_at > ok_at)):
        return st, err
    return st, None


def day_text(day: str) -> str:
    """A day's planned workouts in full, or one explicit line: none on the calendar, fetch failed, or not fetched."""
    files = files_for(day)
    st, err = _status()
    if files:
        text = "\n".join(render(read_json(f, {})) + f"\n  file: data/planned/{f.name}" for f in files)
        if err:
            text += (f"\n  (stored copy from {str(st.get('at', '?'))[:16]}; the latest calendar fetch at "
                     f"{str(err.get('at', '?'))[11:16]} failed: {err.get('error')})")
        return text
    if err:
        tail = (f"; the last successful check at {str(st.get('at'))[:16]} found none" if day in (st.get("dates") or [])
                else "")
        return f"fetch failed for {day}: {err.get('error')} (at {str(err.get('at', '?'))[11:16]}{tail})"
    if day in (st.get("dates") or []):
        return f"none scheduled for {day} on Garmin or Intervals.icu (checked {str(st.get('at', '?'))[:16]})"
    return f"unknown for {day}: the Garmin calendar has not been fetched for that day yet"


def day_brief(day: str) -> str:
    """A day's planned workouts as one line each (the steps stay in the file), or day_text's one explicit line."""
    wd = datetime.fromisoformat(day).strftime("%a")
    files = files_for(day)
    if not files:
        return f"{wd}: {day_text(day)}"
    st, err = _status()
    stale = f" (stored copy from {str(st.get('at', '?'))[:16]}; the latest calendar fetch failed)" if err else ""
    return "\n".join(f"{wd} {_head(read_json(f, {}))} - file: data/planned/{f.name}{stale}" for f in files)


def prompt_section(full_days: int = 2, brief_days: int = WINDOW_DAYS - 2) -> str:
    """Today's and tomorrow's planned workouts in full (the athlete asks about tomorrow's set the
    evening before), then one line per workout for the rest of the fetched week."""
    t = today()
    out = ["Scheduled workouts on the athlete's Garmin Connect calendar (club sets land here before they are swum). "
           "Reconcile these with plan/current.md: if a club set is scheduled, it replaces or shapes that day's "
           "session in the sport; say what differs. A 'fetch failed' line means the calendar could not be read "
           "just now; tell the athlete if it matters to the question. Today and tomorrow are shown in full; the "
           "days after are one line per workout, and the file named on the line has the full steps: read it "
           "before you describe or judge that session."]
    for i in range(full_days):
        d = (t + timedelta(days=i)).isoformat()
        label = "Today" if i == 0 else "Tomorrow" if i == 1 else d
        out.append(f"{label} ({d}):\n" + day_text(d))
    if brief_days > 0:
        out.append("The following days:")
    for i in range(full_days, full_days + brief_days):
        out.append(day_brief((t + timedelta(days=i)).isoformat()))
    return "\n".join(out)


def main(argv: list[str]) -> int:
    if argv and argv[0] == "refresh":
        r = refresh()
        print(f"refresh: {r}\n")
        print(prompt_section())
        return 0 if not r.get("error") else 1
    day = argv[0] if argv else today().isoformat()
    print(day_text(day))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
