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
python control.py workers        # live workers: liveness, heartbeat, idle reason
python control.py pause          # stop dispatching (running workers finish)
python control.py resume         # resume dispatching
python control.py retry          # resume a BLOCKED step in-place (budget refreshed)
python control.py abort          # abort the active step, halt roadmap
python control.py log            # recent event ledger
python control.py install-task   # install the once-per-minute tick task
python control.py uninstall-task
```

Telegram commands: `/status /workers /why /help /pause /resume /log /prs`,
`/retry [step-id]` (resume BLOCKED steps in-place — work kept, budget
refreshed; replans from scratch only when the worktree is gone),
`/abort [step-id]` (abort one step;
its dependents never start; independent branches continue),
`/profile auto|max5|max20|pro` (capacity override — debug only),
`/pace auto|economy|balanced|sprint`. `/workers` shows each live worker's
role, elapsed time, deadline, process liveness, and heartbeat — and, when
idle, the reason nothing runs. `/why [step]` narrates a step's failure story.
Unknown slash commands answer with a `/help` pointer. Event notifications
lead with a semantic emoji (▶️ start, 📋 plan, 🔨 built, 🧪/❌ tests, 🔍
review, 🛠 repair, 📦 promoted, ☑️ task done, ✅ accepted, ⛔ blocked) and
carry ladder round / retry counts, durations, commit counts, and roadmap
progress.

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

## GitHub visibility (automatic PRs)

Every time a task passes integration tests, the controller records that
validated `automation/integration` tip and — in a dedicated tick phase
(`ghpr.pr_sync`) — pushes exactly that commit to GitHub and keeps ONE open
pull request per repo (head `automation/integration`, base `pr_base:` from
`roadmap.yaml`, falling back to the repo's default branch if that branch
disappears). The PR body is regenerated from `state.db` each push: baseline,
last validated push, and a per-step table (state, commit count, last
integration). Telegram announces PR opening and every push, with links;
`/prs` lists current PRs on demand.

Semantics worth knowing:

- Only VALIDATED work is pushed — never the live integration tip while a
  validation run is still in flight, never repair-in-progress states.
- The push target is an explicit `https://github.com/<owner>/<repo>.git` URL
  derived from the owner repo's `origin`; clones still carry the invalid
  `DISABLED:` push URL, so workers cannot push under any condition. The
  `gitops.assert_safe` guard applies unchanged (no main/master, no force, no
  deletion) — merging the PR is always a human action on GitHub.
- A merged/closed PR is left alone until the NEXT validated work lands, then
  a fresh PR opens automatically. A repo without a GitHub origin is skipped
  silently. GitHub being down events + notifies once per tip and retries
  every 10 minutes — it never pauses development and never trips the
  controller error streak.
- Requirements: GitHub CLI installed and authenticated (`gh auth login`,
  `repo` scope) — `python control.py doctor` verifies both and shows each
  repo's PR target. `EC_GH_EXE` overrides gh discovery if needed.

## GitHub visibility (automatic README + repo description)

A second tick phase (`ghdocs.docs_sync`) keeps each repo's `README.md` and
its GitHub repo description current. When validated work is published (kv
`pr_pushed` advances), a background sonnet "docs" worker runs in an isolated
worktree checked out at exactly that published tip. It updates (or bootstraps
a missing) `README.md` against what the code actually does now, and proposes
a one-line repo description. The controller then:

- accepts the commit only if the diff touches `README.md` alone and leaks no
  secrets (mechanical whitelist — a docs worker can never change code);
- cherry-picks it onto `automation/integration` (same idempotent `-x`
  provenance path as step promotion, serialized behind in-flight
  validations) and advances `pr_tip`, so the normal PR push carries it. The
  pushed tip stays test-green by construction: byte-identical code to an
  already-validated tip, README-only delta;
- PATCHes the repo description via `gh api` when it changes.

Cost/safety: one docs worker in flight globally, background capacity class
(never steals a roadmap slot), 6h per-repo cooldown (a missing README skips
the cooldown), no dispatch while paused. A rejected commit is discarded and
that tip is never retried — the next validated tip triggers a fresh attempt.
Worker or GitHub failures notify once, back off, and never pause development.

## Recovery

There is nothing to do manually. Every dispatch has a deterministic key and
on-disk evidence (`artifacts/runs/<key>/`); after any crash/sleep/reboot the
next tick adopts finished work, marks vanished workers LOST → step
INTERRUPTED → respawns without consuming repair budget. If a step is BLOCKED,
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
  `DISABLED:` URL; the controller's own GitHub pushes go through
  `gitops.push_url` (explicit URL, validated-tip-only, same guard).
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

implementation → repair rounds (first one resumes the implementer's session) →
independent diagnostic (Opus, read-only) every 3rd round → BLOCKED only when
the budget is spent. The budget counts HOW a round failed, not that it ran:
only hard failures (worker died, no commits, tests red) burn one of 4 rungs; a
round that produced real work — commits, green tests — before an independent
review found NEW gating defects is the process converging and burns nothing. A
total-rounds fuse (`EC_LADDER_TOTAL_MAX`, default 10) still terminates endless
always-something review loops. Review severity is a promotion gate: a REPAIR
verdict whose findings are all minor/info promotes with advisory notes instead
of spending a round. Waits/interruptions never consume budget. `/retry` of a
BLOCKED step resumes in-place — same cycle, plan, worktree, and commits, with
the budget refreshed; it replans from scratch only when the worktree no longer
exists. A BLOCKED step blocks only its dependents — steps on independent
branches continue; dependents are never started over a failed dependency.

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
