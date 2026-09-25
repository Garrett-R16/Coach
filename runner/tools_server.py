"""MCP tool server the coach calls instead of editing files.

Spoken over stdio (newline-delimited JSON-RPC) by `claude -p --mcp-config`.
Every tool writes to a fixed path under the repo; the model never supplies a path.
Stdlib only.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path

from . import config

MAX_CHARS = 40_000
PLAN_FILES = {"current": config.PLAN / "current.md", "progression": config.PLAN / "progression.md"}
CONTEXT_FILES = {"profile": config.ATHLETE / "profile.md", "schedule": config.ATHLETE / "schedule.md"}
BUILD_REQUEST = config.STATE / "build_request.json"
REPLY_FILE = config.STATE / "reply.json"


def _now() -> datetime:
    return datetime.now().astimezone()


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text if text.endswith("\n") else text + "\n")
    tmp.replace(path)


def _check(content: str, field: str = "content") -> None:
    if not isinstance(content, str) or not content.strip():
        raise ValueError(f"{field} must be a non-empty string")
    if len(content) > MAX_CHARS:
        raise ValueError(f"{field} is too long ({len(content)} chars, max {MAX_CHARS})")


# --- tools -----------------------------------------------------------------

def append_log(entry: str) -> str:
    """Append one entry to today's log."""
    _check(entry, "entry")
    day = _now().strftime("%Y-%m-%d")
    path = config.LOG / f"{day}.md"
    existing = path.read_text() if path.exists() else f"# {day}\n\n"
    line = f"- {_now().strftime('%H:%M')} {entry.strip()}\n"
    _atomic_write(path, existing.rstrip("\n") + "\n" + line)
    return f"appended to log/{day}.md"


FEEDBACK_HEADER = "## Coach notes and athlete feedback"


def note_feedback(note: str) -> str:
    """Append a dated note to the feedback section at the end of context/profile.md."""
    _check(note, "note")
    path = CONTEXT_FILES["profile"]
    text = path.read_text() if path.exists() else "# Athlete profile\n"
    if FEEDBACK_HEADER not in text:
        text = text.rstrip("\n") + f"\n\n{FEEDBACK_HEADER}\n"
    line = f"- {_now().strftime('%Y-%m-%d')}: {note.strip()}\n"
    _atomic_write(path, text.rstrip("\n") + "\n" + line)
    return "noted in context/profile.md"


def write_plan(which: str, content: str, reason: str) -> str:
    """Replace plan/current.md or plan/progression.md. Logs the reason."""
    if which not in PLAN_FILES:
        raise ValueError(f"which must be one of {sorted(PLAN_FILES)}")
    _check(content); _check(reason, "reason")
    _atomic_write(PLAN_FILES[which], content)
    append_log(f"plan/{which}.md rewritten: {reason.strip()}")
    return f"wrote plan/{which}.md ({len(content)} chars) and logged the reason"


def update_context(which: str, content: str, reason: str) -> str:
    """Replace context/profile.md or context/schedule.md. Logs the reason."""
    if which not in CONTEXT_FILES:
        raise ValueError(f"which must be one of {sorted(CONTEXT_FILES)}")
    _check(content); _check(reason, "reason")
    _atomic_write(CONTEXT_FILES[which], content)
    append_log(f"context/{which}.md updated: {reason.strip()}")
    return f"wrote context/{which}.md ({len(content)} chars) and logged the reason"


def add_knowledge(name: str, content: str) -> str:
    """Save a reference note under knowledge/<name>.md."""
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    if not slug:
        raise ValueError("name must contain letters or digits")
    _check(content)
    _atomic_write(config.KNOWLEDGE / f"{slug}.md", content)
    return f"wrote knowledge/{slug}.md"


def reply(text: str) -> str:
    """Send this text to the athlete's phone. Call exactly once, as your last action. Only this text is delivered."""
    _check(text, "text")
    REPLY_FILE.write_text(json.dumps({"text": text.strip(), "at": _now().isoformat(timespec="seconds")}))
    return "queued for delivery; you are done, stop here"


def request_build(spec: str) -> str:
    """Ask the builder agent to add or change harness functionality after this run."""
    _check(spec, "spec")
    BUILD_REQUEST.write_text(json.dumps({"spec": spec.strip(), "at": _now().isoformat(timespec="seconds")}))
    return "build request recorded; the builder runs after you reply"


TOOLS = {
    "append_log": (append_log, {
        "type": "object", "properties": {"entry": {"type": "string", "description": "One factual entry: what triggered you, what you concluded, what changed and why."}},
        "required": ["entry"], "additionalProperties": False}),
    "note_feedback": (note_feedback, {
        "type": "object", "properties": {"note": {"type": "string", "description": "Short, specific: what the athlete reported (feel, soreness, pain, sleep, illness, stress, what is working) or feedback on the coaching."}},
        "required": ["note"], "additionalProperties": False}),
    "write_plan": (write_plan, {
        "type": "object", "properties": {
            "which": {"type": "string", "enum": ["current", "progression"]},
            "content": {"type": "string", "description": "Full new file contents (markdown). Read the file first and preserve what should stay."},
            "reason": {"type": "string", "description": "One line: why the plan changed."}},
        "required": ["which", "content", "reason"], "additionalProperties": False}),
    "update_context": (update_context, {
        "type": "object", "properties": {
            "which": {"type": "string", "enum": ["profile", "schedule"]},
            "content": {"type": "string", "description": "Full new file contents (markdown)."},
            "reason": {"type": "string", "description": "One line: what the athlete told you."}},
        "required": ["which", "content", "reason"], "additionalProperties": False}),
    "add_knowledge": (add_knowledge, {
        "type": "object", "properties": {"name": {"type": "string"}, "content": {"type": "string"}},
        "required": ["name", "content"], "additionalProperties": False}),
    "reply": (reply, {
        "type": "object", "properties": {"text": {"type": "string", "description": "The complete message for the athlete. Plain text for a phone."}},
        "required": ["text"], "additionalProperties": False}),
    "request_build": (request_build, {
        "type": "object", "properties": {"spec": {"type": "string", "description": "One paragraph specification for the builder agent."}},
        "required": ["spec"], "additionalProperties": False}),
}


# --- MCP over stdio --------------------------------------------------------

def _tool_list() -> list[dict]:
    return [{"name": n, "description": (fn.__doc__ or "").strip(), "inputSchema": schema}
            for n, (fn, schema) in TOOLS.items()]


def _handle(msg: dict) -> dict | None:
    method, mid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": params.get("protocolVersion", "2025-06-18"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "coach", "version": "1.0"}}}
    if method == "notifications/initialized" or mid is None:
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": _tool_list()}}
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        if name not in TOOLS:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown tool {name}"}}
        try:
            text = TOOLS[name][0](**args)
            return {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": text}], "isError": False}}
        except Exception as e:  # report to the model, never crash the server
            return {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": f"error: {e}"}], "isError": True}}
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unsupported method {method}"}}


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = _handle(msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
