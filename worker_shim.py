"""Standalone worker supervisor shim (stdlib only, no local imports).

Usage: python worker_shim.py <artifact_dir>

Executes the job described by <artifact_dir>/job.json and records the full
lifecycle as files. exitcode.txt is written LAST and atomically — it is the
completion marker the controller trusts. Idempotent: a second invocation
(e.g. a scheduled-task trigger double-fire) exits without side effects if the
job already completed or is still running.
"""
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def pid_alive(pid: int) -> bool:
    p = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                       capture_output=True, text=True)
    out = (p.stdout or "").strip()
    if not out.startswith('"'):
        return False
    image = out.split('","')[0].strip('"').lower()
    return image.startswith("python") and str(pid) in out


def write_atomic(path: Path, text: str):
    fd, tmp = tempfile.mkstemp(dir=str(path.parent))
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, str(path))


def main():
    art = Path(sys.argv[1])
    if (art / "exitcode.txt").exists():
        return 0  # already completed; adopt, don't rerun
    pidf = art / "pid.txt"
    if pidf.exists():
        try:
            other = int(pidf.read_text().strip())
            if other != os.getpid() and pid_alive(other):
                return 0  # another instance is running this job
        except ValueError:
            pass

    job = json.loads((art / "job.json").read_text(encoding="utf-8"))
    write_atomic(art / "started.txt", now())
    write_atomic(pidf, str(os.getpid()))

    env = os.environ.copy()
    for k in job.get("env_unset", []):
        env.pop(k, None)
    env.update(job.get("env_set", {}))

    stdin = subprocess.DEVNULL
    if job.get("stdin"):
        stdin = open(art / job["stdin"], "rb")

    rc = 125
    try:
        with open(art / "stdout.txt", "wb") as out, open(art / "stderr.txt", "wb") as err:
            p = subprocess.run(job["argv"], cwd=job.get("cwd") or None, env=env,
                               stdin=stdin, stdout=out, stderr=err,
                               timeout=job.get("timeout") or 7200)
            rc = p.returncode
    except subprocess.TimeoutExpired:
        rc = 124
    except Exception as e:  # record launch failures, never vanish silently
        with open(art / "stderr.txt", "ab") as err:
            err.write(f"\n[shim] launch error: {e}\n".encode())
        rc = 125
    finally:
        if stdin is not subprocess.DEVNULL:
            stdin.close()
        write_atomic(art / "exitcode.txt", str(rc))
    return 0


if __name__ == "__main__":
    sys.exit(main())
