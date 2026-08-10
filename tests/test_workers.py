"""Worker visibility: /workers live view (probe phase, heartbeat age, idle
reason) and the /status idle explanation.

Regression anchor (2026-08-10): /status read "0 active workers" with no
explanation while the engine was paused via a direct kv write (maintenance
edit window) — the owner could not tell whether that was a bug, a quota
hold, or a pause, nor what any worker was doing.
"""
import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

from harness import CODE, Sandbox

sys.path.insert(0, str(CODE))
import common    # noqa: E402
import control   # noqa: E402
import db        # noqa: E402
import telegram  # noqa: E402


class TestCommandRegistered(unittest.TestCase):
    def test_workers_passes_the_telegram_whitelist(self):
        self.assertIn("/workers", telegram.COMMANDS)

    def test_every_handled_command_passes_the_telegram_whitelist(self):
        # A handler missing from telegram.COMMANDS is silently dropped by
        # consume() as 'ignored' — bit /workers once and /why twice.
        import inspect
        import re
        src = inspect.getsource(control.handle_commands)
        handled = set(re.findall(r'name == "(/\w+)"', src))
        self.assertTrue(handled, "handler source no longer matches pattern")
        self.assertEqual(set(), handled - set(telegram.COMMANDS))


def running_probe(ctx, run):
    return {"phase": "running", "rc": None}


class Base(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()
        self.sb.tick()  # sync steps from roadmap; roadmap not started yet

    def tearDown(self):
        self.sb.cleanup()

    def insert_run(self, conn, key="c1.c0.t0.implementer.1", role="implementer",
                   model="sonnet", status="DISPATCHED", deadline_min=90,
                   created_min_ago=7):
        created = (datetime.now(timezone.utc)
                   - timedelta(minutes=created_min_ago)).isoformat(timespec="seconds")
        deadline = (datetime.now(timezone.utc)
                    + timedelta(minutes=deadline_min)).isoformat(timespec="seconds")
        ext = {"lane": "direct", "pid": 1, "session": common.session_uuid(key)}
        with conn:
            conn.execute(
                "INSERT INTO runs(key,step_id,task_idx,cycle,role,model,lane,"
                "status,external,cwd,artifact_dir,deadline,created_at) "
                "VALUES(?,?,0,0,?,?,'direct',?,?,?,?,?,?)",
                (key, "c1", role, model, status, json.dumps(ext), ".",
                 str(self.sb.root / "artifacts" / "runs" / key), deadline, created))
        return conn.execute("SELECT * FROM runs WHERE key=?", (key,)).fetchone()

    def heartbeat(self, key, min_ago=0):
        d = self.sb.root / "telemetry" / "sessions"
        d.mkdir(parents=True, exist_ok=True)
        ts = (datetime.now(timezone.utc)
              - timedelta(minutes=min_ago)).isoformat(timespec="seconds")
        (d / f"{common.session_uuid(key)}.json").write_text(
            json.dumps({"ts": ts, "session_id": common.session_uuid(key)}))


class TestWorkersActive(Base):
    def test_live_run_shows_phase_elapsed_heartbeat_and_step(self):
        conn = self.sb.conn()
        run = self.insert_run(conn, created_min_ago=7)
        self.heartbeat(run["key"], min_ago=2)
        txt = control.workers_text(self.sb.ctx(), conn, probe=running_probe)
        self.assertIn("workers: 1 active", txt)
        self.assertIn("c1 implementer/sonnet [running]", txt)
        self.assertIn("7m in", txt)
        self.assertIn("active 2m ago", txt)
        self.assertIn(run["key"], txt)
        self.assertIn("canary step", txt)  # step title from roadmap
        conn.close()

    def test_transcript_heartbeat_and_tool_metadata(self):
        conn = self.sb.conn()
        run = self.insert_run(conn)
        sid = common.session_uuid(run["key"])
        proj = self.sb.dir / "claude-projects" / "C--somewhere"
        proj.mkdir(parents=True)
        (proj / f"{sid}.jsonl").write_text(
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Bash",
                 "input": {"command": "SECRET-CMD"}}]}}) + "\n")
        os.environ["EC_CLAUDE_PROJECTS"] = str(self.sb.dir / "claude-projects")
        self.addCleanup(os.environ.pop, "EC_CLAUDE_PROJECTS", None)
        txt = control.workers_text(self.sb.ctx(), conn, probe=running_probe)
        self.assertIn("active 0m ago", txt)
        self.assertIn("tool: Bash", txt)
        self.assertNotIn("SECRET-CMD", txt)  # metadata only, never content
        conn.close()

    def test_overdue_and_missing_heartbeat_are_called_out(self):
        conn = self.sb.conn()
        self.insert_run(conn, deadline_min=-5, created_min_ago=100)
        txt = control.workers_text(self.sb.ctx(), conn, probe=running_probe)
        self.assertIn("OVERDUE", txt)
        self.assertIn("no heartbeat yet", txt)
        conn.close()

    def test_finished_runs_listed_with_outcome(self):
        conn = self.sb.conn()
        run = self.insert_run(conn, status="DISPATCHED")
        db.finish_run(conn, self.sb.ctx(), run["id"], "FAILED",
                      exit_code=1, note="boom happened")
        txt = control.workers_text(self.sb.ctx(), conn, probe=running_probe)
        self.assertIn("recent:", txt)
        self.assertIn("[FAILED]", txt)
        self.assertIn("boom happened", txt)
        conn.close()


class TestIdleReason(Base):
    def test_not_started(self):
        conn = self.sb.conn()
        txt = control.workers_text(self.sb.ctx(), conn, probe=running_probe)
        self.assertIn("workers: 0 active", txt)
        self.assertIn("roadmap not started", txt)
        conn.close()

    def test_direct_kv_pause_is_named_as_such(self):
        conn = self.sb.conn()
        db.kv_set(conn, "roadmap_started", "1")
        db.kv_set(conn, "paused", "1")  # no paused_why: outside pause
        txt = control.workers_text(self.sb.ctx(), conn, probe=running_probe)
        self.assertIn("PAUSED", txt)
        self.assertIn("state.db", txt)
        self.assertIn("/resume", txt)
        conn.close()

    def test_telegram_pause_records_attribution(self):
        conn = self.sb.conn()
        db.kv_set(conn, "roadmap_started", "1")
        sent = []

        class T:
            def send(self, t):
                sent.append(t)

        control.handle_commands(self.sb.ctx(), conn, T(), ["/pause"])
        self.assertIn("/pause at", db.kv_get(conn, "paused_why"))
        control.handle_commands(self.sb.ctx(), conn, T(), ["/workers"])
        self.assertIn("PAUSED: /pause at", sent[-1])
        control.handle_commands(self.sb.ctx(), conn, T(), ["/resume"])
        self.assertEqual(db.kv_get(conn, "paused_why"), "")
        conn.close()

    def test_quota_hold_wins_over_ready(self):
        conn = self.sb.conn()
        db.kv_set(conn, "roadmap_started", "1")
        db.kv_set(conn, "quota_hold_until", common.iso_in(1800))
        txt = control.workers_text(self.sb.ctx(), conn, probe=running_probe)
        self.assertIn("quota hold until", txt)
        conn.close()

    def test_status_explains_zero_active(self):
        conn = self.sb.conn()
        db.kv_set(conn, "roadmap_started", "1")
        db.kv_set(conn, "paused", "1")
        txt = control.status_text(self.sb.ctx(), conn)
        self.assertIn("0 active", txt)
        self.assertIn("idle — PAUSED", txt)
        self.assertIn("/workers", txt)
        conn.close()


DEP_STEPS = """
steps:
  - id: a1
    ordinal: 1
    title: step a
    repos: [canary]
    depends_on: []
    objective: obj a
    acceptance: acc a
  - id: b1
    ordinal: 2
    title: step b
    repos: [canary]
    depends_on: [a1]
    objective: obj b
    acceptance: acc b
"""


class TestNotificationContext(unittest.TestCase):
    """Owner-facing messages carry their try/retry/dependency context.

    Regression anchor (2026-08-10): 'step started' and friends reported the
    event with no metadata — the owner could not tell a first try from a
    last chance, nor what a BLOCK stalled downstream."""

    def setUp(self):
        self.sb = Sandbox(steps_yaml=DEP_STEPS)
        self.sb.tick()

    def tearDown(self):
        self.sb.cleanup()

    def test_blocked_message_names_attempts_stalls_and_actions(self):
        conn = self.sb.conn()
        control.goto_blocked(self.sb.ctx(), conn,
                             self.sb.step_row(conn, "a1"), "boom reason")
        txt = conn.execute("SELECT text FROM notifications WHERE key LIKE "
                           "'blocked:a1%'").fetchone()["text"]
        self.assertIn("BLOCKED", txt)
        self.assertIn("boom reason", txt)
        self.assertIn("attempts 0/4", txt)
        self.assertIn("stalls: b1", txt)
        self.assertIn("/retry a1", txt)
        conn.close()

    def test_status_shows_dependency_waits_and_ready(self):
        conn = self.sb.conn()
        txt = control.status_text(self.sb.ctx(), conn)
        self.assertIn("a1 READY", txt)       # no unmet deps
        self.assertIn("waits a1", txt)       # b1 gated on a1
        conn.close()

    def test_accepted_message_carries_progress_and_unblocks(self):
        conn = self.sb.conn()
        db.transition(conn, self.sb.ctx(), "a1", "ACCEPTED", "test")
        txt = control.accepted_text(conn, self.sb.step_row(conn, "a1"))
        self.assertIn("a1 ACCEPTED", txt)
        self.assertIn("roadmap 1/2 accepted", txt)
        self.assertIn("unblocks: b1", txt)
        conn.close()

    def test_quota_message_carries_streak_and_retry_time(self):
        conn = self.sb.conn()
        control.wait_quota(self.sb.ctx(), conn, self.sb.step_row(conn, "a1"))
        txt = conn.execute("SELECT text FROM notifications WHERE key LIKE "
                           "'quota:a1%'").fetchone()["text"]
        self.assertIn("hit 1", txt)
        self.assertIn("resumes automatically", txt)
        conn.close()

    def test_help_and_unknown_command_get_replies(self):
        conn = self.sb.conn()
        sent = []

        class T:
            def send(self, t):
                sent.append(t)

        control.handle_commands(self.sb.ctx(), conn, T(), ["/help"])
        self.assertIn("/why", sent[-1])
        self.assertIn("/retry", sent[-1])
        control.handle_commands(self.sb.ctx(), conn, T(), ["/bogus"])
        self.assertIn("unknown command /bogus", sent[-1])
        self.assertIn("/help", sent[-1])
        conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
