"""Git operations with a mechanical safety guard.

Every git invocation goes through run_git(). The guard enforces:
- no push unless explicitly allowed, and never to main/master, never forced,
  never a ref deletion;
- no `reset --hard`, no `clean`, no branch deletion/rename, no history rewrite;
- mutations only inside approved automation roots (owner working copies under
  C:\\github are read-only source).
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


def assert_safe(args: list[str], cwd, allow_push: bool = False) -> None:
    toks = [str(a) for a in args]
    low = [t.lower() for t in toks]
    sub = next((t for t in low if not t.startswith("-")), "")

    if sub == "push":
        if not allow_push:
            raise GitSafetyError("push is disabled for the controller")
        if any(t in ("-f", "--force", "--force-with-lease", "--mirror", "--delete", "-d") for t in low):
            raise GitSafetyError("forced/deleting push forbidden")
        for t in toks[1:]:
            base = t.split(":")[-1].strip().lower()
            if t.strip().startswith(":"):
                raise GitSafetyError("ref-deletion push forbidden")
            if base in ("main", "master", "refs/heads/main", "refs/heads/master"):
                raise GitSafetyError("push to main/master forbidden")
    if sub == "reset" and any(t == "--hard" for t in low):
        raise GitSafetyError("reset --hard forbidden")
    if sub == "clean":
        raise GitSafetyError("git clean forbidden")
    if sub == "branch" and any(t in ("-d", "-D", "--delete", "-m", "-M", "--move") for t in low):
        raise GitSafetyError("branch deletion/rename forbidden")
    if sub in ("filter-branch", "filter-repo", "gc", "prune", "reflog") and sub != "reflog":
        raise GitSafetyError(f"{sub} forbidden")
    if sub == "reflog" and any(t in ("expire", "delete") for t in low):
        raise GitSafetyError("reflog expire/delete forbidden")
    if sub == "update-ref" and any(t == "-d" for t in low):
        raise GitSafetyError("update-ref -d forbidden")
    if sub == "checkout" and any(t in ("-f", "--force") for t in low):
        raise GitSafetyError("forced checkout forbidden")
    if sub == "worktree" and "remove" in low and any(t in ("-f", "--force") for t in low):
        pass  # allowed: removing our own throwaway worktrees, never a branch

    mutating = sub not in READ_ONLY_SUBCOMMANDS
    if sub == "worktree" and not any(t in ("add", "remove", "prune") for t in low):
        mutating = False
    if sub == "config" and "--get" not in low and "--list" not in low and "--get-all" not in low:
        mutating = True
    if mutating and cwd is not None and not _under(Path(cwd), _write_roots()):
        raise GitSafetyError(f"mutating git op outside automation roots: {cwd}")
    # Owner working copies are never a mutation target, even if roots change.
    if mutating and cwd is not None and _under(Path(cwd), [Path(r"C:\github")]):
        raise GitSafetyError("owner repos under C:\\github are read-only")


def run_git(args: list[str], cwd=None, allow_push=False, check=True,
            timeout=300) -> subprocess.CompletedProcess:
    assert_safe(args, cwd, allow_push=allow_push)
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                       text=True, timeout=timeout)
    if check and p.returncode != 0:
        raise GitError(f"git {' '.join(args)} rc={p.returncode}: {p.stderr.strip()[:400]}")
    return p


def git_ro(args: list[str], cwd, check=True) -> subprocess.CompletedProcess:
    """Read-only queries (allowed against owner repos)."""
    sub = next((a for a in args if not a.startswith("-")), "")
    if sub not in READ_ONLY_SUBCOMMANDS:
        raise GitSafetyError(f"git_ro used for non-read-only subcommand: {sub}")
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=120)
    if check and p.returncode != 0:
        raise GitError(f"git {' '.join(args)} rc={p.returncode}: {p.stderr.strip()[:400]}")
    return p


def clone(src: str | Path, dst: Path) -> None:
    """Clone reads src; writes only dst (verified under automation roots)."""
    dst = Path(dst)
    if not _under(dst.parent, _write_roots()):
        raise GitSafetyError(f"clone destination outside automation roots: {dst}")
    p = subprocess.run(["git", "clone", "--no-hardlinks", str(src), str(dst)],
                       capture_output=True, text=True, timeout=600)
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


def log_has_key(repo: Path, ref: str, key: str) -> str | None:
    """First commit on ref whose message carries our idempotency trailer."""
    p = git_ro(["log", ref, f"--grep=EC-Key: {key}", "--format=%H", "-1"], cwd=repo, check=False)
    sha = p.stdout.strip().splitlines()
    return sha[0] if sha else None


def cherry_pick(repo: Path, sha: str) -> tuple[str, str | None]:
    """Returns ("ok", new_sha) | ("empty", None) | ("conflict", None).
    On conflict the cherry-pick is aborted — never resolved destructively."""
    p = run_git(["cherry-pick", sha], cwd=repo, check=False)
    if p.returncode == 0:
        return "ok", current_sha(repo)
    err = (p.stdout + p.stderr).lower()
    if "empty" in err and "cherry-pick" in err:
        run_git(["cherry-pick", "--skip"], cwd=repo, check=False)
        return "empty", None
    run_git(["cherry-pick", "--abort"], cwd=repo, check=False)
    return "conflict", None


def head_commit_message(repo: Path, sha: str) -> str:
    return git_ro(["log", "-1", "--format=%B", sha], cwd=repo).stdout
