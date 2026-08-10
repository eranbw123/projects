"""Plan-detection (§39), live-telemetry (§40) and governor policy tests.
All deterministic: fixture entitlement/auth files, no real Claude calls."""
import json
import os
import subprocess
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from harness import (AUTH_SUBSCRIPTION, CODE, PY, Sandbox, claude_json_fixture,
                     now_ms)

sys.path.insert(0, str(CODE))
import capacity as cap      # noqa: E402
import common               # noqa: E402
import db                   # noqa: E402

FAKE_TG_TOKEN = "123456789:AAFakeFakeFakeFakeFakeFakeFakeFake12345"


def hours_ago_ms(h):
    return now_ms() - int(h * 3600 * 1000)


class Base(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()
        self.ctx = self.sb.ctx()
        self.conn = self.sb.conn()

    def tearDown(self):
        self.conn.close()
        self.sb.cleanup()

    def detect(self, force=True):
        return cap.detect(self.ctx, self.conn, force=force, trigger="test")

    def write_claude_json(self, obj):
        self.sb.claude_json.write_text(json.dumps(obj))

    def write_auth(self, obj):
        self.sb.auth_json.write_text(json.dumps(obj))


class TestPlanDetection(Base):

    def test_01_exact_max5(self):
        self.sb.set_capacity(tier="max5")
        v = self.detect()
        self.assertEqual((v["tier"], v["confidence"]), ("max5", "confirmed"))
        self.assertEqual(v["auth_mode"], "subscription")

    def test_02_exact_max20(self):
        self.sb.set_capacity(tier="max20")
        v = self.detect()
        self.assertEqual((v["tier"], v["confidence"]), ("max20", "confirmed"))

    def test_03_generic_max_no_exact_tier(self):
        self.sb.set_capacity(tier="none")  # org type max, no rate tier string
        v = self.detect()
        self.assertEqual(v["tier"], "max_unknown")
        self.assertEqual(cap.ENVELOPES["max_unknown"], cap.ENVELOPES["max5"])

    def test_04_no_subscription_metadata(self):
        self.write_claude_json({})
        self.write_auth({"loggedIn": True, "authMethod": "claude.ai",
                         "apiProvider": "firstParty"})
        v = self.detect()
        self.assertEqual(v["tier"], "unknown")
        self.assertEqual(v["confidence"], "conservative_fallback")
        self.assertTrue(cap.auth_ok(v))  # auth fine; capacity just unknown

    def test_05_malformed_metadata(self):
        self.sb.claude_json.write_text("{this is not json")
        v = self.detect()  # must not crash; auth still answers
        self.assertIn(v["tier"], ("max_unknown", "unknown"))

    def test_06_inaccessible_metadata(self):
        self.sb.claude_json.unlink()
        v = self.detect()
        self.assertIn(v["tier"], ("max_unknown", "unknown"))

    def test_07_conflicting_exact_fields(self):
        obj = claude_json_fixture(tier="max5")
        obj["oauthAccount"]["userRateLimitTier"] = "default_claude_max_20x"
        self.write_claude_json(obj)
        v = self.detect()
        self.assertEqual(v["tier"], "max5")  # conservative under exact conflict
        self.assertEqual(v["confidence"], "conflict")

    def test_08_stale_family_vs_fresh_exact(self):
        """Fresh exact max20 profile beats a stale family-level 'pro' claim
        (probable, keeps probing) — the real machine's current situation."""
        obj = claude_json_fixture(tier="max20", fetched_ms=hours_ago_ms(0.1))
        self.write_claude_json(obj)
        old = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat(timespec="seconds")
        self.write_auth({**AUTH_SUBSCRIPTION, "subscriptionType": "pro",
                         "checked_at": old})
        v = self.detect()
        self.assertEqual((v["tier"], v["confidence"]), ("max20", "probable"))

    def test_09_fresh_conservative_vs_stale_exact(self):
        """A FRESHER conservative claim (pro) against a stale exact max20
        forces the conservative envelope + conflict until refresh resolves."""
        obj = claude_json_fixture(tier="max20", fetched_ms=hours_ago_ms(6))
        self.write_claude_json(obj)
        fresh = common.now()
        self.write_auth({**AUTH_SUBSCRIPTION, "subscriptionType": "pro",
                         "checked_at": fresh})
        v = self.detect()
        self.assertEqual(v["confidence"], "conflict")
        self.assertEqual(v["tier"], "pro")  # smallest claim wins during conflict

    def test_10_transition_5_to_20(self):
        self.sb.set_capacity(tier="max5")
        v1 = self.detect()
        time.sleep(0.02)
        self.sb.set_capacity(tier="max20")
        v2 = self.detect(force=False)  # file change alone must trigger re-eval
        self.assertEqual(v1["tier"], "max5")
        self.assertEqual(v2["tier"], "max20")
        self.assertEqual(v2["prev_tier"], "max5")
        rows = self.conn.execute("SELECT * FROM plan_detections ORDER BY id").fetchall()
        self.assertEqual([r["tier"] for r in rows][-2:], ["max5", "max20"])

    def test_11_transition_20_to_5(self):
        self.sb.set_capacity(tier="max20")
        self.detect()
        time.sleep(0.02)
        self.sb.set_capacity(tier="max5")
        v = self.detect(force=False)
        self.assertEqual(v["tier"], "max5")
        self.assertEqual(v["prev_tier"], "max20")

    def test_12_generic_becomes_exact_after_refresh(self):
        self.sb.set_capacity(tier="none")
        v = self.detect()
        self.assertEqual(v["tier"], "max_unknown")
        time.sleep(0.02)
        self.sb.set_capacity(tier="max20")
        v = self.detect(force=False)
        self.assertEqual((v["tier"], v["confidence"]), ("max20", "confirmed"))

    def test_13_not_logged_in(self):
        self.write_auth({"loggedIn": False})
        v = self.detect()
        self.assertEqual(v["auth_mode"], "none")
        self.assertFalse(cap.auth_ok(v))

    def test_14_api_key_shadowing(self):
        self.write_auth({"loggedIn": True, "authMethod": "apiKey",
                         "apiProvider": "firstParty"})
        v = self.detect()
        self.assertFalse(cap.auth_ok(v))
        # env override also blocks even with clean subscription auth
        self.write_auth(AUTH_SUBSCRIPTION)
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test1234567890"
        try:
            v = self.detect()
            self.assertIn("ANTHROPIC_API_KEY", v["billing_overrides"])
            self.assertFalse(cap.auth_ok(v))
        finally:
            del os.environ["ANTHROPIC_API_KEY"]

    def test_15_other_provider_gateway(self):
        os.environ["CLAUDE_CODE_USE_BEDROCK"] = "1"
        try:
            v = self.detect()
            self.assertFalse(cap.auth_ok(v))
        finally:
            del os.environ["CLAUDE_CODE_USE_BEDROCK"]

    def test_16_custom_config_dir(self):
        cfg = self.sb.dir / "customcfg"
        cfg.mkdir()
        (cfg / ".claude.json").write_text(json.dumps(claude_json_fixture(tier="max5")))
        ctx = self.sb.ctx()
        ctx.env.pop("EC_CLAUDE_JSON", None)
        ctx.env["CLAUDE_CONFIG_DIR"] = str(cfg)
        self.assertEqual(cap.claude_json_path(ctx), cfg / ".claude.json")
        v = cap.detect(ctx, self.conn, force=True, trigger="test")
        self.assertEqual(v["tier"], "max5")

    def test_17_18_idempotent_reticks_and_live_change(self):
        self.sb.set_capacity(tier="max20")
        self.detect()
        n0 = self.conn.execute("SELECT COUNT(*) c FROM plan_detections").fetchone()["c"]
        for _ in range(5):
            cap.detect(self.ctx, self.conn, force=False, trigger="tick")
        n1 = self.conn.execute("SELECT COUNT(*) c FROM plan_detections").fetchone()["c"]
        self.assertEqual(n0, n1, "unchanged evidence must not append detections")
        time.sleep(0.02)
        self.sb.set_capacity(tier="max5")   # change while controller active
        cap.detect(self.ctx, self.conn, force=False, trigger="tick")
        n2 = self.conn.execute("SELECT COUNT(*) c FROM plan_detections").fetchone()["c"]
        self.assertEqual(n2, n1 + 1)

    def test_19_unresolved_falls_back_safely(self):
        self.write_claude_json({})
        self.write_auth({"loggedIn": True, "authMethod": "claude.ai",
                         "apiProvider": "firstParty"})
        v = self.detect()
        gov = cap.governor(self.ctx, self.conn, v, None, critical_ready=5)
        self.assertEqual(gov["target"], cap.ENVELOPES["unknown"]["normal"])

    def test_20_no_secret_reaches_storage(self):
        secret = "sk-ant-abcdef1234567890SECRET"
        obj = claude_json_fixture(tier="max20",
                                  extra_fields={"accessToken": secret,
                                                "refreshToken": secret})
        self.write_claude_json(obj)
        self.detect()
        for table, col in (("plan_detections", "evidence"), ("events", "payload"),
                           ("kv", "v")):
            for r in self.conn.execute(f"SELECT {col} x FROM {table}"):
                self.assertNotIn(secret, r["x"] or "")

    def test_probe_dispatch_and_pacing(self):
        """Unresolved tier triggers ONE bounded probe (stub worker), never a
        rapid loop; resolved tier stops probing."""
        self.sb.set_capacity(tier="none")
        ctx = self.sb.ctx()
        ctx.env["EC_DISABLE_PROBE"] = "0"
        v = cap.detect(ctx, self.conn, force=True, trigger="test")
        self.assertEqual(v["tier"], "max_unknown")
        probes = self.conn.execute(
            "SELECT COUNT(*) c FROM runs WHERE role='probe'").fetchone()["c"]
        self.assertEqual(probes, 1)
        v = cap.detect(ctx, self.conn, force=True, trigger="test")
        probes = self.conn.execute(
            "SELECT COUNT(*) c FROM runs WHERE role='probe'").fetchone()["c"]
        self.assertEqual(probes, 1, "probe must be rate-limited/single-flight")


class TestTelemetry(Base):

    def capture(self, payload: dict | str, tdir=None) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env["EC_TELEMETRY_DIR"] = str(tdir or (self.ctx.root / "telemetry" / "sessions"))
        raw = payload if isinstance(payload, str) else json.dumps(payload)
        return subprocess.run([PY, str(CODE / "telemetry_capture.py")],
                              input=raw, capture_output=True, text=True, env=env)

    def sessions_dir(self):
        return self.ctx.root / "telemetry" / "sessions"

    def test_1_no_rate_fields_yet(self):
        p = self.capture({"session_id": "s1", "model": {"id": "sonnet"}})
        self.assertEqual(p.returncode, 0)
        n0 = self.conn.execute("SELECT COUNT(*) c FROM telemetry "
                               "WHERE source='statusline'").fetchone()["c"]
        cap.ingest_telemetry(self.ctx, self.conn)
        n1 = self.conn.execute("SELECT COUNT(*) c FROM telemetry "
                               "WHERE source='statusline'").fetchone()["c"]
        self.assertEqual(n0, n1, "no-percentages record must not be ingested")

    def test_2_3_4_partial_and_full_windows(self):
        self.capture({"session_id": "s5", "usage":
                      {"five_hour": {"utilization": 41, "resets_at": "2026-08-10T20:00:00Z"}}})
        self.capture({"session_id": "s7", "usage":
                      {"seven_day": {"utilization": 12, "resets_at": "2026-08-17T00:00:00Z"}}})
        self.capture({"session_id": "sb", "usage":
                      {"five_hour": {"utilization": 50},
                       "seven_day": {"utilization": 20}}})
        cap.ingest_telemetry(self.ctx, self.conn)
        rows = {r["session_id"]: r for r in self.conn.execute(
            "SELECT * FROM telemetry WHERE source='statusline'")}
        self.assertEqual(rows["s5"]["pct5"], 41)
        self.assertIsNone(rows["s5"]["pct7"])
        self.assertEqual(rows["s7"]["pct7"], 12)
        self.assertEqual((rows["sb"]["pct5"], rows["sb"]["pct7"]), (50, 20))

    def test_5_malformed_stdin(self):
        p = self.capture("{not json")
        self.assertEqual(p.returncode, 0)

    def test_6_7_concurrent_and_repeated_sessions(self):
        self.capture({"session_id": "cA", "usage": {"five_hour": {"utilization": 10}}})
        self.capture({"session_id": "cB", "usage": {"five_hour": {"utilization": 11}}})
        cap.ingest_telemetry(self.ctx, self.conn)
        time.sleep(1.1)  # capture ts has second precision
        self.capture({"session_id": "cA", "usage": {"five_hour": {"utilization": 15}}})
        cap.ingest_telemetry(self.ctx, self.conn)
        cap.ingest_telemetry(self.ctx, self.conn)  # re-ingest: no dup
        rows = self.conn.execute(
            "SELECT * FROM telemetry WHERE source='statusline' AND session_id='cA' "
            "ORDER BY fetched_at").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[-1]["pct5"], 15)

    def test_8_ordering_newest_wins(self):
        with self.conn:
            self.conn.execute(
                "INSERT INTO telemetry(ts,session_id,source,fetched_at,pct5,pct7)"
                " VALUES(?,?,?,?,?,?)",
                (common.now(), "old", "statusline",
                 common.iso_in(-3600), 90, 90))
            self.conn.execute(
                "INSERT INTO telemetry(ts,session_id,source,fetched_at,pct5,pct7)"
                " VALUES(?,?,?,?,?,?)",
                (common.now(), "new", "statusline", common.now(), 10, 5))
        u = cap.current_usage(self.conn)
        self.assertEqual(u["session_id"], "new")

    def test_9_partial_write_ignored(self):
        d = self.sessions_dir()
        d.mkdir(parents=True, exist_ok=True)
        (d / "torn.json").write_text('{"session_id": "torn", "pct5"')
        n = cap.ingest_telemetry(self.ctx, self.conn)  # must not raise
        rows = [r["session_id"] for r in self.conn.execute(
            "SELECT session_id FROM telemetry")]
        self.assertNotIn("torn", rows)

    def test_10_ingest_after_restart(self):
        self.capture({"session_id": "rst", "usage": {"five_hour": {"utilization": 33}}})
        cap.ingest_telemetry(self.ctx, self.conn)
        conn2 = self.sb.conn()  # fresh controller connection
        rows = conn2.execute("SELECT * FROM telemetry WHERE session_id='rst'").fetchall()
        conn2.close()
        self.assertEqual(len(rows), 1)

    def test_11_stale_ignored_for_decisions(self):
        with self.conn:
            self.conn.execute("DELETE FROM telemetry")
            self.conn.execute(
                "INSERT INTO telemetry(ts,session_id,source,fetched_at,pct5,pct7)"
                " VALUES(?,?,?,?,?,?)",
                (common.now(), "stale", "statusline", common.iso_in(-7200), 90, 90))
        u = cap.current_usage(self.conn)
        self.assertTrue(u["stale"])
        self.assertEqual(cap.pressure_level(u), "unknown")

    def test_12_13_no_transcript_or_credentials_stored(self):
        secret = "sk-ant-XYZsecretXYZ12345678"
        self.capture({"session_id": "leaky", "transcript_path": r"C:\priv\t.jsonl",
                      "apiKey": secret, "prompt": "raw user text",
                      "usage": {"five_hour": {"utilization": 5}}})
        f = self.sessions_dir() / "leaky.json"
        body = f.read_text()
        for bad in (secret, "transcript", "priv", "raw user text"):
            self.assertNotIn(bad, body)

    def test_14_owner_global_settings_untouched(self):
        import claude_runner as cr
        user_settings = Path.home() / ".claude" / "settings.json"
        before = user_settings.read_text() if user_settings.exists() else None
        p = cr.ensure_automation_settings(self.ctx)
        self.assertTrue(str(p).startswith(str(self.ctx.root)))
        after = user_settings.read_text() if user_settings.exists() else None
        self.assertEqual(before, after)
        obj = json.loads(p.read_text())
        self.assertIn("telemetry_capture.py", obj["statusLine"]["command"])


class TestGovernorPolicy(Base):

    def gov(self, tier="max20", usage=None, ready=5):
        view = {"tier": tier, "confidence": "confirmed",
                "auth_mode": "subscription", "billing_overrides": []}
        return cap.governor(self.ctx, self.conn, view, usage, ready)

    def fresh(self, p5, p7):
        return {"pct5": p5, "pct7": p7, "fetched_at": common.now(),
                "age_min": 0.1, "stale": False}

    def test_envelope_targets(self):
        self.assertEqual(self.gov("max5", self.fresh(10, 5), ready=1)["target"], 2)
        self.assertEqual(self.gov("max5", self.fresh(10, 5), ready=9)["target"], 3)
        self.assertEqual(self.gov("max20", self.fresh(10, 5), ready=1)["target"], 4)
        self.assertEqual(self.gov("max20", self.fresh(10, 5), ready=9)["target"], 5)
        self.assertEqual(self.gov("pro", self.fresh(10, 5), ready=9)["target"], 2)
        self.assertEqual(self.gov("unknown", self.fresh(10, 5), ready=9)["target"], 1)

    def test_pressure_bands(self):
        self.assertEqual(cap.pressure_level(self.fresh(10, 5)), "plenty")
        self.assertEqual(cap.pressure_level(self.fresh(60, 5)), "moderate")
        self.assertEqual(cap.pressure_level(self.fresh(75, 5)), "high")
        self.assertEqual(cap.pressure_level(self.fresh(90, 5)), "very_high")
        self.assertEqual(cap.pressure_level(self.fresh(96, 5)), "critical")
        self.assertEqual(cap.pressure_level(self.fresh(10, 93)), "very_high")

    def test_pressure_shrinks_target_and_classes(self):
        g = self.gov("max20", self.fresh(75, 10))
        self.assertEqual(g["target"], 3)
        self.assertNotIn("start", g["allowed"])
        g = self.gov("max20", self.fresh(90, 10))
        self.assertEqual(g["target"], 1)
        self.assertEqual(g["allowed"], {"finish"})
        g = self.gov("max20", self.fresh(96, 10))
        self.assertEqual(g["target"], 0)
        self.assertFalse(g["allowed"])

    def test_quota_hold_is_critical(self):
        db.kv_set(self.conn, "quota_hold_until", common.iso_in(600))
        g = self.gov("max20", self.fresh(5, 5))
        self.assertEqual(g["pressure"], "critical")
        db.kv_set(self.conn, "quota_hold_until", common.iso_in(-5))
        g = self.gov("max20", self.fresh(5, 5))
        self.assertEqual(g["pressure"], "plenty")

    def test_fable_and_opus_caps(self):
        g = self.gov("max20", self.fresh(5, 5))
        with self.conn:
            self.conn.execute(
                "INSERT INTO runs(key,step_id,role,model,status,created_at)"
                " VALUES('k1','s1','planner','fable','DISPATCHED',?)", (common.now(),))
        self.assertFalse(cap.can_dispatch(self.ctx, self.conn, g, "planner", "fable"))
        self.assertTrue(cap.can_dispatch(self.ctx, self.conn, g, "planner", "opus"))
        with self.conn:
            self.conn.execute(
                "INSERT INTO runs(key,step_id,role,model,status,created_at)"
                " VALUES('k2','s2','reviewer','opus','DISPATCHED',?)", (common.now(),))
            self.conn.execute(
                "INSERT INTO runs(key,step_id,role,model,status,created_at)"
                " VALUES('k3','s3','reviewer','opus','DISPATCHED',?)", (common.now(),))
        self.assertFalse(cap.can_dispatch(self.ctx, self.conn, g, "reviewer", "opus"))

    def test_manual_override_visible_and_clearable(self):
        db.kv_set(self.conn, "plan_override", "max5")
        g = self.gov("max20", self.fresh(5, 5), ready=1)
        self.assertEqual((g["tier"], g["target"], g["override"]), ("max5", 2, "max5"))
        db.kv_set(self.conn, "plan_override", "")
        g = self.gov("max20", self.fresh(5, 5), ready=1)
        self.assertEqual((g["tier"], g["override"]), ("max20", None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
