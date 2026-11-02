"""记忆建议（suggested memories）的离线测试：闸门、名额、信号、聊天端到端、草稿改动。不连网、不调真模型。"""

import asyncio
import itertools
from datetime import datetime, timedelta, timezone
from typing import Any

from mailagent.agent import scheduler as scheduler_mod
from mailagent.agent.chat import ChatAgent
from mailagent.agent.scheduler import Scheduler
from mailagent.agent.suggest import Candidate, Suggester, signoff, similar
from mailagent.agent.timeutil import utcnow
from mailagent.config import Config, MailboxConfig, Prefs, TelegramConfig
from mailagent.db import Database, iso
from mailagent.models import Inbound, MailMessage, OutMessage

LA = "America/Los_Angeles"
CARD = "💡 Remember this?"
MB = MailboxConfig(id="me", address="me@mycorp.example", app_password="x")


def _cfg() -> Config:
    return Config(mailboxes=[MB], telegram=TelegramConfig(bot_token="x", owner_chat_id="42"),
                  prefs=Prefs(timezone=LA, quiet_start="00:00", quiet_end="00:00"))


def _sug(db: Database | None = None) -> tuple[Database, Suggester]:
    db = db or Database(":memory:")
    return db, Suggester(_cfg(), db)


def _no_cap(s: Suggester) -> None:
    s.cap = timedelta(0)
    s.cap_unanswered = timedelta(0)


_uids = itertools.count(1)


def _mail(frm: str, subject: str = "FYI") -> MailMessage:
    uid = next(_uids)
    return MailMessage(
        mailbox_id="me", uid=uid, gm_msgid="", gm_thrid="", message_id=f"<m{uid}@test.example>", in_reply_to="",
        references="", from_addr=frm.lower(), from_name="", to_addrs=["me@mycorp.example"], cc_addrs=[],
        subject=subject, date=datetime.now(timezone.utc), body_text="Just letting you know.")


def _ingest(db: Database, *senders: str) -> list[int]:
    return db.ingest_messages("me", 1, [_mail(s) for s in senders])


class Capture:
    name = "telegram"

    def __init__(self) -> None:
        self.msgs: dict[str, OutMessage] = {}
        self.n = 0

    async def send(self, chat_id: str, msg: OutMessage) -> str:
        self.n += 1
        mid = str(self.n)
        self.msgs[mid] = msg
        return mid

    async def edit(self, chat_id: str, message_id: str, msg: OutMessage) -> None:
        self.msgs[message_id] = msg

    async def delete_message(self, chat_id: str, message_id: str) -> bool:
        return True

    async def ack_callback(self, inbound: Inbound, toast: str = "") -> None:
        pass

    async def typing(self, chat_id: str) -> None:
        pass

    async def close(self) -> None:
        pass

    def cards(self) -> list[tuple[str, OutMessage]]:
        return [(mid, m) for mid, m in self.msgs.items() if m.title == CARD]


class ScriptLLM:
    name = "script"

    def __init__(self) -> None:
        self.systems: list[str] = []
        self.suggest: str | None = None
        self.preference: str | None = None
        self.pref_calls = 0

    async def complete(self, system: str, prompt: str, schema: Any = None, timeout: Any = None) -> dict[str, Any]:
        if "preference" in (schema or {}).get("properties", {}):
            self.pref_calls += 1
            return {"preference": self.preference}
        self.systems.append(system)
        return {"reply": "ok", "tool": None, "args_json": None, "suggest_memory": self.suggest}


# ---- similar / signoff ----


def test_similar():
    assert similar("I'm a PhD student", "I am a PhD student at Stanford")
    assert not similar("My manager is Dana", "My manager is Bob")
    assert similar("我是博士生", "我是博士生，在斯坦福")
    assert not similar("我的经理是王伟", "我的经理是李明")
    assert not similar("", "anything")


def test_signoff():
    assert signoff("Best,\nJamie") == "Best, Jamie"
    assert signoff("See you Monday.\n\nCheers, HX") == "Cheers, HX"
    assert signoff("Thanks for your help.\nBest,\nJamie") == "Best, Jamie"
    assert signoff("Can you send the file?\nI need it today.") is None
    assert signoff("Sounds good.\n\nBest,\nJamie\n--\nJamie Park | Acme Corp\nwww.acme.example") == "Best, Jamie"
    assert signoff("好的，周五前发你。\n\n祝好\n洪希") == "祝好 洪希"


# ---- 闸门 ----


def test_first_shown_second_queued_same_key_never_again():
    db, s = _sug()
    first = s.offer(Candidate("chat", "chat:a", "I am a PhD student.", "r"))
    assert first is not None and first["status"] == "shown"
    assert s.offer(Candidate("chat", "chat:b", "My manager is Dana Li.", "r")) is None
    rows = {r["key"]: r for r in db.list_suggestions()}
    assert rows["chat:b"]["status"] == "queued"
    _no_cap(s)
    assert s.offer(Candidate("chat", "chat:a", "Something else entirely.", "r")) is None
    assert len(db.list_suggestions()) == 2


def test_declined_rephrase_and_existing_memory_are_dropped():
    db, s = _sug()
    _no_cap(s)
    row = s.offer(s.from_chat("I'm a PhD student at Stanford."))
    assert row is not None and db.decline_suggestion(row["id"])
    assert s.offer(s.from_chat("I am a PhD student at Stanford University.")) is None
    assert len(db.list_suggestions()) == 1
    db.add_memory("My manager is Dana Li (dana@acme.example).")
    assert s.offer(s.from_chat("My manager is Dana Li.")) is None
    assert len(db.list_suggestions()) == 1


def test_template_kinds_dont_block_each_other():
    db, s = _sug()
    _no_cap(s)
    a = s.offer(Candidate("seen_sender", "seen_sender:a@acme.example",
                          "Mail from a@acme.example is usually just FYI for me.", "r"))
    b = s.offer(Candidate("seen_sender", "seen_sender:b@acme.example",
                          "Mail from b@acme.example is usually just FYI for me.", "r"))
    assert a is not None and b is not None
    assert len(db.list_suggestions()) == 2


# ---- 名额 ----


def test_cap_answered_20h_unanswered_72h():
    db, s = _sug()
    row = s.offer(Candidate("chat", "chat:a", "I am a PhD student.", "r"))
    assert not s.slot_free(utcnow() + timedelta(hours=21))  # 没回应：等 72 小时
    assert not s.slot_free(utcnow() + timedelta(hours=71))
    assert s.slot_free(utcnow() + timedelta(hours=73))
    db.decline_suggestion(row["id"])
    assert not s.slot_free(utcnow() + timedelta(hours=19))
    assert s.slot_free(utcnow() + timedelta(hours=21))


def test_next_due_priority_and_cap():
    db, s = _sug()
    first = s.offer(Candidate("mute_domain", "mute_domain:x.example", "Mail from x.example is low priority.", "r"))
    assert first is not None
    assert s.offer(Candidate("seen_sender", "seen_sender:a@acme.example", "Mail from a@acme.example is FYI.", "r")) is None
    assert s.offer(Candidate("chat", "chat:a", "I am a PhD student.", "r")) is None
    db.decline_suggestion(first["id"])
    assert s.next_due(utcnow()) is None
    due = s.next_due(utcnow() + timedelta(hours=21))
    assert due is not None and due["kind"] == "chat" and due["status"] == "shown"
    assert db.get_suggestion(due["id"])["status"] == "shown"
    assert s.next_due(utcnow() + timedelta(hours=21)) is None  # 刚发的那条还没回应


def test_next_due_drops_stale_queued():
    db, s = _sug()
    stale = db.add_suggestion("chat:old", "chat", "I am a PhD student.", "r", "queued")
    fresh = db.add_suggestion("seen_sender:a@acme.example", "seen_sender", "Mail from a@acme.example is FYI.", "r",
                              "queued")
    with db._tx() as c:
        c.execute("UPDATE memory_suggestions SET created_at = ? WHERE id = ?",
                  (iso(utcnow() - timedelta(days=8)), stale))
    due = s.next_due()
    assert due is not None and due["id"] == fresh
    assert db.get_suggestion(stale) is None


# ---- 保存 / 拒绝 ----


def test_save_and_decline():
    db, s = _sug()
    row = s.offer(Candidate("chat", "chat:a", "I am a PhD student.", "r"))
    mid = db.save_suggestion(row["id"])
    assert mid is not None
    mem = db.list_memories()
    assert len(mem) == 1 and mem[0]["id"] == mid and mem[0]["source"] == "suggested"
    assert mem[0]["text"] == "I am a PhD student."
    assert db.get_suggestion(row["id"])["status"] == "saved"
    assert db.save_suggestion(row["id"]) is None
    assert db.decline_suggestion(row["id"]) is False
    assert len(db.list_memories()) == 1


# ---- ✅ Done 信号 ----


def _tap(db: Database, s: Suggester, msg_id: int):
    db.mark_handled(msg_id)
    return s.from_done_tap(db.get_message(msg_id))


def test_done_taps_sender_on_third():
    db, s = _sug()
    ids = _ingest(db, *["boss@acme.example"] * 3)
    assert _tap(db, s, ids[0]) is None
    assert _tap(db, s, ids[1]) is None
    cand = _tap(db, s, ids[2])
    assert cand is not None and cand.kind == "seen_sender" and cand.key == "seen_sender:boss@acme.example"


def test_done_taps_not_counted_with_action():
    db, s = _sug()
    ids = _ingest(db, *["boss@acme.example"] * 3)
    db.propose_action("send_reply", "me", ids[0], {"body": "hi", "to": ["boss@acme.example"]})
    assert [_tap(db, s, i) for i in ids] == [None, None, None]


def test_done_taps_skipped_when_rule_or_memory_covers_sender():
    db, s = _sug()
    ids = _ingest(db, *["boss@acme.example"] * 3)
    db.add_rule("vip", "from", "boss@acme.example")
    assert [_tap(db, s, i) for i in ids] == [None, None, None]

    db, s = _sug()
    ids = _ingest(db, *["news@acme.example"] * 3)
    db.add_rule("mute", "domain", "acme.example")
    assert [_tap(db, s, i) for i in ids] == [None, None, None]

    db, s = _sug()
    ids = _ingest(db, *["pat@acme.example"] * 3)
    db.add_memory("pat@acme.example is my co-author; always tell me.")
    assert [_tap(db, s, i) for i in ids] == [None, None, None]


def test_done_taps_domain():
    db, s = _sug()
    ids = _ingest(db, "a@acme.example", "a@acme.example", "b@acme.example", "b@acme.example")
    out = [_tap(db, s, i) for i in ids]
    assert out[:3] == [None, None, None]
    assert out[3] is not None and out[3].kind == "seen_domain" and out[3].key == "seen_domain:acme.example"


def test_done_taps_no_domain_for_freemail_or_own_domain():
    for domain in ("gmail.com", "mycorp.example"):
        db, s = _sug()
        ids = _ingest(db, f"a@{domain}", f"a@{domain}", f"b@{domain}", f"b@{domain}")
        assert [_tap(db, s, i) for i in ids] == [None, None, None, None], domain


# ---- 🔕 静音信号 ----


def test_mute_domain():
    db, s = _sug()
    db.add_rule("mute", "from", "a@acme.example")
    assert s.from_mute_tap({"from_addr": "a@acme.example"}) is None
    db.add_rule("mute", "from", "b@acme.example")
    cand = s.from_mute_tap({"from_addr": "b@acme.example"})
    assert cand is not None and cand.kind == "mute_domain" and cand.key == "mute_domain:acme.example"
    assert "a@acme.example" in cand.reason and "b@acme.example" in cand.reason


def test_mute_domain_skips():
    db, s = _sug()
    db.add_rule("mute", "from", "a@gmail.com")
    db.add_rule("mute", "from", "b@gmail.com")
    assert s.from_mute_tap({"from_addr": "b@gmail.com"}) is None

    db, s = _sug()
    db.add_rule("mute", "domain", "acme.example")
    db.add_rule("mute", "from", "a@acme.example")
    db.add_rule("mute", "from", "b@acme.example")
    assert s.from_mute_tap({"from_addr": "b@acme.example"}) is None

    db, s = _sug()
    db.add_rule("vip", "from", "ceo@acme.example")
    db.add_rule("mute", "from", "a@acme.example")
    db.add_rule("mute", "from", "b@acme.example")
    assert s.from_mute_tap({"from_addr": "b@acme.example"}) is None


# ---- 聊天端到端 ----


def _agent(llm: ScriptLLM | None = None) -> tuple[ChatAgent, Database, Capture, ScriptLLM]:
    cfg = _cfg()
    db = Database(":memory:")
    llm = llm or ScriptLLM()
    cap = Capture()
    agent = ChatAgent(cfg, db, llm, {"telegram": cap}, Scheduler(cfg, db, llm))
    return agent, db, cap, llm


_events = itertools.count(1)


def _ib(**kw: Any) -> Inbound:
    return Inbound(channel="telegram", chat_id="42", user_id="42", event_id=f"ev{next(_events)}", **kw)


def test_chat_done_taps_card_decline_save_and_memory_list():
    async def main():
        agent, db, cap, _ = _agent()
        ids = _ingest(db, *["boss@acme.example"] * 4)
        await agent.handle(_ib(callback_data=f"seen:{ids[0]}", message_id="card1"))
        await agent.handle(_ib(callback_data=f"seen:{ids[1]}", message_id="card2"))
        assert cap.cards() == []
        await agent.handle(_ib(callback_data=f"seen:{ids[2]}", message_id="card3"))
        cards = cap.cards()
        assert len(cards) == 1
        mid, card = cards[0]
        sid = db.list_suggestions()[0]["id"]
        assert [b.data for b in card.buttons[0]] == [f"sug:{sid}", f"sugno:{sid}"]
        assert "boss@acme.example" in card.text

        await agent.handle(_ib(callback_data=f"sugno:{sid}", message_id=mid))
        assert db.get_suggestion(sid)["status"] == "declined"
        assert cap.msgs[mid].text.endswith("OK, I won't suggest this again.") and cap.msgs[mid].buttons == []

        await agent.handle(_ib(callback_data=f"seen:{ids[3]}", message_id="card4"))
        assert len(cap.cards()) == 1 and len(db.list_suggestions()) == 1

        _no_cap(agent.suggester)
        other = agent.suggester.offer(Candidate("chat", "chat:phd", "I am a PhD student.", "r"))
        assert other is not None
        await agent.handle(_ib(callback_data=f"sug:{other['id']}", message_id="x1"))
        mem = db.list_memories()
        assert len(mem) == 1 and mem[0]["source"] == "suggested" and mem[0]["text"] == "I am a PhD student."
        assert "🧠 Saved as memory #" in cap.msgs["x1"].text
        n = cap.n
        await agent.handle(_ib(text="/memory"))
        assert cap.msgs[str(n + 1)].title == "🧠 Memories" and "I am a PhD student." in cap.msgs[str(n + 1)].text

    asyncio.run(main())


def test_chat_suggest_memory_free_text():
    async def main():
        agent, db, cap, llm = _agent()
        llm.suggest = "I'm a PhD student at Stanford."
        await agent.handle(_ib(text="Busy week, PhD life at Stanford is a lot."))
        cards = cap.cards()
        assert len(cards) == 1 and "I'm a PhD student at Stanford." in cards[0][1].text
        rows = db.list_suggestions()
        assert len(rows) == 1 and rows[0]["kind"] == "chat"
        assert db.list_memories() == []  # 建议本身不存记忆

        llm.suggest = None
        await agent.handle(_ib(text="What's the weather like?"))
        assert len(cap.cards()) == 1 and len(db.list_suggestions()) == 1

        mid = cards[0][0]
        await agent.handle(_ib(callback_data=f"sugno:{rows[0]['id']}", message_id=mid))
        assert db.get_suggestion(rows[0]["id"])["status"] == "declined"

        _no_cap(agent.suggester)  # 名额不是挡住它的原因
        llm.suggest = "I am a PhD student at Stanford University."
        await agent.handle(_ib(text="As a Stanford PhD student I get a lot of mail."))
        assert len(cap.cards()) == 1 and len(db.list_suggestions()) == 1

        llm.suggest = None
        await agent.handle(_ib(text="Anything new?"))
        system = llm.systems[-1]
        head = "Memory suggestions already offered to the user"
        assert head in system
        assert "I'm a PhD student at Stanford." in system[system.index(head):system.index("Tools (")]
        assert db.list_memories() == []

    asyncio.run(main())


# ---- 改过的草稿 ----

DRAFT = "Sure, the Q4 sheet is attached.\n\nBest,\nJamie"
PAYLOAD = {"body": DRAFT, "subject": "Re: Q4", "in_reply_to": "<a@x>", "to": ["li@x.example"]}


def _edited(sent: str, preference: str | None = None):
    async def main():
        db = Database(":memory:")
        llm = ScriptLLM()
        llm.preference = preference
        s = Scheduler(_cfg(), db, llm)
        action = db.propose_action("send_reply", "me", None, dict(PAYLOAD))
        row = await s.on_edited_draft(action, sent)
        return db, llm, row

    return asyncio.run(main())


def test_edited_draft_signoff():
    db, llm, row = _edited("Sure, the Q4 sheet is attached.\n\nCheers,\nHX")
    assert row is not None and row["kind"] == "draft_signoff" and "HX" in row["text"]
    out = db._one("SELECT * FROM outbox WHERE dedupe_key = ?", (f"suggest:{row['id']}",))
    assert out is not None and CARD in out["payload"]
    assert llm.pref_calls == 0


def test_edited_draft_style_from_model():
    db, llm, row = _edited("Sheet attached.\n\nBest,\nJamie", preference="I prefer short replies.")
    assert row is not None and row["kind"] == "draft_style" and row["text"] == "I prefer short replies."
    assert llm.pref_calls == 1


def test_edited_draft_no_preference_or_unchanged():
    db, llm, row = _edited("Sheet attached.\n\nBest,\nJamie", preference=None)
    assert row is None and db.list_suggestions() == [] and llm.pref_calls == 1
    db, llm, row = _edited(DRAFT, preference="I prefer short replies.")
    assert row is None and db.list_suggestions() == [] and llm.pref_calls == 0


def test_draft_for_reply_ignores_succeeded():
    db = Database(":memory:")
    since = utcnow() - timedelta(days=1)
    sent = db.propose_action("send_reply", "me", None, dict(PAYLOAD))
    db.finish_action(sent["id"], "succeeded", "<sent@x>")
    assert db.draft_for_reply("me", "<a@x>", since) is None
    pending = db.propose_action("send_reply", "me", None, dict(PAYLOAD))
    assert db.draft_for_reply("me", "<a@x>", since)["id"] == pending["id"]
    assert db.draft_for_reply("me", "<other@x>", since) is None
    assert db.draft_for_reply("other", "<a@x>", since) is None


def test_check_draft_edits(monkeypatch):
    class FakeBox:
        calls: list[list[int]] = []

        def fetch_sent(self, uids):
            FakeBox.calls.append(list(uids))
            return [MailMessage(
                mailbox_id="me", uid=u, gm_msgid="", gm_thrid="", message_id=f"<user{u}@x>", in_reply_to="<a@x>",
                references="", from_addr="me@mycorp.example", from_name="", to_addrs=["li@x.example"], cc_addrs=[],
                subject="Re: Q4", date=datetime.now(timezone.utc),
                body_text="Sure, the Q4 sheet is attached.\n\nCheers,\nHX") for u in uids]

    def fake(mb, fn):
        return fn(FakeBox())

    monkeypatch.setattr(scheduler_mod, "with_imap", fake)

    async def main():
        db = Database(":memory:")
        s = Scheduler(_cfg(), db, ScriptLLM())
        db.propose_action("send_reply", "me", None, dict(PAYLOAD))  # 等待批准的草稿，回 <a@x>
        own = db.propose_action("send_reply", "me", None, dict(PAYLOAD))
        db.finish_action(own["id"], "succeeded", "<sent@x>")
        # agent 自己发出的那封（Message-ID 对得上）：即使 in_reply_to 对得上草稿也跳过
        await s._check_draft_edits(MB, [{"uid": 5, "message_id": "<sent@x>", "in_reply_to": "<a@x>"}])
        assert FakeBox.calls == [] and db.list_suggestions() == []
        item = {"uid": 6, "message_id": "<user6@x>", "in_reply_to": "<a@x>"}
        await s._check_draft_edits(MB, [item])
        await s._check_draft_edits(MB, [item])
        assert FakeBox.calls == [[6]]
        rows = db.list_suggestions()
        assert len(rows) == 1 and rows[0]["kind"] == "draft_signoff" and "HX" in rows[0]["text"]

    asyncio.run(main())


# ---- 调度器发排队的建议 ----


def test_send_queued_suggestion_once_cap_free():
    db = Database(":memory:")
    s = Scheduler(_cfg(), db, None)
    first = s.suggester.offer(Candidate("chat", "chat:a", "I am a PhD student.", "r"))
    assert s.suggester.offer(Candidate("chat", "chat:b", "My manager is Dana Li.", "r")) is None
    queued = db.list_suggestions(("queued",))[0]
    db.decline_suggestion(first["id"])
    s.send_queued_suggestion()  # 20 小时名额还没到
    assert db._one("SELECT * FROM outbox WHERE dedupe_key = ?", (f"suggest:{queued['id']}",)) is None
    s.suggester.cap = timedelta(0)
    s.send_queued_suggestion()
    out = db._one("SELECT * FROM outbox WHERE dedupe_key = ?", (f"suggest:{queued['id']}",))
    assert out is not None and "Dana Li" in out["payload"]
    assert db.get_suggestion(queued["id"])["status"] == "shown"
    s.send_queued_suggestion()
    assert db._one("SELECT COUNT(*) AS n FROM outbox")["n"] == 1


def test_send_queued_suggestion_quiet_hours(monkeypatch):
    monkeypatch.setattr(scheduler_mod, "in_quiet", lambda *a, **k: True)
    db = Database(":memory:")
    s = Scheduler(_cfg(), db, None)
    sid = db.add_suggestion("chat:a", "chat", "I am a PhD student.", "r", "queued")
    s.send_queued_suggestion()
    assert db.get_suggestion(sid)["status"] == "queued"
    assert db._one("SELECT COUNT(*) AS n FROM outbox")["n"] == 0
