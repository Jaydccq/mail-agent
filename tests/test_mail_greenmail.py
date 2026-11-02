"""对本地 GreenMail（docker）做 IMAP/SMTP 集成测试。没起 GreenMail 时整个文件跳过。

docker run -d --name mailagent-greenmail -e GREENMAIL_OPTS='-Dgreenmail.setup.test.all -Dgreenmail.hostname=0.0.0.0
  -Dgreenmail.users=me:apppass@localhost -Dgreenmail.users.login=email' -p 127.0.0.1:3143:3143 -p 127.0.0.1:3993:3993
  -p 127.0.0.1:3025:3025 -p 127.0.0.1:3465:3465 greenmail/standalone:2.1.3
"""

from __future__ import annotations

import smtplib
import socket
import ssl
import threading
import time

import pytest

from mailagent.config import MailboxConfig
from mailagent.mail import smtp as msmtp
from mailagent.mail.imap import AuthError, ImapMailbox
from mailagent.mail.watcher import MailWatcher
from samples import samples


def _up() -> bool:
    try:
        socket.create_connection(("127.0.0.1", 3993), timeout=1).close()
        return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(not _up(), reason="GreenMail 没有运行")

CFG = MailboxConfig(id="t", address="me@localhost", app_password="app pass", imap_host="127.0.0.1", imap_port=3993,
                    smtp_host="127.0.0.1", smtp_port=3465)


@pytest.fixture(autouse=True)
def _insecure_tls(monkeypatch):
    """GreenMail 用自签名证书，测试里关掉证书校验（生产代码保持默认校验）。"""
    orig = ssl.create_default_context

    def ctx(*a, **k):
        c = orig(*a, **k)
        c.check_hostname = False
        c.verify_mode = ssl.CERT_NONE
        return c

    monkeypatch.setattr(ssl, "create_default_context", ctx)


def _deliver(msg) -> None:
    with smtplib.SMTP("127.0.0.1", 3025, timeout=10) as s:
        s.send_message(msg, to_addrs=["me@localhost"])


def _uidnext() -> int:
    mb = ImapMailbox(CFG)
    mb.connect()
    try:
        return mb.select_inbox()[1]
    finally:
        mb.close()


def test_bad_password_is_auth_error():
    mb = ImapMailbox(MailboxConfig(id="x", address="me@localhost", app_password="wrong", imap_host="127.0.0.1",
                                   imap_port=3993))
    with pytest.raises(AuthError):
        mb.connect()


def test_fetch_and_parse_samples():
    start = _uidnext()
    by_subject = {}
    for name, m, _ in samples():
        by_subject[str(m["Subject"])] = name
        _deliver(m)
    time.sleep(1)
    mb = ImapMailbox(CFG)
    mb.connect()
    try:
        msgs = mb.fetch_new(start - 1)
        # UID n:* 在没有新邮件时也会返回最后一封，必须过滤
        assert mb.fetch_new(max(m.uid for m in msgs)) == []
        flags = mb.client.get_flags([m.uid for m in msgs])
    finally:
        mb.close()
    got = {by_subject[m.subject]: m for m in msgs if m.subject in by_subject}
    assert set(got) == {n for n, _, _ in samples()}
    assert all(b"\\Seen" not in f for f in flags.values()), "抓取不能把邮件标成已读"
    promo = got["promo"]
    assert promo.from_addr == "deals@shopnow.example" and promo.from_name == "ShopNow" and promo.list_unsubscribe
    assert promo.subject.startswith("🔥 双十一大促")
    assert promo.date.tzinfo is not None and promo.is_unread
    q = got["reply_quoted"]
    assert "停车场集合" in q.body_text and "截止报名" not in q.body_text and "截止报名" in q.quoted_text
    assert got["boss_deadline"].to_addrs == ["me@localhost"]


def test_watcher_baseline_then_idle_push_and_retry():
    events = {"baseline": None, "batches": [], "status": [], "fail_once": True}
    got_mail = threading.Event()

    def on_messages(uidvalidity, msgs):
        if events["fail_once"]:
            events["fail_once"] = False
            raise RuntimeError("模拟入库失败：这一批必须被重试")
        events["batches"].append(msgs)
        got_mail.set()

    cursor = {"v": (None, None)}

    def on_baseline(uv, last):
        events["baseline"] = (uv, last)
        cursor["v"] = (uv, last)

    w = MailWatcher(CFG, load_cursor=lambda: cursor["v"], on_baseline=on_baseline, on_messages=on_messages,
                    on_status=lambda s, e: events["status"].append(s))
    w.start()
    try:
        for _ in range(100):
            if events["baseline"]:
                break
            time.sleep(0.1)
        assert events["baseline"], "首次运行要建基线"
        uv, last = events["baseline"]
        assert last == _uidnext() - 1, "基线 = 当前最后一封，不回溯旧邮件"
        name, m, _ = samples()[2]
        del m["Subject"]
        m["Subject"] = "IDLE 推送测试"
        t0 = time.time()
        _deliver(m)
        # 第一次 on_messages 失败 → 退避 5s 重连后重试同一批
        assert got_mail.wait(40), f"没收到新邮件推送；status={events['status']}"
        print(f"push+retry latency {time.time() - t0:.1f}s")
        subjects = [x.subject for b in events["batches"] for x in b]
        assert subjects.count("IDLE 推送测试") == 1
        for _ in range(50):
            if events["status"][-1] == "ok":
                break
            time.sleep(0.1)
        assert "error" in events["status"] and events["status"][-1] == "ok"
    finally:
        t = time.time()
        w.stop()
        assert time.time() - t < 15


def test_idle_wait_wakes_on_new_mail():
    mb = ImapMailbox(CFG)
    mb.connect()
    try:
        mb.select_inbox()
        threading.Timer(1.5, _deliver, args=(samples()[0][1],)).start()
        t0 = time.time()
        assert mb.idle_wait(30) is True
        assert time.time() - t0 < 15
    finally:
        mb.close()


def test_build_reply_and_smtp_send():
    from mailagent.mail.parse import parse_message

    orig = samples()[2][1]
    original = parse_message(orig.as_bytes(), mailbox_id="t", uid=1)
    reply = msmtp.build_reply(CFG, original, "好的，周五中午前发你。")
    assert reply["Subject"] == "Re: " + original.subject
    assert reply["In-Reply-To"] == original.message_id and original.message_id in reply["References"]
    assert reply["To"] == "li.wei@company.example"
    # GreenMail 的 SMTPS 端口不要求登录，这里只验证 send 能走通并返回 Message-ID
    try:
        mid, refused = msmtp.send(CFG, reply)
    except msmtp.SendFailed as e:
        pytest.skip(f"GreenMail SMTPS 不接受登录：{e}")
    assert mid == reply["Message-ID"] and refused == []
