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
per-session JSON) · `worker_shim.py` · `gitops.py` · `telegram.py` ·
`validators.py`.

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
task; ancestry + cherry-pick conflict → REPAIR, never reset. A BLOCKED step
blocks only its dependents; /retry [step] re-arms, /abort [step] prunes.
Controller error streak ≥8 → auto-pause + notify (never blocks a healthy step).

## Capacity (all AUTO; no owner action on plan changes)
plan_mode auto (kv plan_override = debug escape hatch, /profile). Evidence:
`claude auth status` JSON (cached 30m) + whitelisted `.claude.json`
oauthAccount fields (organizationRateLimitTier default_claude_max_5x/20x →
max5/max20; organizationType; freshness=profileFetchedAt) + credentials-file
mtime. Never parses credentials. Conflicts: fresher exact tier wins
(probable) else conservative-min (conflict); bounded haiku refresh probe
(≥10m apart, ≤3/episode, quota-aware) while unresolved/contested. Measured
(smoke, CLI 2.1.226): headless -p sessions refresh NO caches — freshness
arrives passively (token refresh → family-level subscriptionType; any
interactive session → profile+usage), stale telemetry degrades to the
conservative unknown band, and CLI quota hits are the hard backstop. Plan
transitions therefore propagate: family-level within ~a token refresh,
exact 5x/20x on next interactive/daemon profile fetch — safe both directions
(under-parallelize until evidence; overshoot caught by WAITING_QUOTA). Tiers →
envelopes: max20 4/5 (opus 2), max5+max_unknown 2/3, pro 1/2, unknown 1/1;
fable cap 1 everywhere. Utilization: cachedUsageUtilization (primary) +
automation statusline files (`telemetry/sessions/`, atomic, whitelist-only)
→ telemetry table; stale >45m = decisions treat as unknown (normal, no
burst). Pressure bands gate role classes (finish-first, §17); CLI quota hit
→ global quota_hold (reset-aware only when fresh usage corroborates ≥90%).
Workers run `--setting-sources project --settings automation-settings.json`
(owner user settings never loaded/modified — their ntfy hooks would fire on
every worker otherwise) + billing/nesting env stripped.

## Auth/billing gate
Not subscription (api key/gateway env/logged out) → all steps WAITING_AUTH,
notify once, auto-recheck; `cmd_start` refuses launch. WAITING_AUTH is a
WAITING state (never consumes attempts).

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
- Live conflict observed: profile said max20 (fresh), credentials said pro
  (stale) — detector resolves via freshness+probe; do not "fix" by hand.

## Tests
tests/: test_infra 13 · test_flow 19 · test_capacity 36 (detector §39 matrix,
telemetry §40, governor policy) · test_dag 13 (topology, parallel diamond,
background lane, upgrade/downgrade, pressure, quota hold, fable cap, audit
step, overlap conflict, concurrent recovery). All green 2026-08-10.
`scripts/smoke_real.py` = tiny real-Claude smoke (§45).

## Start the roadmap
`python control.py start` (verifies DAG, auth, tick task, backs up state.db,
persists roadmap_run_id, notifies). Telegram absent → steps WAITING_CONFIG
until .env has DEV_TELEGRAM_*; then fully autonomous.
