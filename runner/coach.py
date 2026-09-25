"""Invoke the coach (or builder) as a headless Claude Code run.

The coach is files-only: it may read the repo and write under context/, plan/, log/.
The builder has code-editing tools and is only started on explicit request.
Runs are serialised with a file lock because two processes (runner service, morning
timer) can both trigger them.
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import config
from .util import read_json, today, write_json, now

log = logging.getLogger("coach")

SESSION_FILE = config.STATE / "session.json"
LOCK_FILE = config.STATE / "coach.lock"
RUN_LOG = config.STATE / "runs.jsonl"
TIMEOUT_S = 20 * 60

COACH_TOOLS = ["Read", "Glob", "Grep"]  # no Write, no Edit, no Bash: writes go through the tool server
_COACH_DIRS = ["context", "plan", "log", "knowledge", "data", "inbox"]
COACH_MCP_TOOLS = ["reply", "append_log", "note_feedback", "write_plan", "update_context", "add_knowledge", "request_build"]
COACH_ALLOWED = (
    ["Glob", "Grep", "Read(README.md)", "Read(CLAUDE.md)"]
    + [f"Read({d}/**)" for d in _COACH_DIRS]
    + [f"mcp__coach__{t}" for t in COACH_MCP_TOOLS]
)
COACH_MCP_CONFIG = json.dumps({"mcpServers": {"coach": {
    "command": "/usr/bin/python3", "args": ["-m", "runner.tools_server"],
    "env": {"PYTHONPATH": str(config.ROOT), "COACH_DATA": str(config.DATA_ROOT)}}}})
BUILD_REQUEST = config.STATE / "build_request.json"
REPLY_FILE = config.STATE / "reply.json"

BUILDER_ALLOWED = [
    "Read", "Glob", "Grep", "Write", "Edit",
    "Bash(python3 *)", "Bash(python *)", "Bash(pip *)", "Bash(pip3 *)",
    "Bash(git *)", "Bash(ls *)", "Bash(cat *)", "Bash(mkdir *)", "Bash(chmod *)",
    "Bash(systemctl --user *)", "Bash(journalctl --user *)",
    "Bash(curl *)", "Bash(grep *)", "Bash(sed *)", "Bash(head *)", "Bash(tail *)",
]


@dataclass
class Result:
    text: str
    session_id: str | None
    ok: bool
    build_request: str | None = None
    raw: dict | None = None


def _session_for_today() -> str | None:
    st = read_json(SESSION_FILE, {})
    if st.get("date") == today().isoformat():
        return st.get("session_id")
    return None


def _remember_session(session_id: str | None) -> None:
    if session_id:
        write_json(SESSION_FILE, {"date": today().isoformat(), "session_id": session_id})


def reset_session() -> None:
    if SESSION_FILE.exists():
        SESSION_FILE.unlink()


def _base_cmd(system_prompt_file: Path, model: str | None) -> list[str]:
    cmd = [config.CLAUDE_BIN, "-p", "--output-format", "json",
           "--permission-prompts", "none",
           "--append-system-prompt", system_prompt_file.read_text()]
    if model:
        cmd += ["--model", model]
    return cmd


def _run(cmd: list[str], prompt: str, cwd: Path | None = None) -> tuple[int, str, str]:
    # minimal environment: the runner has no secrets, but keep the child clean anyway
    keep = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "TZ", "XDG_CONFIG_HOME", "XDG_RUNTIME_DIR")
    env = {k: v for k, v in os.environ.items() if k in keep or k.startswith("CLAUDE_") or k.startswith("ANTHROPIC_")}
    env.setdefault("HOME", str(Path.home()))
    flags = [c for c in cmd[1:] if c.startswith("--") or c in ("-p",)]
    log.info("claude run: %s (prompt %d chars)", " ".join(flags), len(prompt))
    proc = subprocess.run(
        cmd, input=prompt, text=True, capture_output=True,
        cwd=cwd or config.ROOT, env=env, timeout=TIMEOUT_S,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _parse(stdout: str) -> dict:
    stdout = stdout.strip()
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        # stream-ish output or noise before JSON: take the last JSON object line
        for line in reversed(stdout.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    continue
    return {"result": stdout, "is_error": True}


def _record(kind: str, trigger: str, data: dict, rc: int) -> None:
    with RUN_LOG.open("a") as f:
        f.write(json.dumps({
            "at": now().isoformat(timespec="seconds"), "kind": kind, "trigger": trigger, "rc": rc,
            "session_id": data.get("session_id"), "cost_usd": data.get("total_cost_usd"),
            "turns": data.get("num_turns"), "duration_ms": data.get("duration_ms"),
            "is_error": data.get("is_error"),
        }) + "\n")


def _git_commit(msg: str) -> None:
    """Commit the coach's own files in the data repo (never code)."""
    repo = config.DATA_ROOT
    if not (repo / ".git").exists():
        return
    try:
        paths = [p for p in ("context", "plan", "log", "knowledge", "data/daily", "data/workouts") if (repo / p).exists()]
        subprocess.run(["git", "add", "-A", "--", *paths], cwd=repo, capture_output=True, timeout=30)
        # pathspec commit: only the coach's own directories, never unrelated staged work
        subprocess.run(["git", "commit", "-q", "-m", msg, "--", *paths], cwd=repo, capture_output=True, timeout=30)
    except Exception as e:  # never let git break a reply
        log.warning("git commit skipped: %s", e)


def run_coach(trigger: str, prompt: str, resume: bool = True) -> Result:
    """Run the coach with `prompt`. Returns the reply text for Telegram."""
    header = f"[trigger: {trigger}] [now: {now().strftime('%A %Y-%m-%d %H:%M %Z')}]\n\n"
    full_prompt = header + prompt
    with LOCK_FILE.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        cmd = _base_cmd(config.CONTEXT / "instructions.md", config.COACH_MODEL)
        cmd += ["--permission-mode", "dontAsk", "--tools", ",".join(COACH_TOOLS),
                "--mcp-config", COACH_MCP_CONFIG, "--strict-mcp-config",
                "--allowedTools", *COACH_ALLOWED]
        for stale in (BUILD_REQUEST, REPLY_FILE):
            if stale.exists():
                stale.unlink()
        sid = _session_for_today() if resume else None
        if sid:
            rc, out, err = _run(cmd + ["--resume", sid], full_prompt, cwd=config.DATA_ROOT)
            if rc != 0:
                log.warning("resume of %s failed (rc=%s): %s -- starting fresh", sid, rc, err[-500:])
                reset_session()
                sid = None
        if not sid:
            rc, out, err = _run(cmd, full_prompt, cwd=config.DATA_ROOT)
        data = _parse(out)
        _record("coach", trigger, data, rc)
        if rc != 0 or data.get("is_error"):
            log.error("coach run failed rc=%s stderr=%s stdout=%s", rc, err[-2000:], out[-2000:])
            text = data.get("result") or err.strip()[-1500:] or "coach run failed with no output"
            return Result(text=f"Coach run failed:\n{text}", session_id=None, ok=False, raw=data)
        _remember_session(data.get("session_id"))
        _git_commit(f"coach: {trigger} {now().strftime('%Y-%m-%d %H:%M')}")
    text = (data.get("result") or "").strip()
    if REPLY_FILE.exists():  # the reply function is authoritative; the final text is only a fallback
        text = (read_json(REPLY_FILE, {}) or {}).get("text") or text
        REPLY_FILE.unlink()
    else:
        log.warning("coach did not call reply(); falling back to final text")
    build = None
    if BUILD_REQUEST.exists():
        build = (read_json(BUILD_REQUEST, {}) or {}).get("spec")
        BUILD_REQUEST.unlink()
    return Result(text=text, session_id=data.get("session_id"), ok=True, build_request=build, raw=data)


def run_builder(spec: str) -> Result:
    """Run the builder agent on a feature request. Full code access, no session reuse."""
    prompt = f"Feature request from the athlete:\n\n{spec}\n\nWhen done, summarise what changed and how to test it, in plain text for a phone message."
    with LOCK_FILE.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        cmd = _base_cmd(config.CONTEXT / "builder.md", config.COACH_MODEL)
        cmd += ["--permission-mode", "acceptEdits", "--allowedTools", *BUILDER_ALLOWED]
        rc, out, err = _run(cmd, prompt)
        data = _parse(out)
        _record("builder", "build", data, rc)
    if rc != 0 or data.get("is_error"):
        log.error("builder failed rc=%s stderr=%s", rc, err[-2000:])
        text = data.get("result") or err.strip()[-1500:] or "builder failed with no output"
        return Result(text=f"Builder failed:\n{text}", session_id=None, ok=False, raw=data)
    return Result(text=(data.get("result") or "").strip(), session_id=data.get("session_id"), ok=True, raw=data)
