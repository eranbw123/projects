# PROJECT_STATE.md — `engine-control`

Updated 2026-08-10 (Step 0 complete, post-audit repairs). Imported by
CLAUDE.md. Current state only.

## Implemented
Tick-based orchestrator (Scheduled Task `engine-control-tick`, 1/min):
`control.py` (state machine + CLI) · `db.py` (SQLite WAL, append-only events)
· `claude_runner.py` (job contract, direct/task lanes, probe/adopt/kill,
quota + fable guards) · `worker_shim.py` (detached supervisor; exitcode.txt =
completion marker; idempotent vs double-fire) · `gitops.py` (assert_safe
mechanical rails incl. invocation-level classification, worktrees,
cherry-pick -x promotion) · `telegram.py` (dev-bot commands + keyed idempotent
notifications) · `validators.py` (canonical repo tests via `cmd /d /c`,
schema/secret/forbidden checks).

States: PENDING→PLANNING→IMPLEMENTING→TESTING→REVIEWING→VALIDATING→
(SOAKING)→ACCEPTED, plus WAITING_CONFIG/USER/QUOTA, INTERRUPTED, BLOCKED,
ABORTED. Ladder: impl→repair1(resume)→repair2(fresh)→diagnostic→final→
BLOCKED; waits/LOST never consume rungs. Dispatch is atomic (run row +
active_run_id + transition in one txn), keys deterministic
(`step.cCYCLE.tTASK.role.seq` → uuid5 session id); key collision on replay =
adopt, never duplicate. Multi-task steps track `done_tasks` keyed by the
validation run's own task_idx — task advancement is re-entrant (audit F1).
Handoffs: `artifacts/handoffs/<step>.json` (schema v1) — planners consume
these, not transcripts.

## Fable audit (2026-08-10) → REPAIR → repaired, matrix re-run green
Fixed: F1 crash-atomic task advancement · F2 assert_safe invocation-level
classification (worktree/branch/remote/config), -C/--git-dir ban, +refspec
ban, cwd required for mutations · F3 quota = CLI-reported only
(`cli_quota`), streak cap 6 → repair ladder · F4 orchestration errors
notify at x1/x5 and BLOCK the step at x5 (no silent loops) · F5 fable guard
matches conversation BODY shapes, not store names · F6 pre-existing
CHERRY_PICK_HEAD aborted before integrating · F7 pid probe/kill require a
python image (PID-reuse guard) · F8 workers get specific git verbs (no
push/branch/remote/worktree/config — permission-layer, not prose) · F9
commit rows recorded idempotently in both integrate branches · F11
sync_steps upserts; steps removed from roadmap → ABORTED, never wedge.

## Non-obvious decisions
- Shim+SchedTask/detached lanes over `claude --bg` (v2.1.226 has it): one
  verified probe path; Task Scheduler is the proven SSH-independent
  supervisor here. CLI `--json-schema` exists but is unused by design
  (workers write result files; uniform stub-testable contract).
- cmd.exe AutoRun on this machine breaks `shell=True` cwd AND bare .CMD
  spawns (claude.CMD!) → every shell/batch invocation goes through
  `cmd /d /c`. Regression risk if bypassed.
- No global git identity on machine → workspaces get local
  user.name=engine-control at init.
- Repair1 resumes the implementer session (`--resume <uuid5> --fork-session`);
  repair2+ fresh. Cherry-pick idempotency = `-x` provenance grep.

## Workspaces (created by `control.py init`)
`C:\projects\automation-workspace\internet` — baseline `8af6de99` (engine-lab
tip; descends from main→PR#1→PR#4). `...\ai` — baseline `8c7e08d2`
(commit-pending-export-work tip = main+resilience work). Both on
`automation/integration`, push URL DISABLED, local git identity set.

## Tests
`tests/test_infra.py` (13) + `tests/test_flow.py` (19 canary scenarios) —
full 20-item Step-0 acceptance matrix + audit regressions (multi-task crash
safety, /retry cycle, reviewer BLOCK, guard extensions). Manual reboot
procedure: `tests/MANUAL_REBOOT_TEST.md`.

## Known gaps / residual risk (accepted for Step 0)
- Telegram creds absent → WAITING_CONFIG until owner creates the dev bot.
- Worker `Bash(python:*)` could in principle write outside its worktree;
  git verbs are permission-scoped, the rest is gated by acceptance checks +
  independent review. Full sandboxing deferred.
- Repair-after-cherry-conflict expects the repair worker to rebase onto
  integration; canary verifies entry into REPAIRING only.
- step-10's independent fable audit uses the `audit` role prompt; controller
  wiring for it lands when step-10 is reached. WAITING_USER has no automatic
  entry (reserved for future /pause-at-step semantics). Deadline-kill path
  unit-logic only, not canary-timed.

## Start the roadmap
`python control.py start` (after Telegram .env). Everything else is the tick.
