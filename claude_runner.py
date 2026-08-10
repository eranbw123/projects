"""Dispatch and reconcile Claude Code workers (and deterministic test runs).

Execution contract (all lanes identical, crash-recoverable):
  artifacts/runs/<key>/
    prompt.md      controller-written prompt (stdin for claude -p)
    job.json       argv/cwd/env contract executed by worker_shim.py
    started.txt    written by shim when it begins
    pid.txt        shim pid
    stdout.txt     worker stdout (claude -p --output-format json)
    stderr.txt     worker stderr
    exitcode.txt   written LAST by shim = completion marker
    result.json    role result artifact written by the worker itself

Lanes:
  direct  detached child (DETACHED_PROCESS) — survives the tick exiting; ticks
          themselves run under Task Scheduler, so workers are SSH-independent.
  task    one-shot Windows Scheduled Task (machine's proven convention for
          long runs). Trigger double-fire is neutralized by shim idempotence.

Identity is deterministic: run key -> session uuid (uuid5) and task name, so a
crash after dispatch but before recording can always re-find its worker.
Note: CLI 2.1.226 also offers `claude --bg` (managed by `claude agents`); we
keep the shim contract instead because it is one probe path for every worker
type and its supervisor (Task Scheduler / OS) is verified on this machine.
"""
from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import common

PYTHON = sys.executable

ROLES = {
    "planner":     dict(model="opus", effort="high", pm=None, deadline_min=25,
                        tools=["Read", "Grep", "Glob", "Write",
                               "Bash(git log:*)", "Bash(git diff:*)", "Bash(git show:*)"]),
    "implementer": dict(model="sonnet", effort="high", pm="acceptEdits", deadline_min=120,
                        tools=["Read", "Edit", "Write", "Grep", "Glob",
                               "Bash(git:*)", "Bash(python:*)"]),
    "repair":      dict(model="sonnet", effort="high", pm="acceptEdits", deadline_min=90,
                        tools=["Read", "Edit", "Write", "Grep", "Glob",
                               "Bash(git:*)", "Bash(python:*)"]),
    "reviewer":    dict(model="opus", effort="high", pm=None, deadline_min=35,
                        tools=["Read", "Grep", "Glob", "Write",
                               "Bash(git log:*)", "Bash(git diff:*)", "Bash(git show:*)",
                               "Bash(python:*)"]),
    "diagnostic":  dict(model="opus", effort="high", pm=None, deadline_min=35,
                        tools=["Read", "Grep", "Glob", "Write",
                               "Bash(git log:*)", "Bash(git diff:*)", "Bash(git show:*)",
                               "Bash(python:*)"]),
    "audit":       dict(model="fable", effort="high", pm=None, deadline_min=60,
                        tools=["Read", "Grep", "Glob", "Write",
                               "Bash(git log:*)", "Bash(git diff:*)", "Bash(git show:*)"]),
    "test":        dict(model=None, effort=None, pm=None, deadline_min=45, tools=[]),
}

MODEL_IDS = {"sonnet": "sonnet", "opus": "opus", "fable": "claude-fable-5"}

# Accidental API billing must never happen: strip billing env from workers.
BILLING_ENV = ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_API_KEY",
               "ANTHROPIC_BASE_URL"]

QUOTA_RE = re.compile(
    r"(usage limit|rate.?limit|limit (?:reached|will reset)|out of (?:usage|quota)"
    r"|quota|too many requests|overloaded|resets at|\b429\b)", re.I)

FABLE_FORBIDDEN_RE = re.compile(
    r"(conversations\.db|raw_conversations|chatgpt_export|full conversation transcript)", re.I)


def fable_payload_ok(prompt_text: str) -> bool:
    """Fable privacy boundary: no raw conversation stores in fable prompts.
    Architecture summaries/diffs/metrics only (enforced again by prompt policy)."""
    return not FABLE_FORBIDDEN_RE.search(prompt_text)


def quota_hit(text: str) -> bool:
    return bool(QUOTA_RE.search(text or ""))


def art_dir(ctx: common.Ctx, key: str) -> Path:
    return ctx.art / "runs" / key


def task_name(key: str) -> str:
    return "ec-" + re.sub(r"[^A-Za-z0-9\-]", "-", key)


def resolve_claude(ctx) -> str | None:
    exe = ctx.getenv("EC_CLAUDE_EXE") or "claude"
    return shutil.which(exe)


def build_job(ctx: common.Ctx, key: str, role: str, prompt_text: str, cwd: str,
              model: str | None, effort: str | None, resume_session: str | None = None,
              deadline_min: int | None = None, test_cmds: list[str] | None = None) -> Path:
    """Write prompt.md + job.json. Idempotent: same key -> same files."""
    rd = art_dir(ctx, key)
    rd.mkdir(parents=True, exist_ok=True)
    result_path = rd / "result.json"
    prompt_text = (prompt_text
                   .replace("<<RESULT_PATH>>", str(result_path))
                   .replace("<<RUN_KEY>>", key))
    common.write_atomic(rd / "prompt.md", prompt_text)
    spec = ROLES[role]
    dl = (deadline_min or spec["deadline_min"]) * 60

    if role == "test":
        argv = [PYTHON, str(common.CODE_DIR / "validators.py"), "--run-tests",
                "--cwd", cwd, "--result", str(result_path),
                "--"] + (test_cmds or [])
        stdin = None
    else:
        override = ctx.getenv("EC_WORKER_CMD")
        if override:
            # posix=False keeps Windows backslashes; strip token quoting.
            argv = [t.strip('"') for t in shlex.split(override, posix=False)]
            stdin = "prompt.md"
        else:
            claude = resolve_claude(ctx)
            if not claude:
                raise RuntimeError("claude CLI not found on PATH (set EC_CLAUDE_EXE)")
            if model == "fable" and not fable_payload_ok(prompt_text):
                raise RuntimeError("fable privacy boundary: raw conversation data in prompt")
            # npm installs claude as a .CMD shim; batch files start cmd.exe,
            # and this machine's cmd AutoRun hijacks the working directory
            # (worker would run in the owner's repos!). /d disables AutoRun.
            prefix = ["cmd", "/d", "/c"] if claude.lower().endswith((".cmd", ".bat")) else []
            argv = [*prefix, claude, "-p", "--output-format", "json",
                    "--model", MODEL_IDS.get(model, model),
                    "--session-id", common.session_uuid(key),
                    "--add-dir", str(rd)]
            if effort:
                argv += ["--effort", effort]
            if spec["pm"]:
                argv += ["--permission-mode", spec["pm"]]
            if resume_session:
                argv += ["--resume", resume_session, "--fork-session"]
            if spec["tools"]:
                argv += ["--allowedTools"] + spec["tools"]
            stdin = "prompt.md"

    job = {
        "argv": argv, "cwd": cwd, "stdin": stdin, "timeout": dl,
        "env_unset": BILLING_ENV,
        "env_set": {"EC_ROLE": role, "EC_KEY": key,
                    "EC_PROMPT": str(rd / "prompt.md"),
                    "EC_RESULT": str(result_path), "EC_CWD": cwd,
                    **{k: ctx.getenv(k) for k in
                       ("EC_STUB_SCRIPT", "EC_STUB_LOG", "EC_STUB_HANG")
                       if ctx.getenv(k)}},
    }
    common.write_atomic(rd / "job.json", json.dumps(job, indent=1))
    return rd


def dispatch_evidence(ctx, key) -> str | None:
    """Has this key already been launched? (crash between spawn and record)"""
    rd = art_dir(ctx, key)
    if (rd / "exitcode.txt").exists():
        return "completed"
    if (rd / "started.txt").exists():
        return "started"
    p = subprocess.run(["schtasks", "/query", "/tn", task_name(key)],
                       capture_output=True, text=True)
    if p.returncode == 0:
        return "task-exists"
    return None


def spawn(ctx: common.Ctx, key: str, lane: str) -> dict:
    """Launch worker_shim for an already-built job. Returns external descriptor."""
    rd = art_dir(ctx, key)
    shim = str(common.CODE_DIR / "worker_shim.py")
    if lane == "task":
        tn = task_name(key)
        # /st is a far-future fallback; we /run immediately. Shim idempotence
        # neutralizes a later trigger fire. en-US time format (machine-local tool).
        st = (datetime.now() + timedelta(hours=12))
        cmd = f'"{PYTHON}" "{shim}" "{rd}"'
        cr = subprocess.run(
            ["schtasks", "/create", "/tn", tn, "/sc", "once",
             "/st", st.strftime("%H:%M"), "/sd", st.strftime("%m/%d/%Y"),
             "/f", "/tr", cmd], capture_output=True, text=True)
        if cr.returncode != 0:
            raise RuntimeError(f"schtasks create failed: {cr.stderr.strip()[:300]}")
        subprocess.run(["schtasks", "/run", "/tn", tn], capture_output=True, text=True)
        return {"lane": "task", "task": tn, "session": common.session_uuid(key)}
    # direct: detached; survives tick exit (ticks run under Task Scheduler,
    # not SSH, so workers are not SSH-session children).
    DETACHED = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED|NEW_GROUP|NO_WINDOW
    p = subprocess.Popen([PYTHON, shim, str(rd)],
                         creationflags=DETACHED, close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, cwd=str(ctx.root))
    return {"lane": "direct", "pid": p.pid, "session": common.session_uuid(key)}


def pid_alive(pid: int) -> bool:
    p = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                       capture_output=True, text=True)
    line = (p.stdout or "").strip().lower()
    return line.startswith('"') and (f",\"{pid}\"," in line.replace(" ", "") or str(pid) in line)


def task_status(tn: str) -> str | None:
    p = subprocess.run(["schtasks", "/query", "/tn", tn, "/fo", "csv", "/nh"],
                       capture_output=True, text=True)
    if p.returncode != 0:
        return None
    parts = (p.stdout or "").strip().splitlines()
    if not parts:
        return None
    cols = parts[0].split('","')
    return cols[-1].strip('" ') if cols else None


def delete_task(key: str) -> None:
    tn = task_name(key)
    assert tn.startswith("ec-")  # never touch non-controller tasks
    subprocess.run(["schtasks", "/delete", "/tn", tn, "/f"], capture_output=True, text=True)


def probe(ctx: common.Ctx, run_row) -> dict:
    """-> {phase: pending|running|done|lost, rc: int|None}"""
    rd = art_dir(ctx, run_row["key"])
    ec = rd / "exitcode.txt"
    if ec.exists():
        try:
            rc = int(ec.read_text().strip() or "125")
        except ValueError:
            rc = 125
        return {"phase": "done", "rc": rc}
    pidf = rd / "pid.txt"
    if pidf.exists():
        try:
            pid = int(pidf.read_text().strip())
        except ValueError:
            return {"phase": "lost", "rc": None}
        if pid_alive(pid):
            return {"phase": "running", "rc": None}
        # brief re-check: shim may be writing exitcode right now
        time.sleep(0.3)
        if ec.exists():
            return {"phase": "done", "rc": int(ec.read_text().strip() or "125")}
        return {"phase": "lost", "rc": None}
    ext = json.loads(run_row["external"] or "{}")
    try:
        from datetime import timezone as _tz
        age = (datetime.now(_tz.utc) - common.parse_iso(run_row["created_at"])).total_seconds()
    except Exception:
        age = 0
    if ext.get("lane") == "task":
        st = task_status(ext.get("task", ""))
        if st == "Running":
            return {"phase": "running", "rc": None}
        grace = 300
    else:
        grace = 120
    return {"phase": "pending" if age < grace else "lost", "rc": None}


def kill_run(ctx: common.Ctx, run_row) -> None:
    rd = art_dir(ctx, run_row["key"])
    pidf = rd / "pid.txt"
    if pidf.exists():
        try:
            pid = int(pidf.read_text().strip())
            if pid_alive(pid):
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               capture_output=True, text=True)
        except ValueError:
            pass
    ext = json.loads(run_row["external"] or "{}")
    if ext.get("lane") == "task":
        subprocess.run(["schtasks", "/end", "/tn", ext.get("task", "ec-none")],
                       capture_output=True, text=True)
        delete_task(run_row["key"])


def read_claude_stdout(ctx, key) -> dict:
    """Parse claude -p --output-format json stdout (or stub equivalent)."""
    rd = art_dir(ctx, key)
    txt = ""
    try:
        txt = (rd / "stdout.txt").read_text(encoding="utf-8", errors="replace")
    except OSError:
        pass
    try:
        return json.loads(txt)
    except ValueError:
        return {"raw": txt[-2000:]}


def stderr_tail(ctx, key) -> str:
    rd = art_dir(ctx, key)
    try:
        return common.tail((rd / "stderr.txt").read_text(encoding="utf-8", errors="replace"), 40)
    except OSError:
        return ""
