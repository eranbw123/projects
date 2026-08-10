"""GitHub docs visibility (ghdocs): trigger gating, README-only acceptance,
integration apply, and repo-description PATCH.

Offline like every suite: gh and launch are injected fakes; the canary
sandbox provides real git plumbing. The "worker" is simulated by committing
into the real docs worktree and marking the run row DONE.
"""
import json
import sys
import unittest
from pathlib import Path

from harness import CODE, Sandbox, git
from test_pr import FakeGh

sys.path.insert(0, str(CODE))
import common      # noqa: E402
import control     # noqa: E402
import db          # noqa: E402
import ghdocs      # noqa: E402
import ghpr        # noqa: E402
import gitops      # noqa: E402


class DocsBase(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()
        self.ctx = self.sb.ctx()
        self.conn = self.sb.conn()
        self.roadmap = control.load_roadmap(self.ctx)
        self.ws = self.sb.ws
        self.tip = self.sb.repos["canary"]["baseline"]
        self.launches = []
        git(["remote", "add", "origin", "https://github.com/o/canary.git"],
            self.sb.src)

    def tearDown(self):
        self.conn.close()
        self.sb.cleanup()

    def sync(self, gh=None):
        ghdocs.docs_sync(self.ctx, self.conn, self.roadmap, gh=gh or FakeGh(),
                         launch=lambda ctx, conn, run: self.launches.append(run["key"]))

    def seed_pushed(self, tip=None):
        db.kv_set(self.conn, "pr_pushed:canary", tip or self.tip)

    def open_docs_run(self):
        return json.loads(db.kv_get(self.conn, "docs_run:canary") or "null")

    def finish_worker(self, commit=True, files=("README.md",), status="done",
                      description="A canary repo for tests.", summary="updated",
                      content="# canary\n\nWhat it does.\n"):
        """Simulate the docs worker: commit in the real worktree, write the
        result artifact, mark the run row DONE."""
        info = self.open_docs_run()
        wt = Path(info["wt"])
        if commit:
            for f in files:
                (wt / f).write_text(content)
            git(["add", "-A"], wt)
            git(["commit", "-m", f"docs: refresh\n\nEC-Key: {info['key']}"], wt)
        run = db.run_by_key(self.conn, info["key"])
        (Path(run["artifact_dir"]) / "result.json").write_text(json.dumps(
            {"version": 1, "status": status, "summary": summary,
             "description": description}))
        with self.conn:
            self.conn.execute("UPDATE runs SET status='DONE' WHERE key=?",
                              (info["key"],))
        return info

    def notif_keys(self):
        return [r["key"] for r in
                self.conn.execute("SELECT key FROM notifications ORDER BY ts")]

    def events(self, kind):
        return self.conn.execute(
            "SELECT * FROM events WHERE kind=? ORDER BY id", (kind,)).fetchall()


class TestTrigger(DocsBase):
    def test_nothing_published_means_no_dispatch(self):
        self.sync()
        self.assertEqual(self.launches, [])
        self.assertIsNone(self.open_docs_run())

    def test_missing_readme_bootstraps_immediately(self):
        self.seed_pushed()
        # cooldown active — a missing README must bypass it
        db.kv_set(self.conn, "docs_next_at:canary", common.iso_in(3600))
        self.sync()
        self.assertEqual(len(self.launches), 1)
        info = self.open_docs_run()
        self.assertTrue(Path(info["wt"]).exists())
        self.assertEqual(info["tip"], self.tip)
        run = db.run_by_key(self.conn, info["key"])
        self.assertEqual((run["role"], run["status"]), ("docs", "PREPARED"))
        prompt = (Path(run["artifact_dir"]) / "prompt.md").read_text(encoding="utf-8")
        self.assertIn("canary", prompt)
        self.assertNotIn("<<REPO>>", prompt)
        self.assertTrue(self.events("docs_dispatched"))

    def test_covered_tip_and_cooldown_gate(self):
        self.seed_pushed()
        db.kv_set(self.conn, "docs_tip:canary", self.tip)
        self.sync()
        self.assertEqual(self.launches, [])  # already covered

        # new tip WITH a README at it → cooldown applies
        (self.ws / "README.md").write_text("# canary\n")
        git(["add", "-A"], self.ws)
        git(["commit", "-m", "docs: seed"], self.ws)
        tip2 = git(["rev-parse", "HEAD"], self.ws).strip()
        self.seed_pushed(tip2)
        db.kv_set(self.conn, "docs_next_at:canary", common.iso_in(3600))
        self.sync()
        self.assertEqual(self.launches, [])
        db.kv_set(self.conn, "docs_next_at:canary", common.iso_in(-1))
        self.sync()
        self.assertEqual(len(self.launches), 1)

    def test_rejected_tip_never_retried(self):
        self.seed_pushed()
        db.kv_set(self.conn, "docs_reject:canary", self.tip)
        self.sync()
        self.assertEqual(self.launches, [])

    def test_paused_is_a_full_noop(self):
        self.seed_pushed()
        db.kv_set(self.conn, "paused", "1")
        self.sync()
        self.assertEqual(self.launches, [])
        self.assertIsNone(self.open_docs_run())

    def test_single_flight_globally(self):
        self.seed_pushed()
        self.sync()
        self.assertEqual(len(self.launches), 1)
        self.sync()
        self.assertEqual(len(self.launches), 1)  # in flight → no second dispatch

    def test_no_github_origin_marks_covered_silently(self):
        git(["remote", "remove", "origin"], self.sb.src)
        self.seed_pushed()
        self.sync()
        self.assertEqual(self.launches, [])
        self.assertEqual(db.kv_get(self.conn, "docs_tip:canary"), self.tip)


class TestApply(DocsBase):
    def dispatch(self):
        self.seed_pushed()
        self.sync()
        self.assertEqual(len(self.launches), 1)

    def test_happy_path_applies_readme_and_description(self):
        self.dispatch()
        info = self.finish_worker()
        gh = FakeGh(((("api",), ""),))
        self.sync(gh=gh)

        # README cherry-picked onto integration; pr_tip advanced for ghpr
        new_tip = gitops.current_sha(self.ws, ghdocs.INTEGRATION)
        self.assertNotEqual(new_tip, self.tip)
        self.assertEqual(db.kv_get(self.conn, "pr_tip:canary"), new_tip)
        show = git(["show", f"{new_tip}:README.md"], self.ws)
        self.assertIn("What it does", show)
        # bookkeeping: covered tip, cooldown armed, worktree gone, kv closed
        self.assertEqual(db.kv_get(self.conn, "docs_tip:canary"), self.tip)
        self.assertTrue(db.kv_get(self.conn, "docs_next_at:canary"))
        self.assertFalse(Path(info["wt"]).exists())
        self.assertIsNone(self.open_docs_run())
        # description PATCHed once and cached
        patch = [c for c in gh.calls if c[:3] == ["api", "-X", "PATCH"]]
        self.assertEqual(len(patch), 1)
        self.assertIn("repos/o/canary", patch[0])
        self.assertEqual(db.kv_get(self.conn, "gh_desc:canary"),
                         "A canary repo for tests.")
        self.assertIn(f"docs:canary:{self.tip[:12]}", self.notif_keys())
        self.assertTrue(self.events("docs_applied"))

        gh2 = FakeGh()
        self.sync(gh=gh2)  # nothing left to do: no gh traffic, no dispatch
        self.assertEqual(gh2.calls, [])
        self.assertEqual(len(self.launches), 1)

    def test_non_readme_change_is_discarded(self):
        self.dispatch()
        self.finish_worker(files=("README.md", "app.py"))
        self.sync(gh=FakeGh(((("api",), ""),)))
        self.assertEqual(gitops.current_sha(self.ws, ghdocs.INTEGRATION), self.tip)
        self.assertIsNone(db.kv_get(self.conn, "pr_tip:canary"))
        self.assertEqual(db.kv_get(self.conn, "docs_reject:canary"), self.tip)
        self.assertTrue(self.events("docs_rejected"))
        self.assertIn(f"docs-reject:canary:{self.tip[:12]}", self.notif_keys())
        self.sync()  # rejected tip does not re-trigger
        self.assertEqual(len(self.launches), 1)

    def test_secret_in_readme_is_discarded(self):
        self.dispatch()
        self.finish_worker(
            content="# canary\n\ntoken: sk-ant-api03-abcdefghijklmnopqrstuvwx\n")
        self.sync()
        self.assertEqual(gitops.current_sha(self.ws, ghdocs.INTEGRATION), self.tip)
        why = self.events("docs_rejected")[-1]
        self.assertIn("secret", why["payload"])

    def test_no_commit_still_covers_tip_and_sets_description(self):
        self.dispatch()
        self.finish_worker(commit=False, summary="already accurate")
        gh = FakeGh(((("api",), ""),))
        self.sync(gh=gh)
        self.assertEqual(gitops.current_sha(self.ws, ghdocs.INTEGRATION), self.tip)
        self.assertEqual(db.kv_get(self.conn, "docs_tip:canary"), self.tip)
        self.assertTrue(self.events("docs_no_change"))
        self.assertEqual(db.kv_get(self.conn, "gh_desc:canary"),
                         "A canary repo for tests.")

    def test_integration_busy_defers_until_free(self):
        self.dispatch()
        self.finish_worker()
        with self.conn:
            self.conn.execute(
                "INSERT INTO runs(key,step_id,task_idx,cycle,role,status,cwd,"
                "created_at) VALUES('v1','c1',0,0,'test','DISPATCHED',?,?)",
                (str(self.ws), common.now()))
        self.sync(gh=FakeGh(((("api",), ""),)))
        self.assertEqual(gitops.current_sha(self.ws, ghdocs.INTEGRATION), self.tip)
        self.assertIsNotNone(self.open_docs_run())  # still pending, not lost
        with self.conn:
            self.conn.execute("UPDATE runs SET status='DONE' WHERE key='v1'")
        self.sync(gh=FakeGh(((("api",), ""),)))
        self.assertNotEqual(gitops.current_sha(self.ws, ghdocs.INTEGRATION), self.tip)

    def test_worker_failure_backs_off_and_notifies(self):
        self.dispatch()
        info = self.open_docs_run()
        with self.conn:
            self.conn.execute("UPDATE runs SET status='FAILED' WHERE key=?",
                              (info["key"],))
        self.sync()
        self.assertIsNone(self.open_docs_run())
        until = db.kv_get(self.conn, "docs_backoff:canary")
        self.assertTrue(until and not common.is_past(until))
        self.assertIn(f"docs-fail:canary:{self.tip[:12]}", self.notif_keys())
        self.sync()  # inside backoff: no new dispatch
        self.assertEqual(len(self.launches), 1)
        db.kv_set(self.conn, "docs_backoff:canary", common.iso_in(-1))
        self.sync()
        self.assertEqual(len(self.launches), 2)

    def test_worker_failed_status_in_result_is_reject(self):
        self.dispatch()
        self.finish_worker(commit=False, status="failed", summary="could not")
        self.sync()
        self.assertEqual(db.kv_get(self.conn, "docs_reject:canary"), self.tip)


class TestDescription(DocsBase):
    def test_description_sanitized_and_truncated(self):
        self.seed_pushed()
        self.sync()
        self.finish_worker(description="line one\nline two   " + "x" * 400)
        gh = FakeGh(((("api",), ""),))
        self.sync(gh=gh)
        patch = next(c for c in gh.calls if c[:3] == ["api", "-X", "PATCH"])
        desc = patch[-1].removeprefix("description=")
        self.assertLessEqual(len(desc), ghdocs.DESC_LIMIT)
        self.assertNotIn("\n", desc)
        self.assertTrue(desc.startswith("line one line two"))

    def test_gh_failure_backs_off_then_retries_pending_description(self):
        self.seed_pushed()
        self.sync()
        self.finish_worker()
        self.sync(gh=FakeGh(((("api",), ghpr.GhError("boom")),)))
        # README applied, but description remains wanted; failure backed off
        self.assertEqual(db.kv_get(self.conn, "docs_tip:canary"), self.tip)
        self.assertIsNone(db.kv_get(self.conn, "gh_desc:canary"))
        self.assertTrue(db.kv_get(self.conn, "gh_desc_want:canary"))
        self.assertTrue(self.events("docs_error"))
        db.kv_set(self.conn, "docs_backoff:canary", common.iso_in(-1))
        gh = FakeGh(((("api",), ""),))
        self.sync(gh=gh)
        self.assertEqual(db.kv_get(self.conn, "gh_desc:canary"),
                         "A canary repo for tests.")


class TestPrBodyDocsLine(DocsBase):
    def test_body_reports_docs_freshness(self):
        body = ghpr.pr_body(self.conn, "canary", self.tip, self.tip)
        self.assertIn("first refresh pending", body)
        db.kv_set(self.conn, "docs_tip:canary", self.tip)
        body = ghpr.pr_body(self.conn, "canary", self.tip, self.tip)
        self.assertIn(self.tip[:12], body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
