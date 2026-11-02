"""Send one of each kind of message to your own chat, to check formatting on a real phone.

    .venv/bin/python tests/live_format_check.py            # send to the chat paired in ~/.mail-agent/config.toml
    .venv/bin/python tests/live_format_check.py --dry-run  # print the Telegram HTML instead (no config needed)

Covers model-written chat replies and digests (Markdown: bold, italics, code, lists, a table, a code block, a link,
Chinese with full-width punctuation) and the messages that must show text exactly as written (a notification
for an email full of markup, a reply-draft preview). Titles start with "🧪 format check" and the messages carry
no buttons, so they are safe to delete. Telegram rejects HTML it can't parse (400 "can't parse entities"),
so every message that arrives is also a pass. Not collected by pytest.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mailagent.agent import render  # noqa: E402
from mailagent.models import Analysis, Deadline, MailMessage, OutMessage  # noqa: E402

REPLY_EN = """I found **3 emails from Alex Kim** (`alex@acme.example`) this week:

- **Wed Oct 28: Contract v3, please sign**
  - Legal approved the indemnity change. **Sign by Fri Nov 6** so work can start on the 9th.
  - The DocuSign link expires *Nov 13*, but Alex's deadline is ~~Nov 13~~ **Nov 6**.
- **Tue Oct 27: Re: pricing**: $12.40/unit for orders over 5,000 units, net 30
- **Mon Oct 26: Kickoff agenda**: Nov 9 at 10:00, see [the agenda](https://example.com/kickoff?id=4471&v=2)

Most urgent: **sign the contract (`Contract v3.pdf`) by Friday.** Send `/todo` to see your list."""

REPLY_ZH = """### Alex 上周的邮件
**截止日期：**11月6日（周五）前签合同
时间是**11/9（周一）10:00**开启动会，**注意**：带电脑
- **报价**：超过 5,000 件单价 **$12.40/件**，付款账期 30 天
- 一个孤立的 ** 和 2*3 应该原样显示"""

REPLY_TABLE = """Here's the comparison:

| Email | Key details | Deadline / action |
|---|---|---|
| Alex: Contract v3 | Legal approved the indemnity change | Sign by **Nov 6**; start Nov 9 |
| DocuSign: Contract v3 | Signing request | Link expires **Nov 13** |
| Alex: Kickoff agenda | Scope, timeline, owners | **Nov 9, 10:00** |"""

REPLY_CODE = """## Searching by sender
Use Gmail search syntax, for example:
```
from:alex@acme.example newer_than:7d has:attachment
subject:"Contract v3" -label:promotions
```
Then ask me *"summarize the newest one"*."""

TRICKY_SUBJECT = "**URGENT** <b>Invoice</b> #4471 & `PO-88` [pay now](https://pay.example)"

DRAFT_BODY = """Hi Alex,

Thanks, **v3** looks good. Two things before I sign:
- the indemnity cap in section 4 <see redline>
- the start date: Nov 9 works

# not a heading, just a line starting with a hash
Best,
Jamie"""


def messages(tz: str) -> list[tuple[str, OutMessage]]:
    now = datetime.now(timezone.utc)
    mail = MailMessage(
        mailbox_id="main", uid=1, gm_msgid="", gm_thrid="", message_id="<x@example.com>", in_reply_to="",
        references="", from_addr="billing@vendor.example", from_name="Vendor *Billing* <ops>", to_addrs=["me@x.example"],
        cc_addrs=[], subject=TRICKY_SUBJECT, date=now, body_text="Invoice #4471 for $12.40 is due Friday.")
    due = (now + timedelta(days=5)).date().isoformat()
    analysis = Analysis(summary="Vendor billing sent invoice #4471 for $12.40 (**not bold**, <not a tag>).",
                        action="Pay invoice #4471 & reply with the PO number `PO-88`",
                        deadlines=[Deadline(what="Pay invoice #4471", date=due, time="17:00")])
    action = {"id": 9999, "code": "000000", "payload": {
        "from": "me@x.example", "to": ["alex@acme.example"], "subject": "Re: Contract v3 – please sign", "body": DRAFT_BODY}}
    digest_body = ("**Needs you**\n• Alex Kim — Contract v3: **sign by Nov 6**\n\n"
                   "**Worth a look**\n• Vendor — Invoice #4471: $12.40 due Friday\n• ShopNow — `50% off` sale")
    return [
        ("chat reply (English)", OutMessage(text=REPLY_EN, markdown=True)),
        ("chat reply (Chinese)", OutMessage(text=REPLY_ZH, markdown=True)),
        ("chat reply with a table", OutMessage(text=REPLY_TABLE, markdown=True)),
        ("chat reply with a code block", OutMessage(text=REPLY_CODE, markdown=True)),
        ("digest", render.digest(digest_body, slot_label="08:30", problems=[], paused=False)),
        ("notification (shown exactly)", render.notification(1, mail, analysis, tz=tz, show_draft=True)),
        ("draft preview (shown exactly)", render.action_preview(action)),
        ("todo reminder", render.todo_reminder({"id": 1, "title": "Pay invoice #4471 <ASAP> & **file it**",
                                                "due_date": due, "due_time": "17:00"}, TRICKY_SUBJECT)),
    ]


def labelled(label: str, msg: OutMessage) -> OutMessage:
    title = f"🧪 format check · {label}" + (f" · {msg.title}" if msg.title else "")
    return replace(msg, title=title, buttons=[])


async def main_async(args: argparse.Namespace) -> int:
    if args.dry_run:
        from mailagent.channels.telegram import TelegramChannel

        for label, msg in messages("America/Los_Angeles"):
            print("=" * 60)
            print("\n---\n".join(TelegramChannel._render_chunks(labelled(label, msg))))
        return 0

    from mailagent.agent.pipeline import primary_target
    from mailagent.app import make_channels
    from mailagent.config import load_config

    cfg = load_config()
    target = primary_target(cfg)
    if target is None:
        print("No paired chat. Run mail-agent setup first.")
        return 1
    name, chat_id = target
    ch = make_channels(cfg)[name]
    failed = 0
    try:
        for label, msg in messages(cfg.prefs.timezone):
            try:
                await ch.send(chat_id, labelled(label, msg))
                print(f"  sent    {label}")
            except Exception as e:  # noqa: BLE001
                failed += 1
                print(f"  FAILED  {label}: {e}")
    finally:
        await ch.close()
    print(f"\n{len(messages(cfg.prefs.timezone)) - failed} sent, {failed} failed ({name}). Check them on your phone.")
    return 1 if failed else 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true", help="print the Telegram HTML instead of sending")
    return asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
