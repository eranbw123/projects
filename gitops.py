"""Git operations with a mechanical safety guard.

Every git invocation goes through run_git(). The guard enforces:
- no push unless explicitly allowed, and never to main/master, never forced
  (including +refspec force syntax), never a ref deletion;
- no `reset --hard`, no `clean`, no branch deletion/rename, no history rewrite;
- no -C/--git-dir/--work-tree redirection (a cwd-check bypass);
- mutations require an explicit cwd inside approved automation roots (owner
  working copies under C:\\github are read-only sources, always).
Recovery uses only non-destructive operations (cherry-pick --abort etc.).
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

import common


class GitError(Exception):
    pass


class GitSafetyError(GitError):
    pass


READ_ONLY_SUBCOMMANDS = {
    "status", "log", "rev-parse", "remote", "branch", "show", "diff",
    "merge-base", "ls-files", "ls-tree", "config", "worktree", "rev-list",
    "cat-file", "describe", "shortlog",
}

# Roots where mutation is allowed. Tests run under the temp dir.
def _write_roots() -> list[Path]:
    roots = [Path(r"C:\projects"), Path(tempfile.gettempdir())]
    er = os.environ.get("EC_ROOT")
    if er:
        roots.append(Path(er))
    extra = os.environ.get("EC_GIT_WRITE_ROOTS")
    if extra:
        roots.extend(Path(p) for p in extra.split(";") if p)
    return roots


def _under(path: Path, roots: list[Path]) -> bool:
    p = str(Path(path).resolve()).lower().rstrip("\\/")
    for r in roots:
        rs = str(Path(r).resolve()).lower().rstrip("\\/")
        if p == rs or p.startswith(rs + "\\") or p.startswith(rs + "/"):
            return True
    return False


def _is_mutating(sub: str, toks: list[str], low: list[str]) -> bool:
    """Classify an invocation, not just a subcommand: worktree/branch/remote/
    config have both read and write forms (audit finding: the write forms were
    previously classified read-only)."""
    if sub == "worktree":
        return any(t in ("add", "remove", "prune", "move", "repair", "lock", "unlock")
                   for t in low[1:])
    if sub == "branch":
        # any positional argument means create (delete/rename already raised)
        return bool([t for t in toks[1:] if not t.startswith("-")])
    if sub == "remote":
        return any(t in ("add", "remove", "rm", "rename", "set-url", "set-head",
                         "set-branches", "prune", "update") for t in low)
    if sub == "config":
        return not any(t in ("--get", "--list", "--get-all", "--get-regexp") for t in low)
    return sub not in READ_ONLY_SUBCOMMANDS


def assert_safe(args: list[str], cwd, allow_push: bool = False) -> None:
    toks = [str(a) for a in args]
    low = [t.lower() for t in toks]
    for t in low:
        if t in ("-c", "--git-dir", "--work-tree") or \
           t.startswith(("--git-dir=", "--work-tree=")):
            raise GitSafetyError(f"global redirection flag forbidden: {t}")
    sub = next((t for t in low if not t.startswith("-")), "")

    if sub == "push":
        if not allow_push:
            raise GitSafetyError("push is disabled for the controller")
        if any(t in ("-f", "--force", "--force-with-lease", "--mirror", "--delete", "-d")
               for t in low):
            raise GitSafetyError("forced/deleting push forbidden")
        for t in toks[1:]:
            spec = t.strip()
            if spec.startswith("+"):
                raise GitSafetyError("force refspec (+ref) forbidden")
            if spec.startswith(":"):
                raise GitSafetyError("ref-deletion push forbidden")
            base = spec.split(":")[-1].strip().lower().lstrip("+")
            if base in ("main", "master", "refs/heads/main", "refs/heads/master"):
                raise GitSafetyError("push to main/master forbidden")
    if sub == "reset" and any(t == "--hard" for t in low):
        raise GitSafetyError("reset --hard forbidden")
    if sub == "clean":
        raise GitSafetyError("git clean forbidden")
    if sub == "branch" and any(t in ("-d", "-D", "--delete", "-m", "-M", "--move")
                               for t in low):
        raise GitSafetyError("branch deletion/rename forbidden")
    if sub in ("filter-branch", "filter-repo", "gc", "prune"):
        raise GitSafetyError(f"{sub} forbidden")
    if sub == "reflog" and any(t in ("expire", "delete") for t in low):
        raise GitSafetyError("reflog expire/delete forbidden")
    if sub == "update-ref" and any(t == "-d" for t in low):
        raise GitSafetyError("update-ref -d forbidden")
    if sub == "checkout" and any(t in ("-f", "--force") for t in low):
        raise GitSafetyError("forced checkout forbidden")

    if _is_mutating(sub, toks, low):
        if cwd is None:
            raise GitSafetyError(f"mutating git op requires an explicit cwd: {sub}")
        if not _under(Path(cwd), _write_roots()):
            raise GitSafetyError(f"mutating git op outside automation roots: {cwd}")
        # Owner working copies are never a mutation target, even if roots change.
        if _under(Path(cwd), [Path(r"C:\github")]):
            raise GitSafetyError("owner repos under C:\\github are read-only")


def run_git(args: list[str], cwd=None, allow_push=False, check=True,
            timeout=300) -> subprocess.CompletedProcess:
    assert_safe(args, cwd, allow_push=allow_push)
    # git emits UTF-8; bare text=True decodes with the ANSI codepage (cp1255
    # here), which crashes subprocess's reader thread on real diffs and hands
    # back stdout=None with rc=0.
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                       encoding="utf-8", errors="replace", timeout=timeout)
    if check and p.returncode != 0:
        raise GitError(f"git {' '.join(args)} rc={p.returncode}: {p.stderr.strip()[:400]}")
    return p


def git_ro(args: list[str], cwd, check=True) -> subprocess.CompletedProcess:
    """Read-only queries (allowed against owner repos)."""
    toks = [str(a).lower() for a in args]
    sub = next((a for a in toks if not a.startswith("-")), "")
    if sub not in READ_ONLY_SUBCOMMANDS or _is_mutating(sub, [str(a) for a in args], toks):
        raise GitSafetyError(f"git_ro used for non-read-only invocation: {args}")
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                       encoding="utf-8", errors="replace", timeout=120)
    if check and p.returncode != 0:
        raise GitError(f"git {' '.join(args)} rc={p.returncode}: {p.stderr.strip()[:400]}")
    return p


def clone(src: str | Path, dst: Path) -> None:
    """Clone reads src; writes only dst (verified under automation roots)."""
    dst = Path(dst)
    if not _under(dst.parent, _write_roots()):
        raise GitSafetyError(f"clone destination outside automation roots: {dst}")
    p = subprocess.run(["git", "clone", "--no-hardlinks", str(src), str(dst)],
                       capture_output=True, encoding="utf-8", errors="replace",
                       timeout=600)
    if p.returncode != 0:
        raise GitError(f"clone failed: {p.stderr.strip()[:400]}")


def disable_push(repo: Path) -> None:
    """Mechanical prohibition: origin push URL becomes invalid."""
    run_git(["remote", "set-url", "--push", "origin", "DISABLED:engine-control-no-push"], cwd=repo)


def current_sha(repo: Path, ref="HEAD") -> str:
    return git_ro(["rev-parse", ref], cwd=repo).stdout.strip()


def branch_exists(repo: Path, name: str) -> bool:
    p = git_ro(["rev-parse", "--verify", "--quiet", "refs/heads/" + name], cwd=repo, check=False)
    return p.returncode == 0


def ensure_branch(repo: Path, name: str, base_sha: str) -> None:
    if not branch_exists(repo, name):
        run_git(["branch", name, base_sha], cwd=repo)


def checkout(repo: Path, name: str) -> None:
    run_git(["checkout", name], cwd=repo)


def worktree_add(repo: Path, path: Path, branch: str, base_sha: str) -> None:
    if branch_exists(repo, branch):
        run_git(["worktree", "add", str(path), branch], cwd=repo)
    else:
        run_git(["worktree", "add", "-b", branch, str(path), base_sha], cwd=repo)


def worktree_add_detached(repo: Path, path: Path, sha: str) -> None:
    run_git(["worktree", "add", "--detach", str(path), sha], cwd=repo)


def worktree_remove(repo: Path, path: Path) -> None:
    run_git(["worktree", "remove", "--force", str(path)], cwd=repo, check=False)


def is_ancestor(repo: Path, maybe_ancestor: str, descendant: str) -> bool:
    p = git_ro(["merge-base", "--is-ancestor", maybe_ancestor, descendant], cwd=repo, check=False)
    return p.returncode == 0


def commits_between(repo: Path, base: str, head: str) -> list[str]:
    out = git_ro(["rev-list", "--reverse", f"{base}..{head}"], cwd=repo).stdout
    return [l.strip() for l in out.splitlines() if l.strip()]


def changed_files(repo: Path, base: str, head: str) -> list[str]:
    out = git_ro(["diff", "--name-only", base, head], cwd=repo).stdout
    return [l.strip() for l in out.splitlines() if l.strip()]


def diff_text(repo: Path, base: str, head: str, max_lines=4000) -> str:
    out = git_ro(["diff", base, head], cwd=repo).stdout
    lines = out.splitlines()
    if len(lines) > max_lines:
        lines = lines[:max_lines] + [f"... [diff truncated at {max_lines} lines]"]
    return "\n".join(lines)


def head_commit_message(repo: Path, sha: str) -> str:
    return git_ro(["log", "-1", "--format=%B", sha], cwd=repo).stdout
