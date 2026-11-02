"""模型 Markdown → Telegram HTML / 飞书卡片 markdown。Telegram 的输出都过 tg_html_ok（fake_telegram.py）：只用
Telegram 认的标签、标签平衡、code/pre 不和别的实体嵌套（Telegram 遇到不合法的 HTML 会拒收整条消息）。
飞书那部分按 2026-11-01 在真飞书上看到的效果写：不支持 `行内代码`、# 标题、表格。"""

import html
import json
import random
from collections import Counter
from dataclasses import asdict

import pytest

from fake_telegram import tg_html_ok
from mailagent.agent import render
from mailagent.app import _out_from_payload
from mailagent.channels.lark import _build_card
from mailagent.channels.telegram import MAX_TEXT, TelegramChannel
from mailagent.channels.chat_format import inline, lark_markdown
from mailagent.models import OutMessage


def tg(text: str, markdown: bool = True, title: str | None = None) -> list[str]:
    chunks = TelegramChannel._render_chunks(OutMessage(text=text, title=title, markdown=markdown))
    for c in chunks:
        assert len(c) <= MAX_TEXT
        tg_html_ok(c)
    return chunks


def test_formatting_sample_from_live_telegram():
    """在真 Telegram 上看到的那条 formatting test：**粗体里带 `代码`** 以前原样露出星号。"""
    out = tg("# Heading line\nPaid **$12.40** for `order #4471` <ok> & more\n"
             "a lone ** here, 2*3, and **bold with `code` inside**\n• bullet stays as is",
             title="mail-agent formatting test (safe to delete)")[0]
    assert out.split("\n") == [
        "<b>mail-agent formatting test (safe to delete)</b>",
        "<b>Heading line</b>",
        "Paid <b>$12.40</b> for <code>order #4471</code> &lt;ok&gt; &amp; more",
        "a lone ** here, 2*3, and <b>bold with </b><code>code</code><b> inside</b>",
        "• bullet stays as is",
    ]


@pytest.mark.parametrize("src, want", [
    ("**a *b* c**", "<b>a <i>b</i> c</b>"),
    ("*it* and _it_ and ~~gone~~", "<i>it</i> and <i>it</i> and <s>gone</s>"),
    ("snake_case_name, 2*3, a ** b **, ***", "snake_case_name, 2*3, a ** b **, ***"),
    ("**unclosed bold", "**unclosed bold"),
    ("`**not bold**` and `<b>`", "<code>**not bold**</code> and <code>&lt;b&gt;</code>"),
    (r"\*literal\* and C:\path", "*literal* and C:\\path"),
    ("<b>raw html</b> &amp;", "&lt;b&gt;raw html&lt;/b&gt; &amp;amp;"),
    ("see [the doc](https://ex.com/a?b=1&c=2)", "see the doc (https://ex.com/a?b=1&amp;c=2)"),
    ("[https://ex.com](https://ex.com)", "https://ex.com"),
    ("[Verify account](javascript:alert(1))", "[Verify account](javascript:alert(1))"),
    ("**`code` first**", "<code>code</code><b> first</b>"),
    ("~~**`x`**~~", "<code>x</code>"),
    # CommonMark 的 flanking 规则让紧挨全角标点的 ** 失效（CJK 已知问题）；成对剩下的 ** 照样加粗
    ("**截止日期：**11月6日", "<b>截止日期：</b>11月6日"),
    ("时间是**11/9（周一）**开会，**注意**：带电脑", "时间是<b>11/9（周一）</b>开会，<b>注意</b>：带电脑"),
    ("**“引用”**的 *斜体：*文字", "<b>“引用”</b>的 *斜体：*文字"),
    ("**Note:**tomorrow", "<b>Note:</b>tomorrow"),
    ("*it* **a** b ** c **", "<i>it</i> <b>a</b> b ** c **"),
])
def test_inline(src, want):
    assert inline(src) == want
    tg_html_ok(want)


@pytest.mark.parametrize("src, want", [
    ("## Next steps", "<b>Next steps</b>"),
    ("### **Already bold** and `cmd`", "<b>Already bold and </b><code>cmd</code>"),
    ("#4471 is not a heading", "#4471 is not a heading"),
    ("- **Alice** — contract", "• <b>Alice</b> — contract"),
    ("  * nested", "  • nested"),
    ("+ plus", "• plus"),
    ("1. **first**", "1. <b>first</b>"),
    ("**bold line**", "<b>bold line</b>"),
])
def test_line_rules(src, want):
    assert tg(src) == [want]


def test_code_block_is_one_pre_and_not_parsed():
    out = tg("Try this:\n```text\nfrom:alice **x** <y>\nnewer_than:7d\n```\ndone")[0]
    assert out == "Try this:\n<pre>from:alice **x** &lt;y&gt;\nnewer_than:7d</pre>\ndone"
    assert tg("```\nunterminated **x**") == ["<pre>unterminated **x**</pre>"]
    assert tg("```\n```") == ["\u200b"]  # 空代码块不出 <pre></pre>



def test_inline_triple_backticks_on_one_line_are_not_a_fence():
    # "```ls -la```" 只是行内代码：当成代码块开头的话，这一行会消失，后面整段都变成 <pre>
    out = tg("Run this:\n```ls -la```\nthen check the output.")[0]
    assert "ls -la" in out and "<pre>" not in out and out.endswith("then check the output.")


def test_sender_label_is_clipped():
    assert len(render.sender_label("a@x.example", "N" * 5000)) == render.SENDER_MAX
    assert render.sender_label("a@x.example", "Li Wei") == "Li Wei"

def test_plain_messages_are_shown_exactly():
    src = "# not a heading\n- **Hi** `x` [a](b)\n```"
    out = tg(src, markdown=False)[0]
    assert tg_html_ok(out) == src


def test_draft_preview_stays_literal_while_chat_and_digest_render():
    """草稿预览必须和真正发出去的邮件一字不差；只有模型写的聊天回复和摘要按 Markdown 渲染。"""
    body = "Hi **Alex**,\n- item one\n# not a heading"
    preview = render.action_preview({"id": 3, "code": "123456", "payload": {
        "from": "me@x.example", "to": ["alex@x.example"], "subject": "Re: plan", "body": body}})
    assert preview.markdown is False
    assert body in tg_html_ok("\n".join(TelegramChannel._render_chunks(preview)))
    assert render.digest("**Needs you**\n- x", slot_label="08:30", problems=[], paused=False).markdown is True


def test_outbox_payload_keeps_markdown_flag():
    for md in (True, False):
        msg = OutMessage(text="**x**", title="t", markdown=md)
        assert _out_from_payload(json.dumps(asdict(msg))) == msg


def test_long_code_block_falls_back_to_valid_chunks():
    big = "```\n" + "\n".join(f"line {i} <tag> & **x**" for i in range(400)) + "\n```"
    chunks = tg(big)
    assert len(chunks) >= 2
    assert "line 399 &lt;tag&gt; &amp; **x**" in chunks[-1]


def test_random_markdown_always_valid():
    """随机拼 Markdown 记号，输出必须始终是 Telegram 能收的 HTML，而且字母一个都不丢。"""
    rnd = random.Random(7)
    atoms = ["*", "**", "_", "__", "~~", "`", "[", "]", "(", ")", "](http://e.com/)", "<", ">", "&", "\\",
             "#", "# ", "- ", "a", "bc", " ", "  ", "x_y", "2*3"]
    for _ in range(3000):
        line = "".join(rnd.choice(atoms) for _ in range(rnd.randint(1, 14)))
        out = tg(line)
        if line.lstrip().startswith("```"):
            continue  # 代码块开头那行（语言名）本来就不显示
        seen = html.unescape("".join(tg_html_ok(c) for c in out))
        assert Counter(c for c in line if c.isalpha()) <= Counter(c for c in seen if c.isalpha()), (line, out)


def test_lark_card_plain_is_shown_exactly():
    plain = _build_card("T", "**Hi** <b>", [], markdown=False)["elements"][0]
    assert plain == {"tag": "div", "text": {"tag": "plain_text", "content": "**Hi** <b>"}}


@pytest.mark.parametrize("bad", ["<b>a <code>x</code></b>", "<code><b>x</b></code>", "<b>x", "<b>x</i>",
                                 '<a href="u">x</a>', "a &nbsp; b", "", "x" * 4097])
def test_validator_rejects_what_telegram_rejects(bad):
    with pytest.raises(ValueError):
        tg_html_ok(bad)


def test_chat_reply_reaches_fake_telegram_formatted():
    """聊天：模型的 Markdown 回复经 ChatAgent → TelegramChannel → 假 Bot API（不合法的 HTML 会被拒收）。"""
    import asyncio

    from fake_telegram import FakeTelegram
    from mailagent.agent.chat import ChatAgent
    from mailagent.agent.scheduler import Scheduler
    from mailagent.config import TelegramConfig
    from mailagent.db import Database
    from mailagent.models import Inbound
    from test_suggest import _cfg

    reply = ("**2 emails** from `alice@acme.example`:\n- **Contract v3** — sign by *Friday*\n"
             "- Invoice <#4471> & receipt\nRun **`/todo` to see them**")

    class LLM:
        name = "md"

        async def complete(self, system, prompt, schema=None, timeout=None):
            return {"reply": reply, "tool": None, "args_json": None, "suggest_memory": None}

    async def main():
        fake = FakeTelegram().start()
        ch = TelegramChannel(TelegramConfig(bot_token=fake.token, api_base=fake.api_base))
        cfg, db = _cfg(), Database(":memory:")
        agent = ChatAgent(cfg, db, LLM(), {"telegram": ch}, Scheduler(cfg, db, LLM()))
        try:
            await agent.handle(Inbound(channel="telegram", chat_id="42", user_id="42", event_id="e1",
                                       text="anything from alice?"))
        finally:
            await ch.close()
            fake.stop()
        assert not fake.rejected, fake.rejected
        final = fake.sent[-1]
        assert final["_method"] == "editMessageText"  # "🤔 Thinking…" 改写成回复
        assert final["text"].split("\n") == [
            "<b>2 emails</b> from <code>alice@acme.example</code>:",
            "• <b>Contract v3</b> — sign by <i>Friday</i>",
            "• Invoice &lt;#4471&gt; &amp; receipt",
            "Run <code>/todo</code><b> to see them</b>",
        ]

    asyncio.run(main())


def test_table_becomes_a_list():
    """模型（Codex 被明确要求时）还是会写表格；手机上 | --- | 没法看，转成列表。"""
    out = tg("Here you go:\n| Email | Key details | Deadline / action |\n|---|:---:|---|\n"
             "| Alex: **Contract v3** | Legal approved | Sign by **Nov 6**; start Nov 9 |\n"
             "| `DocuSign` | | Link expires Nov 13 |\nDone.")[0]
    assert out.split("\n") == [
        "Here you go:",
        "• <b>Alex: Contract v3</b>",
        "  Key details: Legal approved",
        "  Deadline / action: Sign by <b>Nov 6</b>; start Nov 9",
        "• <code>DocuSign</code>",
        "  Deadline / action: Link expires Nov 13",
        "Done.",
    ]
    assert tg("| not | a table |\nplain a | b") == ["| not | a table |\nplain a | b"]


def test_lark_markdown_fills_feishu_gaps():
    """飞书卡片 markdown 本身支持粗体/斜体/删除线/列表/代码块（中文标点旁的粗体也行）；
    不支持的三样在这里转换：标题 → 粗体行，`代码` → 去掉反引号，表格 → 嵌套列表。链接和 Telegram 一样露出网址。"""
    src = ("## Alex 上周的邮件\n**截止日期：**11月6日 `alex@acme.example` <ok> & more\n"
           "* nested parent\n  - child with [the agenda](https://ex.com/a?b=1&c=2)\n"
           "| Email | Deadline |\n|---|---|\n| **Contract v3** | Sign by `Nov 6` |\n"
           "```\nfrom:alex <x>\n```\n~~old~~ *new*")
    assert lark_markdown(src).split("\n") == [
        "**Alex 上周的邮件**",
        "**截止日期：**11月6日 alex@acme.example &lt;ok&gt; &amp; more",
        "- nested parent",
        "  - child with the agenda (https://ex.com/a?b=1&amp;c=2)",
        "- **Contract v3**",
        "  - Deadline: Sign by Nov 6",
        "```",
        "from:alex &lt;x&gt;",
        "```",
        "~~old~~ *new*",
    ]


def test_lark_send_converts_model_markdown_only():
    """飞书：模型回复按上面转换后用 markdown 组件；其余消息 plain_text 原样。"""
    md = _build_card("T", lark_markdown("# Hi <b>"), [], markdown=True)["elements"][0]
    assert md == {"tag": "markdown", "content": "**Hi &lt;b&gt;**"}
