"""GitHub PR visibility: guard posture, slug parsing, and the pr_sync phase.

Offline like every suite: gh and push are injected fakes (the telegram
transport pattern); the canary repo provides real git plumbing. The flow test
proves the controller records the validated tip and that repos WITHOUT a
GitHub origin degrade to a silent skip (sandbox repos never sync anywhere).
"""
import json
import sys
import unittest

from harness import CODE, Sandbox, git

sys.path.insert(0, str(CODE))
import common      # noqa: E402
import control     # noqa: E402
import db          # noqa: E402
import ghpr        # noqa: E402
import gitops      # noqa: E402


class FakeGh:
    """Scripted gh runner: first prefix match wins; unexpected calls fail."""

    def __init__(self, script=()):
        self.script = list(script)
        self.calls = []

    def __call__(self, args, timeout=60):
        self.calls.append(list(args))
        for prefix, result in self.script:
            if tuple(args[:len(prefix)]) == tuple(prefix):
                if isinstance(result, Exception):
                    raise result
                return result
        raise AssertionError(f"unexpected gh call: {args}")


class FakePush:
    def __init__(self, exc=None):
        self.calls = []
        self.exc = exc

    def __call__(self, ws, url, sha):
        self.calls.append((str(ws), url, sha))
        if self.exc:
            raise self.exc


class TestGuard(unittest.TestCase):
    """push_url stays inside the mechanical guard: URL pushes can only ever
    fast-forward a non-main branch."""
    URL = "https://github.com/o/r.git"
    CWD = r"C:\projects\x"

    def test_refspec_to_main_or_master_refused(self):
        for spec in ("abc:refs/heads/main", "abc:main", "abc:refs/heads/master"):
            with self.assertRaises(gitops.GitSafetyError, msg=spec):
                gitops.assert_safe(["push", self.URL, spec], cwd=self.CWD,
                                   allow_push=True)

    def test_force_and_delete_refused(self):
        for args in (["push", "--force", self.URL, "a:refs/heads/x"],
                     ["push", self.URL, "+a:refs/heads/x"],
                     ["push", self.URL, ":refs/heads/x"],
                     ["push", "--delete", self.URL, "x"]):
            with self.assertRaises(gitops.GitSafetyError, msg=args):
                gitops.assert_safe(args, cwd=self.CWD, allow_push=True)

    def test_integration_refspec_allowed(self):
        gitops.assert_safe(
            ["push", self.URL, "abc:refs/heads/automation/integration"],
            cwd=self.CWD, allow_push=True)

    def test_push_still_denied_without_allow_flag(self):
        with self.assertRaises(gitops.GitSafetyError):
            gitops.assert_safe(
                ["push", self.URL, "abc:refs/heads/automation/integration"],
                cwd=self.CWD, allow_push=False)


class TestSlug(unittest.TestCase):
    def test_parse(self):
        cases = {
            "https://github.com/eranbw123/ai.git": "eranbw123/ai",
            "https://github.com/eranbw123/ai": "eranbw123/ai",
            "https://GitHub.com/O/R.git/": "O/R",
            "git@github.com:owner/repo.git": "owner/repo",
            "ssh://git@github.com/o/r": "o/r",
            "https://gitlab.com/o/r.git": None,
            "DISABLED:engine-control-no-push": None,
            "": None,
        }
        for url, want in cases.items():
            self.assertEqual(ghpr._parse_slug(url), want, url)


class SyncBase(unittest.TestCase):
    """Unit surface for pr_sync against a real canary sandbox."""

    def setUp(self):
        self.sb = Sandbox()
        self.ctx = self.sb.ctx()
        self.conn = self.sb.conn()
        self.roadmap = control.load_roadmap(self.ctx)
        self.tip = self.sb.repos["canary"]["baseline"]

    def tearDown(self):
        self.conn.close()
        self.sb.cleanup()

    def with_origin(self):
        git(["remote", "add", "origin", "https://github.com/o/canary.git"],
            self.sb.src)

    def seed_tip(self):
        db.kv_set(self.conn, "pr_tip:canary", self.tip)

    def notif_keys(self):
        return [r["key"] for r in
                self.conn.execute("SELECT key FROM notifications ORDER BY ts")]

    def events(self, kind):
        return self.conn.execute(
            "SELECT * FROM events WHERE kind=? ORDER BY id", (kind,)).fetchall()


CREATED_URL = "https://github.com/o/canary/pull/7"
GH_DISCOVER_CREATE = (
    (("pr", "list"), "[]"),
    (("repo", "view"), '{"defaultBranchRef":{"name":"main"}}'),
    (("pr", "create"), CREATED_URL + "\n"),
)


class TestSync(SyncBase):
    def test_push_create_notify_then_idempotent(self):
        self.with_origin()
        self.seed_tip()
        gh, push = FakeGh(GH_DISCOVER_CREATE), FakePush()
        ghpr.pr_sync(self.ctx, self.conn, self.roadmap, gh=gh, push=push)

        self.assertEqual(push.calls,
                         [(str(self.sb.ws), "https://github.com/o/canary.git", self.tip)])
        create = next(c for c in gh.calls if c[:2] == ["pr", "create"])
        self.assertIn("--base", create)
        self.assertEqual(create[create.index("--base") + 1], "main")
        self.assertEqual(db.kv_get(self.conn, "pr_pushed:canary"), self.tip)
        self.assertEqual(db.kv_get(self.conn, "pr_number:canary"), "7")
        self.assertEqual(db.kv_get(self.conn, "pr_url:canary"), CREATED_URL)
        keys = self.notif_keys()
        self.assertIn("pr-open:canary:7", keys)
        self.assertIn(f"pr:canary:{self.tip[:12]}", keys)
        self.assertTrue(self.events("pr_opened"))

        # same tip again: no git, no gh — the phase is two kv reads
        gh2, push2 = FakeGh(), FakePush()
        ghpr.pr_sync(self.ctx, self.conn, self.roadmap, gh=gh2, push=push2)
        self.assertEqual((gh2.calls, push2.calls), ([], []))

    def test_pr_base_used_when_it_exists_remotely(self):
        self.with_origin()
        self.seed_tip()
        self.roadmap["repos"]["canary"]["pr_base"] = "lab"
        gh = FakeGh(((("pr", "list"), "[]"),
                     (("api", "repos/o/canary/branches/lab"), "{}"),
                     (("pr", "create"), CREATED_URL)))
        ghpr.pr_sync(self.ctx, self.conn, self.roadmap, gh=gh, push=FakePush())
        create = next(c for c in gh.calls if c[:2] == ["pr", "create"])
        self.assertEqual(create[create.index("--base") + 1], "lab")

    def test_pr_base_falls_back_when_branch_gone(self):
        self.with_origin()
        self.seed_tip()
        self.roadmap["repos"]["canary"]["pr_base"] = "gone"
        gh = FakeGh(((("pr", "list"), "[]"),
                     (("api",), ghpr.GhError("HTTP 404: Branch not found")),
                     (("repo", "view"), '{"defaultBranchRef":{"name":"main"}}'),
                     (("pr", "create"), CREATED_URL)))
        ghpr.pr_sync(self.ctx, self.conn, self.roadmap, gh=gh, push=FakePush())
        create = next(c for c in gh.calls if c[:2] == ["pr", "create"])
        self.assertEqual(create[create.index("--base") + 1], "main")

    def test_existing_open_pr_updated_not_recreated(self):
        self.with_origin()
        self.seed_tip()
        db.kv_set(self.conn, "pr_number:canary", "7")
        gh = FakeGh(((("pr", "view"), json.dumps({"state": "OPEN", "url": CREATED_URL})),
                     (("pr", "edit"), "")))
        ghpr.pr_sync(self.ctx, self.conn, self.roadmap, gh=gh, push=FakePush())
        self.assertTrue(any(c[:2] == ["pr", "edit"] for c in gh.calls))
        self.assertFalse(any(c[:2] == ["pr", "create"] for c in gh.calls))
        self.assertNotIn("pr-open:canary:7", self.notif_keys())
        self.assertIn(f"pr:canary:{self.tip[:12]}", self.notif_keys())

    def test_merged_pr_replaced_on_next_validated_work(self):
        self.with_origin()
        self.seed_tip()
        db.kv_set(self.conn, "pr_number:canary", "7")
        gh = FakeGh(((("pr", "view"), json.dumps({"state": "MERGED", "url": CREATED_URL})),
                     (("pr", "list"), "[]"),
                     (("repo", "view"), '{"defaultBranchRef":{"name":"main"}}'),
                     (("pr", "create"), "https://github.com/o/canary/pull/8")))
        ghpr.pr_sync(self.ctx, self.conn, self.roadmap, gh=gh, push=FakePush())
        self.assertEqual(db.kv_get(self.conn, "pr_number:canary"), "8")

    def test_lost_kv_adopts_existing_open_pr(self):
        self.with_origin()
        self.seed_tip()
        gh = FakeGh(((("pr", "list"),
                      json.dumps([{"number": 9, "url": "https://github.com/o/canary/pull/9"}])),
                     (("pr", "edit"), "")))
        ghpr.pr_sync(self.ctx, self.conn, self.roadmap, gh=gh, push=FakePush())
        self.assertEqual(db.kv_get(self.conn, "pr_number:canary"), "9")
        self.assertFalse(any(c[:2] == ["pr", "create"] for c in gh.calls))

    def test_no_commits_between_is_benign(self):
        self.with_origin()
        self.seed_tip()
        gh = FakeGh(((("pr", "list"), "[]"),
                     (("repo", "view"), '{"defaultBranchRef":{"name":"main"}}'),
                     (("pr", "create"),
                      ghpr.GhError("GraphQL: No commits between main and "
                                   "automation/integration (createPullRequest)"))))
        ghpr.pr_sync(self.ctx, self.conn, self.roadmap, gh=gh, push=FakePush())
        self.assertEqual(db.kv_get(self.conn, "pr_pushed:canary"), self.tip)
        self.assertIsNone(db.kv_get(self.conn, "pr_url:canary"))
        self.assertTrue(self.events("pr_no_delta"))
        self.assertNotIn(f"pr-fail:canary:{self.tip[:12]}", self.notif_keys())

    def test_failure_backs_off_notifies_once_then_recovers(self):
        self.with_origin()
        self.seed_tip()
        ghpr.pr_sync(self.ctx, self.conn, self.roadmap, gh=FakeGh(),
                     push=FakePush(exc=RuntimeError("network down")))
        self.assertIsNone(db.kv_get(self.conn, "pr_pushed:canary"))
        self.assertIn(f"pr-fail:canary:{self.tip[:12]}", self.notif_keys())
        until = db.kv_get(self.conn, "pr_backoff:canary")
        self.assertTrue(until and not common.is_past(until))

        # inside the backoff window nothing runs
        push2 = FakePush()
        ghpr.pr_sync(self.ctx, self.conn, self.roadmap, gh=FakeGh(), push=push2)
        self.assertEqual(push2.calls, [])

        # after it expires the same tip is retried; success clears the backoff
        db.kv_set(self.conn, "pr_backoff:canary", common.iso_in(-1))
        ghpr.pr_sync(self.ctx, self.conn, self.roadmap,
                     gh=FakeGh(GH_DISCOVER_CREATE), push=FakePush())
        self.assertEqual(db.kv_get(self.conn, "pr_pushed:canary"), self.tip)
        self.assertEqual(db.kv_get(self.conn, "pr_backoff:canary"), "")

    def test_no_github_origin_skips_silently_and_stops(self):
        self.seed_tip()  # canary source has no origin remote at all
        gh, push = FakeGh(), FakePush()
        ghpr.pr_sync(self.ctx, self.conn, self.roadmap, gh=gh, push=push)
        self.assertEqual((gh.calls, push.calls), ([], []))
        self.assertEqual(db.kv_get(self.conn, "pr_pushed:canary"), self.tip)
        self.assertTrue(self.events("pr_skip"))


class TestFlowRecordsValidatedTip(unittest.TestCase):
    """Real controller path (stub model): acceptance must record the
    validated integration tip, and the tick's pr_sync must stay harmless for
    repos without a GitHub origin."""

    def setUp(self):
        self.sb = Sandbox()
        self.sb.start()

    def tearDown(self):
        self.sb.cleanup()

    def test_accepted_step_sets_pr_tip(self):
        self.assertTrue(self.sb.until_state("ACCEPTED"), self.sb.transitions())
        conn = self.sb.conn()
        try:
            tip = db.kv_get(conn, "pr_tip:canary")
            self.assertEqual(tip, gitops.current_sha(self.sb.ws, ghpr.INTEGRATION))
            # no-origin repo: the tick's sync marked it handled, no errors
            self.assertEqual(db.kv_get(conn, "pr_pushed:canary"), tip)
            self.assertFalse(conn.execute(
                "SELECT 1 FROM events WHERE kind='pr_sync_error'").fetchone())
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
