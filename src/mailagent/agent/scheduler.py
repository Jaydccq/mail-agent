"""调度器：每 30 秒检查到期待办提醒、摘要时段、排队的记忆建议；每 5 分钟轮询 Sent 文件夹
（用户自己在 Gmail 回复了 → 关闭 reply_by 待办；回复是我起草后被用户改过的 → 看看能不能提议一条写作偏好记忆）。"""

from __future__ import annotations

import asyncio
import json
import logging
import time as _time
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable, TypeVar

from mailagent.agent import render
from mailagent.agent.pipeline import is_paused, primary_target
from mailagent.agent.suggest import Suggester
from mailagent.agent.timeutil import (
    fmt_local, in_quiet, latest_slot, next_reminder, parse_iso_safe, quiet_deferral, reminder_times,
    shift_due, to_local, today_local, utcnow,
)
from mailagent.ai import analyze
from mailagent.ai.llm.base import LLM
from mailagent.backup import create_backup
from mailagent.config import Config, MailboxConfig
from mailagent.db import Database
from mailagent.mail.imap import ImapMailbox
from mailagent.models import OutMessage

log = logging.getLogger(__name__)

T = TypeVar("T")

TICK_SECONDS = 30
SENT_POLL_SECONDS = 300
PRUNE_SECONDS = 24 * 3600
DRAFT_EDIT_WINDOW = timedelta(days=7)  # 起草后多久内用户自己发出的回复还算"改了我的草稿"


def with_imap(cfg: MailboxConfig, fn: Callable[[ImapMailbox], T]) -> T:
    """同步：新建 IMAP 连接、执行、关闭。用 asyncio.to_thread(with_imap, cfg, fn) 调用。"""
    box = ImapMailbox(cfg)
    box.connect()
    try:
        return fn(box)
    finally:
        try:
            box.close()
        except Exception:  # noqa: BLE001
            pass


def mailbox_problems(cfg: Config, db: Database) -> list[str]:
    states = {s["mailbox_id"]: s for s in db.stats()["mailboxes"]}
    return [render.mailbox_problem(states[m.id], cfg.prefs.timezone)
            for m in cfg.mailboxes if m.id in states and states[m.id]["status"] in ("error", "auth_failed")]


def retime_open_todos(db: Database, tz: str) -> int:
    """用户换了时区后，把存成别的时区的未完成待办挪到 tz：有时间的保持同一时刻，只有日期的日期不变；
    按日程算的下一次提醒在新时区重算（已经提醒过的档不重发），用户自己推迟（snooze）的提醒时刻不动。返回挪了几条。"""
    now = utcnow()
    n = 0
    for t in db.open_todos(limit=10_000):
        old = t["timezone"] or tz
        if old == tz:
            continue
        due_date, due_time = shift_due(t["due_date"], t["due_time"], old, tz)
        remind = parse_iso_safe(t["remind_at"])
        old_slots = reminder_times(t["due_date"], t["due_time"], old)
        if remind is not None and remind in old_slots:
            # 从同一档往后找（已经提醒过的档不再重发）
            later = reminder_times(due_date, due_time, tz)[old_slots.index(remind):]
            remind = next((r for r in later if r > now), None)
        db.retime_todo(t["id"], due_date, due_time, tz, remind)
        n += 1
    return n


class Scheduler:
    def __init__(self, cfg: Config, db: Database, llm: LLM | None):
        self.cfg = cfg
        self.db = db
        self.llm = llm
        self.suggester = Suggester(cfg, db)
        self._last_sent_poll = 0.0
        self._last_prune: float | None = None  # 启动后第一轮就清理一次
        self._last_backup_try: float | None = None

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — 一次失败不影响下一轮
                log.exception("Scheduler tick failed")
            await asyncio.sleep(TICK_SECONDS)

    async def tick(self) -> None:
        await self.backup_if_due()
        self.send_reminders()
        await self.scheduled_digest()
        self.send_queued_suggestion()
        if _time.monotonic() - self._last_sent_poll >= SENT_POLL_SECONDS:
            self._last_sent_poll = _time.monotonic()
            await self.poll_sent()
        if self._last_prune is None or _time.monotonic() - self._last_prune >= PRUNE_SECONDS:
            self._last_prune = _time.monotonic()
            removed = self.db.prune(utcnow())
            if any(removed.values()):
                log.info("Removed old records: %s", removed)

    async def backup_if_due(self) -> None:
        if self.db.path == ":memory:":
            return
        now = utcnow()
        last = parse_iso_safe(self.db.kv_get("backup_last_success"))
        if last is not None and now - last < timedelta(days=1):
            return
        if self._last_backup_try is not None and _time.monotonic() - self._last_backup_try < 3600:
            return
        self._last_backup_try = _time.monotonic()
        try:
            path = await asyncio.to_thread(create_backup, Path(self.db.path))
        except Exception as e:
            self.db.kv_set("backup_error", type(e).__name__)
            log.exception("Database backup failed; will retry in one hour")
        else:
            self.db.kv_set("backup_last_success", now.isoformat())
            self.db.kv_set("backup_error", "")
            log.info("Verified database backup: %s", path.name)

    # ---- 待办提醒 ----

    def send_reminders(self) -> None:
        prefs = self.cfg.prefs
        now = utcnow()
        due = self.db.due_todos(now)
        if not due:
            return
        quiet_until = quiet_deferral(now, prefs.quiet_start, prefs.quiet_end, prefs.timezone)
        if quiet_until > now:
            # 静默时段：不排进 outbox，把提醒推到静默结束再看；那之前办完（关掉）的待办就不会再收到过期提醒
            for todo in due:
                self.db.reschedule_todo(todo["id"], quiet_until)
            return
        target = primary_target(self.cfg)
        for todo in due:
            if target:
                src = self.db.get_message(todo["message_id"]) if todo.get("message_id") else None
                msg = render.todo_reminder(todo, src["subject"] if src else None)
                self.db.enqueue(target[0], target[1], msg, dedupe_key=f"remind:{todo['id']}:{todo['remind_at']}")
            else:
                log.warning("Todo #%s is due but there is no paired channel to remind on", todo["id"])
            self.db.reschedule_todo(todo["id"], next_reminder(todo, now, prefs.timezone), bump_count=True)

    # ---- 记忆建议 ----

    def send_queued_suggestion(self) -> None:
        """每日名额空出来、又不在静默时段时，发一条排队的记忆建议。"""
        prefs = self.cfg.prefs
        now = utcnow()
        target = primary_target(self.cfg)
        if target is None or in_quiet(now, prefs.quiet_start, prefs.quiet_end, prefs.timezone):
            return
        row = self.suggester.next_due(now)
        if row is not None:
            self.db.enqueue(target[0], target[1], render.memory_suggestion(row), dedupe_key=f"suggest:{row['id']}")

    async def on_edited_draft(self, action: dict[str, Any], sent_body: str) -> dict[str, Any] | None:
        """用户把我起草的回复（action）改过再发出去了：看出长期偏好就提议；名额允许就发出去。返回入库的建议（已发的）。"""
        self.db.kv_set(f"draft_edit_checked:{action['id']}", "1")
        payload = json.loads(action["payload"]) if isinstance(action["payload"], str) else action["payload"]
        cand = await self.suggester.from_draft_edit(
            str(payload.get("body") or ""), sent_body, subject=str(payload.get("subject") or ""), llm=self.llm,
            language=self.cfg.prefs.language)
        row = self.suggester.offer(cand)
        target = primary_target(self.cfg)
        if row is not None and target is not None:
            prefs = self.cfg.prefs
            self.db.enqueue(target[0], target[1], render.memory_suggestion(row), dedupe_key=f"suggest:{row['id']}",
                            not_before=quiet_deferral(utcnow(), prefs.quiet_start, prefs.quiet_end, prefs.timezone))
        return row

    async def _check_draft_edits(self, mb: MailboxConfig, items: list[dict[str, Any]]) -> None:
        """Sent 里新出现的回复：回的是我起草过、但不是我发出的那封 → 抓正文和草稿比对。"""
        since = utcnow() - DRAFT_EDIT_WINDOW
        drafts: dict[int, dict[str, Any]] = {}
        for it in items:
            if self.db.sent_by_agent(str(it.get("message_id") or "")):
                continue
            action = self.db.draft_for_reply(mb.id, str(it.get("in_reply_to") or ""), since)
            if action is not None and self.db.kv_get(f"draft_edit_checked:{action['id']}") is None:
                drafts[int(it["uid"])] = action
        if not drafts:
            return
        sent = await asyncio.to_thread(with_imap, mb, lambda b: b.fetch_sent(list(drafts)))
        for msg in sent:
            action = drafts.get(msg.uid)
            if action is not None:
                await self.on_edited_draft(action, msg.body_text)

    # ---- 摘要 ----

    def _digest_todos(self) -> list[dict[str, Any]]:
        """未来三天优先，另列最近五条过期项；更早的由 /overdue 复核。"""
        today = today_local(utcnow(), self.cfg.prefs.timezone)
        return self.db.digest_todos(today.isoformat(), (today + timedelta(days=3)).isoformat())

    async def compose_digest(self, slot_label: str, *, manual: bool) -> tuple[OutMessage | None, list[int]]:
        """生成摘要。没内容且没邮箱异常时：定时摘要返回 None（不发），手动摘要返回一条"没有新内容"。"""
        prefs = self.cfg.prefs
        mails = self.db.undigested_messages()
        todos = self._digest_todos()
        problems = mailbox_problems(self.cfg, self.db)
        paused = is_paused(self.db)
        if not mails and not todos and not problems:
            if not manual:
                return None, []
            text = "Nothing new." + (" All mailboxes are syncing fine; there's just no new mail." if self.cfg.mailboxes else "")
            return render.digest(text, slot_label=slot_label, problems=[], paused=paused), []
        if mails or todos:
            body = await analyze.write_digest(self.llm, mails, todos, language=prefs.language, tz=prefs.timezone)
        else:
            body = "No new mail in the mailboxes that are syncing fine."
        if problems:
            body += "\n\n(Mailboxes that failed to sync may have mail I haven't fetched yet; I'll catch up once they're fixed.)"
        _, overdue_count = self.db.overdue_todos(today_local(utcnow(), prefs.timezone).isoformat(), limit=0)
        if overdue_count:
            body += f"\n\n{overdue_count} overdue task(s) remain open. Use /overdue to review, complete or dismiss them."
        return render.digest(body, slot_label=slot_label, problems=problems, paused=paused), [m["id"] for m in mails]

    async def scheduled_digest(self) -> None:
        prefs = self.cfg.prefs
        now = utcnow()
        slot = latest_slot(now, prefs.digest_times, prefs.timezone)
        if slot is None:
            return
        slot_key, slot_at = slot
        last = self.db.last_digest()
        last_at = parse_iso_safe(last["sent_at"]) if last else None
        if last_at is not None and last_at >= slot_at:
            return  # 这个时段（或更晚）已经发过
        target = primary_target(self.cfg)
        if target is None:
            return  # 没配对渠道：不消耗摘要内容，等配好再发
        msg, ids = await self.compose_digest(slot_key[-5:], manual=False)
        if msg is not None:
            self.db.enqueue(target[0], target[1], msg, dedupe_key=f"digest:{slot_key}")
        if not self.db.record_digest(slot_key, ids):
            log.info("Digest slot %s was already sent", slot_key)
        else:
            log.info("Digest slot %s: %s email(s)%s", slot_key, len(ids), "" if msg else " (nothing to send)")

    async def manual_digest(self) -> OutMessage:
        """/digest：立即生成一份，由聊天直接回复；同样登记，避免下一时段重复。"""
        now = utcnow()
        label = fmt_local(now, self.cfg.prefs.timezone, with_date=False)
        msg, ids = await self.compose_digest(label, manual=True)
        assert msg is not None
        slot_key = "manual " + to_local(now, self.cfg.prefs.timezone).strftime("%Y-%m-%d %H:%M:%S")
        self.db.record_digest(slot_key, ids)
        return msg

    # ---- Sent 轮询 ----

    async def poll_sent(self) -> None:
        # 邮箱可以在聊天里热增删：遍历快照，IMAP 往返之后再确认它还在配置里
        for mb in list(self.cfg.mailboxes):
            state = self.db.get_mailbox_state(mb.id) or {}
            if state.get("status") == "auth_failed":
                continue  # 密码失效时不反复登录
            try:
                await self._poll_sent_one(mb, state)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("Polling Sent for %s failed: %s", mb.id, e)

    def _still_configured(self, mb: MailboxConfig) -> bool:
        return any(m.id == mb.id for m in self.cfg.mailboxes)

    async def _poll_sent_one(self, mb: MailboxConfig, state: dict[str, Any]) -> None:
        last_uid = state.get("sent_last_uid")
        last_uv = state.get("sent_uidvalidity")
        if last_uid is None:
            uv, max_uid, _ = await asyncio.to_thread(with_imap, mb, lambda b: b.sent_since(None))
            if not self._still_configured(mb):
                return
            self.db.set_sent_cursor(mb.id, uv, max_uid)
            log.info("%s Sent baseline: uid=%s", mb.id, max_uid)
            return
        uv, max_uid, items = await asyncio.to_thread(with_imap, mb, lambda b: b.sent_since(int(last_uid)))
        if not self._still_configured(mb):
            return  # 轮询期间被删掉了：不要再写它的状态
        if last_uv is not None and uv != last_uv:
            # UIDVALIDITY 变了：旧 UID 作废，重建基线
            uv, max_uid, _ = await asyncio.to_thread(with_imap, mb, lambda b: b.sent_since(None))
            if not self._still_configured(mb):
                return
            self.db.set_sent_cursor(mb.id, uv, max_uid)
            log.info("%s Sent UIDVALIDITY changed, new baseline", mb.id)
            return
        closed = 0
        for it in items:
            if it.get("gm_thrid"):
                closed += self.db.close_reply_todos_for_thread(mb.id, str(it["gm_thrid"]))
        self.db.set_sent_cursor(mb.id, uv, max(max_uid, int(last_uid)))
        if closed:
            log.info("%s: owner replied in Gmail, closed %s reply todo(s)", mb.id, closed)
        try:
            await self._check_draft_edits(mb, items)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — 只是建议，失败不影响 Sent 轮询
            log.warning("Checking edited drafts in %s failed: %s", mb.id, e)
