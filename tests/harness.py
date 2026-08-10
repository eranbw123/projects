"""Sandbox harness: disposable canary repo + isolated controller root."""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE))

import common          # noqa: E402
import control         # noqa: E402
import db              # noqa: E402

PY = sys.executable
STUB = CODE / "tests" / "stub_worker.py"


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


class Sandbox:
    def __init__(self, script=None, telegram_ok=True, extra_env=""):
        self.dir = Path(tempfile.mkdtemp(prefix="ec-t-"))
        self.root = self.dir / "ec"
        self.root.mkdir()
        # canary source repo
        src = self.dir / "canary-src"
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
        self.src = src

        self.stub_log = self.dir / "stub.log"
        self.script_path = self.dir / "stub_script.json"
        self.set_script(script or {})

        ws = self.dir / "aw" / "canary"
        (self.root / "roadmap.yaml").write_text(f"""
version: 1
repos:
  canary:
    source: '{src}'
    workspace: '{ws}'
    baseline: {baseline}
    tests:
      - python test_canary.py
steps:
  - id: c1
    ordinal: 1
    title: canary step
    repos: [canary]
    objective: add a mul() function with a test
    acceptance: mul(2,3)==6 in canonical tests
""")
        env = (f"EC_WORKER_CMD={PY} {STUB}\n"
               f"EC_LANE=direct\n"
               f"EC_STUB_SCRIPT={self.script_path}\n"
               f"EC_STUB_LOG={self.stub_log}\n"
               f"EC_QUOTA_RETRY_MIN=0\n")
        if telegram_ok:
            env += "EC_ALLOW_NO_TELEGRAM=1\n"
        (self.root / ".env").write_text(env + extra_env)
        self.ws = ws
        control.cmd_init(self.ctx()).close()

    # -- helpers --
    def set_script(self, script: dict):
        self.script_path.write_text(json.dumps(script))

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

    def step_row(self, conn):
        return conn.execute("SELECT * FROM steps WHERE id='c1'").fetchone()

    def state(self):
        c = self.conn()
        try:
            return self.step_row(c)["state"]
        finally:
            c.close()

    def runs(self):
        c = self.conn()
        try:
            return c.execute("SELECT * FROM runs ORDER BY id").fetchall()
        finally:
            c.close()

    def transitions(self):
        c = self.conn()
        try:
            return [(t["from_state"], t["to_state"]) for t in
                    c.execute("SELECT * FROM transitions ORDER BY id")]
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

    def until_state(self, state, timeout=90):
        return self.run_until(lambda c: self.step_row(c)["state"] == state, timeout)

    def spawn_tick(self, extra_env=None):
        env = os.environ.copy()
        env.pop("EC_ROOT", None)
        env.update(extra_env or {})
        return subprocess.Popen(
            [PY, str(CODE / "control.py"), "tick", "--root", str(self.root)],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def cleanup(self):
        try:
            rmtree(self.dir)
        except OSError:
            pass
