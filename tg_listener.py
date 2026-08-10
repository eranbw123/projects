"""Low-latency Telegram command listener for engine-control.

The 1-minute scheduled tick stays the reliability backbone; this listener
only shrinks COMMAND latency from ~60s to a few seconds. It long-polls
getUpdates and, the moment anything arrives, wakes the controller
(`python control.py tick` with EC_CONSUME_TG=1). It never parses commands
and never writes state.db (strictly mode=ro reads of the offset): the
controller remains the single writer and the single command interpreter.

Liveness contract with the tick:
- after every successful poll cycle the listener touches listener.alive;
- while that heartbeat is fresh (<90s) the tick skips its own getUpdates
  (avoids long-poll 409 conflicts) UNLESS it was woken by the listener;
- if the listener dies or hangs, the heartbeat goes stale and the scheduled
  tick transparently resumes consuming commands at 1-minute cadence.

Supervised by the Scheduled Task `engine-control-listener` (fires every 5
minutes; the byte lock makes extra instances exit immediately, so a crashed
listener is restarted within 5 minutes and a live one is left alone).
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

POLL_TIMEOUT = 25
HEARTBEAT = "listener.alive"


def read_offset(root: Path) -> int:
    try:
        conn = sqlite3.connect(f"file:{root / 'state.db'}?mode=ro", uri=True,
                               timeout=5)
        row = conn.execute("SELECT v FROM kv WHERE k='tg_offset'").fetchone()
        conn.close()
        return int(row[0]) if row else 0
    except (sqlite3.Error, ValueError, OSError):
        return 0


def fetch_updates(token: str, offset: int, timeout: int) -> list:
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    data = urllib.parse.urlencode({
        "offset": offset, "timeout": timeout,
        "allowed_updates": '["message"]'}).encode()
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=timeout + 15) as r:
        obj = json.loads(r.read().decode())
    if not obj.get("ok"):
        raise RuntimeError("getUpdates not ok")
    return obj.get("result", [])


def heartbeat(ctx) -> None:
    try:
        (ctx.root / HEARTBEAT).write_text(common.now(), encoding="utf-8")
    except OSError:
        pass


def trigger_tick() -> None:
    import os
    env = dict(os.environ, EC_CONSUME_TG="1")
    subprocess.run([sys.executable, str(common.CODE_DIR / "control.py"), "tick"],
                   capture_output=True, env=env, timeout=300)


def cycle(ctx, fetch=fetch_updates, trigger=trigger_tick,
          max_wakes=5, wake_pause=2.0) -> str:
    """One listener cycle. Returns idle|consumed|pending|no-token|error."""
    token = ctx.getenv("DEV_TELEGRAM_BOT_TOKEN")
    if not token:
        return "no-token"
    try:
        updates = fetch(token, read_offset(ctx.root) + 1, POLL_TIMEOUT)
    except Exception:
        return "error"
    heartbeat(ctx)  # only after a SUCCESSFUL poll: a hung listener goes stale
    if not updates:
        return "idle"
    # wake the controller until it has consumed what we saw (a concurrent
    # scheduled tick can make our wake bounce off tick.lock; retry briefly)
    for _ in range(max_wakes):
        trigger()
        try:
            if not fetch(token, read_offset(ctx.root) + 1, 0):
                return "consumed"
        except Exception:
            return "error"
        time.sleep(wake_pause)
    return "pending"


def main() -> int:
    ctx = common.Ctx()
    lock = common.acquire_lock(ctx.root, "listener.lock")
    if lock is None:
        return 0  # another listener instance is alive; supervisor no-op
    ctx.log("tg_listener: started")
    try:
        while True:
            out = cycle(ctx)
            if out == "no-token":
                time.sleep(60)
            elif out == "error":
                time.sleep(3)
    finally:
        common.release_lock(lock)


if __name__ == "__main__":
    sys.exit(main())
