"""Failure-visibility acceptance: quote-clean command transport, NO-TESTS-RAN
classified as harness config (never a repair spiral), per-step controller
error isolation, and the `why` failure narrative.

Regression anchors (2026-08-10, roadmap run 5dc1ff46):
- step-02 burned its whole repair budget because cmd.exe received the
  discover pattern with literal quotes and collected 0 tests;
- one stdout=None crash in ONE step's advance froze every step for 6 ticks
  and the events carried neither step id nor traceback.
"""
import json
import sys
import unittest
from pathlib import Path

from harness import CODE, PY, Sandbox, mkroot, rmtree

sys.path.insert(0, str(CODE))
import common     # noqa: E402
import control    # noqa: E402
import db         # noqa: E402
import validators  # noqa: E402


class TestQuoteTransport(unittest.TestCase):
    """run_cmd must deliver interior double quotes to the child intact."""

    def setUp(self):
        self.dir = mkroot()

    def tearDown(self):
        rmtree(self.dir)

    def test_quoted_argument_reaches_child_unmangled(self):
        rc, out = validators.run_cmd(f'"{PY}" -c "print(40 + 2)"', self.dir)
        self.assertEqual(rc, 0, out)
        self.assertIn("42", out)  # broken transport evaluates a bare string: no output

    def test_quoted_discover_pattern_collects_tests(self):
        (self.dir / "test_it.py").write_text(
            "import unittest\n\nclass T(unittest.TestCase):\n"
            "    def test_ok(self):\n        pass\n")
        ok, report, no_tests = validators.repo_tests(
            ['python -m unittest discover -p "test_*.py"'], self.dir)
        self.assertTrue(ok, report)
        self.assertFalse(no_tests)
        self.assertIn("Ran 1 test", report)

    def test_autorun_guard_kept(self):
        calls = []
        orig = validators.subprocess.run

        def spy(cmd, **kw):
            calls.append(cmd)
            return orig(cmd, **kw)

        validators.subprocess.run = spy
        try:
            validators.run_cmd("exit 0", self.dir)
        finally:
            validators.subprocess.run = orig
        self.assertTrue(calls and isinstance(calls[0], str)
                        and calls[0].startswith('cmd /d /s /c "'), calls)


class TestNoTestsClassification(unittest.TestCase):
    def setUp(self):
        self.dir = mkroot()

    def tearDown(self):
        rmtree(self.dir)

    def test_exit_5_everywhere_means_no_tests(self):
        ok, report, no_tests = validators.repo_tests(
            ['python -m unittest discover -p "test_*.py"'], self.dir)
        self.assertFalse(ok)
        self.assertTrue(no_tests)
        self.assertIn("NO TESTS COLLECTED", report)

    def test_real_failure_outranks_no_tests(self):
        ok, report, no_tests = validators.repo_tests(
            ['python -m unittest discover -p "test_*.py"',
             f'"{PY}" -c "raise SystemExit(1)"'], self.dir)
        self.assertFalse(ok)
        self.assertFalse(no_tests, "a real rc=1 failure must go to repair")

    def test_tail_tolerates_none(self):
        self.assertEqual(common.tail(None), "")
        self.assertEqual(common.tail("a\nb", 1), "b")


class TestNoTestsBlocksNotRepairs(unittest.TestCase):
    """A collected-nothing suite is engine config, not broken code: the step
    must BLOCK with the command in the reason, and never dispatch repair."""

    def setUp(self):
        self.sb = Sandbox(tests_cmds=('python -m unittest discover -p "absent_*.py"',))
        self.sb.start()

    def tearDown(self):
        self.sb.cleanup()

    def test_blocked_with_reason_and_zero_repairs(self):
        self.assertTrue(self.sb.until_state("BLOCKED"), self.sb.transitions())
        conn = self.sb.conn()
        reason = conn.execute(
            "SELECT reason FROM transitions WHERE to_state='BLOCKED' "
            "ORDER BY id DESC LIMIT 1").fetchone()["reason"]
        self.assertIn("NO TESTS", reason)
        self.assertIn("absent_*.py", reason)
        repairs = conn.execute(
            "SELECT COUNT(*) c FROM runs WHERE role IN ('repair','diagnostic')"
            ).fetchone()["c"]
        self.assertEqual(repairs, 0, "repair rungs spent on a config problem")
        test_note = conn.execute(
            "SELECT note FROM runs WHERE role='test' ORDER BY id DESC LIMIT 1"
            ).fetchone()["note"]
        self.assertIn("no tests collected", test_note)

        # the why narrative reconstructs the story without artifact spelunking
        why = control.why_text(self.sb.ctx(), conn)
        self.assertIn("c1", why)
        self.assertIn("BLOCKED", why)
        self.assertIn("NO TESTS", why)
        self.assertIn("artifacts:", why)
        self.assertEqual(control.why_text(self.sb.ctx(), conn, "nope"),
                         "engine-control: unknown step 'nope'")
        conn.close()


TWO_STEPS = """
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
    depends_on: []
    objective: obj b
    acceptance: acc b
"""


class TestPerStepErrorIsolation(unittest.TestCase):
    """A controller bug advancing one step: loud with step id + traceback,
    contained to that step (others advance, no global pause), and the step
    blocks at the streak cap."""

    def setUp(self):
        self._orig = control.advance_step
        self.sb = Sandbox(steps_yaml=TWO_STEPS)
        self.sb.start()

    def tearDown(self):
        control.advance_step = self._orig
        self.sb.cleanup()

    def test_one_bad_step_never_stalls_the_rest(self):
        self.assertTrue(self.sb.run_until(
            lambda c: self.sb.step_row(c, "a1")["state"] != "PENDING", 60))

        orig = self._orig

        def boom(ctx, conn, roadmap, step):
            if step["id"] == "a1":
                raise RuntimeError("injected-a1")
            return orig(ctx, conn, roadmap, step)

        control.advance_step = boom
        conn = self.sb.conn()
        b_before = len([t for t in conn.execute(
            "SELECT * FROM transitions WHERE step_id='b1'")])
        conn.close()

        self.assertTrue(self.sb.run_until(
            lambda c: self.sb.step_row(c, "a1")["state"] == "BLOCKED", 90),
            self.sb.states())

        conn = self.sb.conn()
        # loud and attributed: events carry step id, error, traceback
        errs = [json.loads(r["payload"]) for r in conn.execute(
            "SELECT * FROM events WHERE kind='advance_error' AND step_id='a1'")]
        self.assertGreaterEqual(len(errs), control.STEP_ERR_BLOCK_AT)
        self.assertIn("injected-a1", errs[0]["err"])
        self.assertIn("RuntimeError", errs[0]["tb"])
        # blocked reason names the controller, not the step's work
        reason = conn.execute(
            "SELECT reason FROM transitions WHERE step_id='a1' AND to_state='BLOCKED' "
            "ORDER BY id DESC LIMIT 1").fetchone()["reason"]
        self.assertIn("controller error", reason)
        # contained: b1 kept moving, no global pause, no global streak
        b_after = len([t for t in conn.execute(
            "SELECT * FROM transitions WHERE step_id='b1'")])
        self.assertGreater(b_after, b_before, "healthy step stalled")
        self.assertNotEqual(db.kv_get(conn, "paused"), "1")
        self.assertEqual(db.kv_get(conn, "orch_err_streak", "0"), "0")
        conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
