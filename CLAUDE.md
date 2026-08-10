# CLAUDE.md — `engine-control`

Development control plane orchestrating autonomous roadmap work on the
`internet` and `ai` repos.

@PROJECT_STATE.md

## Work from maintained context

CLAUDE.md + PROJECT_STATE.md are the authoritative starting context.

- Do not rediscover, map, or broadly scan the repo; read only what the task needs.
- Trust PROJECT_STATE.md; verify only code relevant to the current task.
- Update PROJECT_STATE.md after meaningful changes; keep it under 500 words.

## Core constraints

- Python 3.14 on Windows. Stdlib only, plus PyYAML solely for `roadmap.yaml`.
- SQLite `state.db` is the single authoritative store; `events` is append-only.
- No ORM, no workflow/multi-agent framework, no async rewrite, no new deps.
- Owner repos under `C:\github` are READ-ONLY sources. All mutation happens in
  clones under `C:\projects\automation-workspace\`. Never weaken
  `gitops.assert_safe`.
- Never touch main/master anywhere; integration branch is `automation/integration`.
- Secrets live in `.env` (gitignored); everything user-visible passes
  `ctx.redact`. Fable prompts must never contain raw conversation data.
- The tick must stay short-lived, idempotent, and safe to overlap (lock) —
  never turn the controller into a long-running process.
- Every dispatch needs a deterministic key + atomic run-row/stage transition
  (see `control.dispatch`); recovery relies on it.
- `subprocess` shell commands must use `["cmd","/d","/c",...]` — this machine
  has a cmd AutoRun hook that breaks `shell=True` cwd (found in Step 0).

## Testing

```
cd tests && python test_infra.py && python -m unittest test_flow
```

Tests stay offline and model-free (stub worker). Never point tests at the real
`internet`/`ai` repos; they use disposable canary repos. One test creates a
real one-shot Scheduled Task (`ec-canary-*`); it cleans up after itself.

## Keep it simple

Prefer deleting code over adding. No abstractions for single call sites.
Scripts stay flat files with `__main__` + argparse.
