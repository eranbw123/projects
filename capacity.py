"""Automatic Claude plan/capacity detection, live utilization telemetry and
the concurrency policy that turns both into a dispatch target.

Billing/privacy rules:
- Entitlement evidence comes ONLY from `claude auth status` (sanitized CLI
  output) and a WHITELIST of non-secret fields in .claude.json (oauthAccount
  profile, cachedUsageUtilization). `.credentials.json` is NEVER parsed —
  only its mtime serves as freshness evidence.
- No token, key, or credential file content ever enters the DB, events, logs
  or prompts.
- plan_mode defaults to AUTO. /profile max5|max20|pro is a debug override
  only; /profile auto clears it.

Tier model (separate concepts, per commissioning spec):
  auth_method   subscription | api_key | none | unknown
  family        max | pro | other | unknown
  tier          max20 | max5 | max_unknown | pro | unknown
  confidence    confirmed | probable | conflict | conservative_fallback
Operational capacity is always taken from ENVELOPES[tier] (max_unknown and
conflict fall back to the conservative envelope of the smallest claim).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import common
import db

# ---------------------------------------------------------------- envelopes

ENVELOPES = {
    # normal/burst: concurrent Claude workers (probes and local test runs
    # excluded). model caps bound expensive models. background: max
    # background-lane steps. speculative reserved for future use.
    "max20":       dict(normal=4, burst=5, opus=2, fable=1, background=2, speculative=1),
    "max5":        dict(normal=2, burst=3, opus=1, fable=1, background=1, speculative=0),
    "max_unknown": dict(normal=2, burst=3, opus=1, fable=1, background=1, speculative=0),
    "pro":         dict(normal=1, burst=2, opus=1, fable=1, background=0, speculative=0),
    "unknown":     dict(normal=1, burst=1, opus=1, fable=1, background=0, speculative=0),
}

TIER_RANK = {"unknown": 0, "pro": 1, "max_unknown": 2, "max5": 2, "max20": 3}

AUTH_TTL_MIN = 30          # reuse a good `claude auth status` result this long
DETECT_TTL_MIN = 10        # skip full re-detection when nothing changed
USAGE_STALE_MIN = 45       # utilization older than this is not trusted
PROBE_MIN_INTERVAL_MIN = 10
PROBE_EPISODE_CAP = 3      # probes per unresolved-detection episode
PROBE_EPISODE_HOLD_H = 6   # after cap, wait this long before a new episode

_EXACT_TIER_RE = re.compile(r"max[_-]?(5|20)x", re.I)


def now_dt():
    return datetime.now(timezone.utc)


def _age_min(iso: str | None) -> float:
    if not iso:
        return 1e9
    try:
        return (now_dt() - common.parse_iso(iso)).total_seconds() / 60.0
    except ValueError:
        return 1e9


# ------------------------------------------------------------- config paths

def claude_json_path(ctx) -> Path:
    override = ctx.getenv("EC_CLAUDE_JSON")
    if override:
        return Path(override)
    cfg_dir = ctx.getenv("CLAUDE_CONFIG_DIR")
    if cfg_dir and (Path(cfg_dir) / ".claude.json").exists():
        return Path(cfg_dir) / ".claude.json"
    return Path.home() / ".claude.json"


def credentials_path(ctx) -> Path:
    override = ctx.getenv("EC_CREDENTIALS_FILE")  # tests: isolate from machine
    if override:
        return Path(override)
    cfg_dir = ctx.getenv("CLAUDE_CONFIG_DIR")
    base = Path(cfg_dir) if cfg_dir else Path.home() / ".claude"
    return base / ".credentials.json"


def _mtime_iso(p: Path) -> str | None:
    try:
        return datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc
                                      ).isoformat(timespec="microseconds")
    except OSError:
        return None


def file_signature(ctx) -> str:
    """Cheap change detector over the evidence files."""
    parts = []
    for p in (claude_json_path(ctx), credentials_path(ctx)):
        parts.append(_mtime_iso(p) or "absent")
    return "|".join(parts)


# ---------------------------------------------------------------- evidence

BILLING_OVERRIDE_ENV = [
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_API_KEY",
    "ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY", "AWS_BEARER_TOKEN_BEDROCK",
]


def auth_status(ctx, conn, force=False) -> dict:
    """Sanitized `claude auth status`. Cached in kv (no secrets in output).
    Test override: EC_AUTH_STATUS_FILE points at a fixture JSON."""
    fixture = ctx.getenv("EC_AUTH_STATUS_FILE")
    if fixture:
        obj = common.read_json(Path(fixture)) or {}
        obj.setdefault("checked_at", common.now())
        return obj
    cached = json.loads(db.kv_get(conn, "auth_status_cache") or "{}")
    if not force and cached and _age_min(cached.get("checked_at")) < AUTH_TTL_MIN:
        return cached
    import claude_runner as cr
    exe = cr.resolve_claude(ctx)
    if not exe:
        return cached or {"loggedIn": None, "error": "claude CLI not found",
                          "checked_at": common.now()}
    prefix = ["cmd", "/d", "/c"] if exe.lower().endswith((".cmd", ".bat")) else []
    try:
        p = subprocess.run([*prefix, exe, "auth", "status"], capture_output=True,
                           text=True, timeout=30)
        obj = json.loads(p.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired) as e:
        # transient CLI failure: keep the last good answer rather than
        # flapping the whole system into WAITING_AUTH
        if cached:
            return cached
        return {"loggedIn": None, "error": str(e)[:120], "checked_at": common.now()}
    keep = {k: obj.get(k) for k in
            ("loggedIn", "authMethod", "apiProvider", "subscriptionType")}
    keep["checked_at"] = common.now()
    db.kv_set(conn, "auth_status_cache", json.dumps(keep))
    return keep


def oauth_profile(ctx) -> dict:
    """Whitelisted non-secret entitlement fields from .claude.json."""
    p = claude_json_path(ctx)
    obj = common.read_json(p)
    if not isinstance(obj, dict):
        return {"present": False, "malformed": obj is not None}
    oa = obj.get("oauthAccount") or {}
    fetched = oa.get("profileFetchedAt")
    if isinstance(fetched, (int, float)) and fetched > 1e12:
        fetched_iso = datetime.fromtimestamp(fetched / 1000, tz=timezone.utc
                                             ).isoformat(timespec="seconds")
    else:
        fetched_iso = _mtime_iso(p)
    return {
        "present": bool(oa),
        "organizationType": oa.get("organizationType"),
        "organizationRateLimitTier": oa.get("organizationRateLimitTier"),
        "userRateLimitTier": oa.get("userRateLimitTier"),
        "billingType": oa.get("billingType"),
        "fetched_at": fetched_iso,
    }


def _tier_from_rate_limit(value) -> str | None:
    if not isinstance(value, str):
        return None
    m = _EXACT_TIER_RE.search(value)
    if m:
        return "max5" if m.group(1) == "5" else "max20"
    return None


def _family_from(org_type, sub_type) -> str:
    if isinstance(org_type, str):
        if "max" in org_type.lower():
            return "max"
        if "pro" in org_type.lower():
            return "pro"
    if isinstance(sub_type, str):
        s = sub_type.lower()
        if s == "max":
            return "max"
        if s == "pro":
            return "pro"
        if s:
            return "other"
    return "unknown"


# --------------------------------------------------------------- detection

def classify(ctx, auth: dict, profile: dict) -> dict:
    """Pure classification of evidence -> capacity view (no side effects)."""
    creds_seen = _mtime_iso(credentials_path(ctx))
    # authentication mode
    if auth.get("loggedIn") is False:
        auth_mode = "none"
    elif auth.get("loggedIn") and auth.get("authMethod") == "claude.ai" \
            and auth.get("apiProvider") in ("firstParty", None):
        auth_mode = "subscription"
    elif auth.get("loggedIn"):
        auth_mode = "api_key" if "api" in str(auth.get("authMethod", "")).lower() \
            else "unknown"
    else:
        auth_mode = "unknown"
    overrides = [k for k in BILLING_OVERRIDE_ENV if os.environ.get(k)]

    claims = []  # (tier, source, freshness_iso, exact?)
    prof_family = _family_from(profile.get("organizationType"), None)
    if profile.get("present"):
        for field in ("organizationRateLimitTier", "userRateLimitTier"):
            t = _tier_from_rate_limit(profile.get(field))
            if t:
                claims.append((t, f"oauth_profile.{field}",
                               profile.get("fetched_at"), True))
        if not claims and prof_family == "max":
            claims.append(("max_unknown", "oauth_profile", profile.get("fetched_at"), False))
        elif not claims and prof_family == "pro":
            claims.append(("pro", "oauth_profile", profile.get("fetched_at"), False))

    sub = str(auth.get("subscriptionType") or "").lower()
    auth_family = _family_from(None, auth.get("subscriptionType"))
    if sub == "pro":
        claims.append(("pro", "auth_status", creds_seen or auth.get("checked_at"), False))
    elif sub == "max":
        claims.append(("max_unknown", "auth_status", creds_seen or auth.get("checked_at"), False))

    family = prof_family if prof_family != "unknown" else auth_family

    if not claims:
        tier, confidence = "unknown", "conservative_fallback"
    else:
        tiers = {c[0] for c in claims}
        # max_unknown agrees with any max tier; collapse before conflict test
        effective = {t for t in tiers if t != "max_unknown"} or {"max_unknown"}
        if len(effective) == 1:
            tier = effective.pop()
            confidence = "confirmed" if any(c[3] for c in claims) or tier in ("pro",) \
                else "probable"
        else:
            # genuine conflict (e.g. profile says max20, credentials say pro)
            claims.sort(key=lambda c: c[2] or "", reverse=True)
            freshest = claims[0]
            others = claims[1:]
            margin_min = (_age_min(others[0][2]) - _age_min(freshest[2])) if others else 0
            if freshest[3] and margin_min > 30:
                # exact, clearly fresher evidence wins; keep probing to converge
                tier, confidence = freshest[0], "probable"
            else:
                tier = min((c[0] for c in claims), key=lambda t: TIER_RANK[t])
                confidence = "conflict"
    contested = len({c[0] for c in claims if c[0] != "max_unknown"} or
                    {"max_unknown"}) > 1
    return {
        "auth_mode": auth_mode, "family": family, "tier": tier,
        "confidence": confidence, "contested": contested,
        "billing_overrides": overrides,
        "claims": [{"tier": c[0], "source": c[1], "fresh": c[2], "exact": c[3]}
                   for c in claims],
        "auth_error": auth.get("error"),
    }


def effective_tier(conn, view: dict) -> str:
    override = db.kv_get(conn, "plan_override")
    if override and override in ENVELOPES:
        return override
    return view.get("tier", "unknown")


def detect(ctx, conn, force=False, trigger="tick") -> dict:
    """Re-evaluate capacity when evidence changed / TTL expired / forced.
    Persists transitions to plan_detections + kv, returns the current view."""
    state = json.loads(db.kv_get(conn, "capacity_state") or "{}")
    sig = file_signature(ctx)
    if (not force and state and state.get("sig") == sig
            and _age_min(state.get("detected_at")) < DETECT_TTL_MIN
            and state.get("confidence") not in ("conflict", "conservative_fallback")):
        return state
    auth = auth_status(ctx, conn, force=force or state.get("sig") != sig)
    profile = oauth_profile(ctx)
    view = classify(ctx, auth, profile)
    view["sig"] = sig
    view["detected_at"] = common.now()
    view["trigger"] = trigger
    prev_tier = state.get("tier")
    view["prev_tier"] = prev_tier
    changed = (prev_tier != view["tier"]
               or state.get("confidence") != view["confidence"]
               or state.get("auth_mode") != view["auth_mode"])
    db.kv_set(conn, "capacity_state", json.dumps(view))
    if changed or not state:
        with conn:
            conn.execute(
                "INSERT INTO plan_detections(ts,tier,confidence,auth_mode,family,"
                "prev_tier,trigger,evidence) VALUES(?,?,?,?,?,?,?,?)",
                (view["detected_at"], view["tier"], view["confidence"],
                 view["auth_mode"], view["family"], prev_tier, trigger,
                 json.dumps(view["claims"])[:1500]))
        db.event(conn, ctx, "plan_detected", tier=view["tier"],
                 confidence=view["confidence"], auth=view["auth_mode"],
                 prev=prev_tier, trigger=trigger)
    maybe_dispatch_probe(ctx, conn, view)
    return view


def auth_ok(view: dict) -> bool:
    return view.get("auth_mode") == "subscription" and not view.get("billing_overrides")


# ------------------------------------------------------------ refresh probe

def maybe_dispatch_probe(ctx, conn, view) -> bool:
    """Small bounded haiku request while detection is unresolved; strictly
    rate limited. Measured reality (real smoke, CLI 2.1.226): headless -p
    sessions refresh NEITHER the entitlement profile nor the usage cache, so
    the probe's guaranteed value is only quota evidence (a limited probe =
    hard capacity information). Cache freshness arrives passively instead:
    credentials/token refresh updates the family-level subscriptionType, and
    any interactive session refreshes profile + usage. Stale telemetry
    degrades decisions to the conservative 'unknown' band, and CLI-reported
    quota hits are the hard backstop — the probe is never relied upon."""
    if ctx.getenv("EC_DISABLE_PROBE") == "1":
        return False
    if view.get("confidence") not in ("conflict", "conservative_fallback") \
            and view.get("tier") not in ("max_unknown", "unknown") \
            and not view.get("contested"):
        return False
    if not auth_ok(view):
        return False
    hold = db.kv_get(conn, "quota_hold_until")
    if hold and not common.is_past(hold):
        return False
    ep = json.loads(db.kv_get(conn, "probe_episode") or "{}")
    if ep.get("sig") != view.get("sig"):
        ep = {"sig": view.get("sig"), "count": 0, "last": None}
    if ep["count"] >= PROBE_EPISODE_CAP:
        if _age_min(ep.get("last")) < PROBE_EPISODE_HOLD_H * 60:
            return False
        ep = {"sig": view.get("sig"), "count": 0, "last": None}
    if ep.get("last") and _age_min(ep["last"]) < PROBE_MIN_INTERVAL_MIN:
        return False
    open_probe = conn.execute(
        "SELECT 1 FROM runs WHERE role='probe' AND status IN ('PREPARED','DISPATCHED')"
    ).fetchone()
    if open_probe:
        return False
    import claude_runner as cr
    seq = conn.execute("SELECT COUNT(*) c FROM runs WHERE role='probe'").fetchone()["c"] + 1
    key = f"_capacity.c0.t0.probe.{seq}"
    try:
        cr.build_job(ctx, key, "probe", "Reply with exactly: ok", str(ctx.root),
                     "haiku", None)
        with conn:
            conn.execute(
                "INSERT OR IGNORE INTO runs(key,step_id,task_idx,cycle,role,model,"
                "effort,lane,status,cwd,artifact_dir,deadline,created_at) "
                "VALUES(?, '_capacity', 0, 0, 'probe', 'haiku', NULL, 'direct',"
                "'PREPARED', ?, ?, ?, ?)",
                (key, str(ctx.root), str(cr.art_dir(ctx, key)),
                 common.iso_in(8 * 60), common.now()))
        run = db.run_by_key(conn, key)
        if run and run["status"] == "PREPARED":
            import control
            control.launch(ctx, conn, run)
        ep["count"] += 1
        ep["last"] = common.now()
        db.kv_set(conn, "probe_episode", json.dumps(ep))
        db.event(conn, ctx, "capacity_probe", key=key, count=ep["count"])
        return True
    except Exception as e:
        ctx.log(f"probe dispatch failed: {e}")
        return False


def on_probe_result(ctx, conn, run, status):
    """Called by reconcile when a probe run finishes."""
    if status == "QUOTA":
        usage = current_usage(conn)
        until = None
        if usage and usage.get("reset5"):
            until = usage["reset5"]
        db.kv_set(conn, "quota_hold_until", until or common.iso_in(30 * 60))
        db.event(conn, ctx, "probe_quota", until=until)
    else:
        detect(ctx, conn, force=True, trigger="probe_done")


# ----------------------------------------------------------- live telemetry

def telemetry_dir(ctx) -> Path:
    d = ctx.root / "telemetry" / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def read_usage_cache(ctx) -> dict | None:
    """5h/7d utilization that Claude Code itself caches in .claude.json."""
    obj = common.read_json(claude_json_path(ctx))
    if not isinstance(obj, dict):
        return None
    cu = obj.get("cachedUsageUtilization") or {}
    u = cu.get("utilization") or {}
    fetched = cu.get("fetchedAtMs")
    if not u or not isinstance(fetched, (int, float)):
        return None
    fh, sd = u.get("five_hour") or {}, u.get("seven_day") or {}
    return {
        "session_id": None, "source": "cli_cache",
        "fetched_at": datetime.fromtimestamp(fetched / 1000, tz=timezone.utc
                                             ).isoformat(timespec="seconds"),
        "pct5": fh.get("utilization"), "reset5": fh.get("resets_at"),
        "pct7": sd.get("utilization"), "reset7": sd.get("resets_at"),
        "model": None,
    }


def ingest_telemetry(ctx, conn) -> int:
    """Pull statusline session files + the CLI usage cache into SQLite.
    Controller is the only writer to control.db; sessions write only their
    own JSON file (atomic replace)."""
    n = 0
    rows = []
    cache = read_usage_cache(ctx)
    if cache:
        rows.append(cache)
    d = telemetry_dir(ctx)
    try:
        files = list(d.glob("*.json"))
    except OSError:
        files = []
    for f in files:
        obj = common.read_json(f)
        if not isinstance(obj, dict):
            continue  # malformed/partial write: ignore safely
        rows.append({
            "session_id": str(obj.get("session_id") or f.stem)[:64],
            "source": "statusline",
            "fetched_at": obj.get("ts") or _mtime_iso(f),
            "pct5": obj.get("pct5"), "reset5": obj.get("reset5"),
            "pct7": obj.get("pct7"), "reset7": obj.get("reset7"),
            "model": obj.get("model"),
        })
    for r in rows:
        if r.get("pct5") is None and r.get("pct7") is None:
            continue
        dup = conn.execute(
            "SELECT 1 FROM telemetry WHERE fetched_at=? AND source=? "
            "AND IFNULL(session_id,'')=IFNULL(?,'')",
            (r["fetched_at"], r["source"], r["session_id"])).fetchone()
        if dup:
            continue
        with conn:
            conn.execute(
                "INSERT INTO telemetry(ts,session_id,source,fetched_at,pct5,"
                "reset5,pct7,reset7,model) VALUES(?,?,?,?,?,?,?,?,?)",
                (common.now(), r["session_id"], r["source"], r["fetched_at"],
                 r["pct5"], r["reset5"], r["pct7"], r["reset7"], r["model"]))
        n += 1
    if n:
        with conn:
            conn.execute("DELETE FROM telemetry WHERE ts < ?",
                         (common.iso_in(-30 * 86400),))
    return n


def current_usage(conn) -> dict | None:
    """Freshest utilization record; stale records are ignored for decisions
    (returned with stale=True so status can still show them)."""
    row = conn.execute(
        "SELECT * FROM telemetry ORDER BY fetched_at DESC LIMIT 1").fetchone()
    if not row:
        return None
    u = dict(row)
    u["age_min"] = _age_min(row["fetched_at"])
    u["stale"] = u["age_min"] > USAGE_STALE_MIN
    return u


# ------------------------------------------------------------------ policy

def pressure_level(usage: dict | None) -> str:
    """plenty | moderate | high | very_high | critical (quota) | unknown"""
    if usage is None or usage.get("stale"):
        return "unknown"
    p5 = usage.get("pct5") or 0
    p7 = usage.get("pct7") or 0
    def band5(p):
        return 0 if p < 50 else 1 if p < 70 else 2 if p < 85 else 3 if p < 95 else 4
    def band7(p):
        return 0 if p < 70 else 1 if p < 85 else 2 if p < 92 else 3 if p < 97 else 4
    b = max(band5(p5), band7(p7))
    return ["plenty", "moderate", "high", "very_high", "critical"][b]


# role classes for pressure gating: finishing work is protected first
ROLE_CLASS = {
    "test": "free", "probe": "probe",
    "reviewer": "finish", "repair": "finish", "diagnostic": "finish",
    "audit": "finish",
    "implementer": "build", "planner": "plan",
}
# classes allowed per pressure level (free/local always allowed)
ALLOWED = {
    "plenty":    {"finish", "build", "plan", "start", "background"},
    "moderate":  {"finish", "build", "plan", "start"},
    "unknown":   {"finish", "build", "plan", "start"},
    "high":      {"finish", "build", "plan"},
    "very_high": {"finish"},
    "critical":  set(),
}


def governor(ctx, conn, view, usage, critical_ready: int) -> dict:
    """Turn tier + utilization into this tick's dispatch policy."""
    hold = db.kv_get(conn, "quota_hold_until")
    if hold and not common.is_past(hold):
        pressure = "critical"
    else:
        if hold:
            db.kv_set(conn, "quota_hold_until", "")
        pressure = pressure_level(usage)
    tier = effective_tier(conn, view)
    env = ENVELOPES.get(tier, ENVELOPES["unknown"])
    pace = db.kv_get(conn, "pace", "auto")
    target = env["normal"]
    if pressure == "plenty" and critical_ready > env["normal"] and pace != "economy":
        target = env["burst"]
    elif pressure == "high":
        target = max(1, env["normal"] - 1)
    elif pressure == "very_high":
        target = 1
    elif pressure == "critical":
        target = 0
    if pace == "economy":
        target = min(target, max(1, env["normal"] - 1))
    elif pace == "sprint" and pressure in ("plenty", "moderate"):
        target = env["burst"]
    active = claude_active(conn)
    return {
        "tier": tier, "confidence": view.get("confidence"),
        "pressure": pressure, "target": target, "active": active,
        "allowed": ALLOWED[pressure], "env": env, "pace": pace,
        "usage": usage, "override": db.kv_get(conn, "plan_override") or None,
    }


def claude_active(conn) -> int:
    return conn.execute(
        "SELECT COUNT(*) c FROM runs WHERE status IN ('PREPARED','DISPATCHED') "
        "AND role NOT IN ('test','probe')").fetchone()["c"]


def model_active(conn, model) -> int:
    return conn.execute(
        "SELECT COUNT(*) c FROM runs WHERE status IN ('PREPARED','DISPATCHED') "
        "AND model=? AND role NOT IN ('test','probe')", (model,)).fetchone()["c"]


def local_tests_active(conn) -> int:
    return conn.execute(
        "SELECT COUNT(*) c FROM runs WHERE status IN ('PREPARED','DISPATCHED') "
        "AND role='test'").fetchone()["c"]


def can_dispatch(ctx, conn, gov, role, model, new_step=False, background=False) -> bool:
    """Single gate every dispatch decision goes through."""
    klass = ROLE_CLASS.get(role, "build")
    if klass == "free":
        return local_tests_active(conn) < int(ctx.getenv("EC_LOCAL_TEST_CAP", "2"))
    if klass == "probe":
        return True
    if klass not in gov["allowed"]:
        return False
    if new_step and "start" not in gov["allowed"]:
        return False
    if background and "background" not in gov["allowed"]:
        return False
    if gov["active"] >= gov["target"] and klass in ("build", "plan"):
        return False
    if model == "fable" and model_active(conn, "fable") >= gov["env"]["fable"]:
        return False
    if model == "opus" and model_active(conn, "opus") >= gov["env"]["opus"]:
        return False
    return True
