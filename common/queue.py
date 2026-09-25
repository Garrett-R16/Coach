"""File queue shared by the broker (secrets side) and the runner (agent side).

Layout under COACH_QUEUE (default /var/lib/coach/queue):
  inbox/   broker -> runner   {"kind": "message"|"health"|"control", ...}
  outbox/  runner -> broker   {"kind": "send"|"typing", "text": ..., "to": ...}
  done/    processed files, pruned after a few days

Files are written to a .tmp name and renamed, so a reader never sees a partial
file. Each side only ever deletes or moves files it has finished with.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

QUEUE = Path(os.environ.get("COACH_QUEUE", "/var/lib/coach/queue"))
INBOX = QUEUE / "inbox"
OUTBOX = QUEUE / "outbox"
DONE = QUEUE / "done"
RETENTION_S = 3 * 24 * 3600


def _name(kind: str) -> str:
    return f"{time.strftime('%Y%m%dT%H%M%S')}_{int(time.time() * 1000) % 1000:03d}_{kind}_{uuid.uuid4().hex[:6]}.json"


def enqueue(directory: Path, obj: dict) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / _name(obj.get("kind", "item"))
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False))
    os.chmod(tmp, 0o660)
    tmp.rename(path)
    return path


def pending(directory: Path) -> list[Path]:
    if not directory.exists():
        return []
    return sorted(p for p in directory.glob("*.json"))


def load(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def finish(path: Path, ok: bool = True) -> None:
    """Move a processed item to done/ (or delete it if that fails)."""
    try:
        DONE.mkdir(parents=True, exist_ok=True)
        target = DONE / (path.stem + ("" if ok else ".failed") + ".json")
        path.rename(target)
    except OSError:
        try:
            path.unlink()
        except OSError:
            pass


def prune() -> None:
    cutoff = time.time() - RETENTION_S
    for p in DONE.glob("*.json") if DONE.exists() else []:
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
        except OSError:
            pass


def send_text(text: str, to: str | None = None) -> Path:
    """Runner side: ask the broker to deliver a message."""
    return enqueue(OUTBOX, {"kind": "send", "text": text, "to": to})
