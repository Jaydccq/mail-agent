"""SQLite 存储（标准库 sqlite3 + WAL）。

只在 asyncio 主线程里调用（IMAP 线程通过 run_coroutine_threadsafe 回到主线程）。
每个公开方法是一个完整的短事务；事务里不做网络请求。
时间统一存 UTC ISO 字符串（datetime.isoformat()，带 +00:00）。
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from mailagent.models import Analysis, MailMessage, OutMessage, Triage

SCHEMA_VERSION = 1
# 运行记录保留多久（db.prune）；邮件、待办、规则、记忆、草稿动作都不清理
KEEP_RECORDS = timedelta(days=30)  # 已发出/已放弃的通知、渠道事件去重、确认卡片
KEEP_CHAT = timedelta(days=90)  # 聊天记录（只有最近几轮会进提示词）
RULE_KINDS = ("mute", "vip")
RULE_FIELDS = ("from", "domain", "subject")

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mailbox_state (
    mailbox_id TEXT PRIMARY KEY,
    uidvalidity INTEGER,
    last_uid INTEGER,
    sent_uidvalidity INTEGER,
    sent_last_uid INTEGER,
    status TEXT NOT NULL DEFAULT 'starting',   -- starting | ok | error | auth_failed
    error TEXT,
    last_sync_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    dedupe_key TEXT NOT NULL UNIQUE,
    mailbox_id TEXT NOT NULL,
    uid INTEGER NOT NULL,
    gm_msgid TEXT NOT NULL DEFAULT '',
    gm_thrid TEXT NOT NULL DEFAULT '',
    message_id TEXT NOT NULL DEFAULT '',
    in_reply_to TEXT NOT NULL DEFAULT '',
    refs TEXT NOT NULL DEFAULT '',
    from_addr TEXT NOT NULL DEFAULT '',
    from_name TEXT NOT NULL DEFAULT '',
    reply_to TEXT NOT NULL DEFAULT '',
    to_addrs TEXT NOT NULL DEFAULT '[]',
    cc_addrs TEXT NOT NULL DEFAULT '[]',
    subject TEXT NOT NULL DEFAULT '',
    date TEXT NOT NULL,
    body_text TEXT NOT NULL DEFAULT '',
    quoted_text TEXT NOT NULL DEFAULT '',
    attachments TEXT NOT NULL DEFAULT '[]',
    labels TEXT NOT NULL DEFAULT '[]',
    list_unsubscribe INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',    -- pending | done | failed
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    importance REAL,
    needs_action REAL,
    has_time REAL,
    category TEXT,
    triage_confidence REAL,
    triage_source TEXT,
    decision TEXT,                             -- notify | digest | skip
    decision_reason TEXT,
    summary TEXT,
    action TEXT,
    analysis_source TEXT,
    handled INTEGER NOT NULL DEFAULT 0,        -- 用户点了"已处理"
    notified_at TEXT,
    digested_at TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_status ON messages(status, id);
CREATE INDEX IF NOT EXISTS messages_digest ON messages(decision, digested_at);
CREATE INDEX IF NOT EXISTS messages_thread ON messages(mailbox_id, gm_thrid);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    subject, from_addr, from_name, body_text,
    content='messages', content_rowid='id', tokenize='trigram'
);
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, subject, from_addr, from_name, body_text)
    VALUES (new.id, new.subject, new.from_addr, new.from_name, new.body_text);
END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, subject, from_addr, from_name, body_text)
    VALUES ('delete', old.id, old.subject, old.from_addr, old.from_name, old.body_text);
END;

CREATE TABLE IF NOT EXISTS todos (
    id INTEGER PRIMARY KEY,
    dedupe_key TEXT UNIQUE,
    mailbox_id TEXT,
    message_id INTEGER REFERENCES messages(id),
    gm_thrid TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'deadline',    -- deadline | meeting | event | renewal | reply_by | other | task
    due_date TEXT,                            -- 本地日期 YYYY-MM-DD
    due_time TEXT,                            -- 本地 HH:MM，可空
    timezone TEXT,
    status TEXT NOT NULL DEFAULT 'open',      -- open | done | dismissed
    remind_at TEXT,                           -- UTC，下一次提醒时间；NULL = 不再提醒
    reminded_count INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'email',     -- email | chat
    closed_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS todos_due ON todos(status, remind_at);

CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY,
    dedupe_key TEXT UNIQUE,
    channel TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    payload TEXT NOT NULL,                    -- OutMessage JSON
    status TEXT NOT NULL DEFAULT 'queued',    -- queued | sent | failed
    not_before TEXT NOT NULL,                 -- UTC；静默时段推迟到这之后
    attempts INTEGER NOT NULL DEFAULT 0,
    provider_message_id TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    sent_at TEXT
);
CREATE INDEX IF NOT EXISTS outbox_ready ON outbox(status, not_before);

CREATE TABLE IF NOT EXISTS inbox_events (
    channel TEXT NOT NULL,
    event_id TEXT NOT NULL,
    received_at TEXT NOT NULL,
    PRIMARY KEY (channel, event_id)
);

CREATE TABLE IF NOT EXISTS chat_turns (
    id INTEGER PRIMARY KEY,
    channel TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    role TEXT NOT NULL,                       -- user | assistant | tool
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS chat_turns_chat ON chat_turns(channel, chat_id, id);

CREATE TABLE IF NOT EXISTS rules (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,                       -- mute（不即时通知）| vip（总是即时通知）
    field TEXT NOT NULL,                      -- from | domain | subject
    pattern TEXT NOT NULL,                    -- 小写；from/domain 精确匹配，subject 包含匹配
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE (kind, field, pattern)
);

CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY,
    text TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'chat',      -- chat（用户在聊天里明确说的）| suggested（agent 提议、用户点了保存）
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_suggestions (
    id INTEGER PRIMARY KEY,
    key TEXT NOT NULL UNIQUE,                 -- 去重键：同一个 key 只提一次，拒绝过的永远留着
    kind TEXT NOT NULL,                       -- chat | draft_signoff | draft_style | seen_sender | seen_domain | mute_domain
    text TEXT NOT NULL,                       -- 用户点保存后存进 memories 的那句话
    reason TEXT NOT NULL DEFAULT '',          -- 给用户看的依据
    status TEXT NOT NULL DEFAULT 'queued',    -- queued（等每日名额）| shown | saved | declined
    memory_id INTEGER,
    created_at TEXT NOT NULL,
    shown_at TEXT,
    decided_at TEXT
);
CREATE INDEX IF NOT EXISTS memory_suggestions_status ON memory_suggestions(status, created_at);

CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,                       -- send_reply | save_draft
    mailbox_id TEXT NOT NULL,
    message_id INTEGER REFERENCES messages(id),
    payload TEXT NOT NULL,                    -- {"to":[...],"cc":[...],"subject":..,"body":..,"in_reply_to":..,"references":..}
    payload_hash TEXT NOT NULL,
    code TEXT NOT NULL,                       -- 4 位确认码，按钮/文字确认都要匹配
    status TEXT NOT NULL DEFAULT 'awaiting_approval',
        -- awaiting_approval | approved | dispatching | succeeded | failed | unknown | cancelled | expired
    result TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pending_writes (
    id INTEGER PRIMARY KEY,
    tool TEXT NOT NULL,                       -- 聊天工具名：add_rule | remove_rule | remember | forget | complete_todo
    args TEXT NOT NULL,                       -- 工具参数 JSON；用户点确认后原样执行
    summary TEXT NOT NULL,                    -- 确认卡片上给用户看的那句话
    status TEXT NOT NULL DEFAULT 'pending',   -- pending | confirmed | cancelled
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS digests (
    slot TEXT PRIMARY KEY,                    -- 本地 "YYYY-MM-DD HH:MM"，防止同一时段重复发
    sent_at TEXT NOT NULL,
    item_count INTEGER NOT NULL
);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        raise ValueError("naive datetime 不能入库")
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def payload_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _new_id(cur: sqlite3.Cursor) -> int:
    """刚插入的行 id。INSERT（不是 OR IGNORE 跳过的）之后 lastrowid 一定有值。"""
    if cur.lastrowid is None:
        raise RuntimeError("INSERT did not produce a row id")
    return cur.lastrowid


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        c = self._conn
        c.execute("PRAGMA journal_mode = WAL")
        c.execute("PRAGMA foreign_keys = ON")
        c.execute("PRAGMA synchronous = FULL")
        c.execute("PRAGMA busy_timeout = 5000")
        version = c.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError(f"database schema {version} is newer than this program ({SCHEMA_VERSION}); upgrade mail-agent")
        c.executescript(SCHEMA)
        c.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def close(self) -> None:
        self._conn.close()

    # ---- 基础 ----

    def _tx(self) -> "_Tx":
        return _Tx(self)

    def _all(self, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, args).fetchall()]

    def _one(self, sql: str, args: tuple = ()) -> dict[str, Any] | None:
        with self._lock:
            r = self._conn.execute(sql, args).fetchone()
            return dict(r) if r else None

    def kv_get(self, key: str, default: str | None = None) -> str | None:
        r = self._one("SELECT value FROM kv WHERE key = ?", (key,))
        return r["value"] if r else default

    def kv_set(self, key: str, value: str) -> None:
        with self._tx() as c:
            c.execute("INSERT INTO kv(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))

    # ---- 邮箱游标 ----

    def get_mailbox_state(self, mailbox_id: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM mailbox_state WHERE mailbox_id = ?", (mailbox_id,))

    def set_mailbox_status(self, mailbox_id: str, status: str, error: str | None = None) -> None:
        now = iso(utcnow())
        with self._tx() as c:
            c.execute(
                """INSERT INTO mailbox_state(mailbox_id, status, error, updated_at) VALUES (?, ?, ?, ?)
                   ON CONFLICT(mailbox_id) DO UPDATE SET status = excluded.status, error = excluded.error,
                   updated_at = excluded.updated_at,
                   last_sync_at = CASE WHEN excluded.status = 'ok' THEN excluded.updated_at ELSE last_sync_at END""",
                (mailbox_id, status, error, now),
            )

    def delete_mailbox_state(self, mailbox_id: str) -> None:
        """删除邮箱时清掉游标（已收邮件、待办保留）；以后再加回来会重新建基线。"""
        with self._tx() as c:
            c.execute("DELETE FROM mailbox_state WHERE mailbox_id = ?", (mailbox_id,))

    def set_cursor(self, mailbox_id: str, uidvalidity: int, last_uid: int) -> None:
        """只在没有新邮件要入库时直接设置游标（首次运行 / UIDVALIDITY 变化）。"""
        now = iso(utcnow())
        with self._tx() as c:
            c.execute(
                """INSERT INTO mailbox_state(mailbox_id, uidvalidity, last_uid, updated_at) VALUES (?, ?, ?, ?)
                   ON CONFLICT(mailbox_id) DO UPDATE SET uidvalidity = excluded.uidvalidity,
                   last_uid = excluded.last_uid, updated_at = excluded.updated_at""",
                (mailbox_id, uidvalidity, last_uid, now),
            )

    def set_sent_cursor(self, mailbox_id: str, uidvalidity: int, last_uid: int) -> None:
        now = iso(utcnow())
        with self._tx() as c:
            c.execute(
                """INSERT INTO mailbox_state(mailbox_id, sent_uidvalidity, sent_last_uid, updated_at) VALUES (?, ?, ?, ?)
                   ON CONFLICT(mailbox_id) DO UPDATE SET sent_uidvalidity = excluded.sent_uidvalidity,
                   sent_last_uid = excluded.sent_last_uid, updated_at = excluded.updated_at""",
                (mailbox_id, uidvalidity, last_uid, now),
            )

    # ---- 邮件 ----

    def ingest_messages(self, mailbox_id: str, uidvalidity: int, msgs: list[MailMessage]) -> list[int]:
        """新邮件入库（status=pending）并在同一事务推进游标。返回新插入的 messages.id（重复的跳过）。"""
        now = iso(utcnow())
        new_ids: list[int] = []
        max_uid = max((m.uid for m in msgs), default=0)
        with self._tx() as c:
            for m in msgs:
                cur = c.execute(
                    """INSERT OR IGNORE INTO messages(
                        dedupe_key, mailbox_id, uid, gm_msgid, gm_thrid, message_id, in_reply_to, refs,
                        from_addr, from_name, reply_to, to_addrs, cc_addrs, subject, date, body_text, quoted_text,
                        attachments, labels, list_unsubscribe, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        m.dedupe_key, m.mailbox_id, m.uid, m.gm_msgid, m.gm_thrid, m.message_id, m.in_reply_to,
                        m.references, m.from_addr, m.from_name, m.reply_to, json.dumps(m.to_addrs),
                        json.dumps(m.cc_addrs), m.subject, iso(m.date), m.body_text, m.quoted_text,
                        json.dumps(m.attachments, ensure_ascii=False), json.dumps(m.labels, ensure_ascii=False),
                        int(m.list_unsubscribe), now,
                    ),
                )
                if cur.rowcount:
                    new_ids.append(_new_id(cur))
            if msgs:
                c.execute(
                    """INSERT INTO mailbox_state(mailbox_id, uidvalidity, last_uid, updated_at) VALUES (?, ?, ?, ?)
                       ON CONFLICT(mailbox_id) DO UPDATE SET uidvalidity = excluded.uidvalidity,
                       last_uid = MAX(COALESCE(last_uid, 0), excluded.last_uid), updated_at = excluded.updated_at""",
                    (mailbox_id, uidvalidity, max_uid, now),
                )
        return new_ids

    def pending_message_ids(self, max_attempts: int = 5) -> list[int]:
        return [r["id"] for r in self._all(
            "SELECT id FROM messages WHERE status = 'pending' AND attempts < ? ORDER BY id", (max_attempts,))]

    def get_message(self, msg_id: int) -> dict[str, Any] | None:
        return self._one("SELECT * FROM messages WHERE id = ?", (msg_id,))

    def message_from_row(self, row: dict[str, Any]) -> MailMessage:
        return MailMessage(
            mailbox_id=row["mailbox_id"], uid=row["uid"], gm_msgid=row["gm_msgid"], gm_thrid=row["gm_thrid"],
            message_id=row["message_id"], in_reply_to=row["in_reply_to"], references=row["refs"],
            from_addr=row["from_addr"], from_name=row["from_name"], to_addrs=json.loads(row["to_addrs"]),
            cc_addrs=json.loads(row["cc_addrs"]), subject=row["subject"], date=datetime.fromisoformat(row["date"]),
            body_text=row["body_text"], quoted_text=row["quoted_text"], attachments=json.loads(row["attachments"]),
            labels=json.loads(row["labels"]), list_unsubscribe=bool(row["list_unsubscribe"]),
            reply_to=row["reply_to"],
        )

    def record_attempt_failure(self, msg_id: int, error: str, max_attempts: int = 5) -> None:
        with self._tx() as c:
            c.execute(
                """UPDATE messages SET attempts = attempts + 1, last_error = ?,
                   status = CASE WHEN attempts + 1 >= ? THEN 'failed' ELSE status END WHERE id = ?""",
                (error[:500], max_attempts, msg_id),
            )

    def save_triage(self, msg_id: int, t: Triage) -> None:
        with self._tx() as c:
            c.execute(
                """UPDATE messages SET importance = ?, needs_action = ?, has_time = ?, category = ?,
                   triage_confidence = ?, triage_source = ? WHERE id = ?""",
                (t.importance, t.needs_action, t.has_time, t.category, t.confidence, t.source, msg_id),
            )

    def finish_message(self, msg_id: int, decision: str, reason: str, analysis: Analysis | None) -> None:
        """处理完一封邮件：写决定和分析，status=done。通知/待办由调用方另行写入。"""
        with self._tx() as c:
            c.execute(
                """UPDATE messages SET status = 'done', decision = ?, decision_reason = ?, summary = ?, action = ?,
                   analysis_source = ? WHERE id = ?""",
                (decision, reason, analysis.summary if analysis else None, analysis.action if analysis else None,
                 analysis.source if analysis else None, msg_id),
            )

    def mark_notified(self, msg_id: int) -> None:
        with self._tx() as c:
            c.execute("UPDATE messages SET notified_at = ? WHERE id = ?", (iso(utcnow()), msg_id))

    def mark_handled(self, msg_id: int) -> None:
        with self._tx() as c:
            c.execute("UPDATE messages SET handled = 1 WHERE id = ?", (msg_id,))

    def undigested_messages(self, limit: int = 60) -> list[dict[str, Any]]:
        """待进摘要的邮件：decision=digest 且还没进过摘要。"""
        return self._all(
            """SELECT * FROM messages WHERE status = 'done' AND decision = 'digest' AND digested_at IS NULL
               ORDER BY importance DESC, id DESC LIMIT ?""",
            (limit,),
        )

    def record_digest(self, slot: str, message_ids: list[int]) -> bool:
        """登记一次摘要；同一 slot 已发过则返回 False 且不改任何东西。"""
        now = iso(utcnow())
        with self._tx() as c:
            cur = c.execute("INSERT OR IGNORE INTO digests(slot, sent_at, item_count) VALUES (?, ?, ?)",
                            (slot, now, len(message_ids)))
            if not cur.rowcount:
                return False
            c.executemany("UPDATE messages SET digested_at = ? WHERE id = ?", [(now, i) for i in message_ids])
        return True

    def last_digest(self) -> dict[str, Any] | None:
        return self._one("SELECT * FROM digests ORDER BY sent_at DESC LIMIT 1")

    def search_messages(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """本地已入库邮件的全文检索。trigram 需要 >= 3 个字符，短词退回 LIKE。"""
        q = query.strip()
        if not q:
            return []
        if len(q) >= 3:
            fts = '"' + q.replace('"', '""') + '"'
            return self._all(
                """SELECT m.* FROM messages_fts f JOIN messages m ON m.id = f.rowid
                   WHERE messages_fts MATCH ? ORDER BY m.date DESC LIMIT ?""",
                (fts, limit),
            )
        like = f"%{q}%"
        return self._all(
            """SELECT * FROM messages WHERE subject LIKE ? OR from_addr LIKE ? OR from_name LIKE ? OR body_text LIKE ?
               ORDER BY date DESC LIMIT ?""",
            (like, like, like, like, limit),
        )

    def recent_messages(self, hours: int = 24, min_decision: str | None = None, limit: int = 30) -> list[dict[str, Any]]:
        since = iso(utcnow() - timedelta(hours=hours))
        if min_decision == "notify":
            return self._all("SELECT * FROM messages WHERE date >= ? AND decision = 'notify' ORDER BY date DESC LIMIT ?",
                             (since, limit))
        return self._all("SELECT * FROM messages WHERE date >= ? ORDER BY date DESC LIMIT ?", (since, limit))

    def thread_messages(self, mailbox_id: str, gm_thrid: str, limit: int = 10) -> list[dict[str, Any]]:
        if not gm_thrid:
            return []
        return self._all("SELECT * FROM messages WHERE mailbox_id = ? AND gm_thrid = ? ORDER BY date DESC LIMIT ?",
                         (mailbox_id, gm_thrid, limit))

    # ---- 待办 ----

    def upsert_todo(
        self, *, dedupe_key: str | None, title: str, kind: str, due_date: str | None, due_time: str | None,
        timezone_name: str | None, remind_at: datetime | None, mailbox_id: str | None = None,
        message_id: int | None = None, gm_thrid: str = "", source: str = "email",
    ) -> tuple[int, bool]:
        """新建待办；dedupe_key 已存在则返回 (旧 id, False) 不覆盖。"""
        now = iso(utcnow())
        with self._tx() as c:
            if dedupe_key:
                r = c.execute("SELECT id FROM todos WHERE dedupe_key = ?", (dedupe_key,)).fetchone()
                if r:
                    return r["id"], False
            cur = c.execute(
                """INSERT INTO todos(dedupe_key, mailbox_id, message_id, gm_thrid, title, kind, due_date, due_time,
                   timezone, remind_at, source, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (dedupe_key, mailbox_id, message_id, gm_thrid, title, kind, due_date, due_time, timezone_name,
                 iso(remind_at), source, now, now),
            )
            return _new_id(cur), True

    def has_mail_todos(self, msg_id: int) -> bool:
        """这封邮件是否已经建过待办（含已完成的）。"""
        return self._one("SELECT 1 AS x FROM todos WHERE message_id = ? AND source = 'email' LIMIT 1",
                         (msg_id,)) is not None

    def get_todo(self, todo_id: int) -> dict[str, Any] | None:
        return self._one("SELECT * FROM todos WHERE id = ?", (todo_id,))

    def open_todos(self, limit: int = 50) -> list[dict[str, Any]]:
        return self._all(
            """SELECT * FROM todos WHERE status = 'open'
               ORDER BY due_date IS NULL, due_date, due_time IS NULL, due_time, id LIMIT ?""",
            (limit,),
        )

    def due_todos(self, now: datetime) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM todos WHERE status = 'open' AND remind_at IS NOT NULL AND remind_at <= ? ORDER BY remind_at",
                         (iso(now),))

    def reschedule_todo(self, todo_id: int, remind_at: datetime | None, bump_count: bool = False) -> None:
        with self._tx() as c:
            c.execute(
                """UPDATE todos SET remind_at = ?, reminded_count = reminded_count + ?, updated_at = ?
                   WHERE id = ? AND status = 'open'""",
                (iso(remind_at), 1 if bump_count else 0, iso(utcnow()), todo_id),
            )

    def retime_todo(self, todo_id: int, due_date: str | None, due_time: str | None, timezone_name: str,
                    remind_at: datetime | None) -> None:
        with self._tx() as c:
            c.execute(
                """UPDATE todos SET due_date = ?, due_time = ?, timezone = ?, remind_at = ?, updated_at = ?
                   WHERE id = ? AND status = 'open'""",
                (due_date, due_time, timezone_name, iso(remind_at), iso(utcnow()), todo_id),
            )

    def close_todo(self, todo_id: int, status: str = "done", reason: str = "user") -> bool:
        with self._tx() as c:
            cur = c.execute(
                """UPDATE todos SET status = ?, closed_reason = ?, remind_at = NULL, updated_at = ?
                   WHERE id = ? AND status = 'open'""",
                (status, reason, iso(utcnow()), todo_id),
            )
            return bool(cur.rowcount)

    def close_reply_todos_for_thread(self, mailbox_id: str, gm_thrid: str, reason: str = "replied_in_gmail") -> int:
        """用户自己在 Gmail 回复了：只关闭该线程的 reply_by 待办，其他类型不动。"""
        if not gm_thrid:
            return 0
        with self._tx() as c:
            cur = c.execute(
                """UPDATE todos SET status = 'done', closed_reason = ?, remind_at = NULL, updated_at = ?
                   WHERE mailbox_id = ? AND gm_thrid = ? AND kind = 'reply_by' AND status = 'open'""",
                (reason, iso(utcnow()), mailbox_id, gm_thrid),
            )
            return cur.rowcount

    # ---- 通知队列 ----

    def enqueue(self, channel: str, chat_id: str, msg: OutMessage, dedupe_key: str | None = None,
                not_before: datetime | None = None) -> int | None:
        """放进 outbox；dedupe_key 已存在返回 None（不重复发）。"""
        payload = json.dumps(asdict(msg), ensure_ascii=False)
        now = utcnow()
        with self._tx() as c:
            cur = c.execute(
                """INSERT OR IGNORE INTO outbox(dedupe_key, channel, chat_id, payload, not_before, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (dedupe_key, channel, chat_id, payload, iso(not_before or now), iso(now)),
            )
            return cur.lastrowid if cur.rowcount else None

    def ready_outbox(self, now: datetime, limit: int = 20) -> list[dict[str, Any]]:
        return self._all(
            "SELECT * FROM outbox WHERE status = 'queued' AND not_before <= ? ORDER BY id LIMIT ?", (iso(now), limit))

    def outbox_sent(self, outbox_id: int, provider_message_id: str) -> None:
        with self._tx() as c:
            c.execute("UPDATE outbox SET status = 'sent', provider_message_id = ?, sent_at = ? WHERE id = ?",
                      (provider_message_id, iso(utcnow()), outbox_id))

    def outbox_failed(self, outbox_id: int, error: str, retry_at: datetime | None) -> None:
        """retry_at=None（不可重试）标 failed；否则不限次数，到 retry_at 再试：
        网络/代理断很久时，重要邮件提醒、摘要、待办提醒也要等恢复后送到，不能丢。"""
        with self._tx() as c:
            c.execute(
                """UPDATE outbox SET attempts = attempts + 1, error = ?,
                   status = CASE WHEN ? IS NULL THEN 'failed' ELSE 'queued' END,
                   not_before = COALESCE(?, not_before) WHERE id = ?""",
                (error[:500], iso(retry_at), iso(retry_at), outbox_id),
            )

    def prune(self, now: datetime) -> dict[str, int]:
        """删掉过期的运行记录，返回各表删了几行。排队中的通知不删：它们会一直重试到送达。"""
        records, chat = iso(now - KEEP_RECORDS), iso(now - KEEP_CHAT)
        with self._tx() as c:
            return {
                "outbox": c.execute("DELETE FROM outbox WHERE status IN ('sent', 'failed') AND created_at < ?",
                                    (records,)).rowcount,
                "inbox_events": c.execute("DELETE FROM inbox_events WHERE received_at < ?", (records,)).rowcount,
                "chat_turns": c.execute("DELETE FROM chat_turns WHERE created_at < ?", (chat,)).rowcount,
                "pending_writes": c.execute("DELETE FROM pending_writes WHERE created_at < ?", (records,)).rowcount,
            }

    # ---- 渠道入站去重 & 对话 ----

    def seen_event(self, channel: str, event_id: str) -> bool:
        """第一次见到返回 False 并登记；重复返回 True。"""
        with self._tx() as c:
            cur = c.execute("INSERT OR IGNORE INTO inbox_events(channel, event_id, received_at) VALUES (?, ?, ?)",
                            (channel, event_id, iso(utcnow())))
            return not cur.rowcount

    def add_turn(self, channel: str, chat_id: str, role: str, content: str) -> None:
        with self._tx() as c:
            c.execute("INSERT INTO chat_turns(channel, chat_id, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
                      (channel, chat_id, role, content, iso(utcnow())))

    def recent_turns(self, channel: str, chat_id: str, limit: int = 12) -> list[dict[str, Any]]:
        """最近的对话（/new 之后的），按时间正序。"""
        reset_id = int(self.kv_get(f"chat_reset:{channel}:{chat_id}", "0") or 0)
        rows = self._all(
            "SELECT * FROM chat_turns WHERE channel = ? AND chat_id = ? AND id > ? ORDER BY id DESC LIMIT ?",
            (channel, chat_id, reset_id, limit),
        )
        return list(reversed(rows))

    def reset_chat(self, channel: str, chat_id: str) -> None:
        r = self._one("SELECT MAX(id) AS m FROM chat_turns")
        self.kv_set(f"chat_reset:{channel}:{chat_id}", str((r or {}).get("m") or 0))

    # ---- 规则 & 记忆 ----

    def add_rule(self, kind: str, field: str, pattern: str, note: str = "") -> int | None:
        if kind not in RULE_KINDS or field not in RULE_FIELDS:
            raise ValueError("非法规则")
        with self._tx() as c:
            cur = c.execute("INSERT OR IGNORE INTO rules(kind, field, pattern, note, created_at) VALUES (?, ?, ?, ?, ?)",
                            (kind, field, pattern.strip().lower(), note, iso(utcnow())))
            return cur.lastrowid if cur.rowcount else None

    def list_rules(self) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM rules ORDER BY id")

    def delete_rule(self, rule_id: int) -> bool:
        with self._tx() as c:
            return bool(c.execute("DELETE FROM rules WHERE id = ?", (rule_id,)).rowcount)

    def match_rules(self, from_addr: str, subject: str) -> list[dict[str, Any]]:
        addr = from_addr.lower()
        domain = addr.rsplit("@", 1)[-1] if "@" in addr else ""
        subj = subject.lower()
        hits = []
        for r in self.list_rules():
            if (r["field"] == "from" and r["pattern"] == addr) or \
               (r["field"] == "domain" and domain and (domain == r["pattern"] or domain.endswith("." + r["pattern"]))) or \
               (r["field"] == "subject" and r["pattern"] in subj):
                hits.append(r)
        return hits

    def add_memory(self, text: str, source: str = "chat") -> int:
        with self._tx() as c:
            return _new_id(c.execute("INSERT INTO memories(text, source, created_at) VALUES (?, ?, ?)",
                                     (text.strip(), source, iso(utcnow()))))

    def list_memories(self, limit: int = 30) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM memories ORDER BY id DESC LIMIT ?", (limit,))

    def delete_memory(self, memory_id: int) -> bool:
        with self._tx() as c:
            return bool(c.execute("DELETE FROM memories WHERE id = ?", (memory_id,)).rowcount)

    def get_memory(self, memory_id: int) -> dict[str, Any] | None:
        return self._one("SELECT * FROM memories WHERE id = ?", (memory_id,))

    # ---- 待确认的改动（聊天里读过邮件那一轮提出的，用户点确认才执行） ----

    def add_pending_write(self, tool: str, args: dict[str, Any], summary: str) -> int:
        with self._tx() as c:
            return _new_id(c.execute("INSERT INTO pending_writes(tool, args, summary, created_at) VALUES (?, ?, ?, ?)",
                                     (tool, json.dumps(args, ensure_ascii=False), summary, iso(utcnow()))))

    def get_pending_write(self, write_id: int) -> dict[str, Any] | None:
        return self._one("SELECT * FROM pending_writes WHERE id = ?", (write_id,))

    def decide_pending_write(self, write_id: int, status: str) -> bool:
        """pending → confirmed | cancelled。已处理过（重复点）返回 False：每张卡片只生效一次。"""
        with self._tx() as c:
            return bool(c.execute("UPDATE pending_writes SET status = ? WHERE id = ? AND status = 'pending'",
                                  (status, write_id)).rowcount)

    # ---- 记忆建议（agent 提议，用户点保存才进 memories） ----

    def add_suggestion(self, key: str, kind: str, text: str, reason: str, status: str) -> int | None:
        """status: queued | shown。key 已存在（任何状态，包括拒绝过的）返回 None，不覆盖。"""
        now = iso(utcnow())
        with self._tx() as c:
            cur = c.execute(
                """INSERT OR IGNORE INTO memory_suggestions(key, kind, text, reason, status, created_at, shown_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (key, kind, text.strip(), reason, status, now, now if status == "shown" else None),
            )
            return cur.lastrowid if cur.rowcount else None

    def get_suggestion(self, suggestion_id: int) -> dict[str, Any] | None:
        return self._one("SELECT * FROM memory_suggestions WHERE id = ?", (suggestion_id,))

    def list_suggestions(self, statuses: tuple[str, ...] = (), limit: int = 200) -> list[dict[str, Any]]:
        """最新的在前；statuses 为空 = 全部。"""
        if statuses:
            marks = ",".join("?" * len(statuses))
            return self._all(f"SELECT * FROM memory_suggestions WHERE status IN ({marks}) ORDER BY id DESC LIMIT ?",
                             (*statuses, limit))
        return self._all("SELECT * FROM memory_suggestions ORDER BY id DESC LIMIT ?", (limit,))

    def last_shown_suggestion(self) -> dict[str, Any] | None:
        return self._one("SELECT * FROM memory_suggestions WHERE shown_at IS NOT NULL ORDER BY shown_at DESC, id DESC LIMIT 1")

    def show_suggestion(self, suggestion_id: int) -> bool:
        """queued → shown。"""
        with self._tx() as c:
            return bool(c.execute(
                "UPDATE memory_suggestions SET status = 'shown', shown_at = ? WHERE id = ? AND status = 'queued'",
                (iso(utcnow()), suggestion_id)).rowcount)

    def save_suggestion(self, suggestion_id: int) -> int | None:
        """用户点了保存：同一事务里写 memories 并标 saved，返回 memory id。已处理过（重复点）返回 None。"""
        now = iso(utcnow())
        with self._tx() as c:
            r = c.execute("SELECT * FROM memory_suggestions WHERE id = ?", (suggestion_id,)).fetchone()
            if not r or r["status"] not in ("queued", "shown"):
                return None
            memory_id = c.execute("INSERT INTO memories(text, source, created_at) VALUES (?, 'suggested', ?)",
                                  (r["text"], now)).lastrowid
            c.execute("UPDATE memory_suggestions SET status = 'saved', memory_id = ?, decided_at = ? WHERE id = ?",
                      (memory_id, now, suggestion_id))
            return memory_id

    def decline_suggestion(self, suggestion_id: int) -> bool:
        """用户点了不要：标 declined（行永远保留，同一个 key 不会再提）。已处理过返回 False。"""
        with self._tx() as c:
            return bool(c.execute(
                """UPDATE memory_suggestions SET status = 'declined', decided_at = ?
                   WHERE id = ? AND status IN ('queued', 'shown')""",
                (iso(utcnow()), suggestion_id)).rowcount)

    def drop_stale_suggestions(self, before: datetime) -> int:
        """排队太久还没轮到的直接删掉（不算拒绝；以后证据再出现可以重新提）。"""
        with self._tx() as c:
            return c.execute("DELETE FROM memory_suggestions WHERE status = 'queued' AND created_at < ?",
                             (iso(before),)).rowcount

    def done_taps_by_sender(self, sender: str) -> int:
        """用户点了"已处理"、但没让 agent 起草回复的邮件数（handled=1 且没有关联动作）。"""
        r = self._one(
            """SELECT COUNT(*) AS n FROM messages m WHERE m.from_addr = ? AND m.handled = 1
               AND NOT EXISTS (SELECT 1 FROM actions a WHERE a.message_id = m.id)""", (sender.lower(),))
        return (r or {}).get("n", 0)

    def done_taps_by_domain(self, domain: str) -> dict[str, int]:
        """同上，按发件域：{发件地址: 次数}。"""
        rows = self._all(
            """SELECT m.from_addr AS a, COUNT(*) AS n FROM messages m WHERE m.from_addr LIKE ? AND m.handled = 1
               AND NOT EXISTS (SELECT 1 FROM actions a WHERE a.message_id = m.id) GROUP BY m.from_addr""",
            ("%@" + domain.lower(),))
        return {r["a"]: r["n"] for r in rows}

    # ---- 需要审批的外部动作 ----

    def propose_action(self, kind: str, mailbox_id: str, message_id: int | None, payload: dict[str, Any],
                       ttl: timedelta = timedelta(hours=24)) -> dict[str, Any]:
        now = utcnow()
        code = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(4))
        with self._tx() as c:
            cur = c.execute(
                """INSERT INTO actions(kind, mailbox_id, message_id, payload, payload_hash, code, created_at,
                   expires_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)""",
                (kind, mailbox_id, message_id, json.dumps(payload, ensure_ascii=False), payload_hash(payload), code,
                 iso(now), iso(now + ttl), iso(now)),
            )
            action_id = _new_id(cur)
        action = self.get_action(action_id)
        assert action is not None
        return action

    def get_action(self, action_id: int) -> dict[str, Any] | None:
        return self._one("SELECT * FROM actions WHERE id = ?", (action_id,))

    def approve_action(self, action_id: int, code: str) -> dict[str, Any] | None:
        """一次性批准：状态必须是 awaiting_approval、确认码匹配、未过期、内容哈希未变。成功返回动作行。"""
        now = iso(utcnow())
        with self._tx() as c:
            r = c.execute("SELECT * FROM actions WHERE id = ?", (action_id,)).fetchone()
            if not r or r["status"] != "awaiting_approval" or r["code"] != code.strip().upper():
                return None
            if r["expires_at"] < now:
                c.execute("UPDATE actions SET status = 'expired', updated_at = ? WHERE id = ?", (now, action_id))
                return None
            if payload_hash(json.loads(r["payload"])) != r["payload_hash"]:
                return None
            cur = c.execute(
                "UPDATE actions SET status = 'dispatching', updated_at = ? WHERE id = ? AND status = 'awaiting_approval'",
                (now, action_id),
            )
            if not cur.rowcount:
                return None
        return self.get_action(action_id)

    def finish_action(self, action_id: int, status: str, result: str = "") -> None:
        """status: succeeded | failed | unknown | cancelled。unknown 表示可能已发出，绝不自动重试。"""
        with self._tx() as c:
            c.execute("UPDATE actions SET status = ?, result = ?, updated_at = ? WHERE id = ?",
                      (status, result[:1000], iso(utcnow()), action_id))

    def cancel_action(self, action_id: int) -> bool:
        with self._tx() as c:
            return bool(c.execute(
                "UPDATE actions SET status = 'cancelled', updated_at = ? WHERE id = ? AND status = 'awaiting_approval'",
                (iso(utcnow()), action_id)).rowcount)

    def draft_for_reply(self, mailbox_id: str, in_reply_to: str, since: datetime) -> dict[str, Any] | None:
        """since 之后起草、还没由 agent 发出去的回复草稿里，回的是 in_reply_to 这封的那条（最新的）。"""
        want = (in_reply_to or "").strip()
        if not want:
            return None
        rows = self._all(
            """SELECT * FROM actions WHERE kind = 'send_reply' AND mailbox_id = ? AND created_at >= ?
               AND status IN ('awaiting_approval', 'cancelled', 'expired', 'failed') ORDER BY id DESC""",
            (mailbox_id, iso(since)))
        return next((r for r in rows if str(json.loads(r["payload"]).get("in_reply_to") or "").strip() == want), None)

    def sent_by_agent(self, message_id: str) -> bool:
        """这封邮件是不是 mail-agent 自己发出去的（发给自己或抄送自己时会回到收件箱）。"""
        if not message_id:
            return False
        return self._one("SELECT 1 AS x FROM actions WHERE status IN ('succeeded', 'unknown') AND result = ?",
                         (message_id.strip(),)) is not None

    def pending_actions(self) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM actions WHERE status IN ('awaiting_approval', 'dispatching', 'unknown') ORDER BY id")

    def interrupted_actions(self) -> list[dict[str, Any]]:
        """启动时调用：上次崩溃时停在 dispatching 的动作，结果未知，标 unknown 交给用户核对。"""
        rows = self._all("SELECT * FROM actions WHERE status = 'dispatching'")
        for r in rows:
            self.finish_action(r["id"], "unknown", "进程在发送过程中退出，请到 Gmail 已发送里核对")
        return rows

    # ---- 状态 ----

    def stats(self) -> dict[str, Any]:
        one = lambda sql: (self._one(sql) or {}).get("n", 0)  # noqa: E731
        return {
            "messages": one("SELECT COUNT(*) AS n FROM messages"),
            "pending": one("SELECT COUNT(*) AS n FROM messages WHERE status = 'pending'"),
            "failed": one("SELECT COUNT(*) AS n FROM messages WHERE status = 'failed'"),
            "notified_24h": one(f"SELECT COUNT(*) AS n FROM messages WHERE notified_at >= '{iso(utcnow() - timedelta(hours=24))}'"),
            "open_todos": one("SELECT COUNT(*) AS n FROM todos WHERE status = 'open'"),
            "outbox_queued": one("SELECT COUNT(*) AS n FROM outbox WHERE status = 'queued'"),
            "outbox_failed": one("SELECT COUNT(*) AS n FROM outbox WHERE status = 'failed'"),
            "mailboxes": self._all("SELECT * FROM mailbox_state ORDER BY mailbox_id"),
        }


class _Tx:
    """BEGIN IMMEDIATE ... COMMIT，异常回滚。持有 Database 的锁，避免多协程/线程穿插。"""

    def __init__(self, db: Database):
        self.db = db

    def __enter__(self) -> sqlite3.Connection:
        self.db._lock.acquire()
        self.db._conn.execute("BEGIN IMMEDIATE")
        return self.db._conn

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if exc_type is None:
                self.db._conn.execute("COMMIT")
            else:
                self.db._conn.execute("ROLLBACK")
        finally:
            self.db._lock.release()
