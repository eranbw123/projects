"""Sandbox harness: disposable canary repo(s) + isolated controller root.

Roots are created with plain os.mkdir, NEVER tempfile.mkdtemp: Python 3.13+
mkdtemp applies a restricted DACL on Windows that the Task Scheduler logon
session cannot read, which silently breaks task-lane tests (commissioning
finding). mkroot() inherits the parent's normal ACL instead.
"""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE))

import common          # noqa: E402
import control         # noqa: E402
import db              # noqa: E402

PY = sys.executable
STUB = CODE / "tests" / "stub_worker.py"


def mkroot(prefix="ec-t-") -> Path:
    base = Path(tempfile.gettempdir())
    for _ in range(50):
        p = base / (prefix + uuid.uuid4().hex[:10])
        try:
            p.mkdir()
            return p
        except FileExistsError:
            continue
    raise RuntimeError("could not create sandbox root")


def git(args, cwd):
    p = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                       cwd=cwd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"git {args}: {p.stderr}")
    return p.stdout


def rmtree(path):
    def _onexc(fn, p, exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            fn(p)
        except OSError:
            pass
    shutil.rmtree(path, onexc=_onexc) if sys.version_info >= (3, 12) else \
        shutil.rmtree(path, onerror=lambda f, p, e: _onexc(f, p, e))


def now_ms():
    return int(datetime.now(timezone.utc).timestamp() * 1000)


AUTH_SUBSCRIPTION = {"loggedIn": True, "authMethod": "claude.ai",
                     "apiProvider": "firstParty", "subscriptionType": "max"}

TIER_STRINGS = {"max5": "default_claude_max_5x", "max20": "default_claude_max_20x"}


def claude_json_fixture(tier="max20", pct5=6, pct7=2, fetched_ms=None,
                        org_type="claude_max", extra_fields=None):
    """Synthetic .claude.json with ONLY the whitelisted shapes (plus optional
    decoy fields for secret-leak tests)."""
    fetched_ms = fetched_ms or now_ms()
    reset5 = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    reset7 = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    oa = {"organizationType": org_type, "profileFetchedAt": fetched_ms}
    if tier in TIER_STRINGS:
        oa["organizationRateLimitTier"] = TIER_STRINGS[tier]
    obj = {
        "oauthAccount": {**oa, **(extra_fields or {})},
        "cachedUsageUtilization": {
            "fetchedAtMs": fetched_ms,
            "utilization": {
                "five_hour": {"utilization": pct5, "resets_at": reset5},
                "seven_day": {"utilization": pct7, "resets_at": reset7},
            }},
    }
    return obj


DEFAULT_STEPS = """
steps:
  - id: c1
    ordinal: 1
    title: canary step
    repos: [canary]
    depends_on: []
    objective: add a mul() function with a test
    acceptance: mul(2,3)==6 in canonical tests
"""


class Sandbox:
    def __init__(self, script=None, telegram_ok=True, extra_env="",
                 steps_yaml=None, repos=("canary",), capacity=None,
                 tests_cmds=("python test_canary.py",)):
        self.dir = mkroot()
        self.root = self.dir / "ec"
        self.root.mkdir()
        self.repos = {}
        repo_blocks = []
        for name in repos:
            src = self.dir / f"{name}-src"
            src.mkdir()
            (src / "app.py").write_text("def add(a, b):\n    return a + b\n")
            (src / "test_canary.py").write_text(
                "import unittest\n\nfrom app import add\n\n\n"
                "class T(unittest.TestCase):\n"
                "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n\n\n"
                "if __name__ == '__main__':\n    unittest.main()\n")
            git(["init", "-b", "main"], src)
            git(["add", "-A"], src)
            git(["commit", "-m", "canary base"], src)
            baseline = git(["rev-parse", "HEAD"], src).strip()
            ws = self.dir / "aw" / name
            self.repos[name] = dict(src=src, ws=ws, baseline=baseline)
            tests_yaml = "\n".join(f"      - {c}" for c in tests_cmds)
            repo_blocks.append(
                f"  {name}:\n    source: '{src}'\n    workspace: '{ws}'\n"
                f"    baseline: {baseline}\n    tests:\n{tests_yaml}")
        self.src = self.repos[repos[0]]["src"]
        self.ws = self.repos[repos[0]]["ws"]

        self.stub_log = self.dir / "stub.log"
        self.script_path = self.dir / "stub_script.json"
        self.set_script(script or {})

        (self.root / "roadmap.yaml").write_text(
            "version: 1\nrepos:\n" + "\n".join(repo_blocks) + "\n"
            + (steps_yaml or DEFAULT_STEPS))

        # capacity fixtures (plan detection + auth + usage)
        self.claude_json = self.dir / "fixture-claude.json"
        self.auth_json = self.dir / "fixture-auth.json"
        capacity = capacity or {}
        self.set_capacity(**capacity) if capacity else self.set_capacity()

        env = (f"EC_WORKER_CMD={PY} {STUB}\n"
               f"EC_LANE=direct\n"
               f"EC_STUB_SCRIPT={self.script_path}\n"
               f"EC_STUB_LOG={self.stub_log}\n"
               f"EC_QUOTA_RETRY_MIN=0\n"
               f"EC_CLAUDE_JSON={self.claude_json}\n"
               f"EC_AUTH_STATUS_FILE={self.auth_json}\n"
               f"EC_CREDENTIALS_FILE={self.dir / 'no-creds.json'}\n"
               f"EC_DISABLE_PROBE=1\n")
        if telegram_ok:
            env += "EC_ALLOW_NO_TELEGRAM=1\n"
        (self.root / ".env").write_text(env + extra_env)
        control.cmd_init(self.ctx()).close()

    # -- fixtures --
    def set_capacity(self, tier="max20", pct5=6, pct7=2, auth=None, **kw):
        obj = claude_json_fixture(tier=tier, pct5=pct5, pct7=pct7, **kw)
        self.claude_json.write_text(json.dumps(obj))
        self.auth_json.write_text(json.dumps(auth or AUTH_SUBSCRIPTION))

    def set_script(self, script: dict):
        self.script_path.write_text(json.dumps(script))

    # -- helpers --
    def ctx(self):
        return common.Ctx(self.root)

    def conn(self):
        return db.connect(self.ctx())

    def start(self):
        c = self.conn()
        db.kv_set(c, "roadmap_started", "1")
        c.close()

    def tick(self):
        return control.tick(self.ctx())

    def step_row(self, conn, step_id="c1"):
        return conn.execute("SELECT * FROM steps WHERE id=?", (step_id,)).fetchone()

    def state(self, step_id="c1"):
        c = self.conn()
        try:
            return self.step_row(c, step_id)["state"]
        finally:
            c.close()

    def states(self):
        c = self.conn()
        try:
            return {s["id"]: s["state"] for s in
                    c.execute("SELECT * FROM steps ORDER BY ordinal")}
        finally:
            c.close()

    def runs(self):
        c = self.conn()
        try:
            return c.execute("SELECT * FROM runs ORDER BY id").fetchall()
        finally:
            c.close()

    def transitions(self, step_id=None):
        c = self.conn()
        try:
            rows = c.execute("SELECT * FROM transitions ORDER BY id").fetchall()
            return [(t["from_state"], t["to_state"]) for t in rows
                    if step_id is None or t["step_id"] == step_id]
        finally:
            c.close()

    def stub_calls(self):
        if not self.stub_log.exists():
            return []
        return self.stub_log.read_text().strip().splitlines()

    def run_until(self, pred, timeout=90, pause=0.25):
        end = time.time() + timeout
        while time.time() < end:
            self.tick()
            c = self.conn()
            try:
                if pred(c):
                    return True
            finally:
                c.close()
            time.sleep(pause)
        return False

    def until_state(self, state, timeout=90, step_id="c1"):
        return self.run_until(
            lambda c: self.step_row(c, step_id)["state"] == state, timeout)

    def spawn_tick(self, extra_env=None):
        env = os.environ.copy()
        env.pop("EC_ROOT", None)
        env.update(extra_env or {})
        return subprocess.Popen(
            [PY, str(CODE / "control.py"), "tick", "--root", str(self.root)],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def worker_pids(self):
        pids = []
        for pf in (self.root / "artifacts" / "runs").glob("*/pid.txt"):
            try:
                pids.append(int(pf.read_text().strip()))
            except (OSError, ValueError):
                pass
        return pids

    def kill_workers(self):
        for pid in self.worker_pids():
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, text=True)

    def cleanup(self):
        self.kill_workers()
        try:
            rmtree(self.dir)
        except OSError:
            pass
