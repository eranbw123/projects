"""Unit tests: lock, schema check, redaction, telegram idempotency/auth,
git safety guard, secret/forbidden scanning, quota + fable guards.
Covers acceptance items 13, 14, 15, 18 (partial), 19."""
import json
import subprocess
import sys
import unittest
from pathlib import Path

from harness import CODE, Sandbox, git

sys.path.insert(0, str(CODE))
import claude_runner as cr  # noqa: E402
import common               # noqa: E402
import db                   # noqa: E402
import gitops               # noqa: E402
import telegram as tgm      # noqa: E402
import validators as vd     # noqa: E402

FAKE_TOKEN = "123456789:AAFakeFakeFakeFakeFakeFakeFakeFake12345"


class FakeTransport:
    """Scripted telegram transport."""
    def __init__(self, updates_batches):
        self.batches = list(updates_batches)
        self.sent = []
        self.fail_next_send = 0

    def __call__(self, method, params):
        if method == "getUpdates":
            batch = self.batches.pop(0) if self.batches else []
            return {"ok": True, "result": batch}
        if method == "sendMessage":
            if self.fail_next_send > 0:
                self.fail_next_send -= 1
                raise OSError("network down")
            self.sent.append(params["text"])
            return {"ok": True}
        return {"ok": True}


def upd(uid, chat, text):
    return {"update_id": uid, "message": {"chat": {"id": chat}, "text": text}}


class TestInfra(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sb = Sandbox(extra_env=f"DEV_TELEGRAM_BOT_TOKEN={FAKE_TOKEN}\n"
                                   f"DEV_TELEGRAM_CHAT_ID=42\n")

    @classmethod
    def tearDownClass(cls):
        cls.sb.cleanup()

    def test_lock_single_instance(self):
        root = self.sb.root
        l1 = common.acquire_lock(root)
        self.assertIsNotNone(l1)
        self.assertIsNone(common.acquire_lock(root))
        common.release_lock(l1)
        l2 = common.acquire_lock(root)
        self.assertIsNotNone(l2)
        common.release_lock(l2)

    def test_schema_validator(self):
        schema = common.load_schema("plan.schema.json")
        good = {"version": 1, "step_id": "s", "acceptance": ["a"],
                "tasks": [{"repo": "r", "objective": "o"}]}
        self.assertEqual(common.schema_errors(good, schema), [])
        for bad in [{}, {"version": 1, "step_id": "s", "tasks": [], "acceptance": ["a"]},
                    {"version": 1, "step_id": "s", "acceptance": ["a"],
                     "tasks": [{"repo": "r"}]}]:
            self.assertTrue(common.schema_errors(bad, schema))

    def test_redaction_and_event_storage(self):
        ctx = self.sb.ctx()
        self.assertNotIn(FAKE_TOKEN, ctx.redact(f"token is {FAKE_TOKEN} ok"))
        self.assertNotIn("sk-ant-abcdefgh12345678",
                         ctx.redact("key sk-ant-abcdefgh12345678"))
        conn = self.sb.conn()
        db.event(conn, ctx, "test_secret", secret=FAKE_TOKEN)
        row = conn.execute("SELECT payload FROM events WHERE kind='test_secret'").fetchone()
        conn.close()
        self.assertNotIn(FAKE_TOKEN, row["payload"])

    # -- acceptance 13: duplicate telegram updates do not duplicate commands --
    def test_13_tg_duplicate_updates(self):
        ctx = self.sb.ctx()
        conn = self.sb.conn()
        tr = FakeTransport([[upd(5, 42, "/pause")], [upd(5, 42, "/pause")], []])
        tg = tgm.Telegram(FAKE_TOKEN, "42", transport=tr)
        cmds = tgm.consume(ctx, conn, tg)
        cmds += tgm.consume(ctx, conn, tg)
        cmds += tgm.consume(ctx, conn, tg)
        self.assertEqual(cmds, ["/pause"])
        n = conn.execute("SELECT COUNT(*) c FROM tg_updates").fetchone()["c"]
        conn.close()
        self.assertEqual(n, 1)

    # -- acceptance 14: unauthorized chat ignored --
    def test_14_tg_unauthorized(self):
        ctx = self.sb.ctx()
        conn = self.sb.conn()
        tr = FakeTransport([[upd(9, 666, "/abort")], []])
        tg = tgm.Telegram(FAKE_TOKEN, "42", transport=tr)
        cmds = tgm.consume(ctx, conn, tg)
        self.assertEqual(cmds, [])
        row = conn.execute("SELECT status FROM tg_updates WHERE update_id=9").fetchone()
        self.assertEqual(row["status"], "unauthorized")
        ev = conn.execute("SELECT COUNT(*) c FROM events WHERE kind='tg_unauthorized'").fetchone()["c"]
        conn.close()
        self.assertGreaterEqual(ev, 1)

    # -- acceptance 15: notification retry does not duplicate --
    def test_15_notify_idempotent(self):
        ctx = self.sb.ctx()
        conn = self.sb.conn()
        tr = FakeTransport([])
        tr.fail_next_send = 1
        tg = tgm.Telegram(FAKE_TOKEN, "42", transport=tr)
        tgm.notify(conn, ctx, "k1", "hello")
        tgm.notify(conn, ctx, "k1", "hello again")  # same key -> ignored
        tgm.flush(ctx, conn, tg)   # fails
        row = conn.execute("SELECT * FROM notifications WHERE key='k1'").fetchone()
        self.assertEqual(row["status"], "pending")
        tgm.flush(ctx, conn, tg)   # succeeds
        tgm.flush(ctx, conn, tg)   # no resend
        row = conn.execute("SELECT * FROM notifications WHERE key='k1'").fetchone()
        conn.close()
        self.assertEqual(row["status"], "sent")
        self.assertEqual(len(tr.sent), 1)
        self.assertEqual(tr.sent[0], "hello")

    # -- acceptance 19: main/master mutation mechanically prohibited --
    def test_19_git_guard(self):
        cases = [
            (["push", "origin", "main"], True),
            (["push", "origin", "HEAD:master"], True),
            (["push", "--force", "origin", "topic"], True),
            (["push", "origin", ":topic"], True),
            (["reset", "--hard", "HEAD~1"], False),
            (["clean", "-fdx"], False),
            (["branch", "-D", "anything"], False),
            (["branch", "-M", "old", "new"], False),
            (["update-ref", "-d", "refs/heads/main"], False),
            (["checkout", "-f", "main"], False),
        ]
        for args, needs_push in cases:
            with self.assertRaises(gitops.GitSafetyError, msg=str(args)):
                gitops.assert_safe(args, self.sb.ws, allow_push=needs_push)
        # push disabled entirely without allow_push
        with self.assertRaises(gitops.GitSafetyError):
            gitops.assert_safe(["push", "origin", "topic"], self.sb.ws, allow_push=False)
        # mutations against owner repos always refused
        with self.assertRaises(gitops.GitSafetyError):
            gitops.assert_safe(["commit", "-m", "x"], r"C:\github\internet")
        # read-only against owner repos is fine
        gitops.assert_safe(["log", "-1"], r"C:\github\internet")

    def test_19_pushurl_disabled_in_workspace(self):
        out = subprocess.run(["git", "remote", "get-url", "--push", "origin"],
                             cwd=self.sb.ws, capture_output=True, text=True).stdout.strip()
        self.assertIn("DISABLED", out)
        p = subprocess.run(["git", "push", "origin", "automation/integration"],
                           cwd=self.sb.ws, capture_output=True, text=True)
        self.assertNotEqual(p.returncode, 0)

    # -- acceptance 18 (scanners) --
    def test_18_secret_and_forbidden_scanners(self):
        self.assertTrue(vd.secrets_in_text("x sk-ant-abcdefgh1234567890 y"))
        self.assertTrue(vd.secrets_in_text(f"tok {FAKE_TOKEN}"))
        self.assertFalse(vd.secrets_in_text("nothing here"))
        bad = vd.forbidden_changed([".env", "sub/conversations.db", "ok.py", "a/.env.local"])
        self.assertEqual(sorted(bad), [".env", "a/.env.local", "sub/conversations.db"])
        self.assertTrue(vd.secrets_in_text("val secretvalue123", ["secretvalue123"]))

    def test_quota_detection(self):
        self.assertTrue(cr.quota_hit("Claude usage limit reached. resets at 7pm"))
        self.assertTrue(cr.quota_hit("429 too many requests"))
        self.assertFalse(cr.quota_hit("SyntaxError: invalid syntax"))

    def test_fable_privacy_guard(self):
        self.assertFalse(cr.fable_payload_ok("please analyze conversations.db rows"))
        self.assertFalse(cr.fable_payload_ok("SELECT * FROM raw_conversations"))
        self.assertTrue(cr.fable_payload_ok("architecture summary and diffs only"))

    def test_gitignore_covers_state(self):
        gi = (CODE / ".gitignore").read_text()
        for needed in (".env", "state.db", "artifacts/", "logs/"):
            self.assertIn(needed, gi)


if __name__ == "__main__":
    unittest.main(verbosity=2)
