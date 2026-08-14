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

        # /pause and /resume also flip the news appliance's flag through
        # newsops (a real `python -m app` subprocess) — stub that boundary,
        # same as test_news.py stubs _app.
        news_calls = []
        orig = (control.newsops.pause, control.newsops.resume,
                control.start_sshd, control.start_observatory)
        control.newsops.pause = lambda ctx: news_calls.append("pause") or "news appliance frozen"
        control.newsops.resume = lambda ctx: news_calls.append("resume") or "news appliance resumed"
        control.start_sshd = lambda: "ssh server already running"
        control.start_observatory = lambda ctx: "observatory started · https://x/observatory/"
        try:
            control.handle_commands(self.sb.ctx(), conn, T(), ["/pause"])
            self.assertIn("/pause at", db.kv_get(conn, "paused_why"))
            self.assertIn("news appliance frozen", sent[-1])
            control.handle_commands(self.sb.ctx(), conn, T(), ["/workers"])
            self.assertIn("PAUSED: /pause at", sent[-1])
            control.handle_commands(self.sb.ctx(), conn, T(), ["/resume"])
            self.assertEqual(db.kv_get(conn, "paused_why"), "")
            self.assertIn("news appliance resumed", sent[-1])
            self.assertIn("ssh server already running", sent[-1])
            self.assertIn("observatory started", sent[-1])
            self.assertEqual(news_calls, ["pause", "resume"])
        finally:
            (control.newsops.pause, control.newsops.resume,
             control.start_sshd, control.start_observatory) = orig
        conn.close()

    def test_start_sshd_reports_each_outcome_and_never_raises(self):
        from unittest import mock

        def fake(returncode, stdout=""):
            return mock.Mock(returncode=returncode, stdout=stdout, stderr="")

        # already running: query says RUNNING, no task trigger pulled
        with mock.patch("subprocess.run",
                        side_effect=[fake(0, "STATE : 4  RUNNING")]) as m:
            self.assertEqual(control.start_sshd(), "ssh server already running")
            self.assertEqual(m.call_count, 1)
        # stopped -> task triggered -> service comes up on the second poll
        with mock.patch("time.sleep"), \
             mock.patch("subprocess.run",
                        side_effect=[fake(0, "STATE : 1  STOPPED"), fake(0),
                                     fake(0, "STATE : 2  START_PENDING"),
                                     fake(0, "STATE : 4  RUNNING")]) as m:
            self.assertEqual(control.start_sshd(), "ssh server started")
            self.assertEqual(m.call_args_list[1].args[0],
                             ["schtasks", "/run", "/tn", control.SSHD_TASK])
        # stopped -> task missing/denied: chat text with the fix, no raise
        with mock.patch("subprocess.run",
                        side_effect=[fake(0, "STATE : 1  STOPPED"),
                                     fake(1, "ERROR: The system cannot find the task.")]):
            out = control.start_sshd()
            self.assertIn("ssh start task failed", out)
            self.assertIn("re-register", out)

    def test_tasks_text_filters_foreign_tasks_and_reports_sshd(self):
        from unittest import mock
        header = ('"HostName","TaskName","Next Run Time","Status",'
                  '"Logon Mode","Last Run Time","Last Result"\n')
        csv_out = (
            header
            + '"PC","\\internet-discovery-collect-web","8/14/2026 1:30:00 PM",'
              '"Ready","Interactive","8/14/2026 1:29:00 PM","0"\n'
            + '"PC","\\internet-discovery-collect-web","8/14/2026 1:30:00 PM",'
              '"Ready","Interactive","8/14/2026 1:29:00 PM","0"\n'  # 2nd trigger row
            + header  # schtasks repeats the header per task folder
            + '"PC","\\OneDrive Standalone Update Task","N/A","Ready",'
              '"Interactive","N/A","0"\n'
            + '"PC","\\engine-control-start-sshd","N/A","Ready",'
              '"Interactive","N/A","267011"\n'
        )

        def fake(args, **kw):
            if args[0] == "schtasks":
                return mock.Mock(returncode=0, stdout=csv_out, stderr="")
            return mock.Mock(returncode=0, stdout="STATE : 1  STOPPED", stderr="")

        orig = control._observatory_line
        control._observatory_line = lambda ctx: "🔬 observatory DOWN · http://127.0.0.1:8010/observatory/"
        try:
            with mock.patch("subprocess.run", side_effect=fake):
                out = control.tasks_text(self.sb.ctx())
        finally:
            control._observatory_line = orig
        self.assertIn("news·collect-web: Ready", out)
        self.assertIn("ec·start-sshd: Ready", out)
        self.assertNotIn("OneDrive", out)
        self.assertIn("sshd STOPPED", out)
        self.assertIn("observatory", out)
        self.assertEqual(out.count("collect-web"), 1)  # deduped trigger rows

    def test_observatory_line_prefers_the_live_ngrok_url(self):
        import tempfile
        import types
        from unittest import mock

        class FakeResp:
            def __init__(self, payload):
                self._p = payload

            def read(self):
                return self._p

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with tempfile.TemporaryDirectory() as tmp:
            ctx = types.SimpleNamespace(
                getenv=lambda k, d=None: tmp if k == "EC_NEWS_ROOT" else d)
            tunnels = json.dumps({"tunnels": [
                {"public_url": "https://abc123.ngrok-free.app",
                 "config": {"addr": "http://localhost:8010"}}]}).encode()
            # ngrok up + server up: the public URL, marked up
            with mock.patch("urllib.request.urlopen",
                            return_value=FakeResp(tunnels)), \
                 mock.patch("socket.create_connection",
                            return_value=mock.MagicMock()):
                line = control._observatory_line(ctx)
            self.assertEqual(
                line, "🔬 observatory up · https://abc123.ngrok-free.app/observatory/")
            # ngrok down, no .env override, port closed: local URL, DOWN
            with mock.patch("urllib.request.urlopen", side_effect=OSError), \
                 mock.patch("socket.create_connection", side_effect=OSError):
                line = control._observatory_line(ctx)
            self.assertEqual(
                line, "🔬 observatory DOWN · http://127.0.0.1:8010/observatory/")

    def test_start_observatory_outcomes(self):
        from unittest import mock
        ctx = self.sb.ctx()
        # already up: no task trigger, current URL handed back
        with mock.patch.object(control, "_port_up", return_value=True), \
             mock.patch.object(control, "_observatory_url",
                               return_value="https://abc.ngrok-free.app"):
            self.assertEqual(
                control.start_observatory(ctx),
                "observatory already up · https://abc.ngrok-free.app/observatory/")
        # down -> task triggered -> port up -> fresh ngrok URL
        ports = iter([False, True])
        with mock.patch.object(control, "_port_up",
                               side_effect=lambda *a, **k: next(ports)), \
             mock.patch.object(control, "_ngrok_public_url",
                               return_value="https://abc.ngrok-free.app"), \
             mock.patch("time.sleep"), \
             mock.patch("subprocess.run",
                        return_value=mock.Mock(returncode=0, stdout="", stderr="")) as m:
            self.assertEqual(
                control.start_observatory(ctx),
                "observatory started · https://abc.ngrok-free.app/observatory/")
            self.assertEqual(m.call_args_list[0].args[0],
                             ["schtasks", "/run", "/tn", control.OBS_TASK])
        # task missing/denied: chat text with the fix, no raise
        with mock.patch.object(control, "_port_up", return_value=False), \
             mock.patch("subprocess.run",
                        return_value=mock.Mock(returncode=1,
                                               stdout="ERROR: no task", stderr="")):
            out = control.start_observatory(ctx)
            self.assertIn("observatory task failed", out)
            self.assertIn("re-register", out)

    def test_tasks_command_routes_to_tasks_text(self):
        conn = self.sb.conn()
        sent = []

        class T:
            def send(self, t):
                sent.append(t)

        orig = control.tasks_text
        control.tasks_text = lambda ctx: "🗓 tasks (0) · sshd RUNNING"
        try:
            control.handle_commands(self.sb.ctx(), conn, T(), ["/tasks"])
        finally:
            control.tasks_text = orig
        self.assertIn("🗓 tasks", sent[-1])
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
        self.assertIn("round 0 · hard-failed 0/4", txt)
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
