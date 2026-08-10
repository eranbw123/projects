# PROJECT_STATE.md — `engine-control`

Updated 2026-08-10 (commissioning: DAG scheduler + auto plan detection +
live telemetry). Imported by CLAUDE.md. Current state only.

## Implemented
Tick orchestrator (Scheduled Task `engine-control-tick`, 1/min): `control.py`
(DAG scheduler + step state machine + CLI) · `capacity.py` (plan detection,
utilization telemetry, envelopes, governor) · `db.py` (SQLite WAL; + steps
depends_on/background/audit cols, telemetry, plan_detections) ·
`claude_runner.py` (job contract, direct/task lanes, probe/adopt/kill, quota
+ fable guards, automation settings) · `telemetry_capture.py` (statusline →
per-session JSON) · `tg_listener.py` (long-poll command latency: wakes the
tick with EC_CONSUME_TG=1; heartbeat `listener.alive` <90s makes the tick
skip its own getUpdates — no 409s; stale heartbeat = automatic 1-min
fallback; supervised by Scheduled Task `engine-control-listener` q5min +
byte lock) · `worker_shim.py` · `gitops.py` · `telegram.py` ·
`validators.py`. Probe pacing is signature-INDEPENDENT (kv probe_last_at +
24/day cap): every -p session rewrites .claude.json, so sig-keyed pacing
self-reset into a probe-per-tick loop (observed live, fixed). Contested-but-
operational detection probes hourly; unresolved every 10m.

## GitHub PR visibility (2026-08-10)
`ghpr.py` tick phase: on_validating records the test-green integration tip
(kv pr_tip); pr_sync pushes exactly that sha via `gitops.push_url` (explicit
URL — clones keep DISABLED push remotes; guard unchanged) and keeps one open
PR per repo (head automation/integration, base `pr_base:`/default; replaced
after merge when new work lands). Failures notify once per tip + 10m backoff,
never the error streak; runs while paused; idle cost 2 kv reads. gh CLI =
auth/API (EC_GH_EXE). Telegram: push/PR-open notifications, `/prs`. PR body
is DETAILED (owner ask 2026-08-10): "README updates from this branch"
section (kv docs_summary, verbatim worker changelog), steps table, and
"Every change in this branch" (git log subjects baseline..tip, cap 100,
workspace passed by _body_file; no ws → section absent).

## GitHub docs visibility (2026-08-10)
`ghdocs.py` tick phase (runs before pr_sync; skips itself while paused): when
kv pr_pushed advances past docs_tip/docs_applied, dispatches ONE background
sonnet `docs` worker (ROLES entry; ROLE_CLASS build + background=True —
never steals a slot; NO cooldown since 2026-08-10 — owner wants the README
refreshed on EVERY validated push; kv docs_applied marks the refresh's own
integration commit covered so it never re-triggers on itself) in a worktree
at the published tip. Acceptance is mechanical: README.md-only diff
+ no secrets, else reject (kv docs_reject — that tip never retried; event +
notify). Apply: cherry-pick -x onto integration (provenance-grep idempotent,
defers while repo_integration_busy), advance pr_tip (test-green by
construction: code identical to validated tip). Repo description: worker
result `description` → kv gh_desc_want → `gh api PATCH repos/{slug}` (kv
gh_desc caches; retried via gh_desc_want after backoff). Worker result `summary`
(prompted as a per-section markdown changelog of the README update) → kv
docs_summary → rendered verbatim in the PR body. Failures event + notify
once per tip + 1h backoff; never the error streak. PR body shows docs
freshness line. prompts/docs.md, schemas/docs_result.schema.json,
tests/test_docs.py (19). CI: .github/workflows/tests.yml runs test_infra +
all unit suites on windows-latest for every PR/push to main (internet + ai
repos already have their own tests.yml on GitHub).

## Worker visibility (2026-08-10)
`/workers` (tg + CLI): every open run with probe phase, elapsed/deadline,
lane, step title, and a real heartbeat — session transcript mtime under
`~/.claude/projects/*/<session-uuid>.jsonl` (appended per message even
headless; the statusline NEVER fires in -p mode, so telemetry/sessions is
fallback only) + last tool name (metadata only, never transcript content).
When idle it names the operative gate in tick order: not started / PAUSED
(kv `paused_why`: set by /pause, CLI pause, auto-pause; empty = direct kv
write, reported as maintenance) / quota hold / target 0 / no READY steps.
/status: progress header (`RUNNING · a/n accepted`), per-step lines via
`step_line()` — READY vs `waits dep+dep` for PENDING, `task i/n`,
`round r` + `f/4 hard-failed` (grant-relative), `retry k` (cycle),
time-in-state, soak/quota
timers; run lines show elapsed + activity age (PRs open per validated task,
so a step legitimately keeps running after its first PR). Future timestamps
render day-aware (`common.local_when`: "tomorrow 19:56" — a bare HH:MM on a
24h soak read as overdue, owner confusion 2026-08-10); SOAKING lines drop
ladder bits for `validated — auto-accepts <when> · no action needed`, /why
opens SOAKING with the same explanation, README documents the lifecycle
(soak_minutes/soak_check/:validated). `step_label()`
puts the same 1-based `(task i/n)` on every task-scoped notification;
single-task plans stay a bare step id.

## Notification grammar (2026-08-10)
Owner asked: every message carries its context, not just the event. Emoji
headline (▶️📋🔨🧪❌🔍🛠📦☑️✅⏳⌛⛔🔌🔁⚠) + detail lines + context tail via
`attempts_note` (round r · f/4 hard-failed · retry k), `roadmap_note` (a/n
accepted),
`dependents_of` (BLOCKED names stalls:, ACCEPTED names unblocks: among
now-READY), `accepted_text` (tasks/commits/attempts/duration). tests-FAILED
carries `_fail_hits` lines; review verdicts carry `_findings_lines`
(severity: issue); plan-ready lists task objectives; repair says what it
fixes + model/resumed; quota shows usage% + absolute retry time;
planner/reviewer/audit retries notify (were silent). "engine-control:"
prefix dropped — dedicated bot chat. /help (+/start alias) lists commands;
unknown slash cmds answer with a /help pointer (consume forwards any "/");
/why whitelisted (was handled but dropped by consume — unreachable); tg /log
renders events human (`_fmt_event`), CLI log stays raw. tests:
test_workers.py (16), test_flow TestValidatorGate pins fail-lines+round.

## Repair ladder redesign (2026-08-10)
Owner: genuine review findings must not stop the process; /retry must not
restart from scratch. Budget = 4 HARD failures per grant (`burned`; worker
failed / no commits / tests red; `burn_mark`=active_run_id dedupes capacity
re-entries). Rounds where review finds new gating defects after real work
burn nothing (`progressed=True`); fuse `EC_LADDER_TOTAL_MAX` (10, env) total
rounds per grant → BLOCKED "not converging". Diagnostic fires each 3rd round
of a grant (`diag_round` window). Reviewer severity is a promotion gate
(prompts/reviewer.md): minor/info-only REPAIR → `review_advisory` event +
promote with notes. Warm /retry (plan+worktree alive): same cycle,
`rung_floor` marks grant start (ladder_rounds = ladder_pos − rung_floor),
budget/diag reset, → REPAIRING if last_findings else TESTING; last_findings
cleared on no-tests-collected and controller-error blocks so warm retry
re-enters at tests; REVIEW BLOCK stores findings so it re-enters at repair.
Cold /retry (worktree gone): fresh cycle, replan. tests: test_flow
TestRetryWarm / TestRetryColdWhenWorktreeGone / TestReviewConvergence /
TestAdvisoryFindingsPromote / TestRunawayFuse; stub reviewer mode
`repair_minor`.

## Scheduler (commissioned 2026-08-10)
Roadmap is a dependency DAG (`depends_on`, `:validated` qualifier satisfied
from SOAKING; `background: true` lanes never take critical slots; `audit:
true` steps dispatch the fable audit role directly). Per tick: reconcile →
telemetry ingest → capacity detect → advance in-flight steps
(finishing-first: VALIDATING>REVIEWING>TESTING>REPAIRING>IMPLEMENTING>
PLANNING, critical-path weight next) → start READY steps while
claude_active < dynamic target (≤3 dispatches/tick). Every Claude dispatch
passes `capacity.can_dispatch` (pressure class, target, fable=1/opus caps,
local-test cap). Integration stays serialized per repo
(`repo_integration_busy` defers enter_validation; deferral is idempotent —
DONE runs are reprocessed next tick). Stale parallel work: base recorded per
task; a conflicted historical sha can never be rewritten from a worktree
(add-only), so promotion falls back to a merge-tree squash of the net
base..head change — object-DB merge, ONE commit carrying every source sha's
-x provenance line (idempotency grep intact), landed ff-only. NOT `git apply
--3way`: its --check exits 0 on conflicted merges (git 2.55, measured).
Still-conflicting overlap → REPAIR with a byte-identical-convergence
contract (contested regions = exact integration content, own work in
untouched regions/files — adjacent insertions do NOT merge), never reset.
This unwedged step-04 (2026-08-10: 4 hard-fails burned on repairs that
could not fix a promotion-side conflict). Same day, owner dropped step-01's
24h soak (roadmap now soak-free) and step-01 was accepted directly. A BLOCKED step
blocks only its dependents; /retry [step] resumes in-place (cold replan only
when the worktree is gone), /abort [step] prunes.
Controller error streak ≥8 → auto-pause + notify (never blocks a healthy step).

## Capacity (all AUTO; no owner action on plan changes)
plan_mode auto (kv plan_override = debug escape hatch, /profile). Evidence:
`claude auth status` JSON (cached 30m) + whitelisted `.claude.json`
oauthAccount fields (organizationRateLimitTier default_claude_max_5x/20x →
max5/max20; organizationType; freshness=profileFetchedAt). Never parses
credentials. Resolution: EXACT rate-limit-tier fields always outrank
family-level strings (subscriptionType/organizationType — their derivation
time is unknowable; a token refresh rewrites credentials without re-deriving
them, so freshness arbitration against them flaps — reviewer finding,
removed). Exact-vs-exact disagreement → conservative-min "conflict";
family-only disagreement → conservative-min; exact-vs-family disagreement →
exact wins, "probable" + contested (bounded haiku probe ≥10m apart,
≤3/episode, quota-aware, keeps trying to converge). Known one-directional
blind spot: a REAL Max→Pro downgrade stays invisible until the next
interactive/daemon profile fetch — safe because quota hits only WAIT (no
rungs, no BLOCK). Measured (smoke, CLI 2.1.226): headless -p sessions
refresh NO caches — freshness arrives passively (token refresh →
family-level subscriptionType; any interactive session → profile+usage),
stale telemetry degrades to the conservative unknown band, and CLI quota
hits are the hard backstop. Tiers →
envelopes: max20 4/5 (opus 2), max5+max_unknown 2/3, pro 1/2, unknown 1/1;
fable cap 1 everywhere. Utilization: statusline files
(`telemetry/sessions/`, atomic, whitelist-only, pruned >10d) +
cachedUsageUtilization fallback → telemetry table; stale >45m = decisions
treat as unknown. Statusline capture parses the REAL CLI ≥2.1 payload:
top-level `rate_limits.{five_hour,seven_day}.used_percentage` + epoch
`resets_at`→ISO (2026-08-10 fix: capture only knew speculative key names, so
every statusline row was dropped and /status usage sat frozen on stale
cli_cache — live check showed 6% cached vs 60% real). Headless -p workers
render NO statusline, so the owner's user settings.json statusLine now runs
telemetry_capture.py too (owner opted in; the AUTOMATION still never touches
owner settings) — interactive sessions keep usage fresh. Pressure bands gate role classes (finish-first, §17); CLI quota hit
→ global quota_hold (reset-aware only when fresh usage corroborates ≥90%).
Workers run `--setting-sources project --settings automation-settings.json`
(owner user settings never loaded/modified — their ntfy hooks would fire on
every worker otherwise) + billing/nesting env stripped.

## Auth/billing gate
Not subscription (api key/gateway env/logged out) → all steps WAITING_AUTH,
notify once, auto-recheck; `cmd_start` refuses launch. WAITING_AUTH is a
WAITING state (never consumes repair budget).

## /news — owner control panel for the deployed appliance (2026-08-10)
`newsops.py` (tg `/news` + CLI `control.py news`): status (product `health`
+ install_tasks --status condensed), manual runs via `schtasks /run` on the
installed internet-discovery-* tasks (never blocks the tick), config
show/set. Mutations whitelisted: product .env runtime keys (digest_time/
digest_max/intervals/max_scores, validated; cadence keys auto re-run
`--install` because triggers are derived at install time) + interests.json
`min_score` via `set bar <key> <v>` (0.50–0.99, `app init` reload) +
`set provider claude|chatgpt|anthropic` → DISCOVERY_PROVIDER (aliases →
canonical claude_chat/openai/anthropic; warns on missing OPENAI_/
ANTHROPIC_API_KEY and on a pinned DISCOVERY_MODEL; `config` shows
engine+model; product-side openai web search landed via the internet repo's
`chatgpt-web-search` PR — Responses-API web_search tool). Every
mutated file backed up to product backups/ first; no git ops in the module;
EC_NEWS_ROOT overrides the product root (tests). tests/test_news.py (14).
Product deployed same day: PR #9 merged, six tasks installed+verified,
digest flowing (see owner-repo commit e4485dc for the two live-install
fixes).

## Roadmap DAG (roadmap.yaml)
01,02 parallel roots · 03,04←02 (parallel) · 05←03+04 (fable planner) ·
06,07←05 (parallel) · 08←01+06+07 (fable) · 09a←01:validated (background
evidence lane) · 09←08+09a · 10←08 (fable) · 11←09+10 (audit: true, fable
whole-system audit). Cycles/unknown deps → refuse start / notify, never
silently ignore.

## Known reality of this machine (verified in commissioning)
- schtasks /tr: >260 create-fails; ~241–260 created-but-NEVER-RUNS
  (silent). spawn() enforces len ≤235 and checks /run rc.
- tempfile.mkdtemp dirs (Py3.13+) carry a restricted DACL the Task-Scheduler
  logon session cannot read → tests use harness.mkroot(); never mkdtemp for
  anything a scheduled task must read.
- Live conflict observed: profile says max20 (exact), credentials say pro
  (stale family string predating the 08-04 upgrade) — the exact tier wins by
  rule; detection stays contested + probing. Do not "fix" by hand.

## Tests
tests/: test_infra 13 · test_flow 19 · test_capacity 38 (detector matrix,
telemetry matrix, governor policy, shape-drift, finish-cap) · test_dag 15
(topology, parallel diamond, background lane incl. stale-telemetry,
upgrade/downgrade, pressure, quota hold + sustained outage, fable cap, audit
step, overlap conflict, concurrent recovery). All green 2026-08-10 after
independent Opus review REPAIR round. `scripts/smoke_real.py` = tiny
real-Claude smoke (§45), PASS.

## Start the roadmap
`python control.py start` (verifies DAG, auth, tick task, backs up state.db,
persists roadmap_run_id, notifies). Telegram absent → steps WAITING_CONFIG
until .env has DEV_TELEGRAM_*; then fully autonomous.
