import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from mailagent.agent import pipeline as pipeline_mod
from mailagent.agent import scheduler as scheduler_mod
from mailagent.agent.pipeline import Pipeline, decide
from mailagent.agent.scheduler import Scheduler, retime_open_todos
from mailagent.agent.timeutil import (
    find_timezone, in_quiet, latest_slot, next_reminder, parse_iso_safe, quiet_deferral, reminder_times, shift_due,
)
from mailagent.config import Config, MailboxConfig, Prefs, TelegramConfig, load_config, save_config
from mailagent.db import Database
from mailagent.ai.llm.base import LLMError
from mailagent.models import Inbound, MailMessage, OutMessage, Triage

LA = "America/Los_Angeles"


def _utc(y, mo, d, h, mi=0, tz=LA):
    return datetime(y, mo, d, h, mi, tzinfo=ZoneInfo(tz)).astimezone(timezone.utc)


def test_reminder_times_date_only_and_timed():
    assert reminder_times("2026-11-06", None, LA) == [_utc(2026, 11, 5, 20), _utc(2026, 11, 6, 9)]
    assert reminder_times("2026-11-06", "12:00", LA) == [_utc(2026, 11, 5, 20), _utc(2026, 11, 6, 10)]
    row = {"due_date": "2026-11-06", "due_time": None, "timezone": None}
    assert next_reminder(row, _utc(2026, 11, 5, 21), LA) == _utc(2026, 11, 6, 9)
    assert next_reminder(row, _utc(2026, 11, 6, 10), LA) is None


def test_reminder_across_dst_end():
    # 2026-11-01 是夏令时结束日：前一天 20:00 是 PDT，当天 09:00 是 PST
    a, b = reminder_times("2026-11-01", None, LA)
    assert a == datetime(2026, 11, 1, 3, 0, tzinfo=timezone.utc) and b == datetime(2026, 11, 1, 17, 0, tzinfo=timezone.utc)


def test_quiet_hours_cross_midnight():
    assert in_quiet(_utc(2026, 11, 3, 23, 30), "23:00", "08:00", LA)
    assert in_quiet(_utc(2026, 11, 4, 7, 59), "23:00", "08:00", LA)
    assert not in_quiet(_utc(2026, 11, 4, 8, 0), "23:00", "08:00", LA)
    assert quiet_deferral(_utc(2026, 11, 3, 23, 30), "23:00", "08:00", LA) == _utc(2026, 11, 4, 8)
    assert not in_quiet(_utc(2026, 11, 3, 23, 30), "00:00", "00:00", LA)


def test_latest_slot_catch_up_once():
    slot = latest_slot(_utc(2026, 11, 4, 19), ["08:30", "18:00"], LA)
    assert slot is not None and slot[1] == _utc(2026, 11, 4, 18)


def test_decide_never_skips_uncertain():
    assert decide(Triage(0.1, 0.0, 0.0, "promotion", 0.9), set(), False)[0] == "skip"
    assert decide(Triage(0.1, 0.0, 0.0, "other", 0.2), set(), False)[0] == "digest"
    assert decide(Triage(2.9, 0.9, 0.0, "work", 0.9), {"mute"}, False)[0] == "digest"
    assert decide(Triage(0.1, 0.0, 0.0, "promotion", 0.9), {"vip"}, False)[0] == "notify"
    assert decide(Triage(2.9, 0.9, 0.0, "work", 0.9), set(), True)[0] == "digest"


def test_due_todo_reminder_enqueued_once_and_rescheduled():
    db = Database(":memory:")
    cfg = Config(telegram=TelegramConfig(bot_token="x", owner_chat_id="42"),
                 prefs=Prefs(timezone=LA, quiet_start="00:00", quiet_end="00:00"))
    now = datetime.now(timezone.utc)
    due = (now + timedelta(days=3)).astimezone(ZoneInfo(LA)).date().isoformat()
    tid, _ = db.upsert_todo(dedupe_key="k", title="交预算", kind="deadline", due_date=due, due_time=None,
                            timezone_name=None, remind_at=now - timedelta(minutes=1))
    s = Scheduler(cfg, db, None)
    s.send_reminders()
    s.send_reminders()
    out = db.ready_outbox(now + timedelta(seconds=5))
    assert len(out) == 1 and "交预算" in out[0]["payload"] and f"done:{tid}" in out[0]["payload"]
    t = db.get_todo(tid)
    assert t["reminded_count"] == 1 and t["remind_at"] and t["remind_at"] > now.isoformat()


def test_outbox_retries_transient_failures_forever_and_drops_only_permanent_ones():
    db = Database(":memory:")
    now = datetime.now(timezone.utc).replace(microsecond=0)
    keep = db.enqueue("telegram", "42", OutMessage(text="important mail"), dedupe_key="notify:1")
    drop = db.enqueue("telegram", "42", OutMessage(text="rejected by the platform"))
    for _ in range(20):  # 网络/代理断了很久：每次都是可重试的失败
        db.outbox_failed(keep, "network down", retry_at=now)
    db.outbox_failed(drop, "400 Bad Request", retry_at=None)
    assert [r["id"] for r in db.ready_outbox(now + timedelta(seconds=1))] == [keep]
    assert db.stats()["outbox_failed"] == 1


def test_sent_by_agent_matches_only_succeeded_sends():
    db = Database(":memory:")
    a = db.propose_action("send_reply", "t", None, {"to": ["me@x.example"], "body": "hi"})
    assert not db.sent_by_agent("<m1@x>")
    db.approve_action(a["id"], a["code"])
    db.finish_action(a["id"], "succeeded", "<m1@x>")
    assert db.sent_by_agent("<m1@x>") and not db.sent_by_agent("<other@x>") and not db.sent_by_agent("")


SH = "Asia/Shanghai"


def test_find_timezone_names_cities_aliases():
    assert find_timezone("Asia/Shanghai") == (SH, [])
    assert find_timezone("new york")[0] == "America/New_York"
    assert find_timezone("  shanghai ")[0] == SH
    assert find_timezone("beijing")[0] == SH and find_timezone("PST")[0] == LA
    assert find_timezone("singapore")[0] == "Asia/Singapore"
    assert find_timezone("utc")[0] == "UTC"
    zone, close = find_timezone("Shangai")
    assert zone is None and close[0] == SH
    assert find_timezone("Mars") == (None, []) and find_timezone("") == (None, [])


def test_shift_due_keeps_instant_or_date():
    assert shift_due("2026-11-06", "12:00", LA, SH) == ("2026-11-07", "04:00")
    assert shift_due("2026-11-06", None, LA, SH) == ("2026-11-06", None)
    assert shift_due(None, None, LA, SH) == (None, None)


def _future(days: int) -> str:
    return (datetime.now(ZoneInfo(LA)) + timedelta(days=days)).date().isoformat()


class _FlakyLLM:
    """前 fails 次超时（可重试），之后返回一条带截止日期的分析。"""

    name = "flaky"

    def __init__(self, fails: int):
        self.fails, self.calls = fails, 0

    async def complete(self, system, prompt, schema=None, timeout=None):
        self.calls += 1
        if self.calls <= self.fails:
            raise LLMError("timed out", retryable=True)
        return {"summary": "Sign the contract by Friday.", "action": "Sign it", "reply_suggested": False,
                "deadlines": [{"what": "Sign the contract", "date": _future(3), "time": None, "kind": "deadline",
                               "timezone": None}]}


class _ImportantJev:
    async def triage(self, mail, user_context=""):
        return Triage(2.5, 0.9, 0.9, "work", 0.9)


def _process_one_mail(llm: _FlakyLLM, runs: int) -> tuple[Database, int]:
    db = Database(":memory:")
    cfg = Config(telegram=TelegramConfig(bot_token="x", owner_chat_id="42"),
                 prefs=Prefs(timezone=LA, quiet_start="00:00", quiet_end="00:00"))
    mail = MailMessage(mailbox_id="me", uid=1, gm_msgid="", gm_thrid="", message_id="<c1@acme.example>",
                       in_reply_to="", references="", from_addr="li@acme.example", from_name="Li",
                       to_addrs=["me@example.com"], cc_addrs=[], subject="Contract",
                       date=datetime.now(timezone.utc), body_text="Please sign the contract by Friday.")
    [msg_id] = db.ingest_messages("me", 1, [mail])
    pipe = Pipeline(cfg, db, _ImportantJev(), llm)
    for _ in range(runs):  # 每次相当于 run() 里退避到点后再处理一次
        asyncio.run(pipe.process(msg_id))
    return db, msg_id


def test_transient_llm_failure_is_retried_so_deadlines_become_todos():
    db, msg_id = _process_one_mail(_FlakyLLM(fails=1), runs=2)
    row = db.get_message(msg_id)
    assert (row["status"], row["analysis_source"]) == ("done", "llm")
    assert [t["title"] for t in db.open_todos()] == ["Sign the contract"]


def test_llm_outage_falls_back_to_template_and_still_notifies():
    assert pipeline_mod.LLM_RETRIES < pipeline_mod.MAX_ATTEMPTS  # 否则邮件会先被标 failed
    llm = _FlakyLLM(fails=99)
    db, msg_id = _process_one_mail(llm, runs=pipeline_mod.LLM_RETRIES + 1)
    row = db.get_message(msg_id)
    assert (row["status"], row["decision"], row["analysis_source"]) == ("done", "notify", "template")
    assert llm.calls == pipeline_mod.LLM_RETRIES + 1
    assert [r["dedupe_key"] for r in db.ready_outbox(datetime.now(timezone.utc) + timedelta(seconds=5))] == \
        [f"notify:{msg_id}"]


def test_retime_open_todos_moves_to_new_zone_and_keeps_snoozes():
    db = Database(":memory:")
    due = _future(10)
    timed, _ = db.upsert_todo(dedupe_key="a", title="call", kind="meeting", due_date=due, due_time="12:00",
                              timezone_name=LA, remind_at=reminder_times(due, "12:00", LA)[0])
    dated, _ = db.upsert_todo(dedupe_key="b", title="file", kind="deadline", due_date=due, due_time=None,
                              timezone_name=LA, remind_at=reminder_times(due, None, LA)[0])
    snooze = datetime(2030, 1, 2, 3, 4, tzinfo=timezone.utc)
    snoozed, _ = db.upsert_todo(dedupe_key="c", title="pay", kind="deadline", due_date=due, due_time=None,
                                timezone_name=LA, remind_at=snooze)
    already, _ = db.upsert_todo(dedupe_key="d", title="x", kind="task", due_date=None, due_time=None,
                                timezone_name=SH, remind_at=None)
    second, _ = db.upsert_todo(dedupe_key="e", title="sign", kind="deadline", due_date=due, due_time=None,
                               timezone_name=LA, remind_at=reminder_times(due, None, LA)[1])  # 第一次已提醒过
    assert retime_open_todos(db, SH) == 4
    assert parse_iso_safe(db.get_todo(second)["remind_at"]) == reminder_times(due, None, SH)[1]
    t = db.get_todo(timed)
    assert (t["due_date"], t["due_time"], t["timezone"]) == (*shift_due(due, "12:00", LA, SH), SH)
    assert parse_iso_safe(t["remind_at"]) == reminder_times(t["due_date"], t["due_time"], SH)[0]
    d = db.get_todo(dated)
    assert (d["due_date"], d["due_time"], d["timezone"]) == (due, None, SH)
    assert parse_iso_safe(d["remind_at"]) == reminder_times(due, None, SH)[0]
    assert parse_iso_safe(db.get_todo(snoozed)["remind_at"]) == snooze
    assert db.get_todo(already)["timezone"] == SH
    assert retime_open_todos(db, SH) == 0


def test_timezone_command_saves_and_moves_todos(tmp_path, monkeypatch):
    monkeypatch.setenv("MAIL_AGENT_HOME", str(tmp_path))
    from mailagent.app import App

    sent: list[str] = []

    class Capture:
        async def send(self, chat_id, msg):
            sent.append(msg.text)
            return str(len(sent))

        async def close(self):
            pass

    async def main():
        cfg = Config(mailboxes=[MailboxConfig(id="me", address="me@example.com", app_password="x")],
                     telegram=TelegramConfig(bot_token="x", owner_chat_id="42"), prefs=Prefs(timezone=LA))
        save_config(cfg)
        edited = load_config()  # agent 运行中，用户手改了配置文件、还没 restart
        edited.prefs.digest_times = ["07:15"]
        save_config(edited)
        db = Database(":memory:")
        due = _future(10)
        tid, _ = db.upsert_todo(dedupe_key="a", title="call", kind="meeting", due_date=due, due_time="12:00",
                                timezone_name=LA, remind_at=reminder_times(due, "12:00", LA)[0])
        app = App(cfg, db)
        await app.channels["telegram"].close()
        app.channels["telegram"] = Capture()
        for n, text in enumerate(["/timezone", "/timezone Shangai", "/timezone beijing", "/tz shanghai"]):
            await app.chat.handle(Inbound(channel="telegram", chat_id="42", user_id="42", event_id=f"e{n}", text=text))
        await app.jev.aclose()
        return db.get_todo(tid), due

    todo, due = asyncio.run(main())
    assert LA in sent[0] and "/timezone <zone>" in sent[0]
    assert "Did you mean: Asia/Shanghai" in sent[1]
    assert "Time zone set to Asia/Shanghai" in sent[2] and "Moved 1 open to-do" in sent[2]
    assert sent[3] == "Already using Asia/Shanghai."
    assert load_config().prefs.timezone == SH
    assert load_config().prefs.digest_times == ["07:15"]  # 换时区不能把手改的内容覆盖回内存里的旧值
    assert (todo["due_date"], todo["due_time"], todo["timezone"]) == (*shift_due(due, "12:00", LA, SH), SH)


class _ScriptedAnalysisLLM:
    name = "script"

    def __init__(self, *answers: dict):
        self.answers = list(answers)

    async def complete(self, system, prompt, schema=None, timeout=None):
        return self.answers.pop(0)


def _analysis(*deadlines: tuple[str, str]) -> dict:
    return {"summary": "s", "action": None, "reply_suggested": False,
            "deadlines": [{"what": w, "date": d, "time": None, "kind": "deadline", "timezone": None}
                          for w, d in deadlines]}


def test_two_deadlines_on_the_same_day_with_the_same_prefix_both_become_todos():
    due = _future(5)
    llm = _ScriptedAnalysisLLM(_analysis(("Submit the quarterly report draft", due),
                                         ("Submit the quarterly report final", due)))
    db, _ = _process_one_mail(llm, runs=1)  # type: ignore[arg-type]
    assert sorted(t["title"] for t in db.open_todos()) == ["Submit the quarterly report draft",
                                                           "Submit the quarterly report final"]


def test_reprocessing_after_a_failure_does_not_duplicate_todos(monkeypatch):
    # 存完待办、标记完成前失败 → 重试时模型措辞变了，也不能再建一份
    due = _future(5)
    llm = _ScriptedAnalysisLLM(_analysis(("Sign the contract", due)), _analysis(("Sign the contract by Friday", due)))
    orig, calls = Database.finish_message, []

    def finish_fails_once(self, *a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("disk I/O error")
        return orig(self, *a, **k)

    monkeypatch.setattr(Database, "finish_message", finish_fails_once)
    db, msg_id = _process_one_mail(llm, runs=2)  # type: ignore[arg-type]
    assert db.get_message(msg_id)["status"] == "done"
    assert [t["title"] for t in db.open_todos()] == ["Sign the contract"]


def test_reminder_due_in_quiet_hours_waits_and_is_dropped_if_the_todo_closes(monkeypatch):
    db = Database(":memory:")
    cfg = Config(telegram=TelegramConfig(bot_token="x", owner_chat_id="42"),
                 prefs=Prefs(timezone=LA, quiet_start="23:00", quiet_end="08:00"))
    night, morning = _utc(2026, 11, 3, 23, 30), _utc(2026, 11, 4, 8)
    tid, _ = db.upsert_todo(dedupe_key="rent", title="pay rent", kind="deadline", due_date="2026-11-05",
                            due_time=None, timezone_name=LA, remind_at=night - timedelta(minutes=1))
    s = Scheduler(cfg, db, None)
    monkeypatch.setattr(scheduler_mod, "utcnow", lambda: night)
    s.send_reminders()
    assert db.ready_outbox(morning + timedelta(hours=1)) == []  # 静默时段不排队
    assert parse_iso_safe(db.get_todo(tid)["remind_at"]) == morning
    db.close_todo(tid, "done", "user")  # 早上之前已经办完
    monkeypatch.setattr(scheduler_mod, "utcnow", lambda: morning)
    s.send_reminders()
    assert db.ready_outbox(morning + timedelta(hours=1)) == []


def test_prune_drops_old_operational_records_only():
    db = Database(":memory:")
    now = datetime.now(timezone.utc).replace(microsecond=0)
    old, recent = now - timedelta(days=100), now - timedelta(days=5)
    sent_old = db.enqueue("telegram", "42", OutMessage(text="old"))
    queued_old = db.enqueue("telegram", "42", OutMessage(text="still retrying"))
    sent_new = db.enqueue("telegram", "42", OutMessage(text="new"))
    db.outbox_sent(sent_old, "1")
    db.outbox_sent(sent_new, "2")
    db.seen_event("telegram", "e-old")
    db.seen_event("telegram", "e-new")
    db.add_turn("telegram", "42", "user", "old question")
    db.add_turn("telegram", "42", "user", "new question")
    w_old = db.add_pending_write("remember", {"text": "x"}, "Remember: x")
    db.add_memory("I am a PhD student.")
    with db._tx() as c:  # 把一半的记录挪到 100 天前
        c.execute("UPDATE outbox SET created_at = ? WHERE id IN (?, ?)", (old.isoformat(), sent_old, queued_old))
        c.execute("UPDATE inbox_events SET received_at = ? WHERE event_id = 'e-old'", (old.isoformat(),))
        c.execute("UPDATE chat_turns SET created_at = ? WHERE content = 'old question'", (old.isoformat(),))
        c.execute("UPDATE pending_writes SET created_at = ? WHERE id = ?", (old.isoformat(), w_old))
        c.execute("UPDATE memories SET created_at = ?", (old.isoformat(),))
        c.execute("UPDATE outbox SET created_at = ? WHERE id = ?", (recent.isoformat(), sent_new))
    assert db.prune(now) == {"outbox": 1, "inbox_events": 1, "chat_turns": 1, "pending_writes": 1}
    assert {r["id"] for r in db._all("SELECT id FROM outbox")} == {queued_old, sent_new}
    assert [t["content"] for t in db._all("SELECT content FROM chat_turns")] == ["new question"]
    assert len(db.list_memories()) == 1  # 邮件、记忆、待办、规则都不动


def test_service_logs_go_to_agent_log_only(tmp_path, monkeypatch):
    # 后台服务的 stderr 是 launchd.log：再往里写一份日志，就是一个不轮转、无限增长的副本
    import io
    import logging

    from mailagent.log import setup_logging

    monkeypatch.setenv("MAIL_AGENT_HOME", str(tmp_path))
    stderr = io.StringIO()  # 不是终端
    monkeypatch.setattr("sys.stderr", stderr)
    root = logging.getLogger()
    saved = (list(root.handlers), root.level)
    try:
        setup_logging()
        logging.getLogger("mailagent.test").info("hello from the service")
        for h in root.handlers:
            h.flush()
    finally:
        for h in list(root.handlers):
            root.removeHandler(h)
            h.close()
        for h in saved[0]:
            root.addHandler(h)
        root.setLevel(saved[1])
    assert stderr.getvalue() == ""
    assert "hello from the service" in (tmp_path / "agent.log").read_text()

