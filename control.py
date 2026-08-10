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
import sys
from pathlib import Path

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


def sync_steps(conn, ctx, roadmap):
    for s in roadmap["steps"]:
        if not db.get_step(conn, s["id"]):
            with conn:
                conn.execute(
                    "INSERT INTO steps(id,ordinal,title,repos,state,detail,updated_at)"
                    " VALUES(?,?,?,?, 'PENDING','{}',?)",
                    (s["id"], s["ordinal"], s.get("title", ""),
                     json.dumps(s.get("repos", [])), common.now()))


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
    run = db.run_by_key(conn, key)
    launch(ctx, conn, run)
    db.event(conn, ctx, "dispatched", step_id=step["id"], run_id=run["id"],
             key=key, role=role, model=model, lane=lane, to=to_state)
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
            finalize(ctx, conn, run, pr["rc"])
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
    blob = json.dumps(stdout) + cr.stderr_tail(ctx, run["key"])
    if cr.quota_hit(blob) and (rc != 0 or stdout.get("is_error")):
        status = "QUOTA"
    elif rc == 0:
        status = "DONE"
    else:
        status = "FAILED"
    db.finish_run(conn, ctx, run["id"], status, exit_code=rc,
                  note=common.tail(blob, 5) if status != "DONE" else "")
    if run["lane"] == "task":
        cr.delete_task(run["key"])


# ---------- step state machine ----------

def active_step(conn):
    return conn.execute(
        "SELECT * FROM steps WHERE state != 'ACCEPTED' ORDER BY ordinal LIMIT 1").fetchone()


def set_resume(conn, step, state=None):
    d = db.step_detail(step)
    d["resume"] = state or step["state"]
    db.set_detail(conn, step["id"], d)


def wait_quota(ctx, conn, step):
    set_resume(conn, step)
    retry_min = int(ctx.getenv("EC_QUOTA_RETRY_MIN", str(QUOTA_RETRY_MIN)))
    with conn:
        conn.execute("UPDATE steps SET retry_at=? WHERE id=?",
                     (common.iso_in(retry_min * 60), step["id"]))
    db.transition(conn, ctx, step["id"], "WAITING_QUOTA", "model quota/limit hit")
    tgm.notify(conn, ctx, f"quota:{step['id']}:{common.now()[:13]}",
               f"engine-control: {step['id']} waiting on model quota; retry in {retry_min}m")


def goto_blocked(ctx, conn, step, reason):
    db.transition(conn, ctx, step["id"], "BLOCKED", reason)
    tgm.notify(conn, ctx, f"blocked:{step['id']}:{db.step_detail(step).get('cycle',0)}",
               f"engine-control: {step['id']} BLOCKED — {reason[:300]}\nUse /retry or /abort.")


def repair_or_block(ctx, conn, step, findings: str):
    d = db.step_detail(step)
    task_idx = d.get("task_idx", 0)
    cycle = d.get("cycle", 0)
    pos = db.ladder_pos(conn, step["id"], task_idx, cycle)
    d["last_findings"] = findings[:6000]
    db.set_detail(conn, step["id"], d)
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
    run = dispatch(ctx, conn, step, "repair", prompt, d["task_wt"],
                   "REPAIRING", f"repair rung {pos + 1}",
                   model=model, effort=effort, resume_session=resume_session)
    tgm.notify(conn, ctx, f"repair:{run['key']}",
               f"engine-control: {step['id']} repair started (rung {pos + 1}/{LADDER_MAX})")


def dispatch_diagnostic(ctx, conn, step, findings):
    roadmap = load_roadmap(ctx)
    scfg = step_cfg(roadmap, step["id"])
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
        "- No secrets in code, commits, or your result JSON.\n")


# ---- stage starters ----

def start_planning(ctx, conn, roadmap, step):
    scfg = step_cfg(roadmap, step["id"])
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


def start_task_impl(ctx, conn, roadmap, step):
    d = db.step_detail(step)
    scfg = step_cfg(roadmap, step["id"])
    repo = current_repo(ctx, step)
    rc = repo_cfg(roadmap, repo)
    ws = Path(rc["workspace"])
    cycle, idx = d.get("cycle", 0), d.get("task_idx", 0)
    branch = f"ec/{step['id']}-c{cycle}-t{idx}"
    wt = ctx.art / "wt" / f"{step['id']}-c{cycle}-t{idx}"
    base = d.get("task_base")
    if not base or d.get("task_branch") != branch:
        base = integration_sha(roadmap, repo)
        d.update(task_base=base, task_branch=branch, task_wt=str(wt), diagnosed=False)
        db.set_detail(conn, step["id"], d)
    if not wt.exists():
        gitops.worktree_add(ws, wt, branch, base)
    plan_json = Path(step["plan_path"]).read_text(encoding="utf-8") if step["plan_path"] else "{}"
    model, effort = route_model(scfg, "implementer")
    prompt = fill(prompt_template("implementer.md"), {
        "STEP_ID": step["id"], "OBJECTIVE": task_objective(ctx, step),
        "PLAN": plan_json[:12000], "REPO": repo,
        "TESTS": json.dumps(rc["tests"]),
        "RULES": worker_rules(ctx, step), "RESULT_PATH": "<<RESULT_PATH>>"})
    dispatch(ctx, conn, step, "implementer", prompt, wt, "IMPLEMENTING",
             f"task {idx} implementer dispatched", model=model, effort=effort)


def start_testing(ctx, conn, roadmap, step):
    d = db.step_detail(step)
    repo = current_repo(ctx, step)
    cmds = repo_cfg(roadmap, repo)["tests"]
    dispatch(ctx, conn, step, "test", "", d["task_wt"], "TESTING",
             "canonical repo tests in worktree", test_cmds=cmds)


def start_review(ctx, conn, roadmap, step):
    d = db.step_detail(step)
    scfg = step_cfg(roadmap, step["id"])
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


def enter_validation(ctx, conn, roadmap, step):
    """Controller-owned promotion: acceptance checks + cherry-pick to
    automation/integration (idempotent via -x provenance), then an independent
    test run on the integration checkout."""
    d = db.step_detail(step)
    repo = current_repo(ctx, step)
    rc = repo_cfg(roadmap, repo)
    ws = Path(rc["workspace"])
    head = gitops.current_sha(Path(d["task_wt"]))
    base = d["task_base"]
    ok, findings = vd.git_acceptance(ws, base, head, ctx._secrets)
    if not ok:
        repair_or_block(ctx, conn, step, "git acceptance failed: " + "; ".join(findings))
        return
    cur = gitops.git_ro(["rev-parse", "--abbrev-ref", "HEAD"], cwd=ws).stdout.strip()
    if cur != INTEGRATION:
        gitops.checkout(ws, INTEGRATION)
    for sha in gitops.commits_between(ws, base, head):
        already = gitops.git_ro(
            ["log", INTEGRATION, f"--grep=cherry picked from commit {sha}",
             "--format=%H", "-1"], cwd=ws, check=False).stdout.strip()
        if already:
            continue
        status, new_sha = cherry_pick_x(ws, sha)
        if status == "conflict":
            db.event(conn, ctx, "cherry_conflict", step_id=step["id"], sha=sha)
            repair_or_block(ctx, conn, step,
                            f"cherry-pick conflict integrating {sha[:10]} onto {INTEGRATION}; "
                            "rebase your branch onto the current integration tip and resolve")
            return
        if status == "ok":
            with conn:
                conn.execute(
                    "INSERT INTO commits(step_id,repo,task_idx,run_key,sha,base_sha,"
                    "integrated_sha,integrated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (step["id"], repo, d.get("task_idx", 0), "", sha, base,
                     new_sha, common.now()))
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
    common.write_atomic(hp, json.dumps(handoff, indent=1))
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


# ---- the per-tick advance ----

def advance(ctx, conn, roadmap):
    step = active_step(conn)
    if step is None:
        if db.kv_get(conn, "roadmap_started") == "1":
            tgm.notify(conn, ctx, "roadmap:complete",
                       "engine-control: 🎉 roadmap complete — all steps accepted")
        return
    state = step["state"]
    scfg = step_cfg(roadmap, step["id"])
    d = db.step_detail(step)

    if state in common.HALT_STATES:
        return  # halted; never skip a blocked dependency

    tg_ok = tgm.from_ctx(ctx) is not None or ctx.getenv("EC_ALLOW_NO_TELEGRAM") == "1"
    if not tg_ok:
        if state != "WAITING_CONFIG":
            set_resume(conn, step)
            db.transition(conn, ctx, step["id"], "WAITING_CONFIG",
                          "dev telegram credentials missing")
            ctx.log(tgm.SETUP_HELP)
        return
    if state == "WAITING_CONFIG":
        db.transition(conn, ctx, step["id"], d.get("resume", "PENDING"), "config present")
        return
    if state == "WAITING_QUOTA":
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

    if state == "PENDING":
        start_planning(ctx, conn, roadmap, step)
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
    if role == "planner":
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
    if obj.get("passed"):
        tgm.notify(conn, ctx, f"tests:{run['key']}",
                   f"engine-control: {step['id']} tests green — review")
        start_review(ctx, conn, roadmap, step)
    else:
        tgm.notify(conn, ctx, f"tests:{run['key']}",
                   f"engine-control: {step['id']} tests FAILED — repair path")
        repair_or_block(ctx, conn, step, "tests failed:\n" + common.tail(obj.get("report", ""), 60))


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
    idx = d.get("task_idx", 0)
    if idx + 1 < d.get("tasks_n", 1):
        d["task_idx"] = idx + 1
        d.pop("task_base", None)
        d.pop("task_branch", None)
        d["diagnosed"] = False
        db.set_detail(conn, step["id"], d)
        tgm.notify(conn, ctx, f"task:{step['id']}:t{idx}",
                   f"engine-control: {step['id']} task {idx} integrated; next task")
        step = db.get_step(conn, step["id"])
        start_task_impl(ctx, conn, roadmap, step)
        return
    accept_step(ctx, conn, roadmap, step)


# ---------- telegram commands ----------

def status_text(ctx, conn) -> str:
    lines = []
    started = db.kv_get(conn, "roadmap_started") == "1"
    paused = db.kv_get(conn, "paused") == "1"
    lines.append(f"engine-control — roadmap {'started' if started else 'NOT started'}"
                 f"{' [PAUSED]' if paused else ''}")
    for s in conn.execute("SELECT * FROM steps ORDER BY ordinal"):
        d = db.step_detail(s)
        pos = db.ladder_pos(conn, s["id"], d.get("task_idx", 0), d.get("cycle", 0))
        mark = {"ACCEPTED": "✅", "BLOCKED": "⛔", "ABORTED": "🛑"}.get(s["state"], "·")
        lines.append(f"{mark} {s['id']} [{s['state']}] attempts={pos} — {s['title']}")
        if s["active_run_id"]:
            r = db.get_run(conn, s["active_run_id"])
            if r and r["status"] in ("PREPARED", "DISPATCHED"):
                lines.append(f"   run {r['key']} ({r['role']}/{r['model']}) since {r['created_at']}")
    if not tgm.from_ctx(ctx):
        lines.append("⚠ telegram: WAITING_CONFIG (see README)")
    return "\n".join(lines)


def handle_commands(ctx, conn, tg, cmds):
    for c in cmds:
        name = c.split()[0].split("@")[0]
        db.event(conn, ctx, "tg_command", cmd=name)
        if name == "/status":
            tg.send(status_text(ctx, conn))
        elif name == "/pause":
            db.kv_set(conn, "paused", "1")
            tg.send("engine-control: paused (running workers finish; no new dispatch)")
        elif name == "/resume":
            db.kv_set(conn, "paused", "0")
            step = active_step(conn)
            if step and step["state"] == "WAITING_USER":
                db.transition(conn, ctx, step["id"],
                              db.step_detail(step).get("resume", "PENDING"), "/resume")
            tg.send("engine-control: resumed")
        elif name == "/retry":
            step = active_step(conn)
            if step and step["state"] in common.HALT_STATES:
                d = db.step_detail(step)
                d = {"cycle": d.get("cycle", 0) + 1}
                db.set_detail(conn, step["id"], d)
                with conn:
                    conn.execute("UPDATE steps SET active_run_id=NULL, plan_path=NULL WHERE id=?",
                                 (step["id"],))
                db.transition(conn, ctx, step["id"], "PENDING", "/retry — fresh cycle")
                tg.send(f"engine-control: {step['id']} re-armed (cycle {d['cycle']})")
            else:
                tg.send("engine-control: /retry only applies to a BLOCKED/ABORTED step")
        elif name == "/abort":
            step = active_step(conn)
            if step:
                db.transition(conn, ctx, step["id"], "ABORTED", "/abort")
                tg.send(f"engine-control: {step['id']} aborted; roadmap halted")
        elif name == "/log":
            rows = conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT 15").fetchall()
            out = "\n".join(f"{r['ts'][11:19]} {r['kind']} {r['step_id'] or ''} "
                            f"{(r['payload'] or '')[:80]}" for r in rows[::-1])
            tg.send(out or "no events")


# ---------- tick ----------

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
            cmds = tgm.consume(ctx, conn, tg)
            handle_commands(ctx, conn, tg, cmds)
        reconcile(ctx, conn)
        if db.kv_get(conn, "roadmap_started") == "1" and db.kv_get(conn, "paused") != "1":
            try:
                advance(ctx, conn, roadmap)
            except Exception as e:  # a controller bug must be visible, not a wedge
                ctx.log(f"advance error: {e!r}")
                db.event(conn, ctx, "advance_error", err=repr(e)[:400])
        tgm.flush(ctx, conn, tg)
        conn.close()
        return 0
    finally:
        common.release_lock(lock)


# ---------- CLI ----------

TICK_TASK = "engine-control-tick"


def cmd_install_task(ctx):
    import subprocess
    cmd = f'"{sys.executable}" "{common.CODE_DIR / "control.py"}" tick'
    p = subprocess.run(["schtasks", "/create", "/tn", TICK_TASK, "/sc", "minute",
                        "/mo", "1", "/f", "/tr", cmd], capture_output=True, text=True)
    print(p.stdout.strip() or p.stderr.strip())
    q = subprocess.run(["schtasks", "/query", "/tn", TICK_TASK], capture_output=True, text=True)
    print(q.stdout.strip()[:400])
    return p.returncode


def cmd_uninstall_task(ctx):
    import subprocess
    p = subprocess.run(["schtasks", "/delete", "/tn", TICK_TASK, "/f"],
                       capture_output=True, text=True)
    print(p.stdout.strip() or p.stderr.strip())
    return p.returncode


def cmd_doctor(ctx):
    import shutil as sh
    import subprocess
    bad = 0
    claude = cr.resolve_claude(ctx)
    if claude:
        v = subprocess.run([claude, "--version"], capture_output=True, text=True).stdout.strip()
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
    print("[ok] telegram configured" if tgm.from_ctx(ctx)
          else "[WAITING_CONFIG] dev telegram not configured:\n" + tgm.SETUP_HELP)
    q = subprocess.run(["schtasks", "/query", "/tn", TICK_TASK], capture_output=True, text=True)
    print("[ok] tick task installed" if q.returncode == 0
          else "[..] tick task not installed (python control.py install-task)")
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
    ap = argparse.ArgumentParser(prog="control.py")
    ap.add_argument("cmd", choices=["tick", "start", "status", "pause", "resume",
                                    "retry", "abort", "log", "init", "doctor",
                                    "install-task", "uninstall-task",
                                    "telegram-detect-chat"])
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
        conn = cmd_init(ctx)
        db.kv_set(conn, "roadmap_started", "1")
        db.kv_set(conn, "paused", "0")
        tgm.notify(conn, ctx, "roadmap:started",
                   "engine-control: roadmap started — step 1 begins on the next tick")
        db.event(conn, ctx, "roadmap_started")
        print("roadmap started; the scheduled tick drives it from here")
        return 0
    if args.cmd == "doctor":
        return cmd_doctor(ctx)
    if args.cmd == "install-task":
        return cmd_install_task(ctx)
    if args.cmd == "uninstall-task":
        return cmd_uninstall_task(ctx)
    conn = db.connect(ctx)
    if args.cmd == "status":
        print(status_text(ctx, conn))
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
            print(f"{r['ts']} {r['kind']:<18} {r['step_id'] or '':<10} {(r['payload'] or '')[:100]}")
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
