"""Real-Claude commissioning smoke (§45): SMALL subscription-auth checks that
the plan detector, refresh probe, telemetry and one tool-using worker behave
against the actual installed Claude Code. Total model cost: ~2 tiny haiku
requests. Never starts the roadmap; safe to re-run.

Usage: python scripts\\smoke_real.py
"""
import json
import os
import sys
import time
from pathlib import Path

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE))

import capacity as cap      # noqa: E402
import claude_runner as cr  # noqa: E402
import common               # noqa: E402
import control              # noqa: E402
import db                   # noqa: E402

# behave like the scheduled tick, not like a nested Claude session
for k in cr.BILLING_ENV:
    os.environ.pop(k, None)

REPORT = {}


def check(name, ok, detail=""):
    REPORT[name] = (bool(ok), detail)
    print(f"[{'ok' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return ok


def wait_run_done(ctx, conn, key, timeout=420):
    end = time.time() + timeout
    while time.time() < end:
        control.reconcile(ctx, conn)
        run = db.run_by_key(conn, key)
        if run and run["status"] not in ("PREPARED", "DISPATCHED"):
            return run
        time.sleep(3)
    return db.run_by_key(conn, key)


def main():
    ctx = control.make_ctx()
    conn = db.connect(ctx)
    owner_settings = Path.home() / ".claude" / "settings.json"
    owner_before = owner_settings.read_text() if owner_settings.exists() else None
    cj = cap.claude_json_path(ctx)
    fetched_before = ((common.read_json(cj) or {}).get("cachedUsageUtilization")
                      or {}).get("fetchedAtMs")

    # 1. detection against the real machine
    view = cap.detect(ctx, conn, force=True, trigger="smoke")
    print("detection:", json.dumps({k: view.get(k) for k in
          ("tier", "confidence", "auth_mode", "family", "contested",
           "billing_overrides")}, indent=1))
    check("auth is subscription (billing-safe)", cap.auth_ok(view),
          view.get("auth_mode"))
    check("tier detected or safely conservative",
          view.get("tier") in ("max20", "max5", "max_unknown", "pro"),
          f"{view.get('tier')}/{view.get('confidence')}")

    # 2. the automatic refresh probe (fires only if detection is unresolved)
    probe = conn.execute("SELECT * FROM runs WHERE role='probe' "
                         "ORDER BY id DESC LIMIT 1").fetchone()
    if probe and probe["status"] in ("PREPARED", "DISPATCHED"):
        print(f"probe {probe['key']} dispatched; waiting...")
        run = wait_run_done(ctx, conn, probe["key"])
        check("probe completed", run and run["status"] in ("DONE", "QUOTA"),
              run["status"] if run else "lost")
        view = cap.detect(ctx, conn, force=True, trigger="smoke_post_probe")
        print("post-probe detection:", view.get("tier"), view.get("confidence"))
    else:
        print("no probe needed (detection resolved)")

    # 3. one real tool-using worker through the full production contract
    key = f"_smoke.c0.t0.reviewer.{int(time.time()) % 100000}"
    scratch = ctx.art / "wt" / "smoke-scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    (scratch / "readme.txt").write_text("smoke scratch dir\n")
    rd = cr.build_job(ctx, key, "reviewer",
                      'Use the Write tool to write EXACTLY this JSON to '
                      '<<RESULT_PATH>> and do nothing else: '
                      '{"version": 1, "verdict": "PASS", "findings": []}',
                      str(scratch), "haiku", None)
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO runs(key,step_id,task_idx,cycle,role,model,"
            "effort,lane,status,cwd,artifact_dir,deadline,created_at) VALUES"
            "(?, '_smoke', 0, 0, 'reviewer', 'haiku', NULL, 'direct', 'PREPARED',"
            "?,?,?,?)", (key, str(scratch), str(rd), common.iso_in(600), common.now()))
    control.launch(ctx, conn, db.run_by_key(conn, key))
    print(f"worker {key} dispatched (haiku, Write tool); waiting...")
    run = wait_run_done(ctx, conn, key, timeout=600)
    check("worker session completed & reconciled",
          run and run["status"] == "DONE",
          f"status={run['status'] if run else 'missing'} rc={run['exit_code'] if run else '?'}")
    result = common.read_json(rd / "result.json")
    check("worker wrote result artifact via tools",
          isinstance(result, dict) and result.get("verdict") == "PASS",
          json.dumps(result)[:80])
    stdout = cr.read_claude_stdout(ctx, key)
    check("claude CLI produced structured output",
          isinstance(stdout, dict) and "raw" not in stdout,
          f"session={stdout.get('session_id', '?')[:13]}... "
          f"cost=${stdout.get('total_cost_usd', '?')}")
    check("session id matches deterministic identity",
          stdout.get("session_id") == common.session_uuid(key))
    check("no API billing (subscription reports $0)",
          not stdout.get("total_cost_usd"))

    # 4. telemetry after real responses
    cap.ingest_telemetry(ctx, conn)
    usage = cap.current_usage(conn)
    fetched_after = ((common.read_json(cj) or {}).get("cachedUsageUtilization")
                     or {}).get("fetchedAtMs")
    check("usage telemetry present",
          usage is not None,
          f"5h {usage.get('pct5')}% 7d {usage.get('pct7')}% "
          f"age {int(usage.get('age_min', 0))}m src {usage.get('source')}" if usage else "")
    print(f"cli usage cache advanced by -p sessions: "
          f"{fetched_before} -> {fetched_after} "
          f"({'yes' if (fetched_after or 0) > (fetched_before or 0) else 'no'})")
    slfiles = list((ctx.root / "telemetry" / "sessions").glob("*.json"))
    print(f"statusline capture files: {len(slfiles)} "
          f"({'statusline runs in -p mode' if slfiles else 'statusline not driven by -p; cli_cache is primary'})")

    # 5. safety invariants
    owner_after = owner_settings.read_text() if owner_settings.exists() else None
    check("owner global Claude settings untouched", owner_before == owner_after)
    leaks = 0
    for r in conn.execute("SELECT payload x FROM events WHERE payload IS NOT NULL"):
        if common.GENERIC_SECRET_RES[0].search(r["x"] or ""):
            leaks += 1
    check("no secret shapes in event ledger", leaks == 0)
    job = common.read_json(rd / "job.json")
    check("worker env strips billing/API keys",
          "ANTHROPIC_API_KEY" in job.get("env_unset", []))
    check("worker uses isolated settings (no user hooks/statusline)",
          "--setting-sources" in job["argv"] and "project" in job["argv"]
          and any("automation-settings.json" in a for a in job["argv"]))

    fails = [k for k, (ok, _) in REPORT.items() if not ok]
    print("\nSMOKE:", "PASS" if not fails else f"FAIL ({fails})")
    conn.close()
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
