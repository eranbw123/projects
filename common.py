"""Shared helpers for engine-control: paths, env, redaction, locking, schema check.

Stdlib only, except PyYAML which is imported lazily by load_yaml() for
roadmap.yaml (already installed machine-wide; no other third-party imports).
"""
from __future__ import annotations

import json
import os
import re
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Fixed namespace: run key -> deterministic claude session uuid. Never change,
# or recovery of already-dispatched sessions after a crash breaks.
EC_NS = uuid.UUID("a2c8f4e6-1b3d-4a5e-9c7f-2d4b6a8e0c1f")

CODE_DIR = Path(__file__).resolve().parent

STEP_STATES = {
    "PENDING", "PLANNING", "IMPLEMENTING", "TESTING", "REVIEWING",
    "REPAIRING", "VALIDATING", "SOAKING", "ACCEPTED",
    "WAITING_CONFIG", "WAITING_USER", "WAITING_QUOTA", "WAITING_AUTH",
    "INTERRUPTED", "BLOCKED", "ABORTED",
}
# States where the step is waiting on something external; they never consume
# an implementation attempt.
WAITING_STATES = {"WAITING_CONFIG", "WAITING_USER", "WAITING_QUOTA",
                  "WAITING_AUTH", "INTERRUPTED"}
HALT_STATES = {"BLOCKED", "ABORTED"}

SECRET_KEY_RE = re.compile(r"(TOKEN|SECRET|KEY|PASSWORD|PASSWD)", re.I)
GENERIC_SECRET_RES = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"\b\d{8,10}:[A-Za-z0-9_\-]{30,}\b"),   # telegram bot token shape
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b"),
]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s)


def iso_in(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def is_past(iso: str) -> bool:
    return datetime.now(timezone.utc) >= parse_iso(iso)


def local_str(iso: str, fmt: str = "%H:%M") -> str:
    """UTC-stored timestamp -> machine-local display (owner-facing text)."""
    try:
        return parse_iso(iso).astimezone().strftime(fmt)
    except (ValueError, TypeError):
        return str(iso or "")


def load_env_file(path: Path) -> dict:
    env = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


class Ctx:
    """Everything path/env-scoped. Code lives in CODE_DIR; mutable state lives
    under root (default C:\\projects\\engine-control, overridable with EC_ROOT
    so tests run in a sandbox)."""

    def __init__(self, root: str | Path | None = None):
        self.root = Path(root or os.environ.get("EC_ROOT", str(CODE_DIR)))
        self.art = self.root / "artifacts"
        self.logs = self.root / "logs"
        for d in (self.art, self.art / "runs", self.art / "handoffs",
                  self.art / "wt", self.logs):
            d.mkdir(parents=True, exist_ok=True)
        # Precedence: process env wins over .env file (lets tests override).
        self.env = load_env_file(self.root / ".env")
        for k, v in os.environ.items():
            if k.startswith(("EC_", "DEV_TELEGRAM")):
                self.env[k] = v
        self._secrets = [v for k, v in self.env.items()
                         if SECRET_KEY_RE.search(k) and len(v) >= 8]

    def getenv(self, key: str, default: str | None = None) -> str | None:
        return self.env.get(key, os.environ.get(key, default))

    def redact(self, text: str) -> str:
        if not isinstance(text, str):
            text = str(text)
        for s in self._secrets:
            text = text.replace(s, "[REDACTED]")
        for rx in GENERIC_SECRET_RES:
            text = rx.sub("[REDACTED]", text)
        return text

    def log(self, line: str) -> None:
        p = self.logs / "tick.log"
        try:
            if p.exists() and p.stat().st_size > 2_000_000:
                p.replace(self.logs / "tick.log.1")
            with p.open("a", encoding="utf-8") as f:
                f.write(f"{now()} {self.redact(line)}\n")
        except OSError:
            pass


def acquire_lock(root: Path, name: str = "tick.lock"):
    """Single-instance lock. OS releases the byte lock when the process dies,
    so a crashed holder can never wedge its successor."""
    import msvcrt
    f = open(root / name, "a+b")
    try:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        return f
    except OSError:
        f.close()
        return None


def release_lock(f) -> None:
    import msvcrt
    try:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass
    f.close()


def write_atomic(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def load_yaml(path: Path):
    import yaml  # machine-wide install; the only non-stdlib import
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def session_uuid(run_key: str) -> str:
    return str(uuid.uuid5(EC_NS, run_key))


def schema_errors(obj, schema, path="$") -> list[str]:
    """Minimal JSON-schema subset: type, required, properties, items, enum,
    minItems. Enough for our contracts; unknown keywords are ignored."""
    errs = []
    t = schema.get("type")
    if t:
        ok = {
            "object": lambda o: isinstance(o, dict),
            "array": lambda o: isinstance(o, list),
            "string": lambda o: isinstance(o, str),
            "integer": lambda o: isinstance(o, int) and not isinstance(o, bool),
            "number": lambda o: isinstance(o, (int, float)) and not isinstance(o, bool),
            "boolean": lambda o: isinstance(o, bool),
        }.get(t, lambda o: True)(obj)
        if not ok:
            return [f"{path}: expected {t}, got {type(obj).__name__}"]
    if "enum" in schema and obj not in schema["enum"]:
        errs.append(f"{path}: {obj!r} not in {schema['enum']}")
    if isinstance(obj, dict):
        for req in schema.get("required", []):
            if req not in obj:
                errs.append(f"{path}: missing required '{req}'")
        for k, sub in schema.get("properties", {}).items():
            if k in obj:
                errs.extend(schema_errors(obj[k], sub, f"{path}.{k}"))
    if isinstance(obj, list):
        if "minItems" in schema and len(obj) < schema["minItems"]:
            errs.append(f"{path}: fewer than {schema['minItems']} items")
        if "items" in schema:
            for i, it in enumerate(obj):
                errs.extend(schema_errors(it, schema["items"], f"{path}[{i}]"))
    return errs


def load_schema(name: str) -> dict:
    return json.loads((CODE_DIR / "schemas" / name).read_text(encoding="utf-8"))


def tail(text: str | None, lines: int = 60) -> str:
    # None-tolerant on purpose: callers feed it subprocess output and JSON
    # fields, both of which can legitimately be missing (the 2026-08-10
    # controller stall was one None reaching a .splitlines()).
    return "\n".join((text or "").splitlines()[-lines:])
