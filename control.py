"""engine-control: short-lived idempotent orchestration tick.

Invoked by a Windows Scheduled Task about once per minute:
    python control.py tick
Each tick: single-instance lock -> consume Telegram -> reconcile workers ->
bounded orchestration (at most one dispatch) -> atomic commits -> exit.
Overlapping ticks no-op on the lock. A crash at any point is recovered by the
next tick via deterministic run keys and on-disk dispatch evidence.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import traceback
from pathlib import Path

import capacity as cap
import claude_runner as cr
import common
import db
import gitops
import telegram as tgm
import validators as vd

LADDER_MAX = 4          # implementation + repair1 + repair2(fresh) + final repair
PLANNER_TRIES = 2
REVIEWER_TRIES = 2
QUOTA_RETRY_MIN = 30

INTEGRATION = "automation/integration"


# ---------- config ----------

def make_ctx(root=None) -> common.Ctx:
    return common.Ctx(root)


def load_roadmap(ctx):
    p = ctx.root / "roadmap.yaml"
    if not p.exists():
        p = common.CODE_DIR / "roadmap.yaml"
    return common.load_yaml(p)


def step_cfg(roadmap, step_id) -> dict:
    for s in roadmap["steps"]:
        if s["id"] == step_id:
            return s
    return {}


def repo_cfg(roadmap, name) -> dict:
    return roadmap["repos"][name]


def dag_errors(roadmap) -> list[str]:
    """Unknown dependencies and cycles are configuration errors: the roadmap
    must never start (or silently skip) on a broken DAG."""
    errs = []
    ids = {s["id"] for s in roadmap["steps"]}
    deps = {}
    for s in roadmap["steps"]:
        ds = []
        for d in s.get("depends_on", []) or []:
            base = str(d).split(":")[0]
            if base not in ids:
                errs.append(f"{s['id']}: unknown dependency '{d}'")
            ds.append(base)
        deps[s["id"]] = ds
    seen, stack = {}, []

    def visit(n):
        if seen.get(n) == 1:
            errs.append("dependency cycle: " + " -> ".join(stack + [n]))
            return
        if n in seen:
            return
        seen[n] = 1
        stack.append(n)
        for d in deps.get(n, []):
            visit(d)
        stack.pop()
        seen[n] = 2

    for n in deps:
        visit(n)
    return errs


def crit_weight(roadmap) -> dict:
    """Longest downstream chain per step (unit weights): how many critical
    steps a completion unblocks. Background steps never add weight."""
    dependents = {s["id"]: [] for s in roadmap["steps"]}
    bg = {s["id"]: bool(s.get("background")) for s in roadmap["steps"]}
    for s in roadmap["steps"]:
        for d in s.get("depends_on", []) or []:
            base = str(d).split(":")[0]
            if base in dependents:
                dependents[base].append(s["id"])
    memo = {}

    def w(n):
        if n in memo:
            return memo[n]
        memo[n] = 0 if bg[n] else 1 + max((w(d) for d in dependents[n]
                                           if not bg[d]), default=0)
        return memo[n]

    return {n: w(n) for n in dependents}


def sync_steps(conn, ctx, roadmap):
    ids = [s["id"] for s in roadmap["steps"]]
    for s in roadmap["steps"]:
        row = db.get_step(conn, s["id"])
        vals = (s["ordinal"], s.get("title", ""), json.dumps(s.get("repos", [])),
                json.dumps(s.get("depends_on", []) or []),
                1 if s.get("background") else 0, 1 if s.get("audit") else 0)
        if not row:
            with conn:
                conn.execute(
                    "INSERT INTO steps(id,ordinal,title,repos,depends_on,"
                    "background,audit,state,detail,updated_at)"
                    " VALUES(?,?,?,?,?,?,?, 'PENDING','{}',?)",
                    (s["id"], *vals, common.now()))
        elif (row["ordinal"], row["title"], row["repos"], row["depends_on"],
              row["background"], row["audit"]) != vals:
            with conn:
                conn.execute(
                    "UPDATE steps SET ordinal=?, title=?, repos=?, depends_on=?,"
                    " background=?, audit=? WHERE id=?", (*vals, s["id"]))
    # a step edited out of the roadmap must not wedge the scheduler forever
    for row in conn.execute("SELECT id,state FROM steps").fetchall():
        if row["id"] not in ids and row["state"] not in ("ACCEPTED", "ABORTED"):
            db.transition(conn, ctx, row["id"], "ABORTED", "removed from roadmap.yaml")


def route_model(scfg, role):
    spec = cr.ROLES[role]
    return (scfg.get("models", {}) or {}).get(role, spec["model"]), spec["effort"]


def prompt_template(name) -> str:
    return (common.CODE_DIR / "prompts" / name).read_text(encoding="utf-8")


def fill(template: str, subs: dict) -> str:
    for k, v in subs.items():
        template = template.replace(f"<<{k}>>", str(v))
    return template


# ---------- workspace ----------

def cmd_init(ctx, conn=None):
    conn = conn or db.connect(ctx)
    roadmap = load_roadmap(ctx)
    sync_steps(conn, ctx, roadmap)
    for name, rc in roadmap["repos"].items():
        ws = Path(rc["workspace"])
        if not (ws / ".git").exists():
            ws.parent.mkdir(parents=True, exist_ok=True)
            gitops.clone(rc["source"], ws)
            db.event(conn, ctx, "workspace_cloned", repo=name, dst=str(ws))
        gitops.ensure_branch(ws, INTEGRATION, rc["baseline"])
        cur = gitops.git_ro(["rev-parse", "--abbrev-ref", "HEAD"], cwd=ws).stdout.strip()
        if cur != INTEGRATION:
            gitops.checkout(ws, INTEGRATION)
        gitops.disable_push(ws)
        # This machine has no global git identity; workspace-local identity is
        # required for cherry-picks and covers worker commits in worktrees too.
        gitops.run_git(["config", "user.name", "engine-control"], cwd=ws)
        gitops.run_git(["config", "user.email", "engine-control@local"], cwd=ws)
    db.event(conn, ctx, "init_ok")
    return conn


def integration_sha(roadmap, repo_name) -> str:
    ws = Path(repo_cfg(roadmap, repo_name)["workspace"])
    return gitops.current_sha(ws, INTEGRATION)


# ---------- dispatch ----------

def gate(ctx, conn, role, model, new_step=False, background=False) -> bool:
    """Per-tick dispatch gate (capacity governor). CLI paths without a
    governor context are not gated."""
    gov = getattr(ctx, "gov", None)
    if gov is None:
        return True
    if gov["budget"] <= 0:
        return False
    return cap.can_dispatch(ctx, conn, gov, role, model,
                            new_step=new_step, background=background)


def dispatch(ctx, conn, step, role, prompt, cwd, to_state, reason,
             task_idx=None, model=None, effort=None, test_cmds=None,
             resume_session=None, lane=None):
    """Atomic dispatch: run row + active_run_id + stage transition commit in
    ONE transaction, then the worker is launched (idempotently). A crash at
    any point leaves a state the next tick resumes without duplicating work."""
    detail = db.step_detail(step)
    cycle = detail.get("cycle", 0)
    task_idx = detail.get("task_idx", 0) if task_idx is None else task_idx
    seq = db.next_seq(conn, step["id"], task_idx, role)
    key = f"{step['id']}.c{cycle}.t{task_idx}.{role}.{seq}"
    lane = lane or ctx.getenv("EC_LANE", "task")
    spec = cr.ROLES[role]
    model = model or spec["model"]
    effort = effort or spec["effort"]
    deadline = common.iso_in(spec["deadline_min"] * 60 + 600)
    # Build artifacts BEFORE the DB row: a crash in between leaves only an
    # orphan artifact dir that the same deterministic key overwrites later.
    cr.build_job(ctx, key, role, prompt, str(cwd), model, effort,
                 resume_session=resume_session, test_cmds=test_cmds)
    frm = step["state"]
    import sqlite3 as _sq
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO runs(key,step_id,task_idx,cycle,role,model,effort,lane,"
                "status,cwd,artifact_dir,deadline,created_at) "
                "VALUES(?,?,?,?,?,?,?,?, 'PREPARED', ?,?,?,?)",
                (key, step["id"], task_idx, cycle, role, model, effort, lane,
                 str(cwd), str(cr.art_dir(ctx, key)), deadline, common.now()))
            conn.execute("UPDATE steps SET active_run_id=?, state=?, updated_at=? WHERE id=?",
                         (cur.lastrowid, to_state, common.now(), step["id"]))
            if frm != to_state:
                conn.execute(
                    "INSERT INTO transitions(ts,step_id,from_state,to_state,reason)"
                    " VALUES(?,?,?,?,?)",
                    (common.now(), step["id"], frm, to_state, ctx.redact(reason)[:500]))
    except _sq.IntegrityError:
        # Deterministic key already exists: this is a replay of already-planned
        # work (crash-recovery path). Adopt the existing run instead of failing.
        run = db.run_by_key(conn, key)
        with conn:
            conn.execute("UPDATE steps SET active_run_id=?, state=?, updated_at=? WHERE id=?",
                         (run["id"], to_state, common.now(), step["id"]))
        db.event(conn, ctx, "run_readopted", step_id=step["id"], run_id=run["id"], key=key)
        if run["status"] == "PREPARED":
            launch(ctx, conn, run)
        return run
    run = db.run_by_key(conn, key)
    launch(ctx, conn, run)
    gov = getattr(ctx, "gov", None)
    snap = {}
    if gov:
        gov["budget"] -= 1
        gov["active"] = cap.claude_active(conn)
        u = gov.get("usage") or {}
        snap = dict(tier=gov["tier"], conf=gov["confidence"],
                    pressure=gov["pressure"], target=gov["target"],
                    active=gov["active"], pct5=u.get("pct5"), pct7=u.get("pct7"))
    db.event(conn, ctx, "dispatched", step_id=step["id"], run_id=run["id"],
             key=key, role=role, model=model, lane=lane, frm=frm, to=to_state,
             **snap)
    return run


def launch(ctx, conn, run):
    """Idempotent: adopts pre-existing dispatch evidence instead of spawning a
    duplicate (covers a crash between spawn and the DISPATCHED record)."""
    ev = cr.dispatch_evidence(ctx, run["key"])
    if ev is None:
        ext = cr.spawn(ctx, run["key"], run["lane"])
        if os.environ.get("EC_TEST_CRASH_AFTER") == "spawn":
            os._exit(9)  # test hook: crash before recording dispatch
    else:
        ext = {"lane": run["lane"], "adopted": ev,
               "session": common.session_uuid(run["key"])}
        db.event(conn, ctx, "run_adopted", run_id=run["id"], evidence=ev)
    with conn:
        conn.execute("UPDATE runs SET status='DISPATCHED', external=? WHERE id=?",
                     (json.dumps(ext), run["id"]))


# ---------- reconcile ----------

def reconcile(ctx, conn):
    for run in db.open_runs(conn):
        if run["status"] == "PREPARED":
            launch(ctx, conn, run)
            continue
        pr = cr.probe(ctx, run)
        if pr["phase"] == "done":
            st = finalize(ctx, conn, run, pr["rc"])
            if run["role"] == "probe":
                cap.on_probe_result(ctx, conn, run, st)
        elif pr["phase"] == "lost":
            db.finish_run(conn, ctx, run["id"], "LOST", note="process gone, no exitcode")
            if run["lane"] == "task":
                cr.delete_task(run["key"])
            step = db.get_step(conn, run["step_id"])
            if step and step["active_run_id"] == run["id"] \
                    and step["state"] not in common.HALT_STATES:
                d = db.step_detail(step)
                if step["state"] not in common.WAITING_STATES:
                    d["resume"] = step["state"]
                db.set_detail(conn, step["id"], d)
                db.transition(conn, ctx, step["id"], "INTERRUPTED",
                              f"worker {run['key']} lost")
                tgm.notify(conn, ctx, f"lost:{run['key']}",
                           f"engine-control: worker lost ({run['key']}), will respawn after reconcile")
        elif pr["phase"] == "running" and run["deadline"] and common.is_past(run["deadline"]):
            cr.kill_run(ctx, run)
            db.finish_run(conn, ctx, run["id"], "FAILED", exit_code=124,
                          note="deadline exceeded; killed")


def finalize(ctx, conn, run, rc):
    stdout = cr.read_claude_stdout(ctx, run["key"])
    stderr = cr.stderr_tail(ctx, run["key"])
    if cr.cli_quota(stdout, stderr, rc):
        status = "QUOTA"
    elif rc == 0:
        status = "DONE"
    else:
        status = "FAILED"
    db.finish_run(conn, ctx, run["id"], status, exit_code=rc,
                  note=common.tail(json.dumps(stdout) + "\n" + stderr, 5)
                  if status != "DONE" else "")
    if run["lane"] == "task":
        cr.delete_task(run["key"])
    return status


# ---------- step state machine ----------

def steps_all(conn):
    return conn.execute("SELECT * FROM steps ORDER BY ordinal").fetchall()


def live_steps(conn):
    return [s for s in steps_all(conn) if s["state"] not in ("ACCEPTED", "ABORTED")]


def dep_satisfied(conn, dep: str) -> bool:
    """'step-x' needs ACCEPTED; 'step-x:validated' is satisfied from SOAKING
    on (validated work exists; the soak result is not a dependency)."""
    base, _, qual = str(dep).partition(":")
    row = db.get_step(conn, base)
    if not row:
        return False
    if row["state"] == "ACCEPTED":
        return True
    return qual == "validated" and row["state"] == "SOAKING"


def step_ready(conn, step) -> bool:
    if step["state"] != "PENDING":
        return False
    return all(dep_satisfied(conn, d)
               for d in json.loads(step["depends_on"] or "[]"))


def set_resume(conn, step, state=None):
    d = db.step_detail(step)
    d["resume"] = state or step["state"]
    db.set_detail(conn, step["id"], d)


def wait_quota(ctx, conn, step):
    d = db.step_detail(step)
    streak = d.get("quota_streak", 0) + 1
    d["quota_streak"] = streak
    if step["state"] not in common.WAITING_STATES:
        d["resume"] = step["state"]
    db.set_detail(conn, step["id"], d)
    # a CLI-reported limit is hard capacity information for the whole
    # scheduler, not just this step. Trust the cached reset time only when
    # fresh usage numbers corroborate exhaustion; a quota hit with low cached
    # usage means the cache is stale — back off by the retry interval instead.
    retry_min = int(ctx.getenv("EC_QUOTA_RETRY_MIN", str(QUOTA_RETRY_MIN)))
    usage = cap.current_usage(conn)
    hold = common.iso_in(retry_min * 60)
    if usage and not usage.get("stale") and usage.get("reset5") \
            and max(usage.get("pct5") or 0, usage.get("pct7") or 0) >= 90:
        try:
            if common.is_past(usage["reset5"]):
                raise ValueError
            hold = usage["reset5"]
        except ValueError:
            pass
    db.kv_set(conn, "quota_hold_until", hold)
    cap.detect(ctx, conn, force=True, trigger="quota")
    # A quota wait is NEVER an implementation failure and never reaches the
    # repair ladder (weekly-limit outages can legitimately last days —
    # commissioning review finding). Backoff escalates 1x/2x/4x and the owner
    # is re-notified periodically instead.
    backoff = retry_min * (2 ** min(streak // 5, 2))
    with conn:
        conn.execute("UPDATE steps SET retry_at=? WHERE id=?",
                     (common.iso_in(backoff * 60), step["id"]))
    db.transition(conn, ctx, step["id"], "WAITING_QUOTA", "model quota/limit hit")
    if streak == 1 or streak % 8 == 0:
        tgm.notify(conn, ctx, f"quota:{step['id']}:{streak}",
                   f"engine-control: {step['id']} waiting on model quota "
                   f"(streak {streak}); retry in {backoff}m. Resumes automatically.")


def goto_blocked(ctx, conn, step, reason):
    db.transition(conn, ctx, step["id"], "BLOCKED", reason)
    tgm.notify(conn, ctx, f"blocked:{step['id']}:{db.step_detail(step).get('cycle',0)}",
               f"engine-control: {step['id']} BLOCKED — {reason[:400]}\n"
               f"Full history: /why {step['id']} · then /retry or /abort.")


def repair_or_block(ctx, conn, step, findings: str):
    d = db.step_detail(step)
    task_idx = d.get("task_idx", 0)
    cycle = d.get("cycle", 0)
    pos = db.ladder_pos(conn, step["id"], task_idx, cycle)
    d["last_findings"] = findings[:6000]
    db.set_detail(conn, step["id"], d)
    if not d.get("task_wt"):
        goto_blocked(ctx, conn, step,
                     "failure before implementation stage: " + findings[:200])
        return
    if pos >= LADDER_MAX:
        goto_blocked(ctx, conn, step, f"repair budget exhausted ({pos} attempts). Last: {findings[:200]}")
        return
    if pos == 3 and not d.get("diagnosed"):
        dispatch_diagnostic(ctx, conn, step, findings)
        return
    dispatch_repair(ctx, conn, step, findings, pos)


def dispatch_repair(ctx, conn, step, findings, pos):
    roadmap = load_roadmap(ctx)
    scfg = step_cfg(roadmap, step["id"])
    if not gate(ctx, conn, "repair", route_model(scfg, "repair")[0]):
        return  # findings persisted; retried next tick
    d = db.step_detail(step)
    resume_session = None
    if pos == 1:  # first repair continues the implementer's session context
        impl = conn.execute(
            "SELECT * FROM runs WHERE step_id=? AND task_idx=? AND cycle=? AND role='implementer' "
            "AND status IN ('DONE','FAILED') ORDER BY id DESC LIMIT 1",
            (step["id"], d.get("task_idx", 0), d.get("cycle", 0))).fetchone()
        if impl and not ctx.getenv("EC_WORKER_CMD"):
            resume_session = common.session_uuid(impl["key"])
    model, effort = route_model(scfg, "repair")
    prompt = fill(prompt_template("repair.md"), {
        "STEP_ID": step["id"], "OBJECTIVE": task_objective(ctx, step),
        "FINDINGS": findings, "RESULT_PATH": "<<RESULT_PATH>>",
        "RULES": worker_rules(ctx, step)})
    # The rung transition carries WHAT is being repaired: "repair rung 2"
    # alone forced a trip into artifact dirs to learn why (owner finding).
    why = " — " + (findings or "").strip().splitlines()[0][:160] if findings else ""
    run = dispatch(ctx, conn, step, "repair", prompt, d["task_wt"],
                   "REPAIRING", f"repair rung {pos + 1}{why}",
                   model=model, effort=effort, resume_session=resume_session)
    tgm.notify(conn, ctx, f"repair:{run['key']}",
               f"engine-control: {step['id']} repair started (rung {pos + 1}/{LADDER_MAX})")


def dispatch_diagnostic(ctx, conn, step, findings):
    roadmap = load_roadmap(ctx)
    scfg = step_cfg(roadmap, step["id"])
    if not gate(ctx, conn, "diagnostic", route_model(scfg, "diagnostic")[0]):
        return
    d = db.step_detail(step)
    d["diagnosed"] = True
    db.set_detail(conn, step["id"], d)
    ws = Path(repo_cfg(roadmap, current_repo(ctx, step))["workspace"])
    head = gitops.current_sha(Path(d["task_wt"]))
    diff = gitops.diff_text(ws, d["task_base"], head, max_lines=2500)
    model, effort = route_model(scfg, "diagnostic")
    prompt = fill(prompt_template("diagnostic.md"), {
        "STEP_ID": step["id"], "OBJECTIVE": task_objective(ctx, step),
        "FINDINGS": findings, "DIFF": diff, "RESULT_PATH": "<<RESULT_PATH>>"})
    dispatch(ctx, conn, step, "diagnostic", prompt, d["task_wt"],
             "REPAIRING", "independent diagnostic before final repair",
             model=model, effort=effort)


def current_repo(ctx, step) -> str:
    d = db.step_detail(step)
    plan = common.read_json(Path(step["plan_path"])) if step["plan_path"] else None
    if plan:
        return plan["tasks"][d.get("task_idx", 0)]["repo"]
    return json.loads(step["repos"])[0]


def task_objective(ctx, step) -> str:
    plan = common.read_json(Path(step["plan_path"])) if step["plan_path"] else None
    if plan:
        d = db.step_detail(step)
        t = plan["tasks"][min(d.get("task_idx", 0), len(plan["tasks"]) - 1)]
        return t["objective"]
    return step["title"]


def worker_rules(ctx, step) -> str:
    return (
        "- Work ONLY inside the current working directory (an isolated git worktree).\n"
        "- Never touch .env files, *.db files, or anything outside the worktree "
        "except writing your result JSON to RESULT_PATH.\n"
        "- Never run git push, never merge, never checkout main/master, never "
        "git reset --hard, never delete branches.\n"
        "- Commit your work locally; every commit message MUST end with a line:\n"
        "  EC-Key: <<RUN_KEY>>\n"
        "- Read the repo's CLAUDE.md / PROJECT_STATE.md first and follow its "
        "conventions and token-efficiency rules.\n"
        "- No secrets in code, commits, or your result JSON.\n"
        "- Never touch production stores or live services (discovery.db, "
        "conversations.db, Chrome/CDP, Telegram): work offline against the "
        "repo's test seams. Other workers may be active in parallel worktrees.\n")


# ---- stage starters ----

def start_planning(ctx, conn, roadmap, step, new_step=False):
    scfg = step_cfg(roadmap, step["id"])
    if not gate(ctx, conn, "planner", route_model(scfg, "planner")[0],
                new_step=new_step, background=bool(step["background"])):
        return
    repos = json.loads(step["repos"])
    blocks = []
    for r in repos:
        rc = repo_cfg(roadmap, r)
        blocks.append(f"- repo '{r}': workspace {rc['workspace']} (integration branch "
                      f"{INTEGRATION} @ {integration_sha(roadmap, r)[:10]}), canonical tests: {rc['tests']}")
    handoffs = []
    for row in conn.execute("SELECT id, handoff_path FROM steps WHERE state='ACCEPTED' ORDER BY ordinal"):
        if row["handoff_path"] and Path(row["handoff_path"]).exists():
            handoffs.append(Path(row["handoff_path"]).read_text(encoding="utf-8")[:8000])
    primary = repos[0]
    ws = Path(repo_cfg(roadmap, primary)["workspace"])
    key_preview = f"{step['id']}-planner"
    wt = ctx.art / "wt" / key_preview
    if wt.exists():
        gitops.worktree_remove(ws, wt)
    gitops.worktree_add_detached(ws, wt, integration_sha(roadmap, primary))
    model, effort = route_model(scfg, "planner")
    prompt = fill(prompt_template("planner.md"), {
        "STEP_ID": step["id"], "TITLE": step["title"],
        "OBJECTIVE": scfg.get("objective", step["title"]),
        "ACCEPTANCE": scfg.get("acceptance", ""),
        "EXPERIMENT": "yes" if scfg.get("experiment") else "no",
        "REPOS_BLOCK": "\n".join(blocks),
        "HANDOFFS": "\n---\n".join(handoffs) or "(none yet)",
        "SCHEMA": (common.CODE_DIR / "schemas" / "plan.schema.json").read_text(encoding="utf-8"),
        "RESULT_PATH": "<<RESULT_PATH>>"})
    dispatch(ctx, conn, step, "planner", prompt, wt, "PLANNING",
             "planner dispatched", model=model, effort=effort)
    tgm.notify(conn, ctx, f"start:{step['id']}:{db.step_detail(step).get('cycle',0)}",
               f"engine-control: {step['id']} started — {step['title']}")


def start_task_impl(ctx, conn, roadmap, step, task_idx=None):
    scfg = step_cfg(roadmap, step["id"])
    if not gate(ctx, conn, "implementer", route_model(scfg, "implementer")[0]):
        return
    d = db.step_detail(step)
    cycle = d.get("cycle", 0)
    idx = d.get("task_idx", 0) if task_idx is None else task_idx
    plan = common.read_json(Path(step["plan_path"])) if step["plan_path"] else None
    repo = plan["tasks"][idx]["repo"] if plan else json.loads(step["repos"])[0]
    rc = repo_cfg(roadmap, repo)
    ws = Path(rc["workspace"])
    # Replay defense: if this task already has a live or completed implementer
    # (crash/replay re-entry), adopt it — never dispatch a sibling worker.
    prior = conn.execute(
        "SELECT * FROM runs WHERE step_id=? AND cycle=? AND task_idx=? AND "
        "role='implementer' ORDER BY id DESC LIMIT 1",
        (step["id"], cycle, idx)).fetchone()
    if prior and prior["status"] in ("PREPARED", "DISPATCHED", "DONE"):
        frm = step["state"]
        with conn:
            conn.execute("UPDATE steps SET active_run_id=?, state='IMPLEMENTING', "
                         "updated_at=? WHERE id=?",
                         (prior["id"], common.now(), step["id"]))
            if frm != "IMPLEMENTING":
                conn.execute(
                    "INSERT INTO transitions(ts,step_id,from_state,to_state,reason)"
                    " VALUES(?,?,?,?,?)",
                    (common.now(), step["id"], frm, "IMPLEMENTING",
                     "replay: adopted existing task worker"))
        db.event(conn, ctx, "run_readopted", step_id=step["id"],
                 run_id=prior["id"], key=prior["key"])
        if prior["status"] == "PREPARED":
            launch(ctx, conn, prior)
        return
    branch = f"ec/{step['id']}-c{cycle}-t{idx}"
    wt = ctx.art / "wt" / f"{step['id']}-c{cycle}-t{idx}"
    base = d.get("task_base")
    if not base or d.get("task_branch") != branch:
        base = integration_sha(roadmap, repo)
    d.update(task_idx=idx, task_base=base, task_branch=branch, task_wt=str(wt),
             diagnosed=False)
    db.set_detail(conn, step["id"], d)
    if not wt.exists():
        gitops.worktree_add(ws, wt, branch, base)
    plan_json = Path(step["plan_path"]).read_text(encoding="utf-8") if step["plan_path"] else "{}"
    objective = plan["tasks"][idx]["objective"] if plan else step["title"]
    model, effort = route_model(scfg, "implementer")
    prompt = fill(prompt_template("implementer.md"), {
        "STEP_ID": step["id"], "OBJECTIVE": objective,
        "PLAN": plan_json[:12000], "REPO": repo,
        "TESTS": json.dumps(rc["tests"]),
        "RULES": worker_rules(ctx, step), "RESULT_PATH": "<<RESULT_PATH>>"})
    dispatch(ctx, conn, step, "implementer", prompt, wt, "IMPLEMENTING",
             f"task {idx} implementer dispatched", task_idx=idx,
             model=model, effort=effort)


def start_testing(ctx, conn, roadmap, step):
    if not gate(ctx, conn, "test", None):
        return
    d = db.step_detail(step)
    repo = current_repo(ctx, step)
    if repo_tests_busy(ctx, conn, roadmap, repo, exclude_step=step["id"]):
        return  # one canonical suite per repo at a time; retried next tick
    cmds = repo_cfg(roadmap, repo)["tests"]
    dispatch(ctx, conn, step, "test", "", d["task_wt"], "TESTING",
             "canonical repo tests in worktree", test_cmds=cmds)


def start_review(ctx, conn, roadmap, step):
    scfg = step_cfg(roadmap, step["id"])
    if not gate(ctx, conn, "reviewer", route_model(scfg, "reviewer")[0]):
        return
    d = db.step_detail(step)
    repo = current_repo(ctx, step)
    ws = Path(repo_cfg(roadmap, repo)["workspace"])
    head = gitops.current_sha(Path(d["task_wt"]))
    diff = gitops.diff_text(ws, d["task_base"], head)
    test_report = last_test_report(ctx, conn, step)
    rv_wt = ctx.art / "wt" / f"rv-{step['id']}-c{d.get('cycle',0)}-t{d.get('task_idx',0)}"
    if rv_wt.exists():
        gitops.worktree_remove(ws, rv_wt)
    gitops.worktree_add_detached(ws, rv_wt, head)
    model, effort = route_model(scfg, "reviewer")
    prompt = fill(prompt_template("reviewer.md"), {
        "STEP_ID": step["id"], "OBJECTIVE": task_objective(ctx, step),
        "ACCEPTANCE": scfg.get("acceptance", ""),
        "PLAN": (Path(step["plan_path"]).read_text(encoding="utf-8")[:8000]
                 if step["plan_path"] else "{}"),
        "DIFF": diff, "TEST_REPORT": test_report[:6000],
        "SCHEMA": (common.CODE_DIR / "schemas" / "review.schema.json").read_text(encoding="utf-8"),
        "RESULT_PATH": "<<RESULT_PATH>>"})
    dispatch(ctx, conn, step, "reviewer", prompt, rv_wt, "REVIEWING",
             "independent reviewer dispatched", model=model, effort=effort)


def last_test_report(ctx, conn, step) -> str:
    row = conn.execute(
        "SELECT * FROM runs WHERE step_id=? AND role='test' AND status='DONE' "
        "ORDER BY id DESC LIMIT 1", (step["id"],)).fetchone()
    if not row:
        return "(no test report)"
    obj = common.read_json(Path(row["artifact_dir"]) / "result.json") or {}
    return obj.get("report", "(no report)")


def repo_integration_busy(conn, ws: Path, exclude_step=None) -> bool:
    """True while another step's validation run holds this repo's integration
    checkout. Integration stays serialized per repository even though steps
    run in parallel (worktrees are per-step; the ws checkout is shared)."""
    for r in conn.execute(
            "SELECT step_id, cwd FROM runs WHERE role='test' "
            "AND status IN ('PREPARED','DISPATCHED')"):
        if r["step_id"] != exclude_step and r["cwd"] and \
                Path(r["cwd"]).resolve() == ws.resolve():
            return True
    return False


def repo_tests_busy(ctx, conn, roadmap, repo: str, exclude_step=None) -> bool:
    """At most ONE canonical-test run per repository at a time (worktree or
    integration): repo suites share machine-level fixtures/ports even when
    checkouts are isolated (commissioning review finding)."""
    for r in conn.execute(
            "SELECT step_id FROM runs WHERE role='test' "
            "AND status IN ('PREPARED','DISPATCHED')"):
        if r["step_id"] == exclude_step:
            continue
        step = db.get_step(conn, r["step_id"])
        if step is None:
            continue
        try:
            if current_repo(ctx, step) == repo:
                return True
        except (KeyError, IndexError, TypeError):
            continue
    return False


def enter_validation(ctx, conn, roadmap, step):
    """Controller-owned promotion: acceptance checks + cherry-pick to
    automation/integration (idempotent via -x provenance), then an independent
    test run on the integration checkout. Serialized per repo."""
    d = db.step_detail(step)
    repo = current_repo(ctx, step)
    rc = repo_cfg(roadmap, repo)
    ws = Path(rc["workspace"])
    if repo_integration_busy(conn, ws, exclude_step=step["id"]) or \
            repo_tests_busy(ctx, conn, roadmap, repo, exclude_step=step["id"]):
        return  # stay in current state; retried next tick
    if not gate(ctx, conn, "test", None):
        return
    head = gitops.current_sha(Path(d["task_wt"]))
    base = d["task_base"]
    ok, findings = vd.git_acceptance(ws, base, head, ctx._secrets)
    if not ok:
        repair_or_block(ctx, conn, step, "git acceptance failed: " + "; ".join(findings))
        return
    cur = gitops.git_ro(["rev-parse", "--abbrev-ref", "HEAD"], cwd=ws).stdout.strip()
    if cur != INTEGRATION:
        gitops.checkout(ws, INTEGRATION)
    # A controller death mid-cherry-pick leaves CHERRY_PICK_HEAD; a fresh
    # attempt would fail with "already in progress". Abort is non-destructive.
    gitops.run_git(["cherry-pick", "--abort"], cwd=ws, check=False)
    task_key = f"{step['id']}.c{d.get('cycle', 0)}.t{d.get('task_idx', 0)}"

    def record(sha, integrated):
        if not conn.execute("SELECT 1 FROM commits WHERE step_id=? AND repo=? AND sha=?",
                            (step["id"], repo, sha)).fetchone():
            with conn:
                conn.execute(
                    "INSERT INTO commits(step_id,repo,task_idx,run_key,sha,base_sha,"
                    "integrated_sha,integrated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (step["id"], repo, d.get("task_idx", 0), task_key, sha, base,
                     integrated, common.now()))

    for sha in gitops.commits_between(ws, base, head):
        already = gitops.git_ro(
            ["log", INTEGRATION, f"--grep=cherry picked from commit {sha}",
             "--format=%H", "-1"], cwd=ws, check=False).stdout.strip()
        if already:
            record(sha, already)
            continue
        status, new_sha = cherry_pick_x(ws, sha)
        if status == "conflict":
            db.event(conn, ctx, "cherry_conflict", step_id=step["id"], sha=sha)
            repair_or_block(ctx, conn, step,
                            f"cherry-pick conflict integrating {sha[:10]} onto {INTEGRATION}: "
                            "integration advanced under this task. Re-apply the change as new "
                            "commits that apply cleanly on the current integration content; if "
                            "that is impossible from this worktree, report status failed "
                            "(the owner can /retry to re-plan from the new tip)")
            return
        if status == "ok":
            record(sha, new_sha)
    dispatch(ctx, conn, step, "test", "", ws, "VALIDATING",
             "integration tests on promoted commits", test_cmds=rc["tests"])
    tgm.notify(conn, ctx, f"validate:{step['id']}:c{d.get('cycle',0)}t{d.get('task_idx',0)}",
               f"engine-control: {step['id']} promoted to {INTEGRATION}; validation running")


def cherry_pick_x(repo, sha):
    p = gitops.run_git(["cherry-pick", "-x", sha], cwd=repo, check=False)
    if p.returncode == 0:
        return "ok", gitops.current_sha(repo)
    err = (p.stdout + p.stderr).lower()
    if "empty" in err:
        gitops.run_git(["cherry-pick", "--skip"], cwd=repo, check=False)
        return "empty", None
    gitops.run_git(["cherry-pick", "--abort"], cwd=repo, check=False)
    if "conflict" in err:
        return "conflict", None
    raise gitops.GitError(f"cherry-pick {sha[:10]} failed (not a conflict): "
                          + (p.stderr or p.stdout).strip()[:300])


def accept_step(ctx, conn, roadmap, step):
    scfg = step_cfg(roadmap, step["id"])
    handoff = build_handoff(ctx, conn, roadmap, step)
    hp = ctx.art / "handoffs" / f"{step['id']}.json"
    # handoffs are long-lived cross-step memory consumed by later prompts:
    # redact like every other durable artifact (commissioning review finding)
    common.write_atomic(hp, ctx.redact(json.dumps(handoff, indent=1)))
    errs = common.schema_errors(handoff, common.load_schema("handoff.schema.json"))
    if errs:
        db.event(conn, ctx, "handoff_schema_warn", step_id=step["id"], errs=errs)
    with conn:
        conn.execute("UPDATE steps SET handoff_path=?, active_run_id=NULL WHERE id=?",
                     (str(hp), step["id"]))
    soak = int(scfg.get("soak_minutes", 0) or 0)
    if soak > 0:
        with conn:
            conn.execute("UPDATE steps SET soak_until=? WHERE id=?",
                         (common.iso_in(soak * 60), step["id"]))
        db.transition(conn, ctx, step["id"], "SOAKING", f"soak {soak}m")
        tgm.notify(conn, ctx, f"soak:{step['id']}",
                   f"engine-control: {step['id']} validated; soaking {soak}m before acceptance")
    else:
        db.transition(conn, ctx, step["id"], "ACCEPTED", "validated")
        tgm.notify(conn, ctx, f"accepted:{step['id']}",
                   f"engine-control: ✅ {step['id']} ACCEPTED — {step['title']}")


def build_handoff(ctx, conn, roadmap, step) -> dict:
    d = db.step_detail(step)
    plan = common.read_json(Path(step["plan_path"])) if step["plan_path"] else {}
    impl = conn.execute(
        "SELECT * FROM runs WHERE step_id=? AND role IN ('implementer','repair') "
        "AND status='DONE' ORDER BY id DESC LIMIT 1", (step["id"],)).fetchone()
    ir = (common.read_json(Path(impl["artifact_dir"]) / "result.json") or {}) if impl else {}
    commits = [dict(repo=r["repo"], sha=r["integrated_sha"], original=r["sha"])
               for r in conn.execute(
                   "SELECT * FROM commits WHERE step_id=? AND integrated_sha IS NOT NULL",
                   (step["id"],))]
    failures = [dict(ts=e["ts"], note=(e["payload"] or "")[:300])
                for e in conn.execute(
                    "SELECT * FROM events WHERE step_id=? AND kind IN "
                    "('run_failed','cherry_conflict') ORDER BY id", (step["id"],))]
    return {
        "version": 1, "step_id": step["id"],
        "accepted_commits": commits,
        "behavior": ir.get("summary", ""),
        "decisions": ir.get("decisions", []) if isinstance(ir.get("decisions"), list) else [str(ir.get("decisions", ""))],
        "interfaces": ir.get("interfaces", []) if isinstance(ir.get("interfaces"), list) else [str(ir.get("interfaces", ""))],
        "metrics": {"tests": "green at integration tip", "attempts": db.ladder_pos(
            conn, step["id"], d.get("task_idx", 0), d.get("cycle", 0))},
        "hypotheses_supported": ir.get("hypotheses_supported", []),
        "hypotheses_falsified": ir.get("hypotheses_falsified", []),
        "failures": failures,
        "uncertainty": ir.get("uncertainty", ""),
        "verification": "worktree tests + independent review + integration tests after cherry-pick",
        "implications": ir.get("implications", ""),
        "plan_acceptance": (plan or {}).get("acceptance", []),
    }


# ---- final independent audit steps (audit: true in roadmap) ----

def start_audit(ctx, conn, roadmap, step, new_step=False):
    scfg = step_cfg(roadmap, step["id"])
    model, effort = route_model(scfg, "audit")
    if not gate(ctx, conn, "audit", model, new_step=new_step):
        return
    primary = json.loads(step["repos"])[0]
    ws = Path(repo_cfg(roadmap, primary)["workspace"])
    wt = ctx.art / "wt" / f"{step['id']}-audit"
    if wt.exists():
        gitops.worktree_remove(ws, wt)
    gitops.worktree_add_detached(ws, wt, integration_sha(roadmap, primary))
    blocks = []
    for r in json.loads(step["repos"]):
        rc = repo_cfg(roadmap, r)
        blocks.append(f"- repo '{r}': integration checkout {rc['workspace']} @ "
                      f"{integration_sha(roadmap, r)[:10]}")
    handoffs = []
    for row in conn.execute("SELECT id, handoff_path FROM steps "
                            "WHERE state='ACCEPTED' ORDER BY ordinal"):
        if row["handoff_path"] and Path(row["handoff_path"]).exists():
            handoffs.append(Path(row["handoff_path"]).read_text(encoding="utf-8")[:6000])
    materials = ("Repositories under audit (read-only worktree of the primary "
                 "repo is your cwd):\n" + "\n".join(blocks)
                 + "\n\nAccepted step handoffs:\n" + "\n---\n".join(handoffs))
    prompt = fill(prompt_template("audit.md"), {
        "OBJECTIVE": scfg.get("objective", step["title"]),
        "MATERIALS": materials, "RESULT_PATH": "<<RESULT_PATH>>"})
    dispatch(ctx, conn, step, "audit", prompt, wt, "REVIEWING",
             "final independent audit dispatched", model=model, effort=effort)
    tgm.notify(conn, ctx, f"start:{step['id']}:{db.step_detail(step).get('cycle',0)}",
               f"engine-control: {step['id']} final audit started ({model})")


def on_audit(ctx, conn, roadmap, step, run):
    d = db.step_detail(step)
    if run["status"] == "DONE":
        obj, errs = vd.check_result(Path(run["artifact_dir"]) / "result.json",
                                    "review.schema.json")
        if obj and not errs:
            verdict = obj["verdict"]
            primary = json.loads(step["repos"])[0]
            ws = Path(repo_cfg(roadmap, primary)["workspace"])
            gitops.worktree_remove(ws, ctx.art / "wt" / f"{step['id']}-audit")
            hp = ctx.art / "handoffs" / f"{step['id']}.json"
            common.write_atomic(hp, ctx.redact(json.dumps({
                "version": 1, "step_id": step["id"], "accepted_commits": [],
                "behavior": obj.get("summary", ""), "decisions": [],
                "interfaces": [], "metrics": {"verdict": verdict},
                "hypotheses_supported": [], "hypotheses_falsified": [],
                "failures": [], "uncertainty": "",
                "verification": "independent audit (fresh context, read-only)",
                "implications": json.dumps(obj.get("findings", []))[:3000]}, indent=1)))
            with conn:
                conn.execute("UPDATE steps SET handoff_path=?, active_run_id=NULL "
                             "WHERE id=?", (str(hp), step["id"]))
            if verdict == "PASS":
                db.transition(conn, ctx, step["id"], "ACCEPTED", "audit PASS")
                tgm.notify(conn, ctx, f"accepted:{step['id']}",
                           f"engine-control: ✅ {step['id']} audit PASS — {step['title']}")
            else:
                goto_blocked(ctx, conn, step, f"final audit {verdict}: "
                             + json.dumps(obj.get("findings", []))[:400])
            return
        note = f"audit artifact invalid: {errs[:3]}"
    else:
        note = f"audit run {run['status']}: {run['note']}"
    tries = d.get("reviewer_tries", 0) + 1
    d["reviewer_tries"] = tries
    db.set_detail(conn, step["id"], d)
    if tries >= REVIEWER_TRIES:
        goto_blocked(ctx, conn, step, f"audit failed {tries}x: {note[:200]}")
    else:
        start_audit(ctx, conn, roadmap, step)


# ---- the per-tick advance: DAG scheduler ----

STAGE_ORDER = {"VALIDATING": 0, "REVIEWING": 1, "TESTING": 2, "REPAIRING": 3,
               "IMPLEMENTING": 4, "PLANNING": 5, "SOAKING": 6}


def notify_plan_transition(ctx, conn, view):
    cur = f"{view.get('tier')}|{view.get('confidence')}"
    if db.kv_get(conn, "plan_notified") == cur:
        return
    db.kv_set(conn, "plan_notified", cur)
    prev = view.get("prev_tier")
    label = {"max20": "Max 20x", "max5": "Max 5x", "pro": "Pro",
             "max_unknown": "Max (exact tier pending)",
             "unknown": "unknown"}.get(view.get("tier"), view.get("tier"))
    tgm.notify(conn, ctx, f"plan:{prev}->{view.get('tier')}:{view.get('detected_at')}",
               f"engine-control: plan detection — {label} "
               f"({view.get('confidence')}, auto). Concurrency adapts automatically; "
               "no owner action needed.")


def advance_all(ctx, conn, roadmap):
    live = live_steps(conn)
    if not live:
        if db.kv_get(conn, "roadmap_started") == "1":
            tgm.notify(conn, ctx, "roadmap:complete",
                       "engine-control: 🎉 roadmap complete — all steps accepted")
        return
    errs = dag_errors(roadmap)
    if errs:
        db.event(conn, ctx, "dag_error", errs=errs[:5])
        tgm.notify(conn, ctx, "dag:" + errs[0][:60],
                   "engine-control: ⛔ roadmap DAG invalid — " + "; ".join(errs)[:300])
        return

    view = cap.detect(ctx, conn)
    if not cap.auth_ok(view):
        for s in live:
            if s["state"] in common.HALT_STATES or s["state"] == "WAITING_AUTH":
                continue
            set_resume(conn, s)
            db.transition(conn, ctx, s["id"], "WAITING_AUTH",
                          f"auth mode {view.get('auth_mode')} overrides="
                          f"{view.get('billing_overrides')}")
        tgm.notify(conn, ctx,
                   f"auth:{view.get('auth_mode')}:{'|'.join(view.get('billing_overrides') or [])}",
                   "engine-control: ⚠ Claude auth is not subscription mode "
                   f"(mode={view.get('auth_mode')}, overrides={view.get('billing_overrides')}). "
                   "No work is dispatched; automatic recheck continues.")
        return

    tg_ok = tgm.from_ctx(ctx) is not None or ctx.getenv("EC_ALLOW_NO_TELEGRAM") == "1"
    if not tg_ok:
        for s in live:
            if s["state"] not in ("WAITING_CONFIG",) and s["state"] not in common.HALT_STATES:
                set_resume(conn, s)
                db.transition(conn, ctx, s["id"], "WAITING_CONFIG",
                              "dev telegram credentials missing")
        ctx.log(tgm.SETUP_HELP)
        return

    usage = cap.current_usage(conn)
    weights = crit_weight(roadmap)
    ready = [s for s in live if step_ready(conn, s)]
    critical_ready = len([s for s in ready if not s["background"]])
    gov = cap.governor(ctx, conn, view, usage, critical_ready)
    gov["budget"] = int(ctx.getenv("EC_DISPATCH_BUDGET", "3"))
    ctx.gov = gov
    notify_plan_transition(ctx, conn, view)

    # 1) advance in-flight steps, finishing work first, critical path first.
    # Each step advances inside its own error boundary: a controller bug in
    # ONE step's path must never stall every other step (the 2026-08-10
    # stdout=None crash froze the whole roadmap for 6 ticks and the global
    # streak paused dispatch — with the blast radius actually one step wide).
    active = [s for s in live
              if s["state"] != "PENDING" and s["state"] not in common.HALT_STATES]
    active.sort(key=lambda s: (STAGE_ORDER.get(s["state"], 8),
                               -weights.get(s["id"], 0), s["ordinal"]))
    for s in active:
        try:
            advance_step(ctx, conn, roadmap, db.get_step(conn, s["id"]))
            clear_step_advance_error(conn, s["id"])
        except Exception as e:  # noqa: BLE001 — contained per step, loud below
            step_advance_error(ctx, conn, s["id"], e)

    # 2) start new READY steps up to the dynamic concurrency target
    ready.sort(key=lambda s: (1 if s["background"] else 0,
                              -weights.get(s["id"], 0), s["ordinal"]))
    for s in ready:
        if gov["budget"] <= 0:
            break
        if s["background"]:
            bg_active = conn.execute(
                "SELECT COUNT(*) c FROM steps WHERE background=1 AND state NOT IN "
                "('PENDING','ACCEPTED','ABORTED','BLOCKED')").fetchone()["c"]
            if bg_active >= gov["env"]["background"]:
                continue
        step = db.get_step(conn, s["id"])
        if step["audit"]:
            start_audit(ctx, conn, roadmap, step, new_step=True)
        else:
            start_planning(ctx, conn, roadmap, step, new_step=True)


STEP_ERR_BLOCK_AT = 8   # ticks of repeated per-step controller error -> BLOCKED


def step_advance_error(ctx, conn, step_id, exc):
    """A controller bug advancing ONE step: record it with the step id and
    traceback (the August stall was undiagnosable from `advance_error` events
    that carried neither), notify early, and after STEP_ERR_BLOCK_AT ticks
    block just this step — its dependents stop, everything else keeps moving."""
    tb = traceback.format_exc()
    step = db.get_step(conn, step_id)
    d = db.step_detail(step)
    streak = d.get("advance_err_streak", 0) + 1
    d["advance_err_streak"] = streak
    db.set_detail(conn, step_id, d)
    ctx.log(f"advance error {step_id} x{streak}: {exc!r}\n" + common.tail(tb, 15))
    db.event(conn, ctx, "advance_error", step_id=step_id,
             err=repr(exc)[:300], streak=streak, tb=common.tail(tb, 10)[:1200])
    if streak in (1, 5):
        tgm.notify(conn, ctx, f"step-err:{step_id}:{streak}",
                   f"engine-control: ⚠ controller error advancing {step_id} "
                   f"x{streak}: {exc!r:.160} — other steps unaffected; "
                   f"blocks this step at x{STEP_ERR_BLOCK_AT}. /why {step_id}")
    if streak >= STEP_ERR_BLOCK_AT and step["state"] not in common.HALT_STATES:
        goto_blocked(ctx, conn, step,
                     f"controller error advancing this step x{streak}: "
                     f"{exc!r:.200} — fix the controller, then /retry. "
                     + common.tail(tb, 3))


def clear_step_advance_error(conn, step_id):
    step = db.get_step(conn, step_id)  # refetch: advance just rewrote detail
    d = db.step_detail(step)
    if "advance_err_streak" in d:
        d.pop("advance_err_streak", None)
        db.set_detail(conn, step_id, d)


def advance_step(ctx, conn, roadmap, step):
    state = step["state"]
    scfg = step_cfg(roadmap, step["id"])
    d = db.step_detail(step)

    if state in ("WAITING_CONFIG", "WAITING_AUTH"):
        db.transition(conn, ctx, step["id"], d.get("resume", "PENDING"),
                      "config/auth present")
        return
    if state == "WAITING_QUOTA":
        hold = db.kv_get(conn, "quota_hold_until")
        if hold and not common.is_past(hold):
            return
        if step["retry_at"] and common.is_past(step["retry_at"]):
            run = db.get_run(conn, step["active_run_id"]) if step["active_run_id"] else None
            redispatch_stage(ctx, conn, roadmap, step, run)  # attempts unchanged
        return
    if state == "INTERRUPTED":
        db.transition(conn, ctx, step["id"], d.get("resume", "PENDING"),
                      "reconciled after interruption")
        tgm.notify(conn, ctx, f"recovered:{step['id']}:{common.now()[:16]}",
                   f"engine-control: {step['id']} recovered after interruption")
        return
    if state == "WAITING_USER":
        return
    if state == "SOAKING":
        if step["soak_until"] and common.is_past(step["soak_until"]):
            check = scfg.get("soak_check")
            if check:
                rc_, out = vd.run_cmd(check, repo_cfg(roadmap, current_repo(ctx, step))["workspace"], 600)
                if rc_ != 0:
                    repair_or_block(ctx, conn, step, f"soak check failed: {common.tail(out, 30)}")
                    return
            db.transition(conn, ctx, step["id"], "ACCEPTED", "soak complete")
            tgm.notify(conn, ctx, f"accepted:{step['id']}",
                       f"engine-control: ✅ {step['id']} ACCEPTED — {step['title']}")
        return

    run = db.get_run(conn, step["active_run_id"]) if step["active_run_id"] else None
    if run is None or run["status"] in ("PREPARED", "DISPATCHED"):
        if run is None:
            redispatch_stage(ctx, conn, roadmap, step, None)
        return
    if run["status"] == "QUOTA":
        wait_quota(ctx, conn, step)
        return
    if run["status"] == "LOST":
        redispatch_stage(ctx, conn, roadmap, step, run)
        return

    if run["status"] in ("DONE", "FAILED") and d.get("quota_streak"):
        d.pop("quota_streak", None)
        db.set_detail(conn, step["id"], d)

    if state == "REVIEWING" and run["role"] == "audit":
        on_audit(ctx, conn, roadmap, step, run)
        return
    handler = {
        "PLANNING": on_planning, "IMPLEMENTING": on_impl_like,
        "REPAIRING": on_impl_like, "TESTING": on_testing,
        "REVIEWING": on_review, "VALIDATING": on_validating,
    }.get(state)
    if handler:
        handler(ctx, conn, roadmap, step, run)


def redispatch_stage(ctx, conn, roadmap, step, run):
    role = run["role"] if run else {"PLANNING": "planner", "IMPLEMENTING": "implementer",
                                    "TESTING": "test", "REVIEWING": "reviewer",
                                    "VALIDATING": "test", "REPAIRING": "repair"}.get(step["state"])
    d = db.step_detail(step)
    if role == "audit":
        start_audit(ctx, conn, roadmap, step)
    elif role == "planner":
        start_planning(ctx, conn, roadmap, step)
    elif role == "implementer":
        start_task_impl(ctx, conn, roadmap, step)
    elif role == "repair":
        dispatch_repair(ctx, conn, step, d.get("last_findings", "(findings lost)"),
                        db.ladder_pos(conn, step["id"], d.get("task_idx", 0), d.get("cycle", 0)))
    elif role == "diagnostic":
        d["diagnosed"] = False
        db.set_detail(conn, step["id"], d)
        repair_or_block(ctx, conn, step, d.get("last_findings", "(findings lost)"))
    elif role == "test":
        if step["state"] == "VALIDATING":
            enter_validation(ctx, conn, roadmap, step)
        else:
            start_testing(ctx, conn, roadmap, step)
    elif role == "reviewer":
        start_review(ctx, conn, roadmap, step)


def on_planning(ctx, conn, roadmap, step, run):
    d = db.step_detail(step)
    if run["status"] == "DONE":
        obj, errs = vd.check_result(Path(run["artifact_dir"]) / "result.json", "plan.schema.json")
        if not errs:
            pp = ctx.art / f"plan-{step['id']}.json"
            common.write_atomic(pp, json.dumps(obj, indent=1))
            d.update(task_idx=0, tasks_n=len(obj["tasks"]), planner_tries=0)
            db.set_detail(conn, step["id"], d)
            with conn:
                conn.execute("UPDATE steps SET plan_path=? WHERE id=?", (str(pp), step["id"]))
            roadmap_repos = set(json.loads(step["repos"]))
            bad = [t["repo"] for t in obj["tasks"] if t["repo"] not in roadmap_repos]
            if bad:
                goto_blocked(ctx, conn, step, f"plan targets repos outside step scope: {bad}")
                return
            ws = Path(repo_cfg(roadmap, json.loads(step["repos"])[0])["workspace"])
            gitops.worktree_remove(ws, ctx.art / "wt" / f"{step['id']}-planner")
            tgm.notify(conn, ctx, f"plan:{step['id']}:c{d.get('cycle',0)}",
                       f"engine-control: {step['id']} plan accepted "
                       f"({len(obj['tasks'])} task(s)); implementing")
            step = db.get_step(conn, step["id"])
            start_task_impl(ctx, conn, roadmap, step)
            return
        fail_note = f"plan schema errors: {errs[:5]}"
    else:
        fail_note = f"planner run {run['status']}: {run['note']}"
    tries = d.get("planner_tries", 0) + 1
    d["planner_tries"] = tries
    db.set_detail(conn, step["id"], d)
    if tries >= PLANNER_TRIES:
        goto_blocked(ctx, conn, step, f"planner failed {tries}x: {fail_note[:200]}")
    else:
        start_planning(ctx, conn, roadmap, step)


def on_impl_like(ctx, conn, roadmap, step, run):
    if run["role"] == "diagnostic":
        findings = db.step_detail(step).get("last_findings", "")
        if run["status"] == "DONE":
            obj, errs = vd.check_result(Path(run["artifact_dir"]) / "result.json",
                                        "review.schema.json")
            if obj and not errs:
                findings = "DIAGNOSTIC:\n" + json.dumps(obj.get("findings", []), indent=1)[:4000]
        d = db.step_detail(step)
        d["last_findings"] = findings
        db.set_detail(conn, step["id"], d)
        dispatch_repair(ctx, conn, step, findings,
                        db.ladder_pos(conn, step["id"], d.get("task_idx", 0), d.get("cycle", 0)))
        return
    if run["status"] == "FAILED":
        repair_or_block(ctx, conn, step,
                        f"worker process failed rc={run['exit_code']}: {run['note']}\n"
                        + cr.stderr_tail(ctx, run["key"]))
        return
    obj, errs = vd.check_result(Path(run["artifact_dir"]) / "result.json",
                                "impl_result.schema.json")
    if errs:
        repair_or_block(ctx, conn, step, f"missing/malformed result artifact: {errs[:5]}")
        return
    if obj.get("status") != "done":
        repair_or_block(ctx, conn, step,
                        f"worker reported failure: {obj.get('summary','')[:400]}")
        return
    d = db.step_detail(step)
    head = gitops.current_sha(Path(d["task_wt"]))
    if head == d["task_base"]:
        repair_or_block(ctx, conn, step, "result claims done but no commits exist in worktree")
        return
    tgm.notify(conn, ctx, f"impl:{run['key']}",
               f"engine-control: {step['id']} implementation finished — testing")
    start_testing(ctx, conn, roadmap, step)


def on_testing(ctx, conn, roadmap, step, run):
    if run["status"] == "FAILED":
        repair_or_block(ctx, conn, step, f"test harness failed: {run['note']}")
        return
    obj = common.read_json(Path(run["artifact_dir"]) / "result.json")
    if not obj:
        repair_or_block(ctx, conn, step, "test run produced no result artifact")
        return
    report = common.tail(obj.get("report"), 60)
    if obj.get("passed"):
        tgm.notify(conn, ctx, f"tests:{run['key']}",
                   f"engine-control: {step['id']} tests green — review")
        start_review(ctx, conn, roadmap, step)
    elif obj.get("no_tests"):
        # Exit code 5 = the runner COLLECTED NOTHING. That is a roadmap.yaml
        # test-command vs repo-layout mismatch, not broken code; repair
        # workers cannot reach engine config, so spending rungs is pure waste
        # (step-02 burned its whole budget on exactly this).
        db.set_run_note(conn, ctx, run["id"], "no tests collected (exit 5)")
        cmds = repo_cfg(roadmap, current_repo(ctx, step))["tests"]
        goto_blocked(ctx, conn, step,
                     "test harness collected NO TESTS (exit code 5) — the "
                     f"roadmap.yaml test command(s) {json.dumps(cmds)} match "
                     "nothing in the worktree. Fix the command or the layout; "
                     "repair workers cannot fix engine config.\n" + report)
    else:
        db.set_run_note(conn, ctx, run["id"],
                        "tests failed: " + _fail_lines(report, 3))
        tgm.notify(conn, ctx, f"tests:{run['key']}",
                   f"engine-control: {step['id']} tests FAILED — repair path")
        repair_or_block(ctx, conn, step, "tests failed:\n" + report)


def _fail_lines(report: str, n: int) -> str:
    """First n lines that look like actual failures — the part of a test
    report a human wants first."""
    hits = [l.strip() for l in (report or "").splitlines()
            if re.search(r"FAIL|ERROR|Error|error:|failed|rc=[1-9]", l)]
    return " | ".join(hits[:n]) or (report or "").strip()[:200]


def on_review(ctx, conn, roadmap, step, run):
    d = db.step_detail(step)
    if run["status"] == "DONE":
        obj, errs = vd.check_result(Path(run["artifact_dir"]) / "result.json",
                                    "review.schema.json")
        if obj and not errs:
            d["reviewer_tries"] = 0
            db.set_detail(conn, step["id"], d)
            verdict = obj["verdict"]
            tgm.notify(conn, ctx, f"review:{run['key']}",
                       f"engine-control: {step['id']} review: {verdict}")
            repo = current_repo(ctx, step)
            ws = Path(repo_cfg(roadmap, repo)["workspace"])
            gitops.worktree_remove(
                ws, ctx.art / "wt" / f"rv-{step['id']}-c{d.get('cycle',0)}-t{d.get('task_idx',0)}")
            if verdict == "PASS":
                enter_validation(ctx, conn, roadmap, step)
            elif verdict == "REPAIR":
                repair_or_block(ctx, conn, step,
                                "REVIEW FINDINGS:\n" + json.dumps(obj["findings"], indent=1)[:4000])
            else:
                goto_blocked(ctx, conn, step,
                             "reviewer BLOCK: " + json.dumps(obj["findings"])[:300])
            return
        note = f"review artifact invalid: {errs[:3]}"
    else:
        note = f"reviewer run {run['status']}: {run['note']}"
    tries = d.get("reviewer_tries", 0) + 1
    d["reviewer_tries"] = tries
    db.set_detail(conn, step["id"], d)
    if tries >= REVIEWER_TRIES:
        goto_blocked(ctx, conn, step, f"reviewer failed {tries}x: {note[:200]}")
    else:
        start_review(ctx, conn, roadmap, step)


def on_validating(ctx, conn, roadmap, step, run):
    if run["status"] == "FAILED":
        repair_or_block(ctx, conn, step, f"validation harness failed: {run['note']}")
        return
    obj = common.read_json(Path(run["artifact_dir"]) / "result.json")
    if not obj or not obj.get("passed"):
        repair_or_block(ctx, conn, step, "integration tests failed:\n"
                        + common.tail((obj or {}).get("report", "no artifact"), 60))
        return
    d = db.step_detail(step)
    idx = run["task_idx"]  # the task this validation run actually validated
    done = d.get("done_tasks", [])
    if idx not in done:
        done = sorted(done + [idx])
        d["done_tasks"] = done
        db.set_detail(conn, step["id"], d)
        tgm.notify(conn, ctx, f"task:{step['id']}:c{d.get('cycle',0)}t{idx}:done",
                   f"engine-control: {step['id']} task {idx} integrated and validated")
    if len(done) < d.get("tasks_n", 1):
        step = db.get_step(conn, step["id"])
        start_task_impl(ctx, conn, roadmap, step, task_idx=len(done))
        return
    accept_step(ctx, conn, roadmap, step)


# ---------- telegram commands ----------

TIER_LABEL = {"max20": "Max 20x", "max5": "Max 5x", "pro": "Pro",
              "max_unknown": "Max (exact tier pending)", "unknown": "unknown"}


def status_text(ctx, conn) -> str:
    lines = []
    started = db.kv_get(conn, "roadmap_started") == "1"
    paused = db.kv_get(conn, "paused") == "1"
    run_id = db.kv_get(conn, "roadmap_run_id") or "-"
    lines.append(f"engine-control — roadmap {'started' if started else 'NOT started'}"
                 f"{' [PAUSED]' if paused else ''} (run {run_id})")
    view = json.loads(db.kv_get(conn, "capacity_state") or "{}")
    usage = cap.current_usage(conn)
    ready = [s["id"] for s in live_steps(conn) if step_ready(conn, s)]
    gov = cap.governor(ctx, conn, view, usage,
                       len([r for r in ready if not db.get_step(conn, r)["background"]]))
    mode = "auto" if not gov["override"] else f"override:{gov['override']}"
    lines.append(f"Plan: {TIER_LABEL.get(gov['tier'], gov['tier'])} "
                 f"({mode}, {view.get('confidence', '?')}) · auth {view.get('auth_mode', '?')}")
    if usage:
        stale = " STALE" if usage.get("stale") else ""
        lines.append(f"Usage: 5h {usage.get('pct5', '?')}% · 7d {usage.get('pct7', '?')}%"
                     f" (age {int(usage.get('age_min', 0))}m{stale}, {usage.get('source')})")
    else:
        lines.append("Usage: no telemetry yet")
    lines.append(f"Claude workers: {gov['active']} active · target {gov['target']}"
                 f" · pressure {gov['pressure']} · pace {gov['pace']}")
    if ready:
        lines.append(f"READY: {', '.join(ready)}")
    for s in conn.execute("SELECT * FROM steps ORDER BY ordinal"):
        d = db.step_detail(s)
        pos = db.ladder_pos(conn, s["id"], d.get("task_idx", 0), d.get("cycle", 0))
        mark = {"ACCEPTED": "✅", "BLOCKED": "⛔", "ABORTED": "🛑"}.get(s["state"], "·")
        lines.append(f"{mark} {s['id']} [{s['state']}] attempts={pos} — {s['title']}")
        if s["state"] == "BLOCKED":
            why = conn.execute(
                "SELECT reason FROM transitions WHERE step_id=? AND to_state='BLOCKED' "
                "ORDER BY id DESC LIMIT 1", (s["id"],)).fetchone()
            if why and why["reason"]:
                first = why["reason"].strip().splitlines()[0]
                lines.append(f"   ⛔ {first[:150]}  (/why {s['id']})")
        if s["active_run_id"]:
            r = db.get_run(conn, s["active_run_id"])
            if r and r["status"] in ("PREPARED", "DISPATCHED"):
                lines.append(f"   run {r['key']} ({r['role']}/{r['model']}) "
                             f"since {common.local_str(r['created_at'], '%m-%d %H:%M')}")
    if not tgm.from_ctx(ctx):
        lines.append("⚠ telegram: WAITING_CONFIG (see README)")
    return "\n".join(lines)


def _run_outcome(conn, run) -> str:
    """One line saying what a run actually did — derived from its result
    artifact so it works for runs recorded before notes existed."""
    if run["status"] in ("FAILED", "QUOTA", "LOST"):
        return f"{run['status']}: {(run['note'] or '').strip()[:160]}"
    obj = common.read_json(Path(run["artifact_dir"]) / "result.json") or {}
    role = run["role"]
    if role == "test":
        if obj.get("passed"):
            return "tests green"
        if obj.get("no_tests"):
            return "NO TESTS COLLECTED (exit 5) — command/layout mismatch"
        if obj:
            return "tests failed: " + _fail_lines(obj.get("report") or "", 2)[:200]
        return run["note"] or "no result artifact"
    if role in ("implementer", "repair"):
        if obj.get("status") == "done":
            return "done: " + (obj.get("summary") or "")[:160]
        return f"reported {obj.get('status', '?')}: {(obj.get('summary') or '')[:160]}"
    if role in ("reviewer", "diagnostic", "audit"):
        v = obj.get("verdict") or obj.get("status") or "?"
        n = len(obj.get("findings") or [])
        return f"{v}, {n} finding(s)" if obj else (run["note"] or "no artifact")
    if role == "planner":
        return f"plan with {len(obj.get('tasks', []))} task(s)" if obj else "no plan artifact"
    return (run["note"] or obj.get("summary") or "").strip()[:160] or run["status"]


def why_text(ctx, conn, step_id=None) -> str:
    """The failure story of a step: full last-block reason, then every run of
    the relevant cycle with its outcome. Everything /status truncates lives
    here untruncated (within Telegram's message budget)."""
    step = db.get_step(conn, step_id) if step_id else None
    if step_id and not step:
        return f"engine-control: unknown step '{step_id}'"
    if not step:  # default: most recently halted, else most recently updated
        row = conn.execute(
            "SELECT * FROM steps WHERE state IN ('BLOCKED','ABORTED') "
            "ORDER BY updated_at DESC LIMIT 1").fetchone() or conn.execute(
            "SELECT * FROM steps WHERE state NOT IN ('PENDING','ACCEPTED') "
            "ORDER BY updated_at DESC LIMIT 1").fetchone()
        if not row:
            return "engine-control: nothing in flight and nothing halted"
        step = row
    d = db.step_detail(step)
    cycle = d.get("cycle", 0)
    lines = [f"why {step['id']} — [{step['state']}] cycle {cycle} — {step['title']}"]
    for t in conn.execute(
            "SELECT * FROM transitions WHERE step_id=? ORDER BY id DESC LIMIT 3",
            (step["id"],)).fetchall()[::-1]:
        reason = (t["reason"] or "").strip().replace("\n", " ¶ ")
        lines.append(f"{common.local_str(t['ts'], '%H:%M')} {t['from_state']}"
                     f"→{t['to_state']}: {reason[:400]}")
    runs = conn.execute(
        "SELECT * FROM runs WHERE step_id=? AND cycle=? AND role!='probe' "
        "ORDER BY id", (step["id"], cycle)).fetchall()
    if not runs and cycle > 0:  # fresh /retry cycle with nothing yet: show previous
        cycle -= 1
        lines.append(f"(cycle {cycle + 1} has no runs yet; showing cycle {cycle})")
        runs = conn.execute(
            "SELECT * FROM runs WHERE step_id=? AND cycle=? AND role!='probe' "
            "ORDER BY id", (step["id"], cycle)).fetchall()
    for r in runs:
        who = r["role"] + (f"/{r['model']}" if r["model"] else "")
        lines.append(f"· {common.local_str(r['created_at'], '%H:%M')} "
                     f"{who} [{r['status']}] {_run_outcome(conn, r)}")
    if runs:
        lines.append(f"artifacts: {runs[-1]['artifact_dir']}")
    return "\n".join(lines)[:3900]  # telegram hard limit 4096


def rearm_step(ctx, conn, step) -> str:
    d = {"cycle": db.step_detail(step).get("cycle", 0) + 1}
    db.set_detail(conn, step["id"], d)
    with conn:
        conn.execute("UPDATE steps SET active_run_id=NULL, plan_path=NULL WHERE id=?",
                     (step["id"],))
    db.transition(conn, ctx, step["id"], "PENDING", "/retry — fresh cycle")
    return f"{step['id']} re-armed (cycle {d['cycle']})"


def handle_commands(ctx, conn, tg, cmds):
    for c in cmds:
        parts = c.split()
        name = parts[0].split("@")[0]
        arg = parts[1] if len(parts) > 1 else None
        db.event(conn, ctx, "tg_command", cmd=name, arg=arg)
        if name == "/status":
            tg.send(status_text(ctx, conn))
        elif name == "/pause":
            db.kv_set(conn, "paused", "1")
            tg.send("engine-control: paused (running workers finish; no new dispatch)")
        elif name == "/resume":
            db.kv_set(conn, "paused", "0")
            for step in live_steps(conn):
                if step["state"] == "WAITING_USER":
                    db.transition(conn, ctx, step["id"],
                                  db.step_detail(step).get("resume", "PENDING"), "/resume")
            tg.send("engine-control: resumed")
        elif name == "/retry":
            halted = [s for s in steps_all(conn) if s["state"] in common.HALT_STATES
                      and (arg is None or s["id"] == arg)]
            if halted:
                tg.send("engine-control: " + "; ".join(
                    rearm_step(ctx, conn, s) for s in halted))
            else:
                tg.send("engine-control: no BLOCKED/ABORTED step"
                        + (f" named {arg}" if arg else "") + " to retry")
        elif name == "/abort":
            targets = [s for s in live_steps(conn)
                       if s["state"] not in common.HALT_STATES
                       and (arg is None or s["id"] == arg)]
            if arg is None and len(targets) > 1:
                tg.send("engine-control: several steps active — use /abort <step-id>: "
                        + ", ".join(s["id"] for s in targets))
            elif targets:
                for s in targets:
                    db.transition(conn, ctx, s["id"], "ABORTED", "/abort")
                tg.send("engine-control: aborted " + ", ".join(s["id"] for s in targets)
                        + " (dependents will not start)")
            else:
                tg.send("engine-control: nothing to abort" + (f" ({arg})" if arg else ""))
        elif name == "/profile":
            if arg in (None, "auto"):
                db.kv_set(conn, "plan_override", "")
                tg.send("engine-control: plan detection AUTO (override cleared)")
            elif arg in cap.ENVELOPES:
                db.kv_set(conn, "plan_override", arg)
                tg.send(f"engine-control: plan OVERRIDE {arg} (debug only — "
                        "/profile auto to return to automatic detection)")
            else:
                tg.send(f"engine-control: unknown profile '{arg}' "
                        f"(auto|{'|'.join(sorted(cap.ENVELOPES))})")
        elif name == "/pace":
            if arg in (None, "auto"):
                db.kv_set(conn, "pace", "auto")
                tg.send("engine-control: pace AUTO")
            elif arg in ("economy", "balanced", "sprint"):
                db.kv_set(conn, "pace", arg)
                tg.send(f"engine-control: pace {arg}")
            else:
                tg.send("engine-control: /pace auto|economy|balanced|sprint")
        elif name == "/why":
            tg.send(why_text(ctx, conn, arg))
        elif name == "/log":
            rows = conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT 15").fetchall()
            out = "\n".join(f"{common.local_str(r['ts'], '%H:%M:%S')} {r['kind']} "
                            f"{r['step_id'] or ''} {(r['payload'] or '')[:160]}"
                            for r in rows[::-1])
            tg.send(out or "no events")


# ---------- tick ----------

def should_consume_tg(ctx) -> bool:
    """While the low-latency listener is alive (fresh heartbeat) the tick
    leaves getUpdates to it — two concurrent pollers 409 each other. A tick
    woken BY the listener (EC_CONSUME_TG=1) always consumes; a stale
    heartbeat (listener dead/hung) restores 1-minute fallback consumption."""
    if os.environ.get("EC_CONSUME_TG") == "1":
        return True
    hb = ctx.root / "listener.alive"
    try:
        age = (datetime_now_ts() - hb.stat().st_mtime)
    except OSError:
        return True
    return age > 90


def datetime_now_ts() -> float:
    import time as _t
    return _t.time()


def tick(ctx) -> int:
    lock = common.acquire_lock(ctx.root)
    if lock is None:
        return 0  # another tick holds the lock; safe no-op
    try:
        conn = db.connect(ctx)
        try:
            roadmap = load_roadmap(ctx)
        except Exception as e:
            ctx.log(f"roadmap load failed: {e}")
            db.event(conn, ctx, "roadmap_error", err=str(e)[:300])
            return 1
        sync_steps(conn, ctx, roadmap)
        if os.environ.get("EC_TEST_HOLD_LOCK"):
            import time as _t
            _t.sleep(float(os.environ["EC_TEST_HOLD_LOCK"]))
        tg = tgm.from_ctx(ctx)
        if tg is None:
            if db.kv_get(conn, "warned_no_tg") != "1":
                db.kv_set(conn, "warned_no_tg", "1")
                db.event(conn, ctx, "waiting_config", what="dev telegram")
                ctx.log(tgm.SETUP_HELP)
        else:
            db.kv_set(conn, "warned_no_tg", "0")
            try:
                if should_consume_tg(ctx):
                    cmds = tgm.consume(ctx, conn, tg)
                    handle_commands(ctx, conn, tg, cmds)
            except Exception as e:
                # a command-handler bug must never cost the tick its
                # reconcile/advance work (commissioning review finding)
                ctx.log(f"telegram command error: {e!r}")
                db.event(conn, ctx, "tg_handler_error", err=repr(e)[:300])
        try:
            reconcile(ctx, conn)
            cap.ingest_telemetry(ctx, conn)
            backup_state(ctx, conn)
            if db.kv_get(conn, "roadmap_started") == "1" and db.kv_get(conn, "paused") != "1":
                advance_all(ctx, conn, roadmap)
            if db.kv_get(conn, "orch_err_streak", "0") != "0":
                db.kv_set(conn, "orch_err_streak", "0")
        except Exception as e:
            # A controller bug must be loud, bounded, and never a silent loop.
            streak = int(db.kv_get(conn, "orch_err_streak", "0")) + 1
            db.kv_set(conn, "orch_err_streak", str(streak))
            if streak == 1:
                episode = int(db.kv_get(conn, "orch_err_episode", "0")) + 1
                db.kv_set(conn, "orch_err_episode", str(episode))
            episode = db.kv_get(conn, "orch_err_episode", "0")
            import traceback
            ctx.log(f"orchestration error x{streak}: {e!r}\n"
                    + common.tail(traceback.format_exc(), 15))
            db.event(conn, ctx, "advance_error", err=repr(e)[:400], streak=streak)
            if streak in (1, 5):
                tgm.notify(conn, ctx, f"orch-error:{episode}:{streak}",
                           f"engine-control: orchestration error x{streak}: {e!r:.200}")
            if streak >= 8:
                # never block a healthy step over a controller bug: with the
                # DAG scheduler the safe containment is pausing dispatch
                if db.kv_get(conn, "paused") != "1":
                    db.kv_set(conn, "paused", "1")
                    tgm.notify(conn, ctx, f"orch-pause:{episode}",
                               "engine-control: persistent controller error — dispatch "
                               "PAUSED (running workers finish). /resume after repair.")
        tgm.flush(ctx, conn, tg)
        conn.close()
        return 0
    finally:
        common.release_lock(lock)


def backup_state(ctx, conn):
    """Daily durable checkpoint of state.db (WAL-safe via the backup API)."""
    today = common.now()[:10]
    if db.kv_get(conn, "last_backup") == today:
        return
    db.kv_set(conn, "last_backup", today)
    import sqlite3
    bdir = ctx.root / "backups"
    bdir.mkdir(exist_ok=True)
    dest = sqlite3.connect(bdir / f"state-{today}.db")
    with dest:
        conn.backup(dest)
    dest.close()
    keep = sorted(bdir.glob("state-*.db"))
    for old in keep[:-7]:
        try:
            old.unlink()
        except OSError:
            pass


# ---------- CLI ----------

TICK_TASK = "engine-control-tick"
LISTENER_TASK = "engine-control-listener"


def ensure_listener_task(ctx) -> None:
    """Supervisor for the low-latency Telegram listener: fires every 5
    minutes; the listener's byte lock makes redundant fires exit instantly,
    so this is restart-on-crash, not duplication."""
    import subprocess
    cmd = f'"{sys.executable}" "{common.CODE_DIR / "tg_listener.py"}"'
    q = subprocess.run(["schtasks", "/query", "/tn", LISTENER_TASK, "/fo", "csv", "/nh"],
                       capture_output=True, text=True)
    if q.returncode != 0:
        subprocess.run(["schtasks", "/create", "/tn", LISTENER_TASK, "/sc", "minute",
                        "/mo", "5", "/f", "/tr", cmd], capture_output=True, text=True)
    elif "Disabled" in q.stdout:
        subprocess.run(["schtasks", "/change", "/tn", LISTENER_TASK, "/enable"],
                       capture_output=True, text=True)
    subprocess.run(["schtasks", "/run", "/tn", LISTENER_TASK],
                   capture_output=True, text=True)


def cmd_start(ctx) -> int:
    """Launch (or re-arm) the autonomous roadmap. Verifies the §48 launch
    preconditions, persists a stable run id BEFORE any dispatch, and leaves
    everything else to the scheduled tick."""
    import subprocess
    import uuid as _uuid
    conn = cmd_init(ctx)
    roadmap = load_roadmap(ctx)
    errs = dag_errors(roadmap)
    if errs:
        print("REFUSED: roadmap DAG invalid: " + "; ".join(errs))
        return 1
    view = cap.detect(ctx, conn, force=True, trigger="start")
    if not cap.auth_ok(view):
        print(f"REFUSED: Claude auth is not subscription mode "
              f"(mode={view.get('auth_mode')}, overrides={view.get('billing_overrides')})")
        return 1
    q = subprocess.run(["schtasks", "/query", "/tn", TICK_TASK, "/fo", "csv", "/nh"],
                       capture_output=True, text=True)
    if q.returncode != 0:
        print("tick task missing — installing")
        cmd_install_task(ctx)
    elif "Disabled" in q.stdout:
        subprocess.run(["schtasks", "/change", "/tn", TICK_TASK, "/enable"],
                       capture_output=True, text=True)
        print("tick task re-enabled")
    ensure_listener_task(ctx)
    if db.kv_get(conn, "roadmap_started") == "1":
        print(f"roadmap already started (run {db.kv_get(conn, 'roadmap_run_id')}); "
              "no duplicate run created")
        return 0
    cap.ingest_telemetry(ctx, conn)
    backup_state(ctx, conn)
    run_id = f"run-{common.now()[:10]}-{_uuid.uuid4().hex[:8]}"
    db.kv_set(conn, "roadmap_run_id", run_id)
    db.kv_set(conn, "paused", "0")
    db.kv_set(conn, "pace", db.kv_get(conn, "pace", "auto"))
    db.kv_set(conn, "roadmap_started", "1")
    db.event(conn, ctx, "roadmap_started", run_id=run_id, tier=view.get("tier"),
             confidence=view.get("confidence"))
    usage = cap.current_usage(conn)
    ready = [s for s in live_steps(conn) if step_ready(conn, s)]
    gov = cap.governor(ctx, conn, view, usage,
                       len([s for s in ready if not s["background"]]))
    label = TIER_LABEL.get(gov["tier"], gov["tier"])
    ulines = (f"Usage: 5h {usage.get('pct5', '?')}% · 7d {usage.get('pct7', '?')}%"
              if usage else "Usage: telemetry pending")
    branches = "\n".join(f"• {s['id']} — {s['title']}" for s in ready[:4])
    tgm.notify(conn, ctx, f"roadmap:started:{run_id}",
               "🚀 AUTONOMOUS ROADMAP STARTED\n"
               f"Run: {run_id}\n"
               f"Plan: {label} ({view.get('confidence')}, auto-detected)\n"
               f"{ulines}\n"
               f"Scheduler: AUTO · Claude target now: {gov['target']}\n"
               f"Roadmap: {len(roadmap['steps'])} steps (dependency DAG)\n"
               f"First parallel branches:\n{branches}\n"
               "Background recovery: enabled\nOwner action: none")
    print(f"roadmap started (run {run_id}); the scheduled tick drives it from here")
    print(f"plan: {label} ({view.get('confidence')}) · target {gov['target']} "
          f"· ready: {[s['id'] for s in ready]}")
    return 0


def cmd_install_task(ctx):
    import subprocess
    cmd = f'"{sys.executable}" "{common.CODE_DIR / "control.py"}" tick'
    p = subprocess.run(["schtasks", "/create", "/tn", TICK_TASK, "/sc", "minute",
                        "/mo", "1", "/f", "/tr", cmd], capture_output=True, text=True)
    print(p.stdout.strip() or p.stderr.strip())
    ensure_listener_task(ctx)
    print(f"listener task ensured ({LISTENER_TASK})")
    q = subprocess.run(["schtasks", "/query", "/tn", TICK_TASK], capture_output=True, text=True)
    print(q.stdout.strip()[:400])
    return p.returncode


def cmd_uninstall_task(ctx):
    import subprocess
    p = subprocess.run(["schtasks", "/delete", "/tn", TICK_TASK, "/f"],
                       capture_output=True, text=True)
    print(p.stdout.strip() or p.stderr.strip())
    subprocess.run(["schtasks", "/end", "/tn", LISTENER_TASK],
                   capture_output=True, text=True)
    q = subprocess.run(["schtasks", "/delete", "/tn", LISTENER_TASK, "/f"],
                       capture_output=True, text=True)
    print(q.stdout.strip() or q.stderr.strip())
    return p.returncode


def cmd_doctor(ctx):
    import shutil as sh
    import subprocess
    bad = 0
    claude = cr.resolve_claude(ctx)
    if claude:
        argv = (["cmd", "/d", "/c"] if claude.lower().endswith((".cmd", ".bat")) else []) \
            + [claude, "--version"]
        v = subprocess.run(argv, capture_output=True, text=True).stdout.strip()
        print(f"[ok] claude: {claude} ({v})")
    else:
        print("[FAIL] claude CLI not found"); bad = 1
    print(f"[ok] python: {sys.version.split()[0]} at {sys.executable}")
    try:
        import yaml  # noqa
        print("[ok] pyyaml importable")
    except ImportError:
        print("[FAIL] pyyaml missing (pip install pyyaml)"); bad = 1
    print(f"[ok] git: {sh.which('git')}")
    if os.environ.get("ANTHROPIC_API_KEY"):
        print("[WARN] ANTHROPIC_API_KEY is set in the environment — API billing risk. "
              "Workers strip it, but remove it from the machine env.")
    else:
        print("[ok] no ANTHROPIC_API_KEY in environment (subscription auth)")
    try:
        conn0 = db.connect(ctx)
        view = cap.detect(ctx, conn0, force=True, trigger="doctor")
        cap.ingest_telemetry(ctx, conn0)
        usage = cap.current_usage(conn0)
        mark = "[ok]" if cap.auth_ok(view) else "[FAIL]"
        if not cap.auth_ok(view):
            bad = 1
        print(f"{mark} plan detection: tier={view.get('tier')} "
              f"confidence={view.get('confidence')} auth={view.get('auth_mode')}"
              + (f" overrides={view.get('billing_overrides')}"
                 if view.get("billing_overrides") else ""))
        if usage:
            print(f"[ok] usage telemetry: 5h {usage.get('pct5')}% · "
                  f"7d {usage.get('pct7')}% (age {int(usage.get('age_min', 0))}m)")
        else:
            print("[..] usage telemetry: none yet (populates after first session)")
        conn0.close()
    except Exception as e:
        print(f"[FAIL] plan detection: {e!r:.150}")
        bad = 1
    print("[ok] telegram configured" if tgm.from_ctx(ctx)
          else "[WAITING_CONFIG] dev telegram not configured:\n" + tgm.SETUP_HELP)
    q = subprocess.run(["schtasks", "/query", "/tn", TICK_TASK], capture_output=True, text=True)
    print("[ok] tick task installed" if q.returncode == 0
          else "[..] tick task not installed (python control.py install-task)")
    hb = ctx.root / "listener.alive"
    try:
        import time as _t
        fresh = (_t.time() - hb.stat().st_mtime) < 90
    except OSError:
        fresh = False
    print("[ok] telegram listener alive (commands answer in seconds)" if fresh
          else "[..] telegram listener not running (commands at 1-minute tick "
               "cadence; python control.py install-task starts it)")
    try:
        roadmap = load_roadmap(ctx)
        print(f"[ok] roadmap: {len(roadmap['steps'])} steps, repos: {list(roadmap['repos'])}")
        for name, rc in roadmap["repos"].items():
            ws = Path(rc["workspace"])
            state = "ready" if (ws / ".git").exists() else "MISSING (python control.py init)"
            print(f"     {name}: {ws} [{state}]")
    except Exception as e:
        print(f"[FAIL] roadmap: {e}"); bad = 1
    return bad


def main(argv=None):
    # Console codepage on this machine is cp1255; make CLI output safe for the
    # status marks regardless of terminal encoding.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    ap = argparse.ArgumentParser(prog="control.py")
    ap.add_argument("cmd", choices=["tick", "start", "status", "why", "pause",
                                    "resume", "retry", "abort", "log", "init",
                                    "doctor", "install-task", "uninstall-task",
                                    "telegram-detect-chat"])
    ap.add_argument("target", nargs="?", default=None,
                    help="step id for `why` (default: last halted step)")
    ap.add_argument("--root", default=None)
    args = ap.parse_args(argv)
    ctx = make_ctx(args.root)
    if args.cmd == "tick":
        return tick(ctx)
    if args.cmd == "init":
        cmd_init(ctx)
        print("workspace initialized")
        return 0
    if args.cmd == "start":
        return cmd_start(ctx)
    if args.cmd == "doctor":
        return cmd_doctor(ctx)
    if args.cmd == "install-task":
        return cmd_install_task(ctx)
    if args.cmd == "uninstall-task":
        return cmd_uninstall_task(ctx)
    conn = db.connect(ctx)
    if args.cmd == "status":
        print(status_text(ctx, conn))
    elif args.cmd == "why":
        print(why_text(ctx, conn, args.target))
    elif args.cmd == "pause":
        db.kv_set(conn, "paused", "1"); print("paused")
    elif args.cmd == "resume":
        db.kv_set(conn, "paused", "0"); print("resumed")
    elif args.cmd == "retry":
        class _T:  # reuse telegram handler without a bot
            def send(self, t): print(t)
        handle_commands(ctx, conn, _T(), ["/retry"])
    elif args.cmd == "abort":
        class _T:
            def send(self, t): print(t)
        handle_commands(ctx, conn, _T(), ["/abort"])
    elif args.cmd == "log":
        for r in conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT 30").fetchall()[::-1]:
            print(f"{common.local_str(r['ts'], '%Y-%m-%d %H:%M:%S')} "
                  f"{r['kind']:<18} {r['step_id'] or '':<10} {(r['payload'] or '')[:220]}")
    elif args.cmd == "telegram-detect-chat":
        tok = ctx.getenv("DEV_TELEGRAM_BOT_TOKEN")
        if not tok:
            print(tgm.SETUP_HELP); return 1
        t = tgm.Telegram(tok, "0")
        for u in t.get_updates(0):
            m = u.get("message") or {}
            print(f"chat_id={m.get('chat',{}).get('id')} from={m.get('from',{}).get('username')} "
                  f"text={m.get('text','')[:40]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
