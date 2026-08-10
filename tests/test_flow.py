"""Canary end-to-end acceptance matrix (items 1-12, 16, 17, 20 + supervision).
Every scenario runs the REAL controller/dispatch/supervision path against a
disposable canary repo; only the model is stubbed (tests/stub_worker.py)."""
import json
import subprocess
import sys
import time
import unittest
from pathlib import Path

from harness import CODE, PY, Sandbox, git

sys.path.insert(0, str(CODE))
import claude_runner as cr  # noqa: E402
import common               # noqa: E402
import control              # noqa: E402
import db                   # noqa: E402
import gitops               # noqa: E402


def ladder(conn):
    row = conn.execute(
        "SELECT COUNT(*) c FROM runs WHERE role IN ('implementer','repair') "
        "AND status IN ('DONE','FAILED')").fetchone()
    return row["c"]


class FlowBase(unittest.TestCase):
    script = {}
    telegram_ok = True

    def setUp(self):
        self.sb = Sandbox(script=self.script, telegram_ok=self.telegram_ok)
        self.sb.start()

    def tearDown(self):
        self.sb.cleanup()


class TestHappyPath(FlowBase):
    """Happy path + acceptance 4 (worktree isolation), 16 (idempotent
    integration), 20 (reproducible checkpoint)."""

    def test_accept_and_checkpoints(self):
        self.assertTrue(self.sb.until_state("ACCEPTED"), self.sb.transitions())
        conn = self.sb.conn()
        ws = self.sb.ws

        # ordered lifecycle happened
        seq = [t[1] for t in self.sb.transitions()]
        for a, b in [("PLANNING", "IMPLEMENTING"), ("IMPLEMENTING", "TESTING"),
                     ("TESTING", "REVIEWING"), ("REVIEWING", "VALIDATING"),
                     ("VALIDATING", "ACCEPTED")]:
            self.assertLess(seq.index(a), seq.index(b), seq)

        # 4: implementer edited a worktree, not the integration checkout
        impl = conn.execute("SELECT * FROM runs WHERE role='implementer'").fetchone()
        self.assertIn("artifacts", impl["cwd"])
        self.assertNotEqual(Path(impl["cwd"]).resolve(), Path(ws).resolve())
        st = subprocess.run(["git", "status", "--porcelain"], cwd=ws,
                            capture_output=True, text=True).stdout.strip()
        self.assertEqual(st, "", "integration checkout must stay clean")

        # integration got exactly the cherry-picked commit(s), with provenance
        commits = conn.execute("SELECT * FROM commits WHERE integrated_sha IS NOT NULL").fetchall()
        self.assertGreaterEqual(len(commits), 1)
        msg = gitops.head_commit_message(Path(ws), commits[0]["integrated_sha"])
        self.assertIn("cherry picked from commit", msg)
        self.assertIn("EC-Key:", msg)

        # 16: replaying the review->validation path cannot duplicate commits
        count_before = gitops.git_ro(["rev-list", "--count", "automation/integration"],
                                     cwd=ws).stdout.strip()
        step = self.sb.step_row(conn)
        review = conn.execute(
            "SELECT * FROM runs WHERE role='reviewer' AND status='DONE' "
            "ORDER BY id DESC LIMIT 1").fetchone()
        roadmap = control.load_roadmap(self.sb.ctx())
        control.on_review(self.sb.ctx(), conn, roadmap, step, review)  # replay
        count_after = gitops.git_ro(["rev-list", "--count", "automation/integration"],
                                    cwd=ws).stdout.strip()
        self.assertEqual(count_before, count_after, "replay duplicated cherry-picks")
        conn.close()
        self.assertTrue(self.sb.until_state("ACCEPTED"), "replay must reconverge")

        # 20: accepted checkpoint reproducible from stored commit id alone
        conn = self.sb.conn()
        tip = conn.execute("SELECT integrated_sha FROM commits ORDER BY id DESC LIMIT 1"
                           ).fetchone()["integrated_sha"]
        conn.close()
        wt = self.sb.dir / "checkpoint"
        gitops.worktree_add_detached(Path(ws), wt, tip)
        p = subprocess.run(["cmd", "/d", "/c", "python test_canary.py"], cwd=wt,
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

        # handoff emitted and valid
        conn = self.sb.conn()
        hp = self.sb.step_row(conn)["handoff_path"]
        conn.close()
        handoff = json.loads(Path(hp).read_text())
        self.assertEqual(common.schema_errors(
            handoff, common.load_schema("handoff.schema.json")), [])
        self.assertEqual(handoff["accepted_commits"][0]["sha"], tip)


class TestOverlapAndCrash(FlowBase):
    """Acceptance 1 (duplicate ticks), 2 (killed tick), 3 (recover dispatched
    identity after crash between spawn and record)."""

    def test_1_overlapping_ticks_noop(self):
        p1 = self.sb.spawn_tick({"EC_TEST_HOLD_LOCK": "4"})
        time.sleep(1.5)
        t0 = time.time()
        rc = self.sb.tick()          # must bounce off the lock
        self.assertEqual(rc, 0)
        self.assertLess(time.time() - t0, 2.0)
        p1.wait(timeout=30)
        runs = self.sb.runs()
        self.assertEqual(len([r for r in runs if r["role"] == "planner"]), 1,
                         "duplicate tick dispatched duplicate work")

    def test_2_killed_tick_loses_nothing(self):
        p = self.sb.spawn_tick({"EC_TEST_HOLD_LOCK": "8"})
        time.sleep(1.5)
        p.kill()
        p.wait(timeout=10)
        self.assertEqual(len(self.sb.runs()), 0)   # died before dispatch: no orphan state
        rc = self.sb.tick()                        # lock auto-released by OS
        self.assertEqual(rc, 0)
        self.assertEqual(len([r for r in self.sb.runs() if r["role"] == "planner"]), 1)

    def test_3_crash_after_spawn_recovers_identity(self):
        p = self.sb.spawn_tick({"EC_TEST_CRASH_AFTER": "spawn"})
        p.wait(timeout=30)
        self.assertEqual(p.returncode, 9)
        runs = self.sb.runs()
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["status"], "PREPARED")  # crash before record
        # wait until the orphaned worker has left on-disk evidence, so the
        # recovery tick must take the adoption path (not a guarded respawn)
        art = Path(runs[0]["artifact_dir"])
        deadline = time.time() + 15
        while time.time() < deadline and not (art / "started.txt").exists():
            time.sleep(0.2)
        self.assertTrue((art / "started.txt").exists(), "orphan worker never started")
        # recovery adopts the already-dispatched worker instead of respawning
        self.assertTrue(self.sb.run_until(
            lambda c: c.execute("SELECT COUNT(*) c FROM runs WHERE role='planner' "
                                "AND status='DONE'").fetchone()["c"] == 1, 30))
        conn = self.sb.conn()
        adopted = conn.execute("SELECT COUNT(*) c FROM events WHERE kind='run_adopted'"
                               ).fetchone()["c"]
        planners = conn.execute("SELECT COUNT(*) c FROM runs WHERE role='planner'"
                                ).fetchone()["c"]
        conn.close()
        self.assertEqual(planners, 1)
        self.assertGreaterEqual(adopted, 1)
        launched = [l for l in self.sb.stub_calls() if ".planner." in l]
        self.assertEqual(len(launched), 1, "worker must have executed exactly once")
        self.assertTrue(self.sb.until_state("ACCEPTED"))


class TestFailedWorker(FlowBase):
    """Acceptance 5: failed worker state is detected; repair takes over."""
    script = {"implementer": "fail", "repair": "ok"}

    def test_flow(self):
        self.assertTrue(self.sb.until_state("ACCEPTED"), self.sb.transitions())
        conn = self.sb.conn()
        impl = conn.execute("SELECT * FROM runs WHERE role='implementer'").fetchone()
        self.assertEqual(impl["status"], "FAILED")
        self.assertEqual(ladder(conn), 2)  # failed impl + successful repair
        conn.close()
        self.assertIn(("IMPLEMENTING", "REPAIRING"), self.sb.transitions())


class TestMalformedResult(FlowBase):
    """Acceptance 7: malformed/missing result artifacts are rejected."""
    script = {"implementer.1": "malformed", "implementer.2": "noresult",
              "repair": "ok"}

    def test_flow(self):
        self.assertTrue(self.sb.until_state("REPAIRING", 60), self.sb.transitions())
        conn = self.sb.conn()
        d = db.step_detail(self.sb.step_row(conn))
        conn.close()
        self.assertIn("malformed", d.get("last_findings", ""))
        self.assertTrue(self.sb.until_state("ACCEPTED"), self.sb.transitions())


class TestValidatorGate(FlowBase):
    """Acceptance 8: failing canonical tests prevent acceptance (real test run)."""
    script = {"implementer": "bad_code", "repair": "ok"}

    def test_flow(self):
        self.assertTrue(self.sb.until_state("ACCEPTED"), self.sb.transitions())
        trans = self.sb.transitions()
        self.assertIn(("TESTING", "REPAIRING"), trans,
                      "red tests must route to repair, not review/acceptance")
        conn = self.sb.conn()
        note = conn.execute("SELECT text FROM notifications WHERE text LIKE '%tests FAILED%'"
                            ).fetchone()
        conn.close()
        self.assertIsNotNone(note)


class TestReviewerGate(FlowBase):
    """Acceptance 9: reviewer REPAIR verdict enters REPAIRING, never ACCEPTED."""
    script = {"reviewer.1": "repair", "repair": "ok", "reviewer.2": "pass"}

    def test_flow(self):
        self.assertTrue(self.sb.until_state("ACCEPTED"), self.sb.transitions())
        self.assertIn(("REVIEWING", "REPAIRING"), self.sb.transitions())
        conn = self.sb.conn()
        reviews = conn.execute("SELECT COUNT(*) c FROM runs WHERE role='reviewer'"
                               ).fetchone()["c"]
        conn.close()
        self.assertEqual(reviews, 2)


class TestRepairBudget(FlowBase):
    """Acceptance 10: the ladder blocks infinite repair loops; a blocked step
    halts the roadmap (never skipped)."""
    script = {"implementer": "fail", "repair": "fail"}

    def test_flow(self):
        self.assertTrue(self.sb.until_state("BLOCKED", 120), self.sb.transitions())
        conn = self.sb.conn()
        roles = [r["role"] for r in conn.execute(
            "SELECT role FROM runs ORDER BY id") if r["role"] != "test"]
        self.assertEqual(roles, ["planner", "implementer", "repair", "repair",
                                 "diagnostic", "repair"])
        self.assertEqual(ladder(conn), 4)
        n_runs = len(self.sb.runs())
        conn.close()
        for _ in range(4):
            self.sb.tick()
        self.assertEqual(len(self.sb.runs()), n_runs, "BLOCKED must stop dispatching")
        self.assertEqual(self.sb.state(), "BLOCKED")


class TestQuota(FlowBase):
    """Acceptance 11: WAITING_QUOTA persists, notifies, retries, and does NOT
    consume an implementation attempt."""
    script = {"implementer.1": "quota", "implementer.2": "ok"}

    def test_flow(self):
        self.assertTrue(self.sb.run_until(
            lambda c: ("WAITING_QUOTA" in [t["to_state"] for t in
                       c.execute("SELECT * FROM transitions")])
            or self.sb.step_row(c)["state"] == "ACCEPTED", 60))
        self.assertTrue(self.sb.until_state("ACCEPTED"), self.sb.transitions())
        conn = self.sb.conn()
        quota_runs = conn.execute("SELECT COUNT(*) c FROM runs WHERE status='QUOTA'"
                                  ).fetchone()["c"]
        self.assertEqual(quota_runs, 1)
        self.assertEqual(ladder(conn), 1, "quota wait must not consume an attempt")
        conn.close()
        self.assertIn("WAITING_QUOTA", [t[1] for t in self.sb.transitions()])


class TestWaitingConfig(FlowBase):
    """Acceptance 12: missing telegram creds -> WAITING_CONFIG, no crash, no
    attempt consumed; recovers when config appears."""
    telegram_ok = False

    def test_flow(self):
        for _ in range(3):
            self.assertEqual(self.sb.tick(), 0)
        self.assertEqual(self.sb.state(), "WAITING_CONFIG")
        self.assertEqual(len(self.sb.runs()), 0)
        env = (self.sb.root / ".env").read_text()
        (self.sb.root / ".env").write_text(env + "EC_ALLOW_NO_TELEGRAM=1\n")
        self.assertTrue(self.sb.until_state("ACCEPTED"), self.sb.transitions())
        conn = self.sb.conn()
        self.assertEqual(ladder(conn), 1)
        conn.close()


class TestInterrupted(FlowBase):
    """Acceptance 6: a dispatched worker killed mid-flight is detected as
    LOST, the step goes INTERRUPTED, and reconciliation respawns without
    consuming an attempt for the lost run."""
    script = {"implementer.1": "hang", "implementer.2": "ok"}

    def setUp(self):
        self.sb = Sandbox(script=self.script,
                          extra_env="EC_STUB_HANG=60\n")
        self.sb.start()

    def test_flow(self):
        self.assertTrue(self.sb.run_until(
            lambda c: c.execute("SELECT COUNT(*) c FROM runs WHERE role='implementer' "
                                "AND status='DISPATCHED'").fetchone()["c"] == 1, 45))
        conn = self.sb.conn()
        run = conn.execute("SELECT * FROM runs WHERE role='implementer'").fetchone()
        conn.close()
        pidf = Path(run["artifact_dir"]) / "pid.txt"
        for _ in range(40):
            if pidf.exists():
                break
            time.sleep(0.25)
        pid = pidf.read_text().strip()
        subprocess.run(["taskkill", "/PID", pid, "/T", "/F"],
                       capture_output=True, text=True)
        self.assertTrue(self.sb.run_until(
            lambda c: "INTERRUPTED" in [t["to_state"] for t in
                                        c.execute("SELECT * FROM transitions")], 30))
        self.assertTrue(self.sb.until_state("ACCEPTED"), self.sb.transitions())
        conn = self.sb.conn()
        lost = conn.execute("SELECT COUNT(*) c FROM runs WHERE status='LOST'").fetchone()["c"]
        self.assertEqual(lost, 1)
        self.assertEqual(ladder(conn), 1, "LOST run must not count as an attempt")
        conn.close()


class TestCherryConflict(FlowBase):
    """Acceptance 17: conflicting cherry-pick -> abort + REPAIRING/BLOCKED,
    never a destructive reset; integration stays clean."""
    script = {"reviewer.1": "pass_slow"}

    def test_flow(self):
        self.assertTrue(self.sb.until_state("REVIEWING", 60), self.sb.transitions())
        # advance integration with a conflicting change while review runs
        ws = self.sb.ws
        (Path(ws) / "app.py").write_text("def add(a, b):\n    return b + a  # conflict\n")
        git(["add", "-A"], ws)
        git(["commit", "-m", "owner-side conflicting change"], ws)
        injected_tip = git(["rev-parse", "HEAD"], ws).strip()
        self.assertTrue(self.sb.run_until(
            lambda c: c.execute("SELECT COUNT(*) c FROM events WHERE kind='cherry_conflict'"
                                ).fetchone()["c"] >= 1, 60), self.sb.transitions())
        self.assertIn(self.sb.state(), ("REPAIRING", "BLOCKED"))
        st = subprocess.run(["git", "status", "--porcelain"], cwd=ws,
                            capture_output=True, text=True).stdout.strip()
        self.assertEqual(st, "", "cherry-pick must be aborted cleanly")
        self.assertFalse((Path(ws) / ".git" / "CHERRY_PICK_HEAD").exists())
        tip = git(["rev-parse", "HEAD"], ws).strip()
        self.assertEqual(tip, injected_tip, "no destructive recovery on integration")


class TestMultiTask(FlowBase):
    """Cross-repo-shaped plans: ordered tasks, each with its own worktree and
    integration; step accepted only after every task validates."""
    script = {"planner": "two_tasks"}

    def test_flow(self):
        self.assertTrue(self.sb.until_state("ACCEPTED", 150), self.sb.transitions())
        conn = self.sb.conn()
        d = db.step_detail(self.sb.step_row(conn))
        self.assertEqual(d.get("done_tasks"), [0, 1])
        for idx in (0, 1):
            n = conn.execute(
                "SELECT COUNT(*) c FROM commits WHERE task_idx=? AND integrated_sha "
                "IS NOT NULL", (idx,)).fetchone()["c"]
            self.assertGreaterEqual(n, 1, f"task {idx} not integrated")
        # each task must run under its OWN identity/objective, with no phantom
        # repair compensating for a misdispatched implementer (audit regression)
        repairs = conn.execute("SELECT COUNT(*) c FROM runs WHERE role='repair'"
                               ).fetchone()["c"]
        self.assertEqual(repairs, 0, "phantom repair in multi-task happy path")
        keys = [r["key"] for r in conn.execute(
            "SELECT key FROM runs WHERE role='implementer' ORDER BY id")]
        self.assertEqual(keys, ["c1.c0.t0.implementer.1", "c1.c0.t1.implementer.1"])
        conn.close()
        app = (Path(self.sb.ws) / "app.py").read_text()
        self.assertIn("def mul", app)
        self.assertIn("def sub", app)


class TestTaskAdvanceCrashSafe(FlowBase):
    """Audit finding 1 regression: a crash after task 0 is marked done but
    before task 1 dispatches must resume task 1 — never skip it, never accept
    the step early, never duplicate the task-1 worker."""
    script = {"planner": "two_tasks"}

    def test_flow(self):
        self.assertTrue(self.sb.run_until(
            lambda c: db.step_detail(self.sb.step_row(c)).get("done_tasks") == [0], 120),
            self.sb.transitions())
        conn = self.sb.conn()
        val0 = conn.execute(
            "SELECT * FROM runs WHERE role='test' AND task_idx=0 AND status='DONE' "
            "ORDER BY id DESC LIMIT 1").fetchone()
        self.assertIsNotNone(val0)
        with conn:  # freeze the exact crash window state
            conn.execute("UPDATE steps SET state='VALIDATING', active_run_id=? "
                         "WHERE id='c1'", (val0["id"],))
        conn.close()
        self.assertTrue(self.sb.until_state("ACCEPTED", 150), self.sb.transitions())
        conn = self.sb.conn()
        d = db.step_detail(self.sb.step_row(conn))
        self.assertEqual(d.get("done_tasks"), [0, 1], "a task was skipped")
        impls_t1 = conn.execute(
            "SELECT COUNT(*) c FROM runs WHERE role='implementer' AND task_idx=1"
            ).fetchone()["c"]
        conn.close()
        self.assertEqual(impls_t1, 1, "replay duplicated the task-1 worker")


class TestRetryCycle(FlowBase):
    """/retry re-arms a BLOCKED step with a fresh cycle and a fresh ladder."""
    script = {"implementer": "fail", "repair": "fail"}

    def test_flow(self):
        self.assertTrue(self.sb.until_state("BLOCKED", 150), self.sb.transitions())
        ctx = self.sb.ctx()
        conn = self.sb.conn()
        sent = []

        class T:
            def send(self, t):
                sent.append(t)
                return True
        import control as _c
        _c.handle_commands(ctx, conn, T(), ["/retry"])
        conn.close()
        self.assertTrue(any("re-armed" in s for s in sent), sent)
        self.sb.set_script({})  # new cycle succeeds
        self.assertTrue(self.sb.until_state("ACCEPTED", 150), self.sb.transitions())
        conn = self.sb.conn()
        d = db.step_detail(self.sb.step_row(conn))
        self.assertEqual(d.get("cycle"), 1)
        ok_impl = conn.execute(
            "SELECT COUNT(*) c FROM runs WHERE cycle=1 AND role='implementer' "
            "AND status='DONE'").fetchone()["c"]
        conn.close()
        self.assertEqual(ok_impl, 1)


class TestReviewerBlock(FlowBase):
    """Reviewer BLOCK verdict halts the step without burning repair rungs."""
    script = {"reviewer.1": "block"}

    def test_flow(self):
        self.assertTrue(self.sb.until_state("BLOCKED", 90), self.sb.transitions())
        conn = self.sb.conn()
        repairs = conn.execute("SELECT COUNT(*) c FROM runs WHERE role='repair'"
                               ).fetchone()["c"]
        conn.close()
        self.assertEqual(repairs, 0)


class TestSupervision(unittest.TestCase):
    """Windows process-supervision reality checks (SSH/tick-death immunity)."""

    def test_orphan_worker_survives_dispatcher_death(self):
        import tempfile
        art = Path(tempfile.mkdtemp(prefix="ec-orph-")) / "job"
        p = subprocess.run([PY, str(CODE / "tests" / "helper_orphan.py"), str(art)],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        deadline = time.time() + 20
        while time.time() < deadline and not (art / "exitcode.txt").exists():
            time.sleep(0.5)
        self.assertTrue((art / "exitcode.txt").exists(),
                        "worker died with its dispatcher")
        self.assertEqual((art / "exitcode.txt").read_text().strip(), "0")

    def test_task_lane_end_to_end(self):
        """One real one-shot Scheduled Task run through the shim contract.
        Root must NOT come from tempfile.mkdtemp: its restricted Windows DACL
        is unreadable from the Task Scheduler logon session (commissioning
        finding — this is why this test failed in Step 0)."""
        from harness import mkroot
        root = mkroot("ec-task-")
        ctx = common.Ctx(root)
        key = "canary.tasklane.probe.1"
        rd = cr.art_dir(ctx, key)
        rd.mkdir(parents=True, exist_ok=True)
        (rd / "job.json").write_text(json.dumps({
            "argv": [PY, "-c", "print('task lane ok')"], "cwd": str(rd),
            "stdin": None, "timeout": 120, "env_unset": [], "env_set": {}}))
        try:
            cr.spawn(ctx, key, "task")
        except RuntimeError as e:
            self.skipTest(f"schtasks unavailable: {e}")
        try:
            deadline = time.time() + 120
            while time.time() < deadline and not (rd / "exitcode.txt").exists():
                time.sleep(1)
            self.assertTrue((rd / "exitcode.txt").exists(), "task never ran")
            self.assertEqual((rd / "exitcode.txt").read_text().strip(), "0")
            self.assertIn("task lane ok", (rd / "stdout.txt").read_text())
        finally:
            cr.delete_task(key)


if __name__ == "__main__":
    unittest.main(verbosity=2)
