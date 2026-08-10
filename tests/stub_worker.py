"""Fake Claude worker for canary tests. Honors the exact job contract
(env EC_ROLE/EC_KEY/EC_CWD/EC_RESULT) so the whole supervision path is real
and only the model is stubbed. Behavior comes from EC_STUB_SCRIPT json:
  {"<role>": "<mode>", "<role>.<seq>": "<mode>"}   (seq-specific wins)
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

APP_OK = "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n"
APP_BAD = "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a - b\n"
TEST_BOTH = (
    "import unittest\n\nfrom app import add, mul\n\n\n"
    "class T(unittest.TestCase):\n"
    "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n\n"
    "    def test_mul(self):\n        self.assertEqual(mul(2, 3), 6)\n\n\n"
    "if __name__ == '__main__':\n    unittest.main()\n")


def sh(args, cwd):
    subprocess.run(args, cwd=cwd, capture_output=True, text=True)


def commit_all(cwd, msg, key):
    sh(["git", "add", "-A"], cwd)
    sh(["git", "-c", "user.name=stub", "-c", "user.email=stub@ec",
        "commit", "-m", f"{msg}\n\nEC-Key: {key}"], cwd)


def main():
    role = os.environ["EC_ROLE"]
    key = os.environ["EC_KEY"]
    cwd = os.environ["EC_CWD"]
    result = Path(os.environ["EC_RESULT"])
    seq = key.rsplit(".", 1)[1]
    script = {}
    sp = os.environ.get("EC_STUB_SCRIPT")
    if sp and Path(sp).exists():
        script = json.loads(Path(sp).read_text())
    mode = script.get(f"{role}.{seq}", script.get(role, "ok"))
    log = os.environ.get("EC_STUB_LOG")
    if log:
        with open(log, "a") as f:
            f.write(f"{key} {mode}\n")

    if mode == "hang":
        time.sleep(int(os.environ.get("EC_STUB_HANG", "60")))
        return 0
    if mode == "fail":
        sys.stderr.write("stub worker deliberate failure\n")
        return 3
    if mode == "quota":
        print(json.dumps({"is_error": True,
                          "result": "Claude usage limit reached. Your limit will reset at 7pm."}))
        return 1
    if mode == "malformed":
        result.write_text("{this is not json", encoding="utf-8")
        return 0
    if mode == "noresult":
        return 0

    if role == "planner":
        step_id = key.split(".c")[0]
        result.write_text(json.dumps({
            "version": 1, "step_id": step_id,
            "tasks": [{"repo": "canary",
                       "objective": "add mul(a,b) returning a*b, with a test"}],
            "acceptance": ["mul(2,3)==6 covered by test_canary.py"],
            "rollback_conditions": ["tests red at integration"]}))
        return 0
    if role in ("implementer", "repair"):
        if mode in ("ok", "bad_code"):
            (Path(cwd) / "app.py").write_text(APP_OK if mode == "ok" else APP_BAD)
            (Path(cwd) / "test_canary.py").write_text(TEST_BOTH)
            commit_all(cwd, f"canary: {role} {mode}", key)
        result.write_text(json.dumps({
            "version": 1, "status": "done", "summary": f"{role} {mode} done",
            "decisions": ["stub decision"], "interfaces": ["app.mul"],
            "tests_run": ["python test_canary.py"], "uncertainty": "",
            "hypotheses_supported": [], "hypotheses_falsified": [],
            "implications": "none"}))
        return 0
    if role == "reviewer":
        if mode == "pass_slow":
            time.sleep(3)
            mode = "pass"
        verdict = {"pass": "PASS", "repair": "REPAIR", "block": "BLOCK"}.get(mode, "PASS")
        findings = [] if verdict == "PASS" else [
            {"severity": "major", "issue": "stub finding: mul must be a*b", "file": "app.py"}]
        result.write_text(json.dumps({"version": 1, "verdict": verdict, "findings": findings}))
        return 0
    if role == "diagnostic":
        result.write_text(json.dumps({
            "version": 1, "verdict": "REPAIR",
            "findings": [{"severity": "critical", "issue": "root cause: mul wrong"}]}))
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
