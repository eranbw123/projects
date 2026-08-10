"""Deterministic validation: canonical repo tests, result-artifact schema
checks, forbidden-path and secret scanning, git acceptance checks.

Also runnable as a worker (`--run-tests`) so test execution happens in a
supervised detached run like everything else, writing a result artifact:
  python validators.py --run-tests --cwd <dir> --result <path> -- <cmd> [<cmd>...]
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import subprocess
import sys
from pathlib import Path

import common
import gitops

FORBIDDEN_GLOBS = [
    ".env", ".env.*", "*/.env", "*/.env.*",
    "*.db", "*/*.db", "*.sqlite", "*.sqlite3",
    ".git/*", "*/.git/*",
    "*.pem", "*.key", "*/id_rsa*",
]


def run_cmd(cmd: str, cwd, timeout=1800) -> tuple[int, str]:
    # cmd /d disables cmd.exe AutoRun. This machine has an AutoRun hook that
    # changes directory, which silently breaks shell=True's cwd — discovered
    # by the Step-0 canary suite. Never use bare shell=True here.
    import os
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")  # keep checkouts clean
    try:
        p = subprocess.run(["cmd", "/d", "/c", cmd], cwd=cwd, env=env,
                           capture_output=True, text=True, timeout=timeout)
        out = (p.stdout or "") + ("\n" + p.stderr if p.stderr else "")
        return p.returncode, out
    except subprocess.TimeoutExpired:
        return 124, f"[timeout after {timeout}s] {cmd}"


def repo_tests(commands: list[str], cwd) -> tuple[bool, str]:
    """Run the canonical test commands for a repo (from roadmap.yaml).
    Never replaces repo test frameworks — just invokes them."""
    reports = []
    ok = True
    for cmd in commands:
        rc, out = run_cmd(cmd, cwd)
        reports.append(f"$ {cmd}\n[rc={rc}]\n{common.tail(out, 80)}")
        if rc != 0:
            ok = False
    return ok, "\n\n".join(reports)


def check_result(path: Path, schema_name: str) -> tuple[dict | None, list[str]]:
    obj = common.read_json(path)
    if obj is None:
        return None, [f"missing or malformed JSON: {path}"]
    errs = common.schema_errors(obj, common.load_schema(schema_name))
    return (obj, errs)


def forbidden_changed(files: list[str]) -> list[str]:
    bad = []
    for f in files:
        norm = f.replace("\\", "/")
        for g in FORBIDDEN_GLOBS:
            if fnmatch.fnmatch(norm, g):
                bad.append(f)
                break
    return bad


def secrets_in_text(text: str, extra_values: list[str] | None = None) -> list[str]:
    hits = []
    for rx in common.GENERIC_SECRET_RES:
        if rx.search(text):
            hits.append(rx.pattern)
    for v in extra_values or []:
        if v and len(v) >= 8 and v in text:
            hits.append("known-secret-value")
    return hits


def git_acceptance(repo: Path, base_sha: str, head_sha: str,
                   secret_values: list[str] | None = None) -> tuple[bool, list[str]]:
    """Deterministic acceptance: descends from expected base, has commits,
    touches no forbidden paths, leaks no secrets."""
    findings = []
    if head_sha == base_sha:
        findings.append("no commits produced (head == base)")
        return False, findings
    if not gitops.is_ancestor(repo, base_sha, head_sha):
        findings.append(f"head {head_sha[:10]} does not descend from expected base {base_sha[:10]}")
        return False, findings
    files = gitops.changed_files(repo, base_sha, head_sha)
    bad = forbidden_changed(files)
    if bad:
        findings.append(f"forbidden paths changed: {bad}")
    diff = gitops.diff_text(repo, base_sha, head_sha, max_lines=20000)
    leaks = secrets_in_text(diff, secret_values)
    if leaks:
        findings.append(f"possible secrets in diff: {leaks}")
    return (not findings), findings


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-tests", action="store_true")
    ap.add_argument("--cwd", required=True)
    ap.add_argument("--result", required=True)
    ap.add_argument("cmds", nargs="*")
    args = ap.parse_args()
    if not args.run_tests:
        ap.error("only --run-tests mode is supported")
    ok, report = repo_tests(args.cmds, args.cwd)
    common.write_atomic(Path(args.result), json.dumps(
        {"version": 1, "passed": ok, "report": report[-20000:]}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
