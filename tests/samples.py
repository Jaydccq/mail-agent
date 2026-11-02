"""合成测试邮件（自己编的，不含真实个人数据）。expect 是人工预期的分拣结果。"""

from __future__ import annotations

from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from datetime import datetime, timezone


def _mk(frm: str, subject: str, body: str, *, html: str | None = None, date: datetime | None = None,
        headers: dict[str, str] | None = None, to: str = "me@localhost") -> EmailMessage:
    m = EmailMessage()
    m["From"] = frm
    m["To"] = to
    m["Subject"] = subject
    m["Date"] = format_datetime(date or datetime.now(timezone.utc))
    m["Message-ID"] = make_msgid(domain="example.test")
    for k, v in (headers or {}).items():
        m[k] = v
    m.set_content(body)
    if html:
        m.add_alternative(html, subtype="html")
    return m


def samples() -> list[tuple[str, EmailMessage, str]]:
    """(名字, 邮件, 预期 decision: notify|digest|skip)"""
    now = datetime.now(timezone.utc)
    return [
        ("promo", _mk(
            "ShopNow <deals@shopnow.example>", "🔥 双十一大促：全场 5 折，仅剩 48 小时！",
            "亲爱的会员，双十一大促开始啦！全场商品低至 5 折，活动截止到 11 月 11 日 23:59。\n点击链接立即抢购。\n退订请点击这里。",
            headers={"List-Unsubscribe": "<mailto:unsub@shopnow.example>"}), "skip"),
        ("security", _mk(
            "Google <no-reply@accounts.google.example>", "安全提醒：你的账号在新设备上登录",
            "我们检测到你的 Google 账号刚刚在一台 Windows 设备上登录（位置：圣保罗，巴西）。\n"
            "如果这不是你本人，请立即检查账号活动并修改密码。"), "notify"),
        ("boss_deadline", _mk(
            "Li Wei <li.wei@company.example>", "Q4 预算表请周五前发我",
            "Hi，\n\n财务那边要求我们在本周五（11月6日）下班前把 Q4 部门预算表交上去。"
            "麻烦你把你负责的两个项目的预算填好，周五中午 12 点前发我，我汇总后再提交。\n\n谢谢！\n李伟",
            date=now), "notify"),
        ("meeting_invite", _mk(
            "Sarah Chen <sarah@partner.example>", "Can we meet Tuesday 3pm to review the contract?",
            "Hi,\n\nCould we do a 30-minute call next Tuesday at 3pm PT to go through the revised contract? "
            "Please let me know if that works or suggest another time.\n\nBest,\nSarah"), "notify"),
        ("receipt", _mk(
            "Uber Receipts <receipts@uber.example>", "Your Friday evening trip with Uber",
            "Thanks for riding, here is your receipt. Total: $23.40. Trip from Downtown to Airport.",
            headers={"List-Unsubscribe": "<mailto:x@uber.example>"}), "skip"),
        ("newsletter", _mk(
            "The Weekly Byte <newsletter@byte.example>", "This week in AI: 5 papers you should read",
            "Welcome to this week's issue. 1) A new approach to long context... 2) ...\nUnsubscribe at any time.",
            headers={"List-Unsubscribe": "<https://byte.example/unsub>"}), "skip"),
        ("bill_due", _mk(
            "PG&E <billing@pge.example>", "Your bill is ready: $142.17 due Nov 15",
            "Your monthly energy statement is ready. Amount due: $142.17. Due date: November 15, 2026. "
            "Autopay is NOT enabled on this account."), "notify"),
        ("reply_quoted", _mk(
            "Tom <tom@friend.example>", "Re: 周末爬山",
            "好的，那就周六早上 8 点在停车场集合吧，我带水。\n\n"
            "在 2026年10月30日周五 下午3:12，Me <me@localhost> 写道：\n> 周末一起去爬山吗？截止报名是 10 月 15 日。\n> 我周六有空。"),
         "digest"),
        ("github", _mk(
            "GitHub <notifications@github.example>", "[org/repo] CI failed on main (#1234)",
            "The workflow 'CI' failed on branch main. View the run for details.",
            headers={"List-Unsubscribe": "<mailto:u@github.example>"}), "digest"),
    ]
