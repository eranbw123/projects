"""Owner control panel for the deployed news/discovery appliance.

`/news` on the dev bot (and `python control.py news ...`) gives the owner
visibility, manual runs and guarded configuration of the PRODUCT discovery
engine deployed at C:\\github\\internet. The dev bot is the control panel; the
product bot stays the delivery channel (it has no live listener — commands
sent there would wait for the half-hourly feedback drain).

Guardrails:
- Mutations are whitelisted: runtime keys in the product's gitignored .env
  plus per-interest `min_score` bars in the owner's interests.json. Nothing
  else is writable and no git operation exists in this module.
- Every mutated file is first copied to <product>/backups/<name>-<stamp>.
- Cadence/digest-time changes re-register the Scheduled-Task triggers
  (ops/install_tasks.py --install) — triggers are derived at install time;
  purely-runtime keys change nothing but the file.
- Manual runs go through `schtasks /run` on the already-installed tasks, so
  a triggered run is byte-identical to a scheduled one (same run.cmd, same
  logging, same health counters) and the tick never blocks on a collection.
- Subprocess calls are bounded by timeouts; failures come back as chat text.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

TASKS = {
    "stocks": "internet-discovery-collect-stocks",
    "web": "internet-discovery-collect-web",
    "youtube": "internet-discovery-collect-youtube",
    "digest": "internet-discovery-digest",
    "feedback": "internet-discovery-feedback",
    "health": "internet-discovery-health",
}
COLLECTORS = ("stocks", "web", "youtube")

HELP = (
    "/news (or /news status) — appliance health, tasks, pending sends\n"
    "/news run stocks|web|youtube|all — collect now\n"
    "/news run digest — send pending discovery items now\n"
    "/news config — current settings + notify bars\n"
    "/news set digest_time HH:MM\n"
    "/news set digest_max N          (items per digest)\n"
    "/news set stocks|web|youtube <every: 45m|2h|1h30m>\n"
    "/news set max_scores N          (LLM calls per cycle)\n"
    "/news set bar <interest-key> <0.50-0.99>  (lower = more items)\n"
    "/news help — this cheat sheet"
)


class NewsError(Exception):
    pass


def _root(ctx) -> Path:
    return Path(ctx.getenv("EC_NEWS_ROOT", r"C:\github\internet"))


def _run(args, cwd=None, timeout=90):
    p = subprocess.run([str(a) for a in args], cwd=cwd, capture_output=True,
                       text=True, errors="replace", timeout=timeout)
    return p.returncode, (p.stdout or "") + ("\n" + p.stderr if p.stderr else "")


def _app(ctx, *args, timeout=90):
    return _run([sys.executable, "-X", "utf8", "-m", "app", *args],
                cwd=_root(ctx), timeout=timeout)


def _backup(ctx, path: Path) -> None:
    bdir = _root(ctx) / "backups"
    bdir.mkdir(exist_ok=True)
    shutil.copy2(path, bdir / f"{path.name}-{time.strftime('%Y%m%d-%H%M%S')}")


# ---- validators (value text -> canonical stored string) ----

def _hhmm(v: str) -> str:
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", v or ""):
        raise NewsError(f"'{v}' is not HH:MM")
    return v


def _interval(v: str) -> str:
    m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?|\d+", (v or "").strip())
    if not m or not (v or "").strip():
        raise NewsError(f"'{v}' is not an interval (45m, 2h, 1h30m or seconds)")
    if v.strip().isdigit():
        secs = int(v)
    else:
        secs = int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60
    if not 600 <= secs <= 86400:
        raise NewsError(f"{secs}s is outside 10m..24h")
    return str(secs)


def _int_range(lo, hi):
    def check(v):
        if not (v or "").isdigit() or not lo <= int(v) <= hi:
            raise NewsError(f"'{v}' is not an integer in {lo}..{hi}")
        return str(int(v))
    return check


# key -> (env var, validator, triggers must be re-registered?)
ENV_KEYS = {
    "digest_time": ("DISCOVERY_DIGEST_TIME", _hhmm, True),
    "digest_max": ("DISCOVERY_DIGEST_MAX", _int_range(1, 50), False),
    "stocks": ("DISCOVERY_INTERVAL_STOCKS", _interval, True),
    "web": ("DISCOVERY_INTERVAL_WEB", _interval, True),
    "youtube": ("DISCOVERY_INTERVAL_YOUTUBE", _interval, True),
    "max_scores": ("DISCOVERY_MAX_SCORES", _int_range(1, 200), False),
}

ENV_DEFAULTS = {
    "DISCOVERY_DIGEST_TIME": "08:00", "DISCOVERY_DIGEST_MAX": "10",
    "DISCOVERY_INTERVAL_STOCKS": "3600", "DISCOVERY_INTERVAL_WEB": "14400",
    "DISCOVERY_INTERVAL_YOUTUBE": "14400", "DISCOVERY_MAX_SCORES": "25",
}


def _env_read(ctx) -> dict:
    env = dict(ENV_DEFAULTS)
    p = _root(ctx) / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                if v.strip():
                    env[k.strip()] = v.strip()
    return env


def _env_write(ctx, var: str, value: str) -> None:
    p = _root(ctx) / ".env"
    lines = p.read_text(encoding="utf-8").splitlines() if p.exists() else []
    if p.exists():
        _backup(ctx, p)
    for i, line in enumerate(lines):
        if not line.strip().startswith("#") and line.split("=", 1)[0].strip() == var:
            lines[i] = f"{var}={value}"
            break
    else:
        lines.append(f"{var}={value}")
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _fmt_interval(secs: str) -> str:
    s = int(secs)
    return f"{s // 3600}h{(s % 3600) // 60 or ''}{'m' if s % 3600 else ''}" if s >= 3600 \
        else f"{s // 60}m"


# ---- commands ----

def status(ctx) -> str:
    rc, health = _app(ctx, "health", timeout=90)
    lines = ["📰 news appliance"]
    lines += [l for l in health.splitlines() if l.strip()][:12]
    if rc != 0 and "provider" not in health:
        lines.append(f"(health rc={rc})")
    rc2, out = _run([sys.executable, "-X", "utf8",
                     _root(ctx) / "ops" / "install_tasks.py", "--status"],
                    cwd=_root(ctx), timeout=60)
    if rc2 == 0:
        cur = None
        for line in out.splitlines():
            if "internet-discovery-" in line and "not installed" in line:
                lines.append("⚠ " + line.strip().replace("internet-discovery-", ""))
            elif line.rstrip().endswith(":") and "internet-discovery-" in line:
                cur = {"name": line.strip().rstrip(":").replace("internet-discovery-", "")}
            elif cur is not None and ":" in line:
                k, _, v = line.partition(":")
                cur[k.strip()] = v.strip()
                if k.strip() == "Next Run Time":
                    lines.append(f"{cur['name']}: {cur.get('Status', '?')}"
                                 f" · last rc {cur.get('Last Result', '?')}"
                                 f" · next {v.strip()}")
                    cur = None
    else:
        lines.append(f"(task status unavailable rc={rc2})")
    return "\n".join(lines)[:3900]


def run(ctx, what: str) -> str:
    what = (what or "").lower()
    names = COLLECTORS if what == "all" else (what,)
    if not all(n in TASKS for n in names):
        raise NewsError(f"unknown target '{what}' — " + "|".join([*TASKS, "all"]))
    done = []
    for n in names:
        rc, out = _run(["schtasks", "/run", "/tn", TASKS[n]], timeout=30)
        if rc != 0:
            raise NewsError(f"could not trigger {n}: {out.strip()[:200]}")
        done.append(n)
    tail = ("digest sends any pending discovery items to the news bot now"
            if what == "digest" else
            "alerts send immediately; discovery items queue for the digest")
    return f"▶️ triggered {', '.join(done)} — {tail}. /news for progress"


def config_text(ctx) -> str:
    env = _env_read(ctx)
    lines = [
        "⚙️ news config",
        f"digest {env['DISCOVERY_DIGEST_TIME']} daily"
        f" · max {env['DISCOVERY_DIGEST_MAX']} items"
        f" · ≤{env['DISCOVERY_MAX_SCORES']} LLM calls/cycle",
        "collect every: " + " · ".join(
            f"{k} {_fmt_interval(env[f'DISCOVERY_INTERVAL_{k.upper()}'])}"
            for k in COLLECTORS),
        "bars (min_score; lower = more items):",
    ]
    try:
        data = json.loads((_root(ctx) / "interests.json").read_text(encoding="utf-8"))
        default_bar = data.get("defaults", {}).get("min_score")
        for it in data.get("interests", []):
            bar = it.get("min_score", default_bar)
            lines.append(f"  {it.get('key', '?')} {bar}"
                         f" [{','.join(it.get('sources', []))}]")
    except (OSError, ValueError) as e:
        lines.append(f"  interests.json unreadable: {e}")
    lines.append("change: /news (help shows the set commands)")
    return "\n".join(lines)[:3900]


def config_set(ctx, key: str = "", *vals) -> str:
    key = (key or "").lower()
    if key == "bar":
        if len(vals) != 2:
            raise NewsError("usage: /news set bar <interest-key> <0.50-0.99>")
        return _set_bar(ctx, vals[0], vals[1])
    if key not in ENV_KEYS:
        raise NewsError(f"unknown setting '{key}'\n{HELP}")
    if len(vals) != 1:
        raise NewsError(f"usage: /news set {key} <value>")
    var, validate, reinstall = ENV_KEYS[key]
    value = validate(vals[0])
    old = _env_read(ctx)[var]
    _env_write(ctx, var, value)
    msg = f"⚙️ {key}: {old} → {value}"
    if reinstall:
        rc, out = _run([sys.executable, "-X", "utf8",
                        _root(ctx) / "ops" / "install_tasks.py", "--install"],
                       cwd=_root(ctx), timeout=120)
        msg += ("\ntask triggers re-registered" if rc == 0 else
                f"\n⚠ trigger re-registration failed (rc={rc}) — run "
                f"`python ops\\install_tasks.py --install` manually: {out.strip()[:200]}")
    else:
        msg += " (takes effect next run)"
    return msg


def _set_bar(ctx, interest_key: str, value: str) -> str:
    try:
        bar = float(value)
    except ValueError:
        raise NewsError(f"'{value}' is not a number") from None
    if not 0.50 <= bar <= 0.99:
        raise NewsError(f"{bar} is outside 0.50..0.99")
    path = _root(ctx) / "interests.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    match = [it for it in data.get("interests", []) if it.get("key") == interest_key]
    if not match:
        keys = ", ".join(it.get("key", "?") for it in data.get("interests", []))
        raise NewsError(f"no interest '{interest_key}' — have: {keys}")
    old = match[0].get("min_score", data.get("defaults", {}).get("min_score"))
    _backup(ctx, path)
    match[0]["min_score"] = bar
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")
    rc, out = _app(ctx, "init", timeout=60)
    tail = ("reloaded" if rc == 0 else
            f"⚠ reload failed (rc={rc}) — run `python -m app init` manually")
    return f"⚙️ bar {interest_key}: {old} → {bar} · {tail}"


def command(ctx, sub: str, rest) -> str:
    try:
        if sub == "" or sub == "status":
            return status(ctx)
        if sub == "run":
            return run(ctx, rest[0] if rest else "")
        if sub == "config":
            return config_text(ctx)
        if sub == "set":
            return config_set(ctx, *rest)
        if sub == "help":
            return HELP
        return f"unknown /news subcommand '{sub}'\n{HELP}"
    except NewsError as e:
        return f"news: {e}"
    except subprocess.TimeoutExpired:
        return "news: the appliance command timed out — /news to re-check"
    except Exception as e:  # command must answer, never crash the tick
        return f"news: failed — {type(e).__name__}: {str(e)[:200]}"
