"""Statusline handler for AUTOMATION Claude sessions only (never installed
into the owner's global settings). Reads the statusline JSON from stdin,
extracts ONLY non-secret utilization telemetry, and atomically writes
telemetry/sessions/<session-id>.json for the controller to ingest.

No transcript text, no prompts, no credentials, no file paths beyond the
session id ever leave this process. Missing fields are ignored safely.
Always exits 0 and prints a short status string (its statusline text).
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timezone

DEFAULT_DIR = r"C:\projects\engine-control\telemetry\sessions"


def pick(d, *names):
    if not isinstance(d, dict):
        return None
    lower = {str(k).lower(): v for k, v in d.items()}
    for n in names:
        if n.lower() in lower:
            return lower[n.lower()]
    return None


def pct(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return max(0, min(100, round(float(v), 1)))


def window(obj, *names):
    """Find a rate-limit window dict under any of several plausible keys."""
    for container_keys in (("usage",), ("rate_limits",), ("rateLimits",),
                           ("rate_limit_status",), ()):
        node = obj
        for k in container_keys:
            node = pick(node, k)
        if not isinstance(node, dict):
            continue
        w = pick(node, *names)
        if isinstance(w, dict):
            return w
    return None


def main():
    try:
        raw = sys.stdin.read()
        obj = json.loads(raw) if raw.strip() else {}
    except (OSError, ValueError):
        obj = {}
    sid = pick(obj, "session_id", "sessionId") or ""
    sid = "".join(c for c in str(sid) if c.isalnum() or c in "-_")[:64]
    model = pick(obj, "model")
    if isinstance(model, dict):
        model = pick(model, "id", "display_name")
    out = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "session_id": sid or None,
           "model": str(model)[:60] if model else None}
    fh = window(obj, "five_hour", "fiveHour", "session", "primary")
    sd = window(obj, "seven_day", "sevenDay", "weekly", "secondary")
    if isinstance(fh, dict):
        out["pct5"] = pct(pick(fh, "utilization", "used_pct", "percent", "pct"))
        r = pick(fh, "resets_at", "reset_at", "resetsAt")
        out["reset5"] = str(r)[:40] if r else None
    if isinstance(sd, dict):
        out["pct7"] = pct(pick(sd, "utilization", "used_pct", "percent", "pct"))
        r = pick(sd, "resets_at", "reset_at", "resetsAt")
        out["reset7"] = str(r)[:40] if r else None
    d = os.environ.get("EC_TELEMETRY_DIR", DEFAULT_DIR)
    try:
        os.makedirs(d, exist_ok=True)
        target = os.path.join(d, (sid or "unknown") + ".json")
        fd, tmp = tempfile.mkstemp(dir=d)
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(out))
        os.replace(tmp, target)
    except OSError:
        pass
    p5 = out.get("pct5")
    p7 = out.get("pct7")
    txt = "ec"
    if p5 is not None or p7 is not None:
        txt = f"ec 5h:{p5 if p5 is not None else '?'}% 7d:{p7 if p7 is not None else '?'}%"
    print(txt)
    return 0


if __name__ == "__main__":
    sys.exit(main())
