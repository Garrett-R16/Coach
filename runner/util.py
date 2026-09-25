from __future__ import annotations

import json
import logging
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config


def setup_logging(name: str) -> logging.Logger:
    logging.basicConfig(
        stream=sys.stdout,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return logging.getLogger(name)


def tz():
    return ZoneInfo(config.COACH_TZ) if config.COACH_TZ else None


def now() -> datetime:
    return datetime.now(tz()) if tz() else datetime.now().astimezone()


def today() -> date:
    return now().date()


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True, default=str))
    tmp.replace(path)


def read_text(path: Path, default: str = "") -> str:
    try:
        return path.read_text()
    except FileNotFoundError:
        return default
