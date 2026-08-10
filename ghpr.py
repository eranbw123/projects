"""GitHub PR visibility for validated automation work.

The controller pushes the last VALIDATED integration commit (recorded by
control.on_validating at the only moment the tip is provably test-green;
never the live unvalidated tip) and keeps one open PR per repo from
automation/integration to the configured base. Mechanics:

- Pushes go to an EXPLICIT URL derived from the owner repo's origin (see
  gitops.push_url): clones never hold a pushable remote, so workers cannot
  push. The guard still forbids main/master, force and deletion.
- gh CLI does auth + the PR API, non-interactively (EC_GH_EXE overrides).
- DB is authoritative: kv pr_tip vs pr_pushed; equal means the phase costs
  two kv reads and starts no subprocess.
- Failures event + notify once per tip and back off; a GitHub outage must
  never pause development or feed the controller error streak.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import common
import db
import gitops
import telegram as tgm

INTEGRATION = "automation/integration"
BACKOFF_SEC = 600
GH_DEFAULT = r"C:\Program Files\GitHub CLI\gh.exe"

_SLUG_RES = [
    re.compile(r"^https://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$", re.I),
    re.compile(r"^(?:ssh://)?git@github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?/?$", re.I),
]


class GhError(Exception):
    pass


def _parse_slug(url: str) -> str | None:
    for rx in _SLUG_RES:
        m = rx.match(url.strip())
        if m:
            return f"{m.group(1)}/{m.group(2)}"
    return None


def github_slug(source: Path) -> str | None:
    """owner/name from the owner repo's origin URL; None when there is no
    GitHub origin (PR visibility simply does not apply to that repo)."""
    p = gitops.git_ro(["remote", "get-url", "origin"], cwd=source, check=False)
    if p.returncode != 0:
        return None
    return _parse_slug(p.stdout or "")


def gh_exe(ctx) -> str | None:
    exe = ctx.getenv("EC_GH_EXE")
    if exe:
        return exe if Path(exe).exists() else shutil.which(exe)
    return shutil.which("gh") or (GH_DEFAULT if Path(GH_DEFAULT).exists() else None)


def make_gh(ctx):
    """Real gh runner: argv -> stdout; GhError (stderr tail) on failure."""
    exe = gh_exe(ctx)

    def run(args: list[str], timeout: int = 60) -> str:
        if not exe:
            raise GhError("gh CLI not found (install GitHub CLI or set EC_GH_EXE)")
        env = {**os.environ, "GH_PROMPT_DISABLED": "1",
               "GH_NO_UPDATE_NOTIFIER": "1", "NO_COLOR": "1"}
        p = subprocess.run([exe, *args], capture_output=True, encoding="utf-8",
                           errors="replace", timeout=timeout, env=env)
        if p.returncode != 0:
            raise GhError(f"gh {' '.join(args[:4])}: "
                          f"{((p.stderr or '') + (p.stdout or '')).strip()[:300]}")
        return p.stdout or ""

    return run


def make_push(ctx):
    def push(ws: Path, url: str, sha: str) -> None:
        gitops.push_url(ws, url, f"{sha}:refs/heads/{INTEGRATION}")
    return push


STEP_MARK = {"ACCEPTED": "accepted ✅", "SOAKING": "soaking 🟢"}


def pr_body(conn, repo: str, baseline: str, tip: str) -> str:
    lines = [
        f"Automated integration branch for `{repo}`, maintained by engine-control.",
        "Every commit was cherry-picked here only after worktree tests, an",
        "independent review, and integration tests green at this branch's tip.",
        "",
        f"- Baseline (roadmap pin): `{baseline[:12]}`",
        f"- Last validated push: `{tip[:12]}` at {common.now()}",
        "- README + repo description: maintained automatically"
        + (f" (last refresh covered `{db.kv_get(conn, f'docs_tip:{repo}')[:12]}`)"
           if db.kv_get(conn, f"docs_tip:{repo}") else " (first refresh pending)"),
        "",
        "| roadmap step | state | commits | last integrated |",
        "|---|---|---|---|",
    ]
    for r in conn.execute(
            "SELECT c.step_id, COUNT(*) n, MAX(c.integrated_at) last, s.state, s.title"
            " FROM commits c JOIN steps s ON s.id=c.step_id"
            " WHERE c.repo=? AND c.integrated_sha IS NOT NULL"
            " GROUP BY c.step_id ORDER BY MIN(c.id)", (repo,)):
        mark = STEP_MARK.get(r["state"], (r["state"] or "").lower() + " 🔄")
        lines.append(f"| {r['step_id']} — {r['title']} | {mark} | {r['n']} | {r['last']} |")
    lines += ["", "_Opened automatically by engine-control; the branch is updated only "
              "with validated work. Do not push to it by hand._"]
    return "\n".join(lines)


def pr_sync(ctx, conn, roadmap, gh=None, push=None) -> None:
    """Tick phase. Never raises: per-repo failures event, notify once per
    tip, and back off BACKOFF_SEC before the next attempt."""
    for name, rc in (roadmap.get("repos") or {}).items():
        tip = db.kv_get(conn, f"pr_tip:{name}")
        if not tip or tip == db.kv_get(conn, f"pr_pushed:{name}"):
            continue
        until = db.kv_get(conn, f"pr_backoff:{name}")
        if until and not common.is_past(until):
            continue
        try:
            _sync_repo(ctx, conn, name, rc, tip,
                       gh or make_gh(ctx), push or make_push(ctx))
            db.kv_set(conn, f"pr_backoff:{name}", "")
        except Exception as e:
            db.event(conn, ctx, "pr_sync_error", repo=name, err=repr(e)[:300])
            db.kv_set(conn, f"pr_backoff:{name}", common.iso_in(BACKOFF_SEC))
            tgm.notify(conn, ctx, f"pr-fail:{name}:{tip[:12]}",
                       f"engine-control: ⚠ GitHub sync failed for {name} "
                       f"(retrying every {BACKOFF_SEC // 60}m): {repr(e)[:180]}")


def _sync_repo(ctx, conn, name, rc, tip, gh, push):
    ws = Path(rc["workspace"])
    slug = github_slug(Path(rc["source"]))
    if not slug:
        if db.kv_get(conn, f"pr_skip:{name}") != "1":
            db.kv_set(conn, f"pr_skip:{name}", "1")
            db.event(conn, ctx, "pr_skip", repo=name, why="no GitHub origin on source repo")
        db.kv_set(conn, f"pr_pushed:{name}", tip)  # nothing to sync with; stop retrying
        return
    prev = db.kv_get(conn, f"pr_pushed:{name}")
    span = f"{prev}..{tip}" if prev else f"{rc['baseline']}..{tip}"
    n = gitops.git_ro(["rev-list", "--count", span], cwd=ws,
                      check=False).stdout.strip() or "?"
    push(ws, f"https://github.com/{slug}.git", tip)
    db.event(conn, ctx, "pr_pushed", repo=name, sha=tip, commits=n)
    num, url, opened = _ensure_pr(ctx, conn, name, rc, slug, tip, gh)
    db.kv_set(conn, f"pr_pushed:{name}", tip)
    if opened:
        tgm.notify(conn, ctx, f"pr-open:{name}:{num}",
                   f"engine-control: 🔀 opened PR #{num} for {name}\n{url}")
    if url:
        tgm.notify(conn, ctx, f"pr:{name}:{tip[:12]}",
                   f"engine-control: 📤 {name}: {n} validated commit(s) → GitHub · "
                   f"PR #{num}\n{url}")
    else:
        tgm.notify(conn, ctx, f"pr:{name}:{tip[:12]}",
                   f"engine-control: 📤 {name}: {n} validated commit(s) → GitHub "
                   "(no PR: the base branch already contains them)")


def _ensure_pr(ctx, conn, name, rc, slug, tip, gh):
    """Returns (number, url, opened_now). Self-heals a lost kv via gh pr
    list; a merged/closed PR is replaced when the NEXT validated work lands
    (sync only runs on a new tip, so a deliberately closed PR stays closed
    until there is something new to show)."""
    num = db.kv_get(conn, f"pr_number:{name}")
    if num:
        try:
            st = json.loads(gh(["pr", "view", num, "--repo", slug,
                                "--json", "state,url"]))
        except GhError:
            st = None  # deleted or inaccessible — rediscover below
        if st and st.get("state") == "OPEN":
            db.kv_set(conn, f"pr_url:{name}", st.get("url") or "")
            _edit_body(ctx, conn, name, rc, slug, num, tip, gh)
            return num, st.get("url"), False
        num = None
    rows = json.loads(gh(["pr", "list", "--repo", slug, "--head", INTEGRATION,
                          "--state", "open", "--json", "number,url"]) or "[]")
    if rows:
        num, url = str(rows[0]["number"]), rows[0]["url"]
        db.kv_set(conn, f"pr_number:{name}", num)
        db.kv_set(conn, f"pr_url:{name}", url)
        _edit_body(ctx, conn, name, rc, slug, num, tip, gh)
        return num, url, False
    base = _base_branch(rc, slug, gh)
    body = _body_file(ctx, conn, name, rc, tip)
    try:
        out = gh(["pr", "create", "--repo", slug, "--base", base,
                  "--head", INTEGRATION,
                  "--title", f"Automation integration — {name}",
                  "--body-file", str(body)])
    except GhError as e:
        if "no commits between" in str(e).lower():
            db.event(conn, ctx, "pr_no_delta", repo=name, base=base)
            return None, None, False
        raise
    url = out.strip().splitlines()[-1].strip()
    num = url.rstrip("/").rsplit("/", 1)[-1]
    db.kv_set(conn, f"pr_number:{name}", num)
    db.kv_set(conn, f"pr_url:{name}", url)
    db.event(conn, ctx, "pr_opened", repo=name, number=num, url=url, base=base)
    return num, url, True


def _base_branch(rc, slug, gh):
    want = rc.get("pr_base")
    if want:
        try:
            gh(["api", f"repos/{slug}/branches/{want}"])
            return want
        except GhError:
            pass  # configured base merged+deleted meanwhile: fall back
    info = json.loads(gh(["repo", "view", slug, "--json", "defaultBranchRef"]))
    return (info.get("defaultBranchRef") or {}).get("name") or "main"


def _body_file(ctx, conn, name, rc, tip) -> Path:
    d = ctx.art / "pr"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name}.md"
    common.write_atomic(p, ctx.redact(pr_body(conn, name, rc["baseline"], tip)))
    return p


def _edit_body(ctx, conn, name, rc, slug, num, tip, gh):
    body = _body_file(ctx, conn, name, rc, tip)
    try:
        gh(["pr", "edit", num, "--repo", slug, "--body-file", str(body)])
    except GhError:
        pass  # body refresh is cosmetic; the push and notification stand
