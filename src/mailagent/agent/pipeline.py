"""邮件处理流水线：pending 邮件 → 规则 → Jev 分拣 → 决定 → (分析) → 待办 / 通知 → finish。

崩溃安全：待办和通知都带 dedupe_key，先写它们再 finish_message；重试同一封不会重复。
"""

from __future__ import annotations

import asyncio
import logging
import time as _time
from typing import Any

from mailagent.agent import render
from mailagent.agent.timeutil import (
    next_reminder, parse_date, parse_hhmm, quiet_deferral, to_local, today_local, utcnow,
)
from mailagent.ai import analyze
from mailagent.ai.jev import JevClient, fallback_triage
from mailagent.ai.llm.base import LLM
from mailagent.config import Config
from mailagent.db import Database
from mailagent.models import Analysis, MailMessage, Triage

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
# 模型临时失败（超时/限流/5xx）时先重试，否则这封邮件只剩模板摘要、截止日期也不会变成待办。
# 重试会推迟通知（按下面 process() 的退避，约 1 分钟、2 分钟），所以只重试这几次，之后宁可用模板也按时通知。
# 必须小于 MAX_ATTEMPTS：失败次数用完的邮件会被标 failed，再也不处理。
LLM_RETRIES = 2


def primary_target(cfg: Config) -> tuple[str, str] | None:
    """通知发到哪：(channel, chat_id)。优先 prefs.primary_channel，没配好用另一个；都没有返回 None。"""
    order = [cfg.prefs.primary_channel] + [c for c in ("telegram", "lark") if c != cfg.prefs.primary_channel]
    for name in order:
        if name == "telegram" and cfg.telegram_enabled and cfg.telegram.owner_chat_id:
            return "telegram", cfg.telegram.owner_chat_id
        if name == "lark" and cfg.lark_enabled and cfg.lark.owner_open_id:
            return "lark", cfg.lark.owner_open_id
    return None


def is_paused(db: Database) -> bool:
    return db.kv_get("paused") == "1"


def decide(t: Triage, rule_kinds: set[str], paused: bool) -> tuple[str, str]:
    """返回 (notify|digest|skip, 原因)。Jev 拿不准的永远不 skip。"""
    if "vip" in rule_kinds:
        decision, reason = "notify", "vip rule"
    elif t.importance >= 2.2:
        decision, reason = "notify", f"importance={t.importance:.2f}"
    elif t.needs_action >= 0.7 and t.importance >= 1.5:
        decision, reason = "notify", f"needs_action={t.needs_action:.2f}"
    elif t.category == "security" and t.importance >= 1.5:
        decision, reason = "notify", "security"
    elif t.importance >= 0.8 or t.has_time >= 0.5 or t.needs_action >= 0.5:
        decision, reason = "digest", f"importance={t.importance:.2f}"
    elif t.confidence < 0.35:
        decision, reason = "digest", f"low confidence {t.confidence:.2f}"
    else:
        decision, reason = "skip", f"importance={t.importance:.2f}"
    if decision == "notify" and "mute" in rule_kinds and "vip" not in rule_kinds:
        decision, reason = "digest", "mute rule"
    if decision == "notify" and paused:
        decision, reason = "digest", "paused"
    return decision, reason


def show_draft_button(analysis: Analysis | None, t: Triage, llm: LLM | None) -> bool:
    return llm is not None and (bool(analysis and analysis.reply_suggested) or t.needs_action >= 0.7)


def thread_context(db: Database, mail: MailMessage, msg_id: int) -> list[dict[str, Any]]:
    """同线程的其他邮件（最近几封），给分析做上下文。"""
    rows = db.thread_messages(mail.mailbox_id, mail.gm_thrid, limit=6)
    return [
        {"from_addr": r["from_addr"], "from_name": r["from_name"], "subject": r["subject"], "date": r["date"],
         "body_text": (r["body_text"] or "")[:1500]}
        for r in rows if r["id"] != msg_id
    ][:5]


class Pipeline:
    def __init__(self, cfg: Config, db: Database, jev: JevClient, llm: LLM | None):
        self.cfg = cfg
        self.db = db
        self.jev = jev
        self.llm = llm
        self.wake = asyncio.Event()
        self._next_try: dict[int, float] = {}  # msg_id -> monotonic 时刻（失败退避，只在内存里）

    async def run(self) -> None:
        """常驻：先处理上次没处理完的，然后等 wake（新邮件入库）或定时重查。"""
        while True:
            self.wake.clear()
            ids = self.db.pending_message_ids(MAX_ATTEMPTS)
            now = _time.monotonic()
            waits: list[float] = []
            for msg_id in ids:
                at = self._next_try.get(msg_id, 0.0)
                if at > now:
                    waits.append(at - now)
                    continue
                await self.process(msg_id)
            timeout = max(1.0, min(waits + [60.0]))
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass

    async def process(self, msg_id: int) -> None:
        row = self.db.get_message(msg_id)
        if not row or row["status"] != "pending":
            return
        try:
            await self._process(msg_id, row)
            self._next_try.pop(msg_id, None)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — 单封失败不影响其他
            attempts = (row.get("attempts") or 0) + 1
            log.exception("Processing email #%s failed (attempt %s)", msg_id, attempts)
            self.db.record_attempt_failure(msg_id, f"{type(e).__name__}: {e}", MAX_ATTEMPTS)
            self._next_try[msg_id] = _time.monotonic() + min(30 * 2 ** attempts, 3600)

    async def _process(self, msg_id: int, row: dict[str, Any]) -> None:
        cfg, db = self.cfg, self.db
        prefs = cfg.prefs
        mail = db.message_from_row(row)
        memories = [m["text"] for m in db.list_memories()]

        if db.sent_by_agent(mail.message_id):
            # mail-agent 自己发出的回复（收件人含自己时会回到收件箱）：不分拣、不通知、不进摘要
            db.finish_message(msg_id, "skip", "sent by mail-agent", None)
            log.info("Email #%s → skip (sent by mail-agent)", msg_id)
            return

        # 1. 规则
        rule_kinds = {r["kind"] for r in db.match_rules(mail.from_addr, mail.subject)}

        # 2. Jev 分拣
        try:
            triage = await self.jev.triage(mail, user_context="; ".join(memories)[:1000])
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("Jev triage failed for email #%s, using heuristics: %s", msg_id, e)
            triage = fallback_triage(mail)
        db.save_triage(msg_id, triage)

        # 3. 决定
        decision, reason = decide(triage, rule_kinds, is_paused(db))

        # 4. 分析
        analysis: Analysis | None = None
        if decision == "notify" or triage.has_time >= 0.5 or triage.needs_action >= 0.5:
            now = utcnow()
            analysis = await analyze.analyze_mail(
                self.llm, mail, now_local=to_local(now, prefs.timezone), tz=prefs.timezone,
                language=prefs.language, memories=memories, thread=thread_context(db, mail, msg_id),
                raise_retryable=(row.get("attempts") or 0) < LLM_RETRIES,
            )
            self._save_todos(msg_id, mail, analysis)

        # 5. 通知
        if decision == "notify":
            target = primary_target(cfg)
            if target is None:
                log.warning("No paired channel; email #%s is recorded but not notified", msg_id)
            else:
                now = utcnow()
                not_before = now if "vip" in rule_kinds else \
                    quiet_deferral(now, prefs.quiet_start, prefs.quiet_end, prefs.timezone)
                out = render.notification(msg_id, mail, analysis, tz=prefs.timezone,
                                          show_draft=show_draft_button(analysis, triage, self.llm))
                db.enqueue(target[0], target[1], out, dedupe_key=f"notify:{msg_id}", not_before=not_before)

        # 6. 完成
        db.finish_message(msg_id, decision, reason, analysis)
        log.info("Email #%s → %s (%s, triage=%s)", msg_id, decision, reason, triage.source)

    def _save_todos(self, msg_id: int, mail: MailMessage, analysis: Analysis) -> None:
        if self.db.has_mail_todos(msg_id):
            return  # 重试（上次存完待办、标记完成前失败了）：这次模型的措辞可能不同，不能再建一份
        prefs = self.cfg.prefs
        now = utcnow()
        today = today_local(now, prefs.timezone)
        for d in analysis.deadlines:
            due = parse_date(d.date)
            if due is None or due < today:  # 已经过去的日期不建待办
                continue
            due_time = d.time if parse_hhmm(d.time) else None
            # 模型已把 date/time 换算成用户本地时区（d.timezone 只说明邮件原文用的时区），所以按本地时区存
            tzname = prefs.timezone
            remind = next_reminder({"due_date": d.date, "due_time": due_time, "timezone": tzname}, now, prefs.timezone)
            todo_id, created = self.db.upsert_todo(
                dedupe_key=f"mail:{msg_id}:{d.date}:{due_time or ''}:{d.what}", title=d.what, kind=d.kind or "deadline",
                due_date=d.date, due_time=due_time, timezone_name=tzname, remind_at=remind,
                mailbox_id=mail.mailbox_id, message_id=msg_id, gm_thrid=mail.gm_thrid, source="email",
            )
            if created:
                log.info("Email #%s → new todo #%s (%s %s)", msg_id, todo_id, d.date, due_time or "")
