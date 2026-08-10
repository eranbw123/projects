# PROJECT_STATE.md — `engine-control`

Updated 2026-08-10 (Step 0 complete). Imported by CLAUDE.md. Current state only.

## Implemented
Tick-based orchestrator (Scheduled Task `engine-control-tick`, 1/min):
`control.py` (state machine + CLI) · `db.py` (SQLite WAL, append-only events)
· `claude_runner.py` (job contract, direct/task lanes, probe/adopt/kill,
quota + fable guards) · `worker_shim.py` (detached supervisor; exitcode.txt =
completion marker; idempotent vs double-fire) · `gitops.py` (assert_safe
mechanical rails, worktrees, cherry-pick -x promotion) · `telegram.py`
(dev-bot commands + keyed idempotent notifications) · `validators.py`
(canonical repo tests via `cmd /d /c`, schema/secret/forbidden checks).

States: PENDING→PLANNING→IMPLEMENTING→TESTING→REVIEWING→VALIDATING→
(SOAKING)→ACCEPTED, plus WAITING_CONFIG/USER/QUOTA, INTERRUPTED, BLOCKED,
ABORTED. Ladder: impl→repair1(resume)→repair2(fresh)→diagnostic→final→
BLOCKED; waits/LOST never consume rungs. Dispatch is atomic (run row +
active_run_id + transition in one txn) with deterministic keys
(`step.cCYCLE.tTASK.role.seq` → uuid5 session id); recovery adopts on-disk
evidence. Handoffs: `artifacts/handoffs/<step>.json` (schema v1) — planners
consume these, not transcripts.

## Non-obvious decisions
- Chose shim+SchedTask/detached lanes over `claude --bg` (v2.1.226 has it):
  one verified probe path for all worker types; Task Scheduler is the proven
  SSH-independent supervisor on this machine. Revisit only with evidence.
- cmd.exe AutoRun on this machine breaks `shell=True` cwd → all shell runs go
  through `cmd /d /c` (validators.run_cmd). Regression risk if bypassed.
- No global git identity on machine → workspaces get local
  user.name=engine-control at init.
- Repair1 resumes the implementer session (`--resume <uuid5> --fork-session`);
  repair2+ are fresh.
- Cherry-pick idempotency = `-x` provenance line grep, not commit-message keys.

## Workspaces (created by `control.py init`)
`C:\projects\automation-workspace\internet` — baseline `8af6de99` (engine-lab
tip; descends from main→PR#1→PR#4). `...\ai` — baseline `8c7e08d2`
(commit-pending-export-work tip = main+resilience work). Both on
`automation/integration`, push URL DISABLED.

## Tests
`tests/test_infra.py` (12) + `tests/test_flow.py` (15 canary scenarios) —
full 20-item Step-0 acceptance matrix; model stubbed, supervision/git real.
Manual reboot procedure: `tests/MANUAL_REBOOT_TEST.md`.

## Known gaps
- Telegram creds absent → WAITING_CONFIG until owner creates the dev bot.
- Repair-after-cherry-conflict expects the repair worker to rebase its branch
  onto integration; untested beyond entering REPAIRING (canary stops there).
- `--json-schema` CLI flag exists but unused (worker writes result file
  instead).

## Start the roadmap
`python control.py start` (after Telegram .env). Everything else is the tick.
