"""Automated GitHub docs visibility: README + repo-description maintenance.

Companion tick phase to ghpr. When validated work is already visible on
GitHub (kv pr_pushed), a background sonnet "docs" worker refreshes the repo's
README.md in an isolated worktree checked out at exactly that published tip,
and proposes a one-line GitHub repo description. The controller then:

- accepts the commit ONLY if the diff touches README.md alone and leaks no
  secrets (mechanical whitelist — a docs worker can never change code);
- cherry-picks it onto automation/integration (same idempotent -x provenance
  path as step promotion, serialized behind any in-flight validation) and
  advances kv pr_tip, so the existing ghpr phase pushes it into the open PR.
  The tip stays test-green by construction: the code is byte-identical to an
  already-validated tip, only README.md differs;
- PATCHes the GitHub repo description via gh api when it changes.

Cost/safety posture: at most one docs worker in flight globally, dispatched
through capacity.can_dispatch as background work, only while not paused, with
a per-repo cooldown between refreshes; a rejected tip is never retried (the
next validated tip triggers a fresh attempt). GitHub/worker failures event +
notify once and back off — they never pause development and never feed the
controller error streak.
"""
from __future__ import annotations

import json
from pathlib import Path

import capacity as cap
import claude_runner as cr
import common
import db
import ghpr
import gitops
import telegram as tgm
import validators as vd

INTEGRATION = "automation/integration"
COOLDOWN_SEC = 6 * 3600      # min gap between README refreshes per repo
BACKOFF_SEC = 3600           # after a worker/gh failure
ALLOWED_FILES = {"readme.md"}
DESC_LIMIT = 350             # GitHub caps repo descriptions at 350 chars


def docs_sync(ctx, conn, roadmap, gh=None, launch=None) -> None:
    """Tick phase. Never raises: per-repo failures event, notify once per
    published tip, and back off BACKOFF_SEC before the next attempt."""
    if db.kv_get(conn, "paused") == "1":
        return  # docs are never urgent; a pause is often a maintenance window
    for name, rc in (roadmap.get("repos") or {}).items():
        try:
            _sync_repo(ctx, conn, name, rc, gh, launch)
        except Exception as e:
            db.event(conn, ctx, "docs_error", repo=name, err=repr(e)[:300])
            db.kv_set(conn, f"docs_backoff:{name}", common.iso_in(BACKOFF_SEC))
            tip = db.kv_get(conn, f"pr_pushed:{name}") or "?"
            tgm.notify(conn, ctx, f"docs-fail:{name}:{tip[:12]}",
                       f"engine-control: ⚠ README/description sync failed for "
                       f"{name} (retrying in {BACKOFF_SEC // 60}m): {repr(e)[:180]}")


def _sync_repo(ctx, conn, name, rc, gh, launch):
    open_run = json.loads(db.kv_get(conn, f"docs_run:{name}") or "null")
    if open_run:
        _process(ctx, conn, name, rc, open_run, gh)
        return
    until = db.kv_get(conn, f"docs_backoff:{name}")
    if until and not common.is_past(until):
        return
    _desc_sync(ctx, conn, name, rc, gh)
    _maybe_dispatch(ctx, conn, name, rc, launch)


# ------------------------------------------------------------------ dispatch

def _readme_missing(ws: Path, tip: str) -> bool:
    files = gitops.git_ro(["ls-tree", "--name-only", tip], cwd=ws,
                          check=False).stdout or ""
    return not any(f.strip().lower() in ALLOWED_FILES for f in files.splitlines())


def _maybe_dispatch(ctx, conn, name, rc, launch):
    tip = db.kv_get(conn, f"pr_pushed:{name}")
    if not tip or tip in (db.kv_get(conn, f"docs_tip:{name}"),
                          db.kv_get(conn, f"docs_reject:{name}")):
        return
    if ghpr.github_slug(Path(rc["source"])) is None:
        db.kv_set(conn, f"docs_tip:{name}", tip)  # no GitHub: nothing to maintain
        return
    ws = Path(rc["workspace"])
    nxt = db.kv_get(conn, f"docs_next_at:{name}")
    if not _readme_missing(ws, tip) and nxt and not common.is_past(nxt):
        return  # cooldown (a missing README bootstraps immediately)
    if conn.execute("SELECT 1 FROM runs WHERE role='docs' AND status IN "
                    "('PREPARED','DISPATCHED')").fetchone():
        return  # one docs worker in flight globally
    view = json.loads(db.kv_get(conn, "capacity_state") or "{}")
    gov = cap.governor(ctx, conn, view, cap.current_usage(conn), 0)
    if not cap.can_dispatch(ctx, conn, gov, "docs", "sonnet", background=True):
        return
    seq = conn.execute("SELECT COUNT(*) c FROM runs WHERE role='docs'"
                       ).fetchone()["c"] + 1
    key = f"_docs-{name}.c0.t0.docs.{seq}"
    branch = f"ec/docs-{name}-{seq}"
    wt = ctx.art / "wt" / f"docs-{name}-{seq}"
    if not wt.exists():
        gitops.worktree_add(ws, wt, branch, tip)
    prompt = ((common.CODE_DIR / "prompts" / "docs.md")
              .read_text(encoding="utf-8").replace("<<REPO>>", name))
    cr.build_job(ctx, key, "docs", prompt, str(wt), "sonnet", "high")
    spec = cr.ROLES["docs"]
    # run row + docs_run kv in ONE transaction: a crash leaves either both
    # (processed next tick) or neither (re-dispatched next tick), never an
    # orphan run that would wedge the single-flight gate.
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO runs(key,step_id,task_idx,cycle,role,model,"
            "effort,lane,status,cwd,artifact_dir,deadline,created_at) "
            "VALUES(?,?,0,0,'docs','sonnet','high','task','PREPARED',?,?,?,?)",
            (key, f"_docs:{name}", str(wt), str(cr.art_dir(ctx, key)),
             common.iso_in(spec["deadline_min"] * 60 + 600), common.now()))
        conn.execute(
            "INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (f"docs_run:{name}", json.dumps({"key": key, "tip": tip,
                                             "wt": str(wt)})))
    db.event(conn, ctx, "docs_dispatched", repo=name, key=key, tip=tip)
    run = db.run_by_key(conn, key)
    if run and run["status"] == "PREPARED":
        if launch is None:
            import control
            launch = control.launch
        launch(ctx, conn, run)


# ------------------------------------------------------------------- process

def _close(conn, name, ws, wt: Path):
    if wt.exists():
        gitops.worktree_remove(ws, wt)
    db.kv_set(conn, f"docs_run:{name}", "")


def _process(ctx, conn, name, rc, open_run, gh):
    run = db.run_by_key(conn, open_run["key"])
    ws, wt, base = Path(rc["workspace"]), Path(open_run["wt"]), open_run["tip"]
    if run is None:
        _close(conn, name, ws, wt)
        return
    if run["status"] in ("PREPARED", "DISPATCHED"):
        return  # in flight; reconcile owns liveness/deadline
    if run["status"] != "DONE":
        _close(conn, name, ws, wt)
        db.kv_set(conn, f"docs_backoff:{name}", common.iso_in(BACKOFF_SEC))
        db.event(conn, ctx, "docs_worker_failed", repo=name, key=run["key"],
                 status=run["status"])
        tgm.notify(conn, ctx, f"docs-fail:{name}:{base[:12]}",
                   f"engine-control: ⚠ README worker for {name} ended "
                   f"{run['status']}; retrying in {BACKOFF_SEC // 60}m")
        return

    obj, errs = vd.check_result(Path(run["artifact_dir"]) / "result.json",
                                "docs_result.schema.json")
    if obj is None or errs or obj.get("status") != "done":
        why = "; ".join(errs) if errs else (obj or {}).get("summary", "no result")
        _reject(ctx, conn, name, ws, wt, base, f"worker result unusable: {why[:200]}")
        return
    if not wt.exists():
        _reject(ctx, conn, name, ws, wt, base, "worktree vanished before acceptance")
        return
    head = gitops.current_sha(wt)

    if head != base:
        findings = _acceptance(ctx, ws, base, head)
        if findings:
            _reject(ctx, conn, name, ws, wt, base, "; ".join(findings)[:300])
            return
        import control
        if control.repo_integration_busy(conn, ws):
            return  # validation holds integration; retried next tick
        if gitops.git_ro(["rev-parse", "--abbrev-ref", "HEAD"],
                         cwd=ws).stdout.strip() != INTEGRATION:
            gitops.checkout(ws, INTEGRATION)
        gitops.run_git(["cherry-pick", "--abort"], cwd=ws, check=False)
        for sha in gitops.commits_between(ws, base, head):
            already = gitops.git_ro(
                ["log", INTEGRATION, f"--grep=cherry picked from commit {sha}",
                 "--format=%H", "-1"], cwd=ws, check=False).stdout.strip()
            if already:
                continue
            status, _new = control.cherry_pick_x(ws, sha)
            if status == "conflict":
                _reject(ctx, conn, name, ws, wt, base,
                        f"cherry-pick conflict on {sha[:10]} (integration "
                        "advanced under the docs worktree)")
                return
        new_tip = gitops.current_sha(ws, INTEGRATION)
        db.kv_set(conn, f"pr_tip:{name}", new_tip)
        db.event(conn, ctx, "docs_applied", repo=name, base=base, tip=new_tip)
        tgm.notify(conn, ctx, f"docs:{name}:{base[:12]}",
                   f"engine-control: 📘 {name} README refreshed — lands in the "
                   f"open PR on the next push\n{obj.get('summary', '')[:300]}")
    else:
        db.event(conn, ctx, "docs_no_change", repo=name, tip=base)

    desc = " ".join((obj.get("description") or "").split())
    if desc:
        db.kv_set(conn, f"gh_desc_want:{name}", ctx.redact(desc)[:DESC_LIMIT])
    db.kv_set(conn, f"docs_tip:{name}", base)
    db.kv_set(conn, f"docs_next_at:{name}", common.iso_in(COOLDOWN_SEC))
    db.kv_set(conn, f"docs_backoff:{name}", "")
    _close(conn, name, ws, wt)
    _desc_sync(ctx, conn, name, rc, gh)


def _acceptance(ctx, ws, base, head) -> list[str]:
    """Docs-grade acceptance: strictly README-only on top of the published
    tip, no secrets. Stricter than step acceptance — nothing else may change."""
    findings = []
    if not gitops.is_ancestor(ws, base, head):
        return [f"head {head[:10]} does not descend from published tip {base[:10]}"]
    files = gitops.changed_files(ws, base, head)
    extra = [f for f in files if f.replace("\\", "/").lower() not in ALLOWED_FILES]
    if extra:
        findings.append(f"non-README paths changed: {extra[:5]}")
    leaks = vd.secrets_in_text(gitops.diff_text(ws, base, head), ctx._secrets)
    if leaks:
        findings.append(f"possible secrets in README diff: {leaks}")
    return findings


def _reject(ctx, conn, name, ws, wt, base, why):
    _close(conn, name, ws, wt)
    db.kv_set(conn, f"docs_reject:{name}", base)  # this tip is done; next tip retries
    db.event(conn, ctx, "docs_rejected", repo=name, tip=base, why=why)
    tgm.notify(conn, ctx, f"docs-reject:{name}:{base[:12]}",
               f"engine-control: ⚠ {name} README update discarded — {why}. "
               "Will retry when the next validated work is published.")


# --------------------------------------------------------------- description

def _desc_sync(ctx, conn, name, rc, gh):
    want = db.kv_get(conn, f"gh_desc_want:{name}")
    if not want or want == db.kv_get(conn, f"gh_desc:{name}"):
        return
    slug = ghpr.github_slug(Path(rc["source"]))
    if not slug:
        return
    (gh or ghpr.make_gh(ctx))(["api", "-X", "PATCH", f"repos/{slug}",
                               "-f", f"description={want}"])
    db.kv_set(conn, f"gh_desc:{name}", want)
    db.event(conn, ctx, "docs_desc_set", repo=name, desc=want[:120])
    tgm.notify(conn, ctx, f"docs-desc:{name}:{want[:24]}",
               f"engine-control: 🏷 {name} GitHub description updated:\n{want}")
