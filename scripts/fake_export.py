#!/usr/bin/env python3
"""POST a sample Health Auto Export payload to the local webhook, to test without the phone.

usage: python3 scripts/fake_export.py [--workout] [--url http://localhost:8787]
"""
import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from runner import config  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--workout", action="store_true")
ap.add_argument("--url", default="http://localhost:8787")
a = ap.parse_args()

now = datetime.now().astimezone()
y = (now - timedelta(days=1)).replace(hour=7, minute=0, second=0)
fmt = "%Y-%m-%d %H:%M:%S %z"
payload = {"data": {"metrics": [
    {"name": "resting_heart_rate", "units": "count/min", "data": [{"date": y.strftime(fmt), "qty": 48}]},
    {"name": "heart_rate_variability", "units": "ms", "data": [{"date": y.strftime(fmt), "qty": 62}]},
    {"name": "step_count", "units": "count", "data": [{"date": y.strftime(fmt), "qty": 4100}, {"date": (y + timedelta(hours=6)).strftime(fmt), "qty": 5200}]},
    {"name": "heart_rate", "units": "count/min", "data": [{"date": y.strftime(fmt), "Min": 44, "Avg": 61, "Max": 158}]},
    {"name": "sleep_analysis", "units": "hr", "data": [{"date": y.strftime(fmt), "asleep": 7.1, "inBed": 7.8, "deep": 1.2, "rem": 1.6, "core": 4.3, "awake": 0.4}]},
], "workouts": []}}
if a.workout:
    s = now - timedelta(hours=2)
    payload["data"]["workouts"].append({
        "name": "Outdoor Run", "start": s.strftime(fmt), "end": (s + timedelta(minutes=50)).strftime(fmt),
        "duration": 3000, "distance": {"qty": 10.2, "units": "km"}, "activeEnergyBurned": {"qty": 620, "units": "kcal"},
        "heartRateData": [{"date": (s + timedelta(minutes=i)).strftime(fmt), "Avg": 140 + (i % 15)} for i in range(50)],
    })
req = urllib.request.Request(a.url, data=json.dumps(payload).encode(),
                             headers={"Content-Type": "application/json", "X-Coach-Token": os.environ.get("HEALTH_WEBHOOK_TOKEN", "")})
with urllib.request.urlopen(req, timeout=30) as r:
    print(r.status, r.read().decode())
