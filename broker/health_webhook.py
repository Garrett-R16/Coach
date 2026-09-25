"""HTTP receiver for Health Auto Export. POST JSON with header X-Coach-Token.

Checks the token and drops the payload in the inbox. The runner does the rest.
"""
from __future__ import annotations

import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from common import queue
from . import config, garmin

log = logging.getLogger("health_webhook")
MAX_BODY = 50 * 1024 * 1024


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log.info("%s %s", self.address_string(), fmt % args)

    def _reply(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._reply(200, {"ok": True, "service": "coach-health-webhook"})

    def do_POST(self):
        token = ""
        for name in ("X-Coach-Token", "Coach-Key", "Coach_Key", "Coach-Token"):
            token = (self.headers.get(name) or "").strip()
            if token:
                break
        if not token:
            token = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if not config.HEALTH_WEBHOOK_TOKEN or token != config.HEALTH_WEBHOOK_TOKEN:
            names = [k for k in self.headers.keys() if k.lower() not in ("host", "content-length", "content-type")]
            log.warning("rejected POST from %s (bad token): got %d chars, expected %d; headers present: %s",
                        self.address_string(), len(token), len(config.HEALTH_WEBHOOK_TOKEN or ""), names)
            return self._reply(401, {"ok": False, "error": "bad token"})
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY:
            return self._reply(400, {"ok": False, "error": "bad content length"})
        body = self.rfile.read(length)
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as e:
            log.warning("bad json: %s; first bytes: %r", e, body[:200])
            return self._reply(400, {"ok": False, "error": "invalid json"})
        if not isinstance(payload, dict):
            return self._reply(400, {"ok": False, "error": "expected a JSON object"})
        path = queue.enqueue(queue.INBOX, {"kind": "health", "payload": payload})
        log.info("queued health export %s (%d bytes)", path.name, length)
        self._reply(200, {"ok": True})
        try:
            n = garmin.SOURCE.request_for_payload(payload)
            if n:
                log.info("requested Garmin data for %d ride(s)", n)
        except Exception as e:
            log.warning("garmin request failed: %s", e)


def serve() -> None:
    if not config.HEALTH_WEBHOOK_TOKEN:
        log.error("HEALTH_WEBHOOK_TOKEN not set; webhook disabled")
        return
    srv = ThreadingHTTPServer((config.HEALTH_WEBHOOK_BIND, config.HEALTH_WEBHOOK_PORT), Handler)
    log.info("listening on %s:%s", config.HEALTH_WEBHOOK_BIND, config.HEALTH_WEBHOOK_PORT)
    srv.serve_forever()
