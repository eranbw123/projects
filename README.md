# engine-control

Small, durable autonomous development orchestrator for the `internet` and `ai`
repos. Plans, implements, independently reviews, validates, repairs,
checkpoints and hands off roadmap steps organized as a dependency DAG,
notifying the owner through Telegram. Survives SSH disconnects, Claude
crashes, controller crashes, machine sleep and restarts. Detects the Claude
subscription tier (Pro / Max 5x / Max 20x) and live 5h/7d utilization
automatically and adapts its parallelism — switching plans requires no
configuration.

## How it runs

A Windows Scheduled Task (`engine-control-tick`) runs `python control.py tick`
about once per minute. Each tick is short-lived and idempotent:
single-instance lock → consume Telegram commands → reconcile detached
workers → ingest capacity/utilization telemetry → advance every in-flight
step (finishing work first, critical path next) → start READY steps while
active Claude workers are under the dynamic concurrency target (bounded
dispatches per tick) → exit. All state lives in `state.db` (SQLite, WAL);
`events` is an append-only ledger. Workers (Claude Code CLI or deterministic
test runs) execute detached through `worker_shim.py`, so nothing depends on an
SSH session or on the tick that launched them.

## Automatic capacity detection (plan_mode = AUTO)

The controller infers the effective Claude subscription from sanitized
evidence only: `claude auth status` output plus whitelisted non-secret fields
of `.claude.json` (`organizationRateLimitTier` → max5/max20, organization
type, profile freshness). Credentials files are never parsed. Exact
rate-limit-tier evidence always outranks family-level subscription strings
(whose derivation time is unknowable); ambiguous or conflicting exact
evidence falls back conservatively, and unresolved detection triggers a tiny
bounded haiku "refresh probe" (rate-limited) until it converges. Utilization
(5-hour and 7-day windows) comes from Claude Code's own usage cache plus an
automation-only statusline hook; high pressure first stops background work,
then new implementation, and near exhaustion preserves capacity for reviews,
repairs and validation. A CLI-reported quota hit pauses dispatch until the
known reset (WAITING_QUOTA — never counted as a failure, never billed to an
API key). Upgrading Max 5x → Max 20x mid-run fills new lanes automatically;
downgrading drains gracefully without killing useful in-flight workers.
`/profile max5|max20|pro` exists only as a debugging override; `/profile
auto` (the default) clears it.

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

Telegram commands: `/status /pause /resume /log`, `/retry [step-id]` (re-arm
BLOCKED steps), `/abort [step-id]` (abort one step; its dependents never
start; independent branches continue), `/profile auto|max5|max20|pro`
(capacity override — debug only), `/pace auto|economy|balanced|sprint`.

Commands answer within a few seconds: a long-poll listener
(`tg_listener.py`, supervised by the `engine-control-listener` task) wakes
the controller the moment an update arrives. If the listener dies, its
heartbeat goes stale and commands transparently fall back to the 1-minute
tick cadence until the supervisor restarts it (≤5 min).

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

## Model policy & parallelism

planner/reviewer/diagnostic: Opus · implementer/repair: Sonnet (high effort) ·
fable: step-5/8/10 planning + the step-11 final audit only (see `roadmap.yaml`
`models:`), capped at ONE concurrent fable lane. Speed comes from parallel
independent steps (per detected capacity envelope: Max20 4–5 workers, Max5
2–3, Pro 1–2), not from bigger models. Local test runs use a separate
semaphore and never consume Claude slots. Integration is serialized per
repository; parallel worktrees record their base commit, and overlapping work
resolves through the conflict → repair path (never a reset). Quota/limit hits
→ `WAITING_QUOTA` (persisted, notified, retried; never counts as a failed
attempt, never falls back to API billing).

## Repair ladder

implementation → repair 1 (resumes implementer session) → repair 2 (fresh
session) → independent diagnostic (Opus, read-only) → final targeted repair →
BLOCKED. Waits/interruptions never consume rungs. A BLOCKED step blocks only
its dependents — steps on independent branches continue; dependents are never
started over a failed dependency.

## Tests

```
cd tests
python test_infra.py           # unit: lock, git guard, telegram, redaction...
python -m unittest test_flow   # canary scenarios (acceptance matrix)
python -m unittest test_capacity  # plan-detector + telemetry + governor matrix
python -m unittest test_dag    # DAG, capacity transitions, concurrent recovery
python ..\scripts\smoke_real.py   # tiny REAL-Claude smoke (2 haiku calls)
```

Canary tests stub only the model (`tests/stub_worker.py`); dispatch,
supervision, git promotion and validation paths are real. Test roots use
`harness.mkroot()` — never `tempfile.mkdtemp`, whose restricted Windows DACL
is unreadable from the Task Scheduler session.
