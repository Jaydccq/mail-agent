"""模型写的 Markdown（OutMessage.markdown=True：聊天回复、摘要）→ 聊天渠道能显示的格式。其余消息原样显示，不经过这里。

解析一次，两种输出：
- Telegram HTML（telegram_pieces）
- 飞书 / Lark 卡片 markdown 组件（lark_markdown）：它自己支持粗体、斜体、删除线、列表、``` 代码块，中文标点旁的粗体也行；
  不支持 `行内代码`、# 标题、表格（2026-11-01 在真飞书上看过），这三样在这里转换

规则：
- 行内用 markdown-it-py 按 CommonMark 解析（不认 HTML）：**粗体** *斜体* ~~删除线~~ `代码` [文字](链接)；
  孤立的 **、2*3、snake_case 这类不是格式，原样保留
- CommonMark 的 flanking 规则让紧挨全角标点的粗体失效（**截止：**明天，CJK 的已知问题）：
  解析后文字里还成对剩下的 **x** 照样加粗
- 行级：# 标题 → 粗体行；- / * / + 列表 → Telegram 用 •，飞书留给它自己的列表；``` 代码块 → <pre> / 原样；
  表格 → 列表（每行一项：第一列加粗，其余列 "表头: 值" 各占一行），手机上 | --- | 没法看
- Telegram 不许 code/pre 嵌在别的实体里（https://core.telegram.org/bots/api#formatting-options），
  所以遇到 `代码` 先关掉外层格式、之后再打开：**a `b` c** → <b>a </b><code>b</code><b> c</b>
- 链接渲染成 "文字 (网址)"：网址始终可见，也不会因为 Telegram 不认的 href 让整条消息发送失败
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Callable

from markdown_it import MarkdownIt

_MD = MarkdownIt("zero").enable(["emphasis", "backticks", "strikethrough", "link", "escape"])
_TAGS = {"strong": "b", "em": "i", "s": "s"}
_HEADING = re.compile(r"^ {0,3}#{1,6}[ \t]+(.+?)(?:[ \t]+#+)?[ \t]*$")
_BULLET = re.compile(r"^([ \t]*)[-*+][ \t]+(.*)$")
_FENCE = re.compile(r"^[ \t]*```[\w+.-]*[ \t]*$")  # 只有 ``` 加可选语言名；"```ls -la```" 是行内代码
_TABLE_ROW = re.compile(r"^[ \t]*\|.*\|[ \t]*$")
_TABLE_SEP = re.compile(r"^[ \t]*\|[ \t:|-]*-[ \t:|-]*$")
_LOOSE_BOLD = re.compile(r"(?<!\*)\*\*(?=[^\s*])(.+?)(?<=[^\s*])\*\*(?!\*)")


def _lark_escape(s: str) -> str:
    """飞书 markdown 组件里 & < > 要用 HTML 实体。"""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


@dataclass(frozen=True)
class Style:
    tags: dict[str, tuple[str, str]]  # b / i / s → (开, 关)
    escape: Callable[[str], str]
    code: Callable[[str], str]  # 行内代码
    code_breaks_tags: bool  # Telegram：代码不能在别的实体里
    bullet: str
    sub_item: str  # 表格转成列表时，每列一行的前缀
    pre: Callable[[str], str]  # 代码块


TELEGRAM = Style(tags={"b": ("<b>", "</b>"), "i": ("<i>", "</i>"), "s": ("<s>", "</s>")}, escape=html.escape,
                 code=lambda s: f"<code>{html.escape(s)}</code>", code_breaks_tags=True, bullet="• ", sub_item="  ",
                 pre=lambda s: f"<pre>{html.escape(s)}</pre>")
LARK = Style(tags={"b": ("**", "**"), "i": ("*", "*"), "s": ("~~", "~~")}, escape=_lark_escape,
             code=_lark_escape, code_breaks_tags=False, bullet="- ", sub_item="  - ",
             pre=lambda s: f"```\n{_lark_escape(s)}\n```")


def inline(src: str, style: Style = TELEGRAM, base: tuple[str, ...] = ()) -> str:
    """一行 Markdown → 目标格式。base：整行的外层格式（标题用 ("b",)）。"""
    out: list[str] = []
    want: list[str] = list(base)  # 当前应有的格式（由外到内）
    opened: list[str] = []  # 实际已输出、还没关的标签
    links: list[tuple[str, list[str]]] = []  # (网址, 链接文字)

    def sync(target: list[str]) -> None:
        target = list(dict.fromkeys(target))  # 标题里的 **x** 不再套一层粗体
        keep = 0
        while keep < min(len(opened), len(target)) and opened[keep] == target[keep]:
            keep += 1
        out.extend(style.tags[t][1] for t in reversed(opened[keep:]))
        out.extend(style.tags[t][0] for t in target[keep:])
        opened[keep:] = target[keep:]

    def emit(s: str, tags: list[str]) -> None:
        if s:
            sync(tags)  # 格式标签懒打开：没有文字就不会出现空的 <b></b>
            out.append(style.escape(s))

    def text(s: str) -> None:
        for _, buf in links:
            buf.append(s)
        pos = 0
        for m in _LOOSE_BOLD.finditer(s):
            emit(s[pos:m.start()], want)
            emit(m.group(1), want + ["b"])
            pos = m.end()
        emit(s[pos:], want)

    for tok in _MD.parseInline(src)[0].children or []:
        kind, _, edge = tok.type.rpartition("_")
        if tok.type == "code_inline":
            sync([] if style.code_breaks_tags else want)
            out.append(style.code(tok.content))
            for _, buf in links:
                buf.append(tok.content)
        elif kind in _TAGS and edge == "open":
            want.append(_TAGS[kind])
        elif kind in _TAGS and edge == "close":
            want.pop()
        elif tok.type == "link_open":
            links.append((_MD.normalizeLinkText(str(tok.attrs.get("href", ""))), []))
        elif tok.type == "link_close":
            href, buf = links.pop()
            if href and href != "".join(buf):
                text(f" ({href})")
        else:
            text(tok.content)
    sync([])
    return "".join(out)


def _cells(row: str) -> list[str]:
    return [c.strip() for c in row.strip().strip("|").split("|")]


def _table(header: list[str], rows: list[str], style: Style) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for row in rows:
        first, *rest = _cells(row)
        out.append((f"{style.bullet}{inline(first, style, base=('b',))}", row))
        for label, value in zip(header[1:], rest, strict=False):  # 行比表头多/少几格都照常显示
            if value:
                item = f"{inline(label, style)}: {inline(value, style)}" if label else inline(value, style)
                out.append((style.sub_item + item, row))
    return out


def _pieces(text: str, style: Style) -> list[tuple[str, str]]:
    """Markdown 正文 → [(一段输出, 对应原文)]。每段自身格式完整（一行，或整个代码块）。"""
    lines = text.split("\n")
    out: list[tuple[str, str]] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if _FENCE.match(line):
            end = i + 1
            while end < len(lines) and not _FENCE.match(lines[end]):
                end += 1
            body = "\n".join(lines[i + 1:end])
            if body.strip():
                out.append((style.pre(body), body))
            i = end + 1
            continue
        if _TABLE_ROW.match(line) and i + 1 < len(lines) and _TABLE_SEP.match(lines[i + 1]):
            end = i + 2
            while end < len(lines) and _TABLE_ROW.match(lines[end]):
                end += 1
            out += _table(_cells(line), lines[i + 2:end], style)
            i = end
            continue
        heading, bullet = _HEADING.match(line), _BULLET.match(line)
        if heading:
            out.append((inline(heading.group(1), style, base=("b",)), line))
        elif bullet:
            out.append((f"{bullet.group(1)}{style.bullet}{inline(bullet.group(2), style)}", line))
        else:
            out.append((inline(line, style), line))
        i += 1
    return out


def telegram_pieces(text: str, markdown: bool) -> list[tuple[str, str]]:
    """正文 → [(Telegram HTML 段, 对应原文)]，每段标签自身平衡；原文用于单段超长时退回纯文本硬切。
    markdown=False（通知、草稿预览等，含邮件原文）：逐行 html.escape，原样显示。"""
    if not markdown:
        return [(html.escape(line), line) for line in text.split("\n")]
    return _pieces(text, TELEGRAM)


def lark_markdown(text: str) -> str:
    """模型的 Markdown → 飞书卡片 markdown 组件能正确显示的写法（已转义）。"""
    return "\n".join(p for p, _ in _pieces(text, LARK))
