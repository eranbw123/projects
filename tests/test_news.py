"""Unit tests for newsops (/news): whitelisted config mutation with backups,
manual runs via schtasks, command routing that never raises, and /help
coverage enforcement. Fully offline — both subprocess boundaries (_run/_app)
are stubbed."""
import json
import re
import shutil
import sys
import unittest
from pathlib import Path

from harness import CODE, mkroot

sys.path.insert(0, str(CODE))
import newsops  # noqa: E402


class FakeCtx:
    def __init__(self, root):
        self._root = root

    def getenv(self, key, default=None):
        return str(self._root) if key == "EC_NEWS_ROOT" else default


class NewsBase(unittest.TestCase):
    def setUp(self):
        self.dir = mkroot()
        self.product = self.dir / "internet"
        (self.product / "ops").mkdir(parents=True)
        (self.product / ".env").write_text(
            "# comment kept\nTELEGRAM_BOT_TOKEN=zzz\nDISCOVERY_DIGEST_MAX=10\n",
            encoding="utf-8")
        (self.product / "interests.json").write_text(json.dumps({
            "defaults": {"min_score": 0.78, "sources": ["web_search"]},
            "interests": [
                {"key": "alpha", "title": "A", "min_score": 0.74,
                 "sources": ["web_search"]},
                {"key": "beta", "title": "B", "sources": ["web_search"]},
            ]}), encoding="utf-8")
        self.ctx = FakeCtx(self.product)
        self.calls = []
        self._orig = (newsops._run, newsops._app)

        def fake_run(args, cwd=None, timeout=90):
            self.calls.append([str(a) for a in args])
            return 0, "ok"

        def fake_app(ctx, *args, timeout=90):
            self.calls.append(["app", *args])
            return 0, "ok"

        newsops._run, newsops._app = fake_run, fake_app

    def tearDown(self):
        newsops._run, newsops._app = self._orig
        shutil.rmtree(self.dir, ignore_errors=True)

    def env_text(self):
        return (self.product / ".env").read_text(encoding="utf-8")

    def backups(self):
        b = self.product / "backups"
        return sorted(p.name for p in b.iterdir()) if b.exists() else []


class TestValidators(unittest.TestCase):
    def test_hhmm(self):
        self.assertEqual(newsops._hhmm("08:00"), "08:00")
        self.assertEqual(newsops._hhmm("23:59"), "23:59")
        for bad in ("8:00", "24:00", "12:60", "noon", ""):
            with self.assertRaises(newsops.NewsError):
                newsops._hhmm(bad)

    def test_interval(self):
        self.assertEqual(newsops._interval("45m"), "2700")
        self.assertEqual(newsops._interval("2h"), "7200")
        self.assertEqual(newsops._interval("1h30m"), "5400")
        self.assertEqual(newsops._interval("3600"), "3600")
        for bad in ("", "fast", "5m", "25h", "30"):  # 5m/30s below 10m floor
            with self.assertRaises(newsops.NewsError):
                newsops._interval(bad)

    def test_int_range(self):
        check = newsops._int_range(1, 50)
        self.assertEqual(check("10"), "10")
        for bad in ("0", "51", "x", ""):
            with self.assertRaises(newsops.NewsError):
                check(bad)

    def test_provider(self):
        for alias, canon in (("claude", "claude_chat"), ("claude_chat", "claude_chat"),
                             ("chatgpt", "openai"), ("ChatGPT", "openai"),
                             ("gpt", "openai"), ("openai", "openai"),
                             ("anthropic", "anthropic")):
            self.assertEqual(newsops._provider(alias), canon)
        for bad in ("gemini", "", "webscrape"):
            with self.assertRaises(newsops.NewsError):
                newsops._provider(bad)


class TestConfigSet(NewsBase):
    def test_runtime_key_writes_env_and_backs_up(self):
        out = newsops.config_set(self.ctx, "digest_max", "15")
        self.assertIn("10 → 15", out)
        self.assertIn("DISCOVERY_DIGEST_MAX=15", self.env_text())
        self.assertIn("# comment kept", self.env_text(), "other lines preserved")
        self.assertIn("TELEGRAM_BOT_TOKEN=zzz", self.env_text())
        self.assertTrue(any(n.startswith(".env-") for n in self.backups()))
        self.assertEqual(self.calls, [], "runtime key must not re-install tasks")

    def test_trigger_key_reinstalls_tasks(self):
        out = newsops.config_set(self.ctx, "stocks", "45m")
        self.assertIn("3600 → 2700", out)
        self.assertIn("DISCOVERY_INTERVAL_STOCKS=2700", self.env_text())
        self.assertTrue(any("--install" in c for c in self.calls),
                        f"cadence change must re-register triggers: {self.calls}")

    def test_unknown_key_rejected(self):
        out = newsops.command(self.ctx, "set", ["volume", "11"])
        self.assertIn("unknown setting", out)
        self.assertNotIn("volume", self.env_text())

    def test_set_provider_chatgpt_writes_canonical_value_and_warns_on_missing_key(self):
        out = newsops.config_set(self.ctx, "provider", "chatgpt")
        self.assertIn("claude_chat → openai", out)
        self.assertIn("ChatGPT", out)
        self.assertIn("OPENAI_API_KEY", out, "missing key must be surfaced")
        self.assertIn("DISCOVERY_PROVIDER=openai", self.env_text())
        self.assertTrue(any(n.startswith(".env-") for n in self.backups()))
        self.assertEqual(self.calls, [], "engine switch must not re-install tasks")

    def test_set_provider_back_to_claude_has_no_warning(self):
        newsops.config_set(self.ctx, "provider", "chatgpt")
        out = newsops.config_set(self.ctx, "provider", "claude")
        self.assertIn("openai → claude_chat", out)
        self.assertIn("claude.ai session", out)
        self.assertNotIn("⚠", out)
        self.assertIn("DISCOVERY_PROVIDER=claude_chat", self.env_text())

    def test_set_provider_warns_when_a_pinned_model_overrides_the_default(self):
        (self.product / ".env").write_text(
            "DISCOVERY_MODEL=claude-opus-5\nOPENAI_API_KEY=sk-x\n", encoding="utf-8")
        out = newsops.config_set(self.ctx, "provider", "chatgpt")
        self.assertIn("DISCOVERY_MODEL is pinned", out)
        self.assertNotIn("OPENAI_API_KEY is not set", out)

    def test_set_provider_rejects_unknown_engine(self):
        out = newsops.command(self.ctx, "set", ["provider", "gemini"])
        self.assertIn("news:", out)
        self.assertIn("chatgpt", out, "error names the valid engines")
        self.assertNotIn("DISCOVERY_PROVIDER", self.env_text())

    def test_bar_rewrites_interests_and_reloads(self):
        out = newsops.config_set(self.ctx, "bar", "alpha", "0.7")
        self.assertIn("0.74 → 0.7", out)
        data = json.loads((self.product / "interests.json").read_text(encoding="utf-8"))
        self.assertEqual(data["interests"][0]["min_score"], 0.7)
        self.assertEqual(data["interests"][1].get("min_score"), None,
                         "other interests untouched")
        self.assertTrue(any(n.startswith("interests.json-") for n in self.backups()))
        self.assertIn(["app", "init"], self.calls, "bar change must reload the DB")

    def test_bar_bounds_and_unknown_interest(self):
        for bad in ("0.3", "1.2", "high"):
            self.assertIn("news:", newsops.command(self.ctx, "set", ["bar", "alpha", bad]))
        out = newsops.command(self.ctx, "set", ["bar", "nope", "0.8"])
        self.assertIn("alpha", out, "error must list the real keys")


class TestRunAndRouting(NewsBase):
    def test_run_maps_names_to_tasks(self):
        out = newsops.run(self.ctx, "digest")
        self.assertIn("triggered digest", out)
        self.assertIn(["schtasks", "/run", "/tn", "internet-discovery-digest"],
                      self.calls)

    def test_run_all_triggers_collectors_only(self):
        newsops.run(self.ctx, "all")
        names = [c[3] for c in self.calls if c[:2] == ["schtasks", "/run"]]
        self.assertEqual(names, ["internet-discovery-collect-stocks",
                                 "internet-discovery-collect-web",
                                 "internet-discovery-collect-youtube"])

    def test_run_unknown_target(self):
        self.assertIn("unknown target", newsops.command(self.ctx, "run", ["everything"]))

    def test_config_text_shows_bars_and_intervals(self):
        out = newsops.config_text(self.ctx)
        self.assertIn("alpha 0.74", out)
        self.assertIn("beta 0.78", out, "default bar shown for bar-less interest")
        self.assertIn("stocks 1h", out)

    def test_config_text_shows_the_engine(self):
        self.assertIn("engine: claude_chat", newsops.config_text(self.ctx))
        newsops.config_set(self.ctx, "provider", "chatgpt")
        out = newsops.config_text(self.ctx)
        self.assertIn("engine: openai", out)
        self.assertIn("gpt-5", out)

    def test_command_never_raises(self):
        def boom(ctx):
            raise RuntimeError("kaput")
        orig = newsops.status
        newsops.status = boom
        try:
            out = newsops.command(self.ctx, "status", [])
        finally:
            newsops.status = orig
        self.assertIn("news: failed", out)

    def test_status_parses_task_blocks(self):
        def fake_run(args, cwd=None, timeout=90):
            if "--status" in [str(a) for a in args]:
                return 0, ("internet-discovery-collect-stocks:\n"
                           "  Status: Ready\n  Last Run Time: x\n"
                           "  Last Result: 0\n  Next Run Time: 10:44\n"
                           "internet-discovery-digest: not installed\n")
            return 0, "ok"
        newsops._run = fake_run
        out = newsops.status(self.ctx)
        self.assertIn("collect-stocks: Ready · last rc 0 · next 10:44", out)
        self.assertIn("⚠ digest: not installed", out)


class TestHelpCoverage(unittest.TestCase):
    """Help must never drift from the actual routing: these tests parse the
    real dispatch code, so adding a command without updating /help (or the
    telegram COMMANDS whitelist, or newsops.HELP) fails the suite."""

    def test_every_routed_command_is_in_help_and_whitelist(self):
        import control
        import telegram as tgm
        src = Path(control.__file__).read_text(encoding="utf-8")
        body = re.search(r"def handle_commands\(.*?(?=\ndef )", src, re.S).group(0)
        routed = set(re.findall(r'name == "(/\w+)"', body))
        self.assertGreaterEqual(len(routed), 12, "routing parse broke")
        help_out = control.help_text()
        for cmd in sorted(routed):
            self.assertIn(cmd, help_out, f"{cmd} is routed but missing from /help")
            self.assertIn(cmd, tgm.COMMANDS,
                          f"{cmd} is routed but missing from telegram.COMMANDS")

    def test_every_news_subcommand_is_in_the_cheat_sheet(self):
        csrc = re.search(r"def command\(.*", Path(newsops.__file__).read_text(
            encoding="utf-8"), re.S).group(0)
        subs = set(re.findall(r'sub == "(\w+)"', csrc))
        self.assertGreaterEqual(len(subs), 4, "subcommand parse broke")
        for sub in sorted(subs):
            self.assertIn(f"/news {sub}", newsops.HELP,
                          f"/news {sub} is routed but missing from the cheat sheet")


if __name__ == "__main__":
    unittest.main(verbosity=2)
