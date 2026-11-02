"""README screenshots: real message templates and real model output on made-up sample emails.

    MAIL_AGENT_TEST_JEV_KEY=... python docs/images/make_screenshots.py --capture [--provider claude-cli]
    python docs/images/make_screenshots.py          # only render the PNGs from samples.json

--capture builds a database in a temp directory with the sample emails below (all fictional, .example domains) and
runs the real code on them: the mail pipeline (Jev triage, then notify / digest / skip, model analysis, todos), two
chats, a digest. The model is the CLI or API named by --provider; the Jev key comes from MAIL_AGENT_TEST_JEV_KEY.
Gmail search is replaced by a search of that temp database and saving drafts to Gmail by a no-op, so it never reads
your config or touches your mailbox. Rendering needs only samples.json and Chrome (set CHROME to its path if needed).
Messages are drawn as Telegram shows them, from the same HTML the Telegram channel sends.
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import timedelta
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from mailagent.agent.timeutil import to_local, utcnow  # noqa: E402
from mailagent.channels.telegram import TelegramChannel  # noqa: E402
from mailagent.models import Button, Inbound, OutMessage  # noqa: E402

HERE = Path(__file__).resolve().parent
SAMPLES = HERE / "samples.json"
TZ = "America/Los_Angeles"
ME = "jamie@northwind.example"
WIDTH = 400  # CSS px; screenshots are taken at 2x

# ---- sample emails (fictional) ----


def _mail(frm: str, subject: str, body: str, hours_ago: float, attach: str = "") -> EmailMessage:
    m = EmailMessage()
    m["From"] = frm
    m["To"] = ME
    m["Subject"] = subject
    m["Date"] = format_datetime(utcnow() - timedelta(hours=hours_ago))
    m["Message-ID"] = make_msgid(domain="samples.example")
    m.set_content(body)
    if attach:
        m.add_attachment(b"%PDF-1.4\n% sample\n", maintype="application", subtype="pdf", filename=attach)
    return m


def sample_emails() -> dict[str, EmailMessage]:
    due = (to_local(utcnow(), TZ) + timedelta(days=28)).strftime("%B %-d, %Y")
    return {
        "boss": _mail("Li Wei <li.wei@northwind.example>", "Q4 budget sheet needed by Friday",
                      "Hi Jamie,\n\nFinance needs the Q4 department budget sheet by end of day this Friday. Could you "
                      "fill in the numbers for your two projects and send the sheet back to me? If anything is "
                      "unclear, we can go through it at Thursday's 1:1.\n\nThanks,\nLi Wei", 0.5),
        "invoice": _mail("Acme Cloud Billing <billing@acmecloud.example>", "Invoice INV-2291 for October",
                         f"Hello,\n\nYour invoice INV-2291 for October is attached.\n\nAmount due: $1,240.50\n"
                         f"Due date: {due}\nPay online: https://pay.acmecloud.example/INV-2291\n\n"
                         "Thank you,\nAcme Cloud Billing", 3, attach="INV-2291.pdf"),
        "offsite": _mail("Dana Park <dana@northwind.example>", "Team offsite: vote for a date",
                         "Hi all,\n\nPlease vote for our offsite date by Wednesday: the 19th or the 20th of this "
                         "month. Poll: https://forms.northwind.example/offsite\n\nDana", 5),
        "parcel": _mail("ParcelFast <updates@parcelfast.example>", "Your order has shipped",
                        "Good news! Order #4471 shipped today and should arrive Thursday.\n\nTrack it: "
                        "https://parcelfast.example/t/4471", 6),
        "bank": _mail("CloudBank <alerts@cloudbank.example>", "New sign-in to your account",
                      "We noticed a new sign-in to your CloudBank account from Chrome on macOS near Seattle, WA. "
                      "If this was you, there's nothing to do. If not, reset your password right away.", 7),
        "news": _mail("The Weekly Byte <news@weeklybyte.example>", "This week: 5 tools for faster Python",
                      "In this issue: profiling with py-spy, faster installs with uv, and three more tools.", 9),
        "promo": _mail("ShopNow <deals@shopnow.example>", "48-hour sale: 30% off everything",
                       "Our biggest sale of the season ends Sunday at midnight. Use code FALL30.", 10),
    }


# ---- capture: run the real code with the model ----


def _out(m: OutMessage) -> dict[str, Any]:
    return {"title": m.title, "text": m.text, "markdown": m.markdown,
            "buttons": [[b.label for b in row] for row in m.buttons]}


def _queued(payload: str) -> dict[str, Any]:
    """outbox 里排着的消息（asdict(OutMessage) 的 JSON）。"""
    m = json.loads(payload)
    return {"title": m["title"], "text": m["text"], "markdown": m["markdown"],
            "buttons": [[b["label"] for b in row] for row in m["buttons"]]}


class _Capture:
    name = "telegram"

    def __init__(self) -> None:
        self.msgs: dict[str, OutMessage] = {}
        self.order: list[str] = []

    async def send(self, chat_id: str, msg: OutMessage) -> str:
        mid = str(len(self.order) + 1)
        self.msgs[mid] = msg
        self.order.append(mid)
        return mid

    async def edit(self, chat_id: str, message_id: str, msg: OutMessage) -> None:
        self.msgs[message_id] = msg

    async def delete_message(self, chat_id: str, message_id: str) -> bool:
        return True

    async def ack_callback(self, inbound: Inbound, toast: str = "") -> None:
        pass

    async def typing(self, chat_id: str) -> None:
        pass


async def capture(provider: str, jev_key: str) -> dict[str, Any]:
    import mailagent.agent.chat as chat_module
    from mailagent.agent import render
    from mailagent.app import App
    from mailagent.config import Config, JevConfig, LLMConfig, MailboxConfig, Prefs, TelegramConfig
    from mailagent.db import Database
    from mailagent.mail.parse import parse_message

    tmp = tempfile.mkdtemp(prefix="mail-agent-shots-")
    os.environ["MAIL_AGENT_HOME"] = tmp
    cfg = Config(jev=JevConfig(api_key=jev_key), llm=LLMConfig(provider=provider), prefs=Prefs(timezone=TZ),
                 mailboxes=[MailboxConfig(id="work", address=ME, app_password="unused")],
                 telegram=TelegramConfig(bot_token="unused", owner_chat_id="me"))  # 有已配对的渠道，流水线才排通知
    db = Database(Path(tmp) / "agent.db")
    app = App(cfg, db)
    for ch in list(app.channels.values()):
        await ch.close()
    db.add_memory("I sign my emails as Jamie.")
    ids: dict[str, int] = {}
    for i, (name, m) in enumerate(sample_emails().items()):
        ids[name] = db.ingest_messages("work", 0, [parse_message(m.as_bytes(), mailbox_id="work", uid=100 + i)])[0]

    def clock() -> str:
        return to_local(utcnow(), TZ).strftime("%H:%M")

    screens: dict[str, list[dict[str, Any]]] = {}
    decisions: dict[str, str] = {}
    for name, msg_id in ids.items():  # 真实流水线：Jev 分拣 → 推送 / 摘要 / 跳过 → 模型分析 → 待办 → 排通知
        await app.pipeline.process(msg_id)
        row = db.get_message(msg_id) or {}
        decisions[name] = f"{row.get('decision')} ({row.get('decision_reason')}, triage by {row.get('triage_source')})"
        print(f"pipeline: {name} → {decisions[name]}", flush=True)
    # 截图只放一张通知卡片：上司那封（Jev 没让它推送的话，就用第一封推送的）
    queued = db._all("SELECT dedupe_key, payload FROM outbox WHERE dedupe_key LIKE 'notify:%' ORDER BY id")
    if not queued:
        sys.exit("no sample email was important enough to notify; nothing to show")
    card = next((r for r in queued if r["dedupe_key"] == f"notify:{ids['boss']}"), queued[0])
    screens["notification"] = [{"who": "bot", "time": clock(), "msg": _queued(card["payload"])}]
    boss = db.message_from_row(db.get_message(ids["boss"]) or {})

    # 聊天：Gmail 搜索换成搜这个临时库，存 Gmail 草稿换成空操作
    async def local_search(args: dict[str, Any]) -> dict[str, Any]:
        rows: dict[int, dict[str, Any]] = {}
        for term in str(args.get("query") or "").split():
            term = term.split(":", 1)[-1].strip('"()')
            if len(term) >= 3:
                for r in db.search_messages(term, limit=10):
                    rows[r["id"]] = r
        found = sorted(rows.values(), key=lambda r: r["date"], reverse=True)[:10]
        return {"results": [{"id": r["id"], **app.chat._brief(db.message_from_row(r))} for r in found]}

    app.chat._tool_search_mail = local_search  # type: ignore[method-assign]
    chat_module.with_imap = lambda mb, fn: None  # type: ignore[assignment]
    cap = _Capture()
    app.channels.clear()
    app.channels["telegram"] = cap  # type: ignore[assignment]
    turn = 0

    async def say(screen: str, text: str) -> None:
        nonlocal turn
        turn += 1
        print(f"chat: {text}", flush=True)
        screens.setdefault(screen, []).append({"who": "user", "time": clock(), "text": text})
        before = len(cap.order)
        await app.chat.handle(Inbound(channel="telegram", chat_id="me", user_id="me", event_id=f"shot-{turn}",
                                      text=text))
        for mid in cap.order[before:]:
            screens[screen].append({"who": "bot", "time": clock(), "msg": _out(cap.msgs[mid])})

    await say("chat", "How much is the Acme Cloud invoice, and when is it due?")
    await say("chat", "Remind me to pay it two days before it's due.")
    await say("draft", "Reply to Li Wei: I'll send the sheet by Thursday noon.")

    print("writing the digest…", flush=True)
    digest, _ = await app.scheduler.compose_digest("18:00", manual=False)
    assert digest is not None
    screens["digest"] = [{"who": "bot", "time": "18:00", "msg": _out(digest)}]
    todos = db.open_todos()
    todo = next((t for t in todos if t.get("kind") == "deadline" and "budget" in t["title"].lower()), todos[0])
    screens["reminder"] = [{"who": "bot", "time": "20:00", "msg": _out(
        render.todo_reminder(todo, source_subject=boss.subject))}]
    await app.jev.aclose()
    shutil.rmtree(tmp, ignore_errors=True)
    return {"provider": provider, "decisions": decisions, "screens": screens}


# ---- render: HTML like Telegram, screenshot with headless Chrome ----

CSS = """
* { box-sizing: border-box; }
body { margin: 0; width: %dpx; background: #cfdde6; color: #111;
       font: 15px/1.38 -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", sans-serif; }
.bar { display: flex; align-items: center; gap: 10px; padding: 9px 14px; background: #fff; border-bottom: 1px solid #d3dce2; }
.avatar { width: 38px; height: 38px; border-radius: 50%%; display: flex; align-items: center; justify-content: center;
          font-size: 19px; background: linear-gradient(135deg, #67b7f7, #3b82d6); }
.name { font-weight: 600; } .sub { color: #8b98a5; font-size: 13px; }
.chat { display: flex; flex-direction: column; gap: 6px; padding: 12px 10px 14px; }
.group { display: flex; flex-direction: column; gap: 4px; max-width: 90%%; }
.group.bot { align-self: flex-start; } .group.user { align-self: flex-end; }
.msg { padding: 7px 11px 6px; border-radius: 14px; white-space: pre-wrap; overflow-wrap: anywhere;
       box-shadow: 0 1px 1px rgba(0, 0, 0, .08); }
.bot .msg { background: #fff; border-bottom-left-radius: 5px; }
.user .msg { background: #e2f9cf; border-bottom-right-radius: 5px; }
.time { float: right; margin: 7px 0 -5px 10px; font-size: 11.5px; color: #93a2b0; }
.user .time { color: #62a35c; }
.row { display: flex; gap: 4px; }
.btn { flex: 1; padding: 8px 6px; border-radius: 9px; text-align: center; color: #fff; font-size: 14px; font-weight: 500;
       background: rgba(52, 82, 106, .42); }
code { font: 13.5px ui-monospace, Menlo, monospace; background: #eef2f5; padding: 0 3px; border-radius: 4px; }
pre { margin: 2px 0; font: 13px ui-monospace, Menlo, monospace; background: #eef2f5; padding: 6px 8px; border-radius: 6px;
      white-space: pre-wrap; }
a { color: #2a7fd4; text-decoration: none; }
blockquote { margin: 2px 0; padding-left: 9px; border-left: 3px solid #67b7f7; }
"""


def _bubble(item: dict[str, Any]) -> str:
    time = f'<span class="time">{html.escape(item["time"])}</span>'
    if item["who"] == "user":
        return f'<div class="group user"><div class="msg">{html.escape(item["text"])}{time}</div></div>'
    m = item["msg"]
    msg = OutMessage(text=m["text"], title=m["title"], markdown=m["markdown"],
                     buttons=[[Button(label, "x") for label in row] for row in m["buttons"]])
    chunks = TelegramChannel._render_chunks(msg)  # 和 Telegram 渠道发出去的 HTML 一样；长消息会拆成几条
    out = [f'<div class="group bot"><div class="msg">{c}{time}</div></div>' for c in chunks]
    if m["buttons"]:
        rows = "".join('<div class="row">' + "".join(f'<div class="btn">{html.escape(b)}</div>' for b in row)
                       + "</div>" for row in m["buttons"])
        out[-1] = out[-1].replace("</div></div>", f"</div>{rows}</div>", 1)
    return "".join(out)


def page(items: list[dict[str, Any]]) -> str:
    bubbles = "".join(_bubble(i) for i in items)
    return (f"<!doctype html><html><head><meta charset='utf-8'><style>{CSS % WIDTH}</style></head><body>"
            "<div class='bar'><div class='avatar'>📬</div><div><div class='name'>Mail Agent</div>"
            "<div class='sub'>bot</div></div></div>"
            f"<div class='chat'>{bubbles}</div>"
            "<script>document.body.dataset.height = Math.ceil(document.body.getBoundingClientRect().height);</script>"
            "</body></html>")


def chrome() -> str:
    for c in (os.environ.get("CHROME"), "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
              shutil.which("google-chrome"), shutil.which("chromium")):
        if c and Path(c).exists():
            return c
    sys.exit("Chrome not found; set CHROME to its path")


def shoot(html_text: str, png: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="mail-agent-shot-") as tmp:
        page_path = Path(tmp) / "page.html"
        page_path.write_text(html_text, encoding="utf-8")
        base = [chrome(), "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
                f"--user-data-dir={tmp}/profile"]  # 自己的临时配置：不碰你正在用的 Chrome
        # 第一遍：让页面里的脚本把内容高度写进 DOM，读出来；第二遍按这个高度截图
        dom = subprocess.run(base + [f"--window-size={WIDTH},400", "--virtual-time-budget=3000", "--dump-dom",
                                     page_path.as_uri()], capture_output=True, text=True, timeout=120).stdout
        found = re.search(r'data-height="(\d+)"', dom)
        if not found:
            sys.exit(f"could not measure {png.name}")
        subprocess.run(base + ["--force-device-scale-factor=2", f"--window-size={WIDTH},{found.group(1)}",
                               f"--screenshot={png}", page_path.as_uri()], capture_output=True, timeout=120, check=True)
    print(f"wrote {png.relative_to(HERE.parents[1])}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--capture", action="store_true", help="re-run the model on the sample emails first")
    p.add_argument("--provider", default="claude-cli", help="model for --capture (default claude-cli)")
    args = p.parse_args()
    if args.capture:
        jev_key = os.environ.get("MAIL_AGENT_TEST_JEV_KEY", "")
        if not jev_key:
            sys.exit("--capture needs a Jev key in MAIL_AGENT_TEST_JEV_KEY")
        data = asyncio.run(capture(args.provider, jev_key))
        SAMPLES.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"wrote {SAMPLES.name}")
    data = json.loads(SAMPLES.read_text(encoding="utf-8"))
    for name, items in data["screens"].items():
        shoot(page(items), HERE / f"{name}.png")


if __name__ == "__main__":
    main()
