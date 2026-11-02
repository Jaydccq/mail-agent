import asyncio

import pytest

from fake_telegram import FakeTelegram
from mailagent.channels.base import ChannelError
from mailagent.channels.telegram import TelegramChannel
from mailagent.config import TelegramConfig
from mailagent.models import Button, OutMessage


@pytest.fixture
def fake():
    f = FakeTelegram().start()
    yield f
    f.stop()


def _chan(fake: FakeTelegram, token: str | None = None) -> TelegramChannel:
    return TelegramChannel(TelegramConfig(bot_token=token or fake.token, api_base=fake.api_base))


def test_check_and_bad_token(fake):
    async def go():
        ch = _chan(fake)
        assert "fake_mail_bot" in await ch.check()
        await ch.close()
        bad = _chan(fake, "BAD:TOKEN")
        with pytest.raises(ChannelError) as e:
            await bad.check()
        assert not e.value.retryable
        assert "BAD:TOKEN" not in str(e.value)
        await bad.close()

    asyncio.run(go())


def test_send_escapes_and_buttons(fake):
    async def go():
        ch = _chan(fake)
        mid = await ch.send("42", OutMessage("a <b> & c", title="T<1>", buttons=[[Button("✅", "seen:1")]]))
        s = fake.sent[-1]
        assert mid == str(s["message_id"])
        assert "&lt;b&gt; &amp; c" in s["text"] and "<b>T&lt;1&gt;</b>" in s["text"]
        assert "seen:1" in str(s["reply_markup"])
        # 超长拆分：按钮只在最后一段
        await ch.send("42", OutMessage("行\n" * 3000, buttons=[[Button("x", "done:1")]]))
        chunks = [x for x in fake.sent if x["text"].startswith("行")]
        assert len(chunks) >= 2 and all(len(c["text"]) <= 4096 for c in chunks)
        assert "reply_markup" in chunks[-1] and all("reply_markup" not in c or not c["reply_markup"] for c in chunks[:-1])
        await ch.edit("42", mid, OutMessage("done"))
        assert fake.sent[-1]["_method"] == "editMessageText"
        await ch.close()

    asyncio.run(go())


def test_receive_messages_and_callbacks(fake):
    async def go():
        ch = _chan(fake)
        got = []

        async def on_in(i):
            got.append(i)
            if len(got) == 1:
                raise RuntimeError("处理出错也不能让接收循环退出")

        task = asyncio.create_task(ch.start(on_in))
        fake.push_text("hello")
        fake.push_callback("snooze:3", message_id=100)
        fake.push_text("in group", chat_id=-5, chat_type="group")
        for _ in range(100):
            if len(got) >= 3:
                break
            await asyncio.sleep(0.1)
        task.cancel()
        await ch.close()
        assert [g.text for g in got][0] == "hello" and got[0].is_private and got[0].chat_id == "42"
        assert got[1].callback_data == "snooze:3" and got[1].message_id == "100"
        assert got[2].is_private is False
        assert len({g.event_id for g in got}) == 3

    asyncio.run(go())


def test_pair(fake):
    async def go():
        ch = _chan(fake)
        fake.push_text("wrong")
        fake.push_text(" 123456 ")
        chat_id, user_id = await ch.pair("123456", timeout=10)
        assert (chat_id, user_id) == ("42", "42")
        assert any("Paired" in s["text"] for s in fake.sent)
        fake.push_text("/start 999999")
        fake.push_text("/start 654321")  # 点 t.me/<bot>?start=654321 链接发来的
        assert await ch.pair("654321", timeout=10) == ("42", "42")
        await ch.close()

    asyncio.run(go())


def test_delete_message(fake):
    async def go():
        ch = _chan(fake)
        assert await ch.delete_message("42", "77") is True
        assert "deleteMessage" in fake.calls
        bad = _chan(fake, "BAD:TOKEN")
        assert await bad.delete_message("42", "77") is False  # 失败只返回 False，不抛异常
        await ch.close()
        await bad.close()

    asyncio.run(go())



def test_long_sender_name_still_sends(fake):
    # 标题不参与拆分：超长发件人名曾让整条通知超过 4096、被拒收且不可重试
    async def go():
        ch = _chan(fake)
        await ch.send("42", OutMessage("body", title="📬 " + "N" * 5000))
        await ch.close()

    asyncio.run(go())
    assert not fake.rejected and fake.sent[-1]["text"].endswith("body")


def test_throttled_later_part_is_retried_in_place(fake):
    # 第 2 段被限流：就地重试这一段，不能整条交回 outbox（那样第 1 段会再发一遍）
    fake.throttle_sends = {2}

    async def go():
        ch = _chan(fake)
        await ch.send("42", OutMessage("行\n" * 3000))
        await ch.close()

    asyncio.run(go())
    parts = [s["text"] for s in fake.sent if s["_method"] == "sendMessage"]
    assert len(parts) >= 2 and len(parts) == len(set(parts)) and fake.calls.count("sendMessage") == len(parts) + 1
