# engine-control

Small, durable autonomous development orchestrator for the `internet` and `ai`
repos. Plans, implements, independently reviews, validates, repairs,
checkpoints and hands off sequential roadmap steps, notifying the owner
through Telegram. Survives SSH disconnects, Claude crashes, controller
crashes, machine sleep and restarts.

## How it runs

A Windows Scheduled Task (`engine-control-tick`) runs `python control.py tick`
about once per minute. Each tick is short-lived and idempotent: single-instance
lock → consume Telegram commands → reconcile detached workers → at most one
orchestration action → exit. All state lives in `state.db` (SQLite, WAL);
`events` is an append-only ledger. Workers (Claude Code CLI or deterministic
test runs) execute detached through `worker_shim.py`, so nothing depends on an
SSH session or on the tick that launched them.

## Commands

```
python control.py doctor          # environment/config health check
python control.py init           # clone workspaces, create automation/integration
python control.py start          # START THE ROADMAP (the tick drives it after this)
python control.py status         # roadmap + step + active-run overview
python control.py pause          # stop dispatching (running workers finish)
python control.py resume         # resume dispatching
python control.py retry          # re-arm a BLOCKED step (fresh cycle)
python control.py abort          # abort the active step, halt roadmap
python control.py log            # recent event ledger
python control.py install-task   # install the once-per-minute tick task
python control.py uninstall-task
```

Telegram commands (same actions): `/status /pause /resume /retry /abort /log`.

## Telegram setup (required before the roadmap runs)

The controller uses its OWN bot — never the product discovery bot.

1. Telegram → @BotFather → `/newbot` → e.g. `engine-control-dev`.
2. `C:\projects\engine-control\.env` → `DEV_TELEGRAM_BOT_TOKEN=<token>`
3. Send any message to the new bot, then `python control.py telegram-detect-chat`
4. Add `DEV_TELEGRAM_CHAT_ID=<id>` to `.env`.

Until configured, steps sit in `WAITING_CONFIG` (nothing crashes, nothing is
lost); the tick picks credentials up automatically.

## Recovery

There is nothing to do manually. Every dispatch has a deterministic key and
on-disk evidence (`artifacts/runs/<key>/`); after any crash/sleep/reboot the
next tick adopts finished work, marks vanished workers LOST → step
INTERRUPTED → respawns without consuming an attempt. If a step is BLOCKED,
read `/status` + `/log`, then `/retry` or `/abort`. To inspect deeply:
`state.db` (tables: steps, runs, events, transitions, commits, notifications).

Manual reboot acceptance procedure: `tests/MANUAL_REBOOT_TEST.md`.

## Safety rails

- Owner repos under `C:\github` are read-only sources; automation works in
  clones under `C:\projects\automation-workspace\<repo>`.
- The controller owns `automation/integration` in each clone; workers get
  isolated worktrees + `ec/...` branches; the controller cherry-picks accepted
  commits (idempotent via `-x` provenance).
- `gitops.assert_safe` mechanically refuses controller-side git abuse: any
  push to main/master, forced pushes (incl. `+refspec`), ref-deletion pushes,
  `reset --hard`, `clean`, branch deletion/rename, forced checkout, history
  rewriting, `-C`/`--git-dir` redirection, and any mutating invocation
  (including worktree/branch/remote/config write forms) outside the
  automation roots. Push URLs of clones are additionally set to an invalid
  `DISABLED:` URL.
- Claude workers are permission-scoped to specific git verbs (status, add,
  commit, diff, log, show, rev-parse, ls-files, grep, rm) — no push, no
  branch, no remote, no worktree, no config. Acceptance additionally verifies
  base ancestry, forbidden paths, and secrets before anything is promoted.
- Worker env is stripped of `ANTHROPIC_API_KEY` etc. — subscription auth only,
  no accidental API billing. `doctor` warns if such keys exist.
- Secrets are redacted from events, notifications and logs; result diffs are
  scanned for secret shapes and forbidden paths (.env, *.db) before acceptance.
- Fable never receives raw conversation data (mechanical prompt guard +
  prompt policy).

## Model policy

planner/reviewer/diagnostic: Opus · implementer/repair: Sonnet (high effort) ·
fable: step-5/8/10 planning + final audits only (see `roadmap.yaml` `models:`).
Quota/limit hits → `WAITING_QUOTA` (persisted, notified, retried; never counts
as a failed attempt, never falls back to API billing).

## Repair ladder

implementation → repair 1 (resumes implementer session) → repair 2 (fresh
session) → independent diagnostic (Opus, read-only) → final targeted repair →
BLOCKED. Waits/interruptions never consume rungs. A BLOCKED step halts the
roadmap — later steps are never started over a failed dependency.

## Tests

```
cd tests
python test_infra.py     # 12 unit tests (lock, guard, telegram, redaction...)
python -m unittest test_flow   # 15 canary scenarios (full acceptance matrix)
```

Canary tests stub only the model (`tests/stub_worker.py`); dispatch,
supervision, git promotion and validation paths are real.
