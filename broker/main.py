"""Broker: the only process that holds secrets.

Threads: signal-cli reader (inbound -> inbox), outbox watcher (outbox -> Signal),
health webhook. Runs as the coach-svc system user. Never runs a model.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time

from common import queue
from . import garmin, health_webhook, intervals
from .signal_channel import Signal

logging.basicConfig(stream=sys.stdout, level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("broker")


def outbox_loop(sig: Signal) -> None:
    while True:
        for path in queue.pending(queue.OUTBOX):
            item = queue.load(path)
            if not item:
                queue.finish(path, ok=False)
                continue
            try:
                if item.get("kind") == "typing":
                    sig.typing(item.get("to"))
                elif item.get("kind") == "refresh_planned":
                    # the runner is waiting on this; the fetches run off-thread so Signal sends are not held up
                    threading.Thread(target=garmin.SOURCE.fetch_planned_and_queue, args=(item.get("request"),),
                                     daemon=True, name="garmin-planned").start()
                    if intervals.configured():
                        threading.Thread(target=intervals.SOURCE.fetch_planned_and_queue, args=(item.get("request"),),
                                         daemon=True, name="intervals-planned").start()
                elif item.get("kind") == "schedule_workout":
                    if intervals.configured():
                        threading.Thread(target=intervals.SOURCE.schedule, args=(item,), daemon=True, name="intervals-schedule").start()
                    else:
                        queue.enqueue(queue.INBOX, {"kind": "scheduled", "request": item.get("request"), "ok": False,
                                                    "error": "Intervals.icu is not configured on the broker"})
                else:
                    sig.send(item.get("text") or "(empty)", item.get("to"))
                queue.finish(path)
            except Exception as e:
                log.error("send failed for %s: %s", path.name, e)
                time.sleep(5)
        queue.prune()
        time.sleep(1)


def main() -> None:
    os.umask(0o007)
    for d in (queue.INBOX, queue.OUTBOX, queue.DONE):
        d.mkdir(parents=True, exist_ok=True)
    queue.announce(["send", "typing", "refresh_planned", "schedule_workout"] if intervals.configured() else ["send", "typing", "refresh_planned"])
    sig = Signal()
    sig.start()
    threading.Thread(target=outbox_loop, args=(sig,), daemon=True, name="outbox").start()
    threading.Thread(target=health_webhook.serve, daemon=True, name="webhook").start()
    garmin.start(notify_fn=sig.send)
    intervals.start(notify_fn=sig.send)
    sig.read_loop()  # blocks; raises if signal-cli dies, systemd restarts us


if __name__ == "__main__":
    main()
