"""DAG correctness (§42), scheduler capacity transitions (§41), parallel
worktrees (§43) and recovery under concurrency (§44). Real controller +
dispatch + git paths; only the model is stubbed."""
import json
import subprocess
import sys
import time
import unittest
from pathlib import Path

from harness import CODE, Sandbox

sys.path.insert(0, str(CODE))
import capacity as cap      # noqa: E402
import common               # noqa: E402
import control              # noqa: E402
import db                   # noqa: E402
import gitops               # noqa: E402

FIVE_INDEP = """
steps:
  - {id: A, ordinal: 1, title: a, repos: [canary],  depends_on: [], objective: a, acceptance: a}
  - {id: B, ordinal: 2, title: b, repos: [canary2], depends_on: [], objective: b, acceptance: b}
  - {id: C, ordinal: 3, title: c, repos: [canary],  depends_on: [], objective: c, acceptance: c}
  - {id: D, ordinal: 4, title: d, repos: [canary2], depends_on: [], objective: d, acceptance: d}
  - {id: E, ordinal: 5, title: e, repos: [canary],  depends_on: [], objective: e, acceptance: e}
"""

DIAMOND = """
steps:
  - {id: A,  ordinal: 1, title: a, repos: [canary],  depends_on: [], objective: a, acceptance: a}
  - {id: B,  ordinal: 2, title: b, repos: [canary2], depends_on: [], objective: b, acceptance: b}
  - {id: C,  ordinal: 3, title: c, repos: [canary],  depends_on: [A], objective: c, acceptance: c}
  - {id: D,  ordinal: 4, title: d, repos: [canary],  depends_on: [A], objective: d, acceptance: d}
  - {id: E,  ordinal: 5, title: e, repos: [canary],  depends_on: [C, D], objective: e, acceptance: e}
"""


def alive(pid):
    p = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                       capture_output=True, text=True)
    return (p.stdout or "").strip().startswith('"')


class TestDagTopology(unittest.TestCase):
    def test_cycle_and_unknown_dep_detected(self):
        rm = {"steps": [{"id": "x", "ordinal": 1, "depends_on": ["y"]},
                        {"id": "y", "ordinal": 2, "depends_on": ["x"]}]}
        errs = control.dag_errors(rm)
        self.assertTrue(any("cycle" in e for e in errs), errs)
        rm = {"steps": [{"id": "x", "ordinal": 1, "depends_on": ["ghost"]}]}
        self.assertTrue(any("unknown dependency" in e for e in control.dag_errors(rm)))

    def test_crit_weight_prefers_unblockers(self):
        rm = {"steps": [
            {"id": "a", "ordinal": 1, "depends_on": []},
            {"id": "b", "ordinal": 2, "depends_on": []},
            {"id": "c", "ordinal": 3, "depends_on": ["a"]},
            {"id": "d", "ordinal": 4, "depends_on": ["c"]},
            {"id": "bg", "ordinal": 5, "depends_on": [], "background": True},
        ]}
        w = control.crit_weight(rm)
        self.assertGreater(w["a"], w["b"])   # a unblocks a chain
        self.assertEqual(w["bg"], 0)         # background never critical

    def test_production_roadmap_dag_valid(self):
        roadmap = common.load_yaml(CODE / "roadmap.yaml")
        self.assertEqual(control.dag_errors(roadmap), [])
        w = control.crit_weight(roadmap)
        # step-02 heads the personalization chain: heaviest unblocker
        self.assertEqual(max(w, key=w.get), "step-02")
        self.assertEqual(w["step-09a"], 0)


class TestParallelBranches(unittest.TestCase):
    """§42: independent steps run concurrently; dependents wait; the diamond
    joins; §43: same-repo parallel steps use isolated worktrees and integrate
    serially without corruption."""

    def setUp(self):
        self.sb = Sandbox(steps_yaml=DIAMOND, repos=("canary", "canary2"),
                          script={"B.repo": "canary2"})
        self.sb.start()

    def tearDown(self):
        self.sb.cleanup()

    def test_diamond_flow(self):
        # A and B (independent) both become active before either accepts
        self.assertTrue(self.sb.run_until(
            lambda c: c.execute("SELECT COUNT(DISTINCT step_id) n FROM runs "
                                "WHERE step_id IN ('A','B')").fetchone()["n"] == 2, 60),
            self.sb.states())
        st = self.sb.states()
        self.assertNotIn(st["A"], ("ACCEPTED",))
        # C and D must not start before A is accepted
        self.assertEqual(st["C"], "PENDING")
        self.assertEqual(st["D"], "PENDING")
        self.assertTrue(self.sb.run_until(
            lambda c: all(s["state"] == "ACCEPTED" for s in
                          c.execute("SELECT state FROM steps")), 300),
            self.sb.states())
        conn = self.sb.conn()
        # C, D started only after A accepted; E only after both C and D
        tr = conn.execute("SELECT * FROM transitions ORDER BY id").fetchall()
        idx = {}
        for i, t in enumerate(tr):
            idx.setdefault((t["step_id"], t["to_state"]), i)
        self.assertLess(idx[("A", "ACCEPTED")], idx[("C", "PLANNING")])
        self.assertLess(idx[("A", "ACCEPTED")], idx[("D", "PLANNING")])
        self.assertLess(idx[("C", "ACCEPTED")], idx[("E", "PLANNING")])
        self.assertLess(idx[("D", "ACCEPTED")], idx[("E", "PLANNING")])
        # C and D overlapped (both had dispatched runs before either accepted)
        first_cd_accept = min(idx[("C", "ACCEPTED")], idx[("D", "ACCEPTED")])
        self.assertLess(max(idx[("C", "PLANNING")], idx[("D", "PLANNING")]),
                        first_cd_accept, "C and D should run concurrently")
        # §43: distinct isolated worktrees per step
        cwds = {r["step_id"]: r["cwd"] for r in conn.execute(
            "SELECT * FROM runs WHERE role='implementer'")}
        self.assertEqual(len(set(cwds.values())), len(cwds))
        for c_ in cwds.values():
            self.assertIn("artifacts", c_)
        # all four canary-repo features integrated; linear history; clean tree
        ws = self.sb.repos["canary"]["ws"]
        for s in ("A", "C", "D", "E"):
            self.assertTrue((Path(ws) / f"feat_{s}.py").exists(), s)
        merges = gitops.git_ro(["rev-list", "--merges", "automation/integration"],
                               cwd=ws).stdout.strip()
        self.assertEqual(merges, "")
        stat = subprocess.run(["git", "status", "--porcelain"], cwd=ws,
                              capture_output=True, text=True).stdout.strip()
        self.assertEqual(stat, "")
        # base commit recorded for every implementer run
        for r in conn.execute("SELECT * FROM runs WHERE role='implementer'"):
            self.assertTrue(db.step_detail(self.sb.step_row(conn, r["step_id"]))
                            .get("task_base"))
        conn.close()


class TestBackgroundLane(unittest.TestCase):
    """Background steps never steal a critical-path slot (§23/§29)."""

    def setUp(self):
        steps = """
steps:
  - {id: A,  ordinal: 1, title: a, repos: [canary],  depends_on: [], objective: a, acceptance: a}
  - {id: B,  ordinal: 2, title: b, repos: [canary2], depends_on: [], objective: b, acceptance: b}
  - {id: BG, ordinal: 3, title: bg, repos: [canary2], depends_on: [], background: true, objective: g, acceptance: g}
"""
        self.sb = Sandbox(steps_yaml=steps, repos=("canary", "canary2"),
                          script={"B.repo": "canary2", "BG.repo": "canary2"},
                          capacity=dict(tier="max5"))
        self.sb.start()

    def tearDown(self):
        self.sb.cleanup()

    def test_background_waits_for_critical(self):
        self.assertTrue(self.sb.run_until(
            lambda c: c.execute("SELECT COUNT(DISTINCT step_id) n FROM runs "
                                "WHERE step_id IN ('A','B')").fetchone()["n"] == 2, 60))
        # while both critical steps hold the max5 envelope, BG stays PENDING
        self.assertEqual(self.sb.state("BG"), "PENDING")
        self.assertTrue(self.sb.run_until(
            lambda c: all(s["state"] == "ACCEPTED" for s in
                          c.execute("SELECT state FROM steps")), 300),
            self.sb.states())
        conn = self.sb.conn()
        bg_first = conn.execute("SELECT MIN(id) i FROM runs WHERE step_id='BG'"
                                ).fetchone()["i"]
        crit_first = conn.execute("SELECT MIN(id) i FROM runs WHERE step_id IN "
                                  "('A','B')").fetchone()["i"]
        conn.close()
        self.assertGreater(bg_first, crit_first)


class TestUpgradeDowngrade(unittest.TestCase):
    """§19/§41: Max5 -> Max20 fills lanes automatically; Max20 -> Max5 drains
    without killing useful in-flight workers."""

    def setUp(self):
        self.sb = Sandbox(steps_yaml=FIVE_INDEP, repos=("canary", "canary2"),
                          script={"implementer": "hang",
                                  "B.repo": "canary2", "D.repo": "canary2"},
                          capacity=dict(tier="max5"),
                          extra_env="EC_STUB_HANG=600\n")
        self.sb.start()

    def tearDown(self):
        self.sb.cleanup()

    def active_impls(self, conn):
        return conn.execute(
            "SELECT * FROM runs WHERE role='implementer' AND "
            "status='DISPATCHED'").fetchall()

    def test_upgrade_fills_then_downgrade_drains(self):
        # max5: exactly 2 implementers become active, rest stay PENDING
        self.assertTrue(self.sb.run_until(
            lambda c: len(self.active_impls(c)) >= 2, 90), self.sb.states())
        for _ in range(4):
            self.sb.tick()
            time.sleep(0.2)
        conn = self.sb.conn()
        act = self.active_impls(conn)
        self.assertEqual(len(act), 2, [dict(r) for r in act])
        ids_before = {r["id"] for r in act}
        conn.close()

        # UPGRADE detected from entitlement change alone -> lanes fill to 4
        self.sb.set_capacity(tier="max20")
        self.assertTrue(self.sb.run_until(
            lambda c: len(self.active_impls(c)) >= 4, 120), self.sb.states())
        conn = self.sb.conn()
        act = self.active_impls(conn)
        self.assertGreaterEqual(len(act), 4)
        self.assertTrue(ids_before <= {r["id"] for r in act},
                        "existing workers must not be restarted on upgrade")
        tiers = [r["tier"] for r in conn.execute("SELECT tier FROM plan_detections "
                                                 "ORDER BY id")]
        self.assertIn("max20", tiers)
        conn.close()

        # DOWNGRADE: no new dispatch, nobody killed, drain toward target
        pids = []
        for r in act:
            pf = Path(r["artifact_dir"]) / "pid.txt"
            for _ in range(40):  # detached shim writes pid.txt asynchronously
                if pf.exists():
                    break
                time.sleep(0.25)
            pids.append(int(pf.read_text().strip()))
        self.sb.set_capacity(tier="max5")
        for _ in range(5):
            self.sb.tick()
            time.sleep(0.2)
        conn = self.sb.conn()
        act_after = self.active_impls(conn)
        self.assertEqual({r["id"] for r in act_after}, {r["id"] for r in act},
                         "downgrade must neither kill nor add workers")
        conn.close()
        for pid in pids:
            self.assertTrue(alive(pid), f"worker {pid} was killed on downgrade")
        # 5th step must not start while active >= new target
        st = self.sb.states()
        self.assertIn("PENDING", [st["E"]])


class TestPressureAndQuota(unittest.TestCase):
    """§17/§18/§41: utilization pressure gates new work; a quota hit pauses
    dispatch globally and recovers automatically."""

    def setUp(self):
        self.sb = Sandbox(steps_yaml="""
steps:
  - {id: A, ordinal: 1, title: a, repos: [canary], depends_on: [], objective: a, acceptance: a}
  - {id: B, ordinal: 2, title: b, repos: [canary], depends_on: [], objective: b, acceptance: b}
""", capacity=dict(tier="max20", pct5=90, pct7=10),
            extra_env="EC_QUOTA_RETRY_MIN=3\n")
        self.sb.start()

    def tearDown(self):
        self.sb.cleanup()

    def test_very_high_pressure_blocks_starts_then_recovers(self):
        for _ in range(4):
            self.sb.tick()
            time.sleep(0.1)
        self.assertEqual(self.sb.states(), {"A": "PENDING", "B": "PENDING"})
        self.sb.set_capacity(tier="max20", pct5=10, pct7=5)
        self.assertTrue(self.sb.run_until(
            lambda c: all(s["state"] == "ACCEPTED" for s in
                          c.execute("SELECT state FROM steps")), 240),
            self.sb.states())

    def test_quota_holds_all_dispatch_and_recovers(self):
        self.sb.set_capacity(tier="max20", pct5=10, pct7=5)
        self.sb.set_script({"A.implementer.1": "quota", "B.planner": "hang"})
        # stop B from progressing so only A's quota path runs: B hangs in
        # planning and holds no further dispatch interest
        self.assertTrue(self.sb.run_until(
            lambda c: self.sb.step_row(c, "A")["state"] == "WAITING_QUOTA", 90),
            self.sb.states())
        conn = self.sb.conn()
        hold = db.kv_get(conn, "quota_hold_until")
        conn.close()
        self.assertTrue(hold and not common.is_past(hold),
                        f"3-minute hold must be in the future: {hold}")
        # while held: no new dispatch at all (pressure critical)
        n0 = len(self.sb.runs())
        for _ in range(3):
            self.sb.tick()
            time.sleep(0.1)
        self.assertEqual(len(self.sb.runs()), n0)
        # simulate the reset arriving: hold + step retry timers elapse
        conn = self.sb.conn()
        db.kv_set(conn, "quota_hold_until", common.iso_in(-1))
        with conn:
            conn.execute("UPDATE steps SET retry_at=? WHERE id='A'",
                         (common.iso_in(-1),))
        conn.close()
        self.sb.set_script({})
        self.assertTrue(self.sb.until_state("ACCEPTED", 240, step_id="A"),
                        self.sb.transitions("A"))
        conn = self.sb.conn()
        quota_runs = conn.execute(
            "SELECT COUNT(*) c FROM runs WHERE status='QUOTA'").fetchone()["c"]
        attempts = conn.execute(
            "SELECT COUNT(*) c FROM runs WHERE step_id='A' AND role IN "
            "('implementer','repair') AND status IN ('DONE','FAILED')").fetchone()["c"]
        conn.close()
        self.assertEqual(quota_runs, 1)
        self.assertEqual(attempts, 1, "quota wait consumed a repair attempt")


class TestBackgroundUnderStaleTelemetry(unittest.TestCase):
    """Reviewer finding (major): stale telemetry is the NORMAL production
    state; the background lane (and thus 09a->09->11) must still run."""

    def test_background_runs_with_stale_usage(self):
        sb = Sandbox(steps_yaml="""
steps:
  - {id: BG, ordinal: 1, title: bg, repos: [canary], depends_on: [], background: true, objective: g, acceptance: g}
""", capacity=dict(tier="max20", fetched_ms=__import__('harness').now_ms() - 3 * 3600 * 1000))
        try:
            sb.start()
            ctx = sb.ctx()
            conn = sb.conn()
            cap.ingest_telemetry(ctx, conn)
            u = cap.current_usage(conn)
            conn.close()
            self.assertTrue(u is None or u["stale"], "fixture must be stale")
            self.assertTrue(sb.until_state("ACCEPTED", 240, step_id="BG"),
                            sb.transitions())
        finally:
            sb.cleanup()


class TestSustainedQuotaOutage(unittest.TestCase):
    """Reviewer finding (major): a long quota outage must NEVER consume a
    repair rung or BLOCK a step — not even after many rounds."""

    def test_long_outage_stays_waiting(self):
        sb = Sandbox(script={"implementer": "quota"})
        try:
            sb.start()
            self.assertTrue(sb.run_until(
                lambda c: db.step_detail(sb.step_row(c)).get("quota_streak", 0) >= 8,
                180), sb.transitions())
            conn = sb.conn()
            step = sb.step_row(conn)
            self.assertNotEqual(step["state"], "BLOCKED")
            repairs = conn.execute("SELECT COUNT(*) c FROM runs WHERE "
                                   "role='repair'").fetchone()["c"]
            attempts = conn.execute(
                "SELECT COUNT(*) c FROM runs WHERE role IN ('implementer','repair')"
                " AND status IN ('DONE','FAILED')").fetchone()["c"]
            conn.close()
            self.assertEqual(repairs, 0, "quota outage reached the repair ladder")
            self.assertEqual(attempts, 0, "quota outage consumed attempts")
            # capacity returns -> the roadmap resumes with no owner action
            sb.set_script({})
            conn = sb.conn()
            db.kv_set(conn, "quota_hold_until", common.iso_in(-1))
            with conn:
                conn.execute("UPDATE steps SET retry_at=?", (common.iso_in(-1),))
            conn.close()
            self.assertTrue(sb.until_state("ACCEPTED", 240), sb.transitions())
        finally:
            sb.cleanup()


class TestFableSerialization(unittest.TestCase):
    """§26/§27: at most one concurrent fable lane, enforced mechanically."""

    def setUp(self):
        steps = """
steps:
  - id: F
    ordinal: 1
    title: f
    repos: [canary]
    depends_on: []
    models: {planner: fable}
    objective: f
    acceptance: f
  - id: G
    ordinal: 2
    title: g
    repos: [canary]
    depends_on: []
    models: {planner: fable}
    objective: g
    acceptance: g
"""
        self.sb = Sandbox(steps_yaml=steps, script={"planner": "hang"},
                          capacity=dict(tier="max20"),
                          extra_env="EC_STUB_HANG=600\n")
        self.sb.start()

    def tearDown(self):
        self.sb.cleanup()

    def test_single_fable_lane(self):
        self.assertTrue(self.sb.run_until(
            lambda c: c.execute("SELECT COUNT(*) c FROM runs WHERE model='fable' "
                                "AND status='DISPATCHED'").fetchone()["c"] == 1, 60))
        for _ in range(4):
            self.sb.tick()
            time.sleep(0.2)
        conn = self.sb.conn()
        fables = conn.execute("SELECT COUNT(*) c FROM runs WHERE model='fable'"
                              ).fetchone()["c"]
        conn.close()
        self.assertEqual(fables, 1, "second fable planner must wait for the lane")
        st = self.sb.states()
        self.assertEqual(sorted([st["F"], st["G"]]), ["PENDING", "PLANNING"])


class TestAuditStep(unittest.TestCase):
    def _mk(self, mode):
        return Sandbox(steps_yaml="""
steps:
  - {id: AU, ordinal: 1, title: audit, repos: [canary], depends_on: [], audit: true, objective: audit all, acceptance: pass}
""", script={"AU.audit": mode})

    def test_audit_pass_accepts(self):
        sb = self._mk("pass")
        try:
            sb.start()
            self.assertTrue(sb.until_state("ACCEPTED", 90, step_id="AU"),
                            sb.transitions())
            conn = sb.conn()
            row = sb.step_row(conn, "AU")
            self.assertTrue(row["handoff_path"] and
                            Path(row["handoff_path"]).exists())
            roles = [r["role"] for r in conn.execute("SELECT role FROM runs")]
            conn.close()
            self.assertEqual(roles, ["audit"])
        finally:
            sb.cleanup()

    def test_audit_block_blocks(self):
        sb = self._mk("block")
        try:
            sb.start()
            self.assertTrue(sb.until_state("BLOCKED", 90, step_id="AU"),
                            sb.transitions())
        finally:
            sb.cleanup()


class TestOverlapConflict(unittest.TestCase):
    """§31/§32: two parallel steps touching the SAME file — the second
    integration cherry-pick conflicts, is aborted non-destructively, and the
    losing step routes to repair. Never a destructive reset."""

    def setUp(self):
        self.sb = Sandbox(steps_yaml="""
steps:
  - {id: S1, ordinal: 1, title: s1, repos: [canary], depends_on: [], objective: s, acceptance: s}
  - {id: S2, ordinal: 2, title: s2, repos: [canary], depends_on: [], objective: s, acceptance: s}
""", script={"S1.implementer": "shared_file", "S2.implementer": "shared_file"})
        self.sb.start()

    def tearDown(self):
        self.sb.cleanup()

    def test_conflict_detected_and_contained(self):
        self.assertTrue(self.sb.run_until(
            lambda c: c.execute("SELECT COUNT(*) c FROM events "
                                "WHERE kind='cherry_conflict'").fetchone()["c"] >= 1
            or all(s["state"] == "ACCEPTED" for s in
                   c.execute("SELECT state FROM steps")), 240), self.sb.states())
        conn = self.sb.conn()
        conflicts = conn.execute("SELECT COUNT(*) c FROM events "
                                 "WHERE kind='cherry_conflict'").fetchone()["c"]
        conn.close()
        self.assertGreaterEqual(conflicts, 1,
                                "same-file parallel steps must conflict")
        ws = self.sb.repos["canary"]["ws"]
        stat = subprocess.run(["git", "status", "--porcelain"], cwd=ws,
                              capture_output=True, text=True).stdout.strip()
        self.assertEqual(stat, "", "integration checkout must stay clean")
        self.assertFalse((Path(ws) / ".git" / "CHERRY_PICK_HEAD").exists())
        states = sorted(self.sb.states().values())
        self.assertIn("ACCEPTED", states)          # the winner integrated
        self.assertTrue(any(s in ("REPAIRING", "BLOCKED", "TESTING", "REVIEWING",
                                  "VALIDATING") for s in states),
                        f"loser must be repairing, got {states}")


class TestRecoveryUnderConcurrency(unittest.TestCase):
    """§44: with several live workers, killing ONE worker interrupts only its
    step; a controller kill loses nothing; reconciliation adopts, never
    duplicates."""

    def setUp(self):
        self.sb = Sandbox(steps_yaml=FIVE_INDEP, repos=("canary", "canary2"),
                          script={"implementer": "hang",
                                  "B.repo": "canary2", "D.repo": "canary2"},
                          capacity=dict(tier="max20"),
                          extra_env="EC_STUB_HANG=600\n")
        self.sb.start()

    def tearDown(self):
        self.sb.cleanup()

    def test_one_worker_dies_others_untouched(self):
        self.assertTrue(self.sb.run_until(
            lambda c: c.execute("SELECT COUNT(*) c FROM runs WHERE "
                                "role='implementer' AND status='DISPATCHED'"
                                ).fetchone()["c"] >= 3, 120), self.sb.states())
        # kill controller mid-tick while workers run: nothing may be lost
        p = self.sb.spawn_tick({"EC_TEST_HOLD_LOCK": "5"})
        time.sleep(1.0)
        p.kill()
        p.wait(timeout=10)
        conn = self.sb.conn()
        acts = conn.execute("SELECT * FROM runs WHERE role='implementer' AND "
                            "status='DISPATCHED'").fetchall()
        victim = acts[0]
        others = {r["id"] for r in acts[1:]}
        pid = int((Path(victim["artifact_dir"]) / "pid.txt").read_text().strip())
        conn.close()
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, text=True)
        self.assertTrue(self.sb.run_until(
            lambda c: c.execute("SELECT COUNT(*) c FROM runs WHERE status='LOST'"
                                ).fetchone()["c"] == 1, 60))
        self.assertTrue(self.sb.run_until(
            lambda c: c.execute(
                "SELECT COUNT(*) c FROM runs WHERE step_id=? AND role='implementer'",
                (victim["step_id"],)).fetchone()["c"] == 2, 90),
            "victim step must respawn exactly one replacement")
        conn = self.sb.conn()
        still = {r["id"] for r in conn.execute(
            "SELECT id FROM runs WHERE role='implementer' AND status='DISPATCHED'")}
        self.assertTrue(others <= still, "unrelated workers were disturbed")
        lost_step = self.sb.step_row(conn, victim["step_id"])
        tr = self.sb.transitions(victim["step_id"])
        conn.close()
        self.assertIn(("IMPLEMENTING", "INTERRUPTED"), tr)
        self.assertEqual(lost_step["state"], "IMPLEMENTING")
        # LOST run consumed no ladder attempt
        conn = self.sb.conn()
        att = conn.execute(
            "SELECT COUNT(*) c FROM runs WHERE step_id=? AND role IN "
            "('implementer','repair') AND status IN ('DONE','FAILED')",
            (victim["step_id"],)).fetchone()["c"]
        conn.close()
        self.assertEqual(att, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
