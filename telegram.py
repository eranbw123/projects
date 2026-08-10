"""Development-control Telegram (separate from product discovery credentials).

Env: DEV_TELEGRAM_BOT_TOKEN, DEV_TELEGRAM_CHAT_ID (engine-control/.env).
Missing credentials -> controller runs in WAITING_CONFIG, never crashes.
Notifications are idempotent (keyed); command updates dedup by update_id.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request

import common
import db

SETUP_HELP = (
    "Dev Telegram not configured. Setup:\n"
    "1) In Telegram, talk to @BotFather -> /newbot -> name it (e.g. engine-control-dev).\n"
    "2) Put the token in C:\\projects\\engine-control\\.env as DEV_TELEGRAM_BOT_TOKEN=<token>\n"
    "3) Send any message to the new bot, then run:\n"
    "   python control.py telegram-detect-chat   (prints the chat id)\n"
    "4) Add DEV_TELEGRAM_CHAT_ID=<id> to .env. Next tick picks it up.\n"
    "Use a DIFFERENT bot from the product discovery bot."
)

COMMANDS = ("/status", "/pause", "/resume", "/retry", "/abort", "/log",
            "/profile", "/pace", "/prs")


class Telegram:
    def __init__(self, token: str, chat_id: str, transport=None):
        self.token = token
        self.chat_id = str(chat_id)
        self.transport = transport or self._http

    def _http(self, method: str, params: dict) -> dict:
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        data = urllib.parse.urlencode(params).encode()
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())

    def get_updates(self, offset: int) -> list[dict]:
        resp = self.transport("getUpdates", {
            "offset": offset, "timeout": 0, "allowed_updates": '["message"]'})
        return resp.get("result", []) if resp.get("ok") else []

    def send(self, text: str) -> bool:
        try:
            resp = self.transport("sendMessage", {
                "chat_id": self.chat_id, "text": text[:4000],
                "disable_web_page_preview": "true"})
            return bool(resp.get("ok"))
        except Exception:
            return False


def from_ctx(ctx: common.Ctx, transport=None) -> Telegram | None:
    token = ctx.getenv("DEV_TELEGRAM_BOT_TOKEN")
    chat = ctx.getenv("DEV_TELEGRAM_CHAT_ID")
    if not token or not chat:
        return None
    return Telegram(token, chat, transport=transport)


def consume(ctx, conn, tg: Telegram) -> list[str]:
    """Fetch new updates, dedup by update_id, drop unauthorized chats.
    Returns list of authorized command texts, in order."""
    offset = int(db.kv_get(conn, "tg_offset", "0"))
    try:
        updates = tg.get_updates(offset + 1)
    except Exception as e:
        db.event(conn, ctx, "tg_error", err=str(e)[:200])
        return []
    cmds = []
    for u in updates:
        uid = u.get("update_id")
        if uid is None:
            continue
        msg = u.get("message") or {}
        chat_id = str((msg.get("chat") or {}).get("id", ""))
        text = (msg.get("text") or "").strip()
        with conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO tg_updates(update_id,ts,chat_id,text,status)"
                " VALUES(?,?,?,?,?)",
                (uid, common.now(), chat_id, ctx.redact(text)[:500], "new"))
            fresh = cur.rowcount == 1
            conn.execute(
                "INSERT INTO kv(k,v) VALUES('tg_offset',?)"
                " ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (str(max(offset, uid)),))
        offset = max(offset, uid)
        if not fresh:
            continue  # duplicate delivery — already processed or recorded
        if chat_id != tg.chat_id:
            db.event(conn, ctx, "tg_unauthorized", chat=chat_id, text=text[:80])
            with conn:
                conn.execute("UPDATE tg_updates SET status='unauthorized' WHERE update_id=?", (uid,))
            continue
        first = text.split()[0].split("@")[0] if text else ""
        if first in COMMANDS:
            cmds.append(text)
            with conn:
                conn.execute("UPDATE tg_updates SET status='command' WHERE update_id=?", (uid,))
        else:
            with conn:
                conn.execute("UPDATE tg_updates SET status='ignored' WHERE update_id=?", (uid,))
    return cmds


def notify(conn, ctx, key: str, text: str) -> None:
    """Queue an idempotent notification. Same key never sends twice."""
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO notifications(key,ts,status,text) VALUES(?,?,?,?)",
            (key, common.now(), "pending", ctx.redact(text)[:3900]))


def flush(ctx, conn, tg: Telegram | None, limit: int = 10) -> None:
    rows = conn.execute(
        "SELECT key,text,attempts FROM notifications WHERE status='pending'"
        " ORDER BY ts LIMIT ?", (limit,)).fetchall()
    if not rows:
        return
    if tg is None:
        return  # keep pending until WAITING_CONFIG resolves
    for r in rows:
        ok = tg.send(r["text"])
        with conn:
            if ok:
                conn.execute(
                    "UPDATE notifications SET status='sent', sent_at=?, attempts=attempts+1"
                    " WHERE key=? AND status='pending'", (common.now(), r["key"]))
            else:
                conn.execute(
                    "UPDATE notifications SET attempts=attempts+1,"
                    " status=CASE WHEN attempts>=19 THEN 'expired' ELSE 'pending' END"
                    " WHERE key=?", (r["key"],))
