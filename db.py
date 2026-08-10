"""SQLite state for engine-control. Single authoritative store, WAL,
synchronous=FULL. All multi-statement mutations run inside `with conn:`
transactions. events is the append-only ledger — never UPDATE/DELETE it."""
from __future__ import annotations

import json
import sqlite3

import common

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv(
  k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS steps(
  id TEXT PRIMARY KEY,
  ordinal INTEGER NOT NULL,
  title TEXT, repos TEXT,            -- json list of repo names
  state TEXT NOT NULL DEFAULT 'PENDING',
  detail TEXT NOT NULL DEFAULT '{}', -- json: task_idx, cycle, resume, tries...
  active_run_id INTEGER,
  plan_path TEXT, handoff_path TEXT,
  soak_until TEXT, retry_at TEXT,
  updated_at TEXT);
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  key TEXT UNIQUE NOT NULL,          -- deterministic idempotency key
  step_id TEXT NOT NULL, task_idx INTEGER NOT NULL DEFAULT 0,
  cycle INTEGER NOT NULL DEFAULT 0,  -- bumped by /retry so old runs stop counting
  role TEXT NOT NULL, model TEXT, effort TEXT, lane TEXT,
  status TEXT NOT NULL,              -- PREPARED|DISPATCHED|DONE|FAILED|QUOTA|LOST
  external TEXT,                     -- json: pid / task name / session uuid
  cwd TEXT, branch TEXT, base_sha TEXT,
  artifact_dir TEXT, deadline TEXT,
  created_at TEXT, ended_at TEXT, exit_code INTEGER, note TEXT);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL, kind TEXT NOT NULL,
  step_id TEXT, run_id INTEGER, payload TEXT);
CREATE TABLE IF NOT EXISTS transitions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL, step_id TEXT NOT NULL,
  from_state TEXT, to_state TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS tg_updates(
  update_id INTEGER PRIMARY KEY,
  ts TEXT, chat_id TEXT, text TEXT, status TEXT);
CREATE TABLE IF NOT EXISTS notifications(
  key TEXT PRIMARY KEY,
  ts TEXT, sent_at TEXT, attempts INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'pending',  -- pending|sent|expired
  text TEXT);
CREATE TABLE IF NOT EXISTS commits(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  step_id TEXT, repo TEXT, task_idx INTEGER,
  run_key TEXT, sha TEXT, base_sha TEXT,
  integrated_sha TEXT, integrated_at TEXT);
CREATE TABLE IF NOT EXISTS artifacts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER, kind TEXT, path TEXT, sha256 TEXT, ts TEXT);
"""


def connect(ctx: common.Ctx) -> sqlite3.Connection:
    conn = sqlite3.connect(ctx.root / "state.db", timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.executescript(SCHEMA)
    return conn


def kv_get(conn, k, default=None):
    row = conn.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return row["v"] if row else default


def kv_set(conn, k, v):
    with conn:
        conn.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))


def event(conn, ctx, kind, step_id=None, run_id=None, **payload):
    with conn:
        conn.execute(
            "INSERT INTO events(ts,kind,step_id,run_id,payload) VALUES(?,?,?,?,?)",
            (common.now(), kind, step_id, run_id,
             ctx.redact(json.dumps(payload, default=str)) if payload else None))


def transition(conn, ctx, step_id, to_state, reason=""):
    assert to_state in common.STEP_STATES, to_state
    row = conn.execute("SELECT state FROM steps WHERE id=?", (step_id,)).fetchone()
    frm = row["state"] if row else None
    if frm == to_state:
        return
    with conn:
        conn.execute("UPDATE steps SET state=?, updated_at=? WHERE id=?",
                     (to_state, common.now(), step_id))
        conn.execute(
            "INSERT INTO transitions(ts,step_id,from_state,to_state,reason) VALUES(?,?,?,?,?)",
            (common.now(), step_id, frm, to_state, ctx.redact(reason)[:500]))
    event(conn, ctx, "transition", step_id=step_id, frm=frm, to=to_state, reason=reason)


def get_step(conn, step_id):
    return conn.execute("SELECT * FROM steps WHERE id=?", (step_id,)).fetchone()


def step_detail(step_row) -> dict:
    try:
        return json.loads(step_row["detail"] or "{}")
    except ValueError:
        return {}


def set_detail(conn, step_id, detail: dict):
    with conn:
        conn.execute("UPDATE steps SET detail=?, updated_at=? WHERE id=?",
                     (json.dumps(detail), common.now(), step_id))


def get_run(conn, run_id):
    return conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()


def run_by_key(conn, key):
    return conn.execute("SELECT * FROM runs WHERE key=?", (key,)).fetchone()


def open_runs(conn):
    return conn.execute(
        "SELECT * FROM runs WHERE status IN ('PREPARED','DISPATCHED') ORDER BY id").fetchall()


def finish_run(conn, ctx, run_id, status, exit_code=None, note=""):
    with conn:
        conn.execute(
            "UPDATE runs SET status=?, ended_at=?, exit_code=?, note=? WHERE id=?",
            (status, common.now(), exit_code, ctx.redact(note)[:500], run_id))
    event(conn, ctx, "run_" + status.lower(), run_id=run_id, note=note)


def ladder_pos(conn, step_id, task_idx, cycle) -> int:
    """How many implementation/repair rungs this task has consumed in the
    current cycle. QUOTA and LOST runs never count (spec: waits and
    interruptions are not implementation failures)."""
    row = conn.execute(
        "SELECT COUNT(*) c FROM runs WHERE step_id=? AND task_idx=? AND cycle=? "
        "AND role IN ('implementer','repair') AND status IN ('DONE','FAILED')",
        (step_id, task_idx, cycle)).fetchone()
    return row["c"]


def next_seq(conn, step_id, task_idx, role) -> int:
    row = conn.execute(
        "SELECT COUNT(*) c FROM runs WHERE step_id=? AND task_idx=? AND role=?",
        (step_id, task_idx, role)).fetchone()
    return row["c"] + 1
