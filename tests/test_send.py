import asyncio
from email.message import EmailMessage

from mailagent.agent import chat as chat_mod
from mailagent.mail import smtp
from test_suggest import MB, _agent, _ib


def test_partially_refused_recipients_are_returned(monkeypatch):
    class FakeSMTP:
        def __init__(self, *a, **k):
            pass

        def login(self, user, password):
            pass

        def send_message(self, msg, from_addr, to_addrs):
            return {"gone@x.example": (550, b"No such user")}

        def quit(self):
            pass

    monkeypatch.setattr(smtp.smtplib, "SMTP_SSL", FakeSMTP)
    msg = EmailMessage()
    msg["To"] = "ok@x.example, gone@x.example"
    msg["Subject"] = "hi"
    msg.set_content("hi")
    message_id, refused = smtp.send(MB, msg)
    assert message_id and refused == ["gone@x.example"]


def test_approval_reply_names_refused_recipients(monkeypatch):
    async def main():
        agent, db, cap, _ = _agent()
        payload = {"from": MB.address, "to": ["ok@x.example", "gone@x.example"], "cc": [], "subject": "Re: hi",
                   "body": "hello", "in_reply_to": "", "references": "", "gm_thrid": "", "draft_message_id": ""}
        action = db.propose_action("send_reply", MB.id, None, payload)
        monkeypatch.setattr(chat_mod.smtp, "send", lambda mb, msg: ("<1@x.example>", ["gone@x.example"]))
        await agent.handle(_ib(text=f"confirm {action['id']} {action['code']}"))
        reply = cap.msgs[str(cap.n)].text
        assert "✅ Sent" in reply and "gone@x.example" in reply
        assert db.get_action(action["id"])["status"] == "succeeded"

    asyncio.run(main())
