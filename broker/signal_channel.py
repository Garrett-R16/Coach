"""Signal transport via `signal-cli jsonRpc` over stdio.

One long-lived signal-cli child. Its stdout carries JSON-RPC notifications
(incoming messages) and responses; we write requests to its stdin.
"""
from __future__ import annotations

import base64
import json
import logging
import subprocess
import threading
import uuid
from pathlib import Path

from common import queue
from . import config

log = logging.getLogger("signal")
ATHLETE_FILE = config.STATE / "athlete.json"
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024  # bigger than any phone photo; keeps queue files bounded


def athlete_number() -> str | None:
    if config.SIGNAL_ATHLETE_NUMBER:
        return config.SIGNAL_ATHLETE_NUMBER
    try:
        return json.loads(ATHLETE_FILE.read_text()).get("number")
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def remember_athlete(number: str) -> None:
    ATHLETE_FILE.write_text(json.dumps({"number": number}))
    ATHLETE_FILE.chmod(0o600)


class Signal:
    def __init__(self) -> None:
        if not config.SIGNAL_NUMBER:
            raise SystemExit("host_number not set (Infisical)")
        self._lock = threading.Lock()
        self._pending: dict[str, threading.Event] = {}
        self._results: dict[str, dict] = {}
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        cmd = [config.SIGNAL_CLI, "--config", config.SIGNAL_CONFIG, "-a", config.SIGNAL_NUMBER,
               "jsonRpc", "--ignore-stories", "--ignore-stickers",
               "--send-read-receipts", "--receive-mode", "on-start"]
        log.info("starting signal-cli for %s", config.SIGNAL_NUMBER)
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, bufsize=1)
        threading.Thread(target=self._read_stderr, daemon=True, name="signal-stderr").start()

    def _read_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        for line in self.proc.stderr:
            line = line.rstrip()
            if line:
                log.info("signal-cli: %s", line[:300])

    def _request(self, method: str, params: dict, timeout: float = 60) -> dict:
        assert self.proc and self.proc.stdin
        rid = uuid.uuid4().hex
        ev = threading.Event()
        self._pending[rid] = ev
        with self._lock:
            self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": rid}) + "\n")
            self.proc.stdin.flush()
        if not ev.wait(timeout):
            self._pending.pop(rid, None)
            raise TimeoutError(f"signal-cli {method} timed out")
        return self._results.pop(rid, {})

    def send(self, text: str, to: str | None = None) -> None:
        to = to or athlete_number()
        if not to:
            log.warning("no athlete number yet; dropping message: %s", text[:80])
            return
        res = self._request("send", {"recipient": [to], "message": text})
        if "error" in res:
            raise RuntimeError(f"send failed: {res['error']}")

    def typing(self, to: str | None = None) -> None:
        to = to or athlete_number()
        if to:
            try:
                self._request("sendTyping", {"recipient": [to]}, timeout=10)
            except Exception as e:  # cosmetic
                log.debug("typing failed: %s", e)

    def read_loop(self) -> None:
        """Blocks. Dispatches responses and turns incoming messages into inbox items."""
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                log.warning("non-json from signal-cli: %s", line[:200])
                continue
            if "id" in msg and msg["id"] in self._pending:
                self._results[msg["id"]] = msg
                self._pending.pop(msg["id"]).set()
                continue
            if msg.get("method") == "receive":
                self._on_receive(msg.get("params", {}))
        rc = self.proc.wait()
        raise RuntimeError(f"signal-cli exited with {rc}")

    def _attachments(self, data: dict) -> list[dict]:
        """Read each downloaded attachment (signal-cli stores them under <config>/attachments/<id>)
        and return it base64-encoded so the runner, which cannot read that directory, gets the bytes.
        An attachment that cannot be read is still listed, with "error" set and no "data"."""
        out = []
        for att in data.get("attachments") or []:
            if not isinstance(att, dict):
                continue
            if (att.get("contentType") or "").startswith("text/x-signal-plain"):
                continue  # long-message body, not a real attachment
            item = {"contentType": att.get("contentType"), "filename": att.get("filename"),
                    "size": att.get("size")}
            aid = att.get("id")
            candidates = [Path(att["file"])] if att.get("file") else []
            if aid:
                candidates += sorted(Path(config.SIGNAL_CONFIG).glob(f"attachments/{aid}*"))
            path = next((p for p in candidates if p.is_file()), None)
            if path is None:
                item["error"] = "not downloaded by signal-cli"
                log.warning("attachment %s not found under %s", aid, config.SIGNAL_CONFIG)
            elif path.stat().st_size > MAX_ATTACHMENT_BYTES:
                item["error"] = f"too large ({path.stat().st_size} bytes)"
                log.warning("attachment %s too large: %s", aid, path.stat().st_size)
            else:
                try:
                    item["data"] = base64.b64encode(path.read_bytes()).decode("ascii")
                except OSError as e:
                    item["error"] = str(e)
                    log.warning("attachment %s unreadable: %s", aid, e)
            if path is not None:
                try:
                    path.unlink()  # our copy lives in the queue now
                except OSError:
                    pass
            out.append(item)
        return out

    def _on_receive(self, params: dict) -> None:
        env = params.get("envelope", {})
        data = env.get("dataMessage")
        if not data or not (data.get("message") or data.get("attachments")):
            return  # receipts, typing, sync messages, reactions
        sender = env.get("sourceNumber") or env.get("source")
        text = data.get("message") or ""
        known = athlete_number()
        if not known:
            remember_athlete(sender)
            known = sender
            log.info("adopted athlete number %s", sender)
            self.send("Registered you as the athlete. Send /help for commands.", sender)
        if sender != known:
            log.warning("ignoring message from %s", sender)
            return
        attachments = self._attachments(data)
        log.info("inbound from athlete: %s (%d attachments)", text[:100], len(attachments))
        self.typing(sender)
        queue.enqueue(queue.INBOX, {"kind": "message", "text": text, "from": sender,
                                    "timestamp": data.get("timestamp"), "attachments": attachments})
