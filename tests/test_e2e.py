"""端到端：整个 App 在进程内跑，接 GreenMail（IMAP/SMTP）+ 假 Telegram + 真 Jev + 脚本化生成模型。

需要 GreenMail 在跑（见 test_mail_greenmail.py）和环境变量 MAIL_AGENT_TEST_JEV_KEY。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import smtplib
import ssl
from datetime import date, timedelta
from typing import Any

import pytest

from fake_telegram import FakeTelegram
from mailagent.config import Config, JevConfig, MailboxConfig, Prefs, TelegramConfig, save_config
from mailagent.db import Database
from samples import samples
from test_mail_greenmail import _up

JEV_KEY = os.environ.get("MAIL_AGENT_TEST_JEV_KEY", "")
pytestmark = pytest.mark.skipif(not (_up() and JEV_KEY), reason="需要 GreenMail 和 MAIL_AGENT_TEST_JEV_KEY")

FRIDAY = (date.today() + timedelta(days=5)).isoformat()


class ScriptedLLM:
    """分析返回固定结果；聊天按脚本调用工具；其余返回固定文本。"""

    name = "scripted"

    def __init__(self):
        self.chat_script: list[dict[str, Any]] = []
        self.calls: list[str] = []

    async def complete(self, system, prompt, schema=None, timeout=None):
        props = (schema or {}).get("properties", {})
        if "deadlines" in props:
            self.calls.append("analyze")
            if "预算" in prompt:
                return {"summary": "李伟要你周五中午前交 Q4 预算。", "action": "填好预算发给李伟",
                        "reply_suggested": True,
                        "deadlines": [{"what": "Q4 预算发给李伟", "date": FRIDAY, "time": "12:00", "kind": "deadline",
                                       "timezone": None}]}
            return {"summary": "一封邮件。", "action": None, "reply_suggested": False, "deadlines": []}
        if "args_json" in props:
            self.calls.append("chat")
            step = self.chat_script.pop(0) if self.chat_script else {"reply": "好的", "tool": None, "args_json": None}
            return step
        self.calls.append("text")
        return "好的，我周五中午前把预算发给你。"

    async def check(self):
        return "scripted ok"


@pytest.fixture
def env(tmp_path, monkeypatch):
    orig = ssl.create_default_context

    def ctx(*a, **k):
        c = orig(*a, **k)
        c.check_hostname = False
        c.verify_mode = ssl.CERT_NONE
        return c

    monkeypatch.setattr(ssl, "create_default_context", ctx)
    monkeypatch.setenv("MAIL_AGENT_HOME", str(tmp_path))
    fake = FakeTelegram().start()
    cfg = Config(
        jev=JevConfig(api_key=JEV_KEY),
        mailboxes=[MailboxConfig(id="t", address="me@localhost", app_password="apppass", imap_host="127.0.0.1",
                                 imap_port=3993, smtp_host="127.0.0.1", smtp_port=3465)],
        telegram=TelegramConfig(bot_token=fake.token, owner_chat_id="42", api_base=fake.api_base),
        prefs=Prefs(timezone="America/Los_Angeles", quiet_start="00:00", quiet_end="00:00",
                    digest_times=["03:17"]),
    )
    save_config(cfg)  # 和真实运行一样：配置文件在磁盘上（聊天里增删邮箱是读文件、改、写回）
    yield cfg, fake, tmp_path
    fake.stop()
    assert not fake.rejected, fake.rejected  # 每条消息都必须是 Telegram 能收的 HTML


def _deliver(msg) -> None:
    with smtplib.SMTP("127.0.0.1", 3025, timeout=10) as s:
        s.send_message(msg, to_addrs=["me@localhost"])


async def _until(pred, timeout=60, what=""):
    for _ in range(int(timeout * 10)):
        r = pred()
        if r:
            return r
        await asyncio.sleep(0.1)
    raise TimeoutError(f"超时：{what}")


def _sent_texts(fake):
    return [s.get("text", "") for s in fake.sent]


async def _start(cfg, tmp_path, llm):
    from mailagent.app import App

    db = Database(tmp_path / "agent.db")
    app = App(cfg, db)
    app.llm = app.pipeline.llm = app.chat.llm = app.scheduler.llm = llm
    app.start()
    return app, db


def test_full_flow(env):
    cfg, fake, tmp_path = env
    by_name = {n: m for n, m, _ in samples()}

    async def go():
        llm = ScriptedLLM()
        app, db = await _start(cfg, tmp_path, llm)
        try:
            await _until(lambda: any("Now watching" in t for t in _sent_texts(fake)), 20, "欢迎消息")
            await _until(lambda: (db.get_mailbox_state("t") or {}).get("uidvalidity"), 30, "建基线")

            for n in ("promo", "boss_deadline", "security"):
                _deliver(by_name[n])
            await _until(lambda: db.stats()["messages"] >= 3 and db.stats()["pending"] == 0, 90, "3 封处理完")

            rows = {r["subject"]: r for r in db.recent_messages(hours=48)}
            boss = rows["Q4 预算表请周五前发我"]
            promo = next(r for s, r in rows.items() if "双十一" in s)
            sec = rows["安全提醒：你的账号在新设备上登录"]
            assert boss["decision"] == "notify" and sec["decision"] == "notify", (boss["decision_reason"], sec["decision_reason"])
            assert promo["decision"] == "skip", promo["decision_reason"]

            await _until(lambda: db.get_message(boss["id"])["notified_at"] and db.get_message(sec["id"])["notified_at"],
                         30, "两条通知都发出")
            note = next(s for s in fake.sent if "周五中午前交 Q4 预算" in s.get("text", ""))
            assert f"seen:{boss['id']}" in json.dumps(note.get("reply_markup"))
            assert f"draft:{boss['id']}" in json.dumps(note.get("reply_markup")), "需要回复的邮件要有起草按钮"
            assert not any("双十一" in t for t in _sent_texts(fake)), "营销邮件不能推送"

            todos = db.open_todos()
            assert [t["title"] for t in todos] == ["Q4 预算发给李伟"] and todos[0]["due_date"] == FRIDAY
            assert todos[0]["remind_at"], "要排好提醒时间"

            # 非主人 / 群聊 消息忽略
            n_before = len(fake.sent)
            fake.push_text("/todo", chat_id=999)
            fake.push_text("/todo", chat_id=-100, chat_type="group")
            await asyncio.sleep(3)
            assert len(fake.sent) == n_before

            # /todo 命令
            fake.push_text("/todo")
            await _until(lambda: any("Q4 预算发给李伟" in t for t in _sent_texts(fake)[n_before:]), 15, "/todo 回复")

            # 点「已处理」按钮 → 原消息被改写
            fake.push_callback(f"seen:{boss['id']}", message_id=note["message_id"])
            await _until(lambda: db.get_message(boss["id"])["handled"] == 1, 15, "seen 按钮")
            await _until(lambda: any(s["_method"] == "editMessageText" and str(s.get("message_id")) == str(note["message_id"])
                                     for s in fake.sent), 15, "原消息改写")

            # 聊天：模型调用 draft_reply → 预览带确认按钮；模型不能自己发
            llm.chat_script = [
                {"reply": None, "tool": "draft_reply",
                 "args_json": json.dumps({"message": str(boss["id"]), "instructions": "说周五中午前发"})},
                {"reply": "草稿已发给你确认。", "tool": None, "args_json": None},
            ]
            fake.push_text("帮我回复李伟，说周五中午前发")
            preview = await _until(lambda: next((s for s in fake.sent if "approve:" in json.dumps(s.get("reply_markup"))), None),
                                   30, "发信预览")
            assert "li.wei@company.example" in preview["text"] and "Re: Q4 预算表请周五前发我" in preview["text"]
            data = re.search(r'approve:\d+:[A-Z0-9]{4}', json.dumps(preview["reply_markup"])).group(0)
            action_id = int(data.split(":")[1])
            assert db.get_action(action_id)["status"] == "awaiting_approval"

            # 错误确认码不发；正确的发出
            fake.push_text(f"confirm {action_id} ZZZZ")
            await asyncio.sleep(3)
            assert db.get_action(action_id)["status"] == "awaiting_approval"
            fake.push_callback(data, message_id=preview["message_id"])
            await _until(lambda: db.get_action(action_id)["status"] in ("succeeded", "failed", "unknown"), 30, "发送")
            assert db.get_action(action_id)["status"] == "succeeded", db.get_action(action_id)["result"]
            # 重复点同一个按钮不会再发一次
            fake.push_callback(data, message_id=preview["message_id"])
            await asyncio.sleep(3)
            assert db.get_action(action_id)["status"] == "succeeded"

            # 聊天里管理邮箱：/addmailbox 先删掉含密码的消息，验证登录，热重启 watcher；密码不进模型、不进历史
            calls_before = list(llm.calls)
            n = len(fake.sent)
            fake.push_text("/addmailbox me@localhost app pass")
            await _until(lambda: any("Updated" in (s.get("text") or "") or "Watching" in (s.get("text") or "")
                                     for s in fake.sent[n:]), 40, "/addmailbox 回复")
            assert "deleteMessage" in fake.calls, "含密码的消息要先删掉"
            assert len(app.watchers) == 1
            await _until(lambda: (db.get_mailbox_state("t") or {}).get("status") == "ok", 30, "新 watcher 登录")
            n = len(fake.sent)
            fake.push_text("abcd efgh ijkl mnop")
            await _until(lambda: len(fake.sent) > n, 20, "疑似密码的回复")
            assert llm.calls == calls_before, "密码不能交给模型"
            assert not any("abcd" in t["content"] for t in db.recent_turns("telegram", "42", limit=50))
            n = len(fake.sent)
            fake.push_text("/mailboxes")
            await _until(lambda: any("me@localhost" in (s.get("text") or "") for s in fake.sent[n:]), 20, "/mailboxes")
            n = len(fake.sent)
            fake.push_text("/removemailbox t")
            await _until(lambda: len(fake.sent) > n, 20, "/removemailbox 回复")
            assert "rmmb:" not in json.dumps([s.get("reply_markup") for s in fake.sent[n:]]), "最后一个邮箱不能删"
            assert len(app.cfg.mailboxes) == 1

            # /digest
            fake.push_text("/digest")
            await _until(lambda: any("Digest" in (s.get("text") or "") for s in fake.sent[-5:]), 30, "/digest")
        finally:
            await app.stop()
            db.close()

        # ---- 重启：停机期间到的邮件要补上；已通知的不重复；欢迎消息不再发 ----
        n_notes = sum("周五中午前交 Q4 预算" in t for t in _sent_texts(fake))
        n_welcome = sum("Now watching" in t for t in _sent_texts(fake))
        _deliver(by_name["bill_due"])
        app, db = await _start(cfg, tmp_path, llm)
        try:
            await _until(lambda: any("142.17" in t or "PG&amp;E" in t or "PG&E" in t for t in _sent_texts(fake)), 60,
                         "停机期间的账单邮件补通知")
            await asyncio.sleep(3)
            assert sum("周五中午前交 Q4 预算" in t for t in _sent_texts(fake)) == n_notes
            assert sum("Now watching" in t for t in _sent_texts(fake)) == n_welcome
        finally:
            await app.stop()
            db.close()

    asyncio.run(go())
