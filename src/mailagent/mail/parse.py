"""把原始 RFC 822 字节解析成 MailMessage。

只用标准库。所有解码都尽量"不抛异常"：坏编码用 errors="replace"，
坏头部原样保留，宁可有乱码也不能丢邮件。
"""

from __future__ import annotations

import email
import html as _html
import logging
import re
from datetime import datetime, timezone
from email.header import decode_header
from email.message import Message
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any

from mailagent.models import MailMessage

log = logging.getLogger(__name__)

BODY_MAX = 20000
QUOTED_MAX = 5000
# html.parser 在超大输入上是超线性的（实测 100 万字符要 5～12 秒，在收信线程上）；
# Gmail 网页版也只显示前约 102KB，正文反正只留 BODY_MAX
HTML_MAX = 200_000

# 常见 charset 别名：gb2312/gbk 实际经常混入超集字符，统一用 gb18030 解码
_CHARSET_ALIASES = {
    "gb2312": "gb18030",
    "gbk": "gb18030",
    "x-gbk": "gb18030",
    "cp936": "gb18030",
    "gb_2312-80": "gb18030",
    "ks_c_5601-1987": "cp949",
    "x-sjis": "shift_jis",
    "unicode-1-1-utf-7": "utf-7",
}


# ---- 解码 ----


def _norm_charset(charset: str | None) -> str | None:
    if not charset:
        return None
    cs = charset.strip().strip('"').lower()
    if cs in ("unknown-8bit", "x-unknown", "unknown", "default", "us-ascii", "ascii"):
        # us-ascii 标了但常常混了 8bit 字符，按无 charset 处理（先试 utf-8）
        return None
    return _CHARSET_ALIASES.get(cs, cs)


def _decode_bytes(data: bytes, charset: str | None) -> str:
    cs = _norm_charset(charset)
    if cs:
        try:
            return data.decode(cs)
        except UnicodeDecodeError:
            return data.decode(cs, errors="replace")
        except (LookupError, ValueError):
            pass  # 未知或坏掉的 charset（"undefined" 抛 UnicodeError，带 NUL 抛 ValueError）：走下面的猜测
    for guess in ("utf-8", "gb18030"):
        try:
            return data.decode(guess)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _fix_surrogates(s: str) -> str:
    """compat32 把头部里的裸 8bit 字节存成 surrogateescape，这里还原成文字。"""
    try:
        s.encode("utf-8")
        return s
    except UnicodeEncodeError:
        return _decode_bytes(s.encode("utf-8", errors="surrogateescape"), None)


def decode_header_value(value: Any) -> str:
    """解码 RFC 2047 头部（=?gb2312?B?...?= 等），失败时返回原文。"""
    if value is None:
        return ""
    raw = _fix_surrogates(str(value))
    try:
        parts = decode_header(raw)
    except Exception:
        return raw
    out: list[str] = []
    for part, charset in parts:
        if isinstance(part, bytes):
            if charset is None:
                # 混合头部里未编码的那段，decode_header 用 raw-unicode-escape 转成了 bytes
                out.append(part.decode("raw-unicode-escape", errors="replace"))
            else:
                out.append(_decode_bytes(part, charset))
        else:
            out.append(part)
    text = "".join(out)
    # 折行残留的换行/制表符
    return re.sub(r"[\r\n\t]+", " ", text).strip()


def header_addresses(msg: Message, name: str) -> list[tuple[str, str]]:
    """先按原始头部拆地址（显示名里的逗号不会误拆），再解码显示名。"""
    values = [_fix_surrogates(str(v)) for v in (msg.get_all(name) or [])]
    if not values:
        return []
    try:
        pairs = getaddresses(values)
    except Exception:
        return []
    out: list[tuple[str, str]] = []
    for disp, addr in pairs:
        addr = addr.strip()
        if not addr or "@" not in addr:
            continue
        out.append((decode_header_value(disp).strip('" '), addr))
    return out


def _parse_date(msg: Message, internal_date: datetime | None) -> datetime:
    value = msg.get("Date")
    if value:
        try:
            dt = parsedate_to_datetime(_fix_surrogates(str(value)))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except (TypeError, ValueError, IndexError, OverflowError):
            pass
    if internal_date is not None:
        if internal_date.tzinfo is None:
            internal_date = internal_date.replace(tzinfo=timezone.utc)
        return internal_date.astimezone(timezone.utc)
    return datetime.now(timezone.utc)


def _part_text(part: Message) -> str:
    try:
        payload = part.get_payload(decode=True)
    except Exception:
        payload = None
    if payload is None:
        raw = part.get_payload()
        return raw if isinstance(raw, str) else ""
    if not isinstance(payload, bytes):
        return str(payload)
    return _decode_bytes(payload, part.get_content_charset())


def _extract_body(msg: Message) -> tuple[str, list[str]]:
    """返回 (正文纯文本, 附件文件名)。优先 text/plain，没有再用 text/html。"""
    plain: list[str] = []
    html: list[str] = []
    attachments: list[str] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        disposition = (part.get_content_disposition() or "").lower()
        filename = part.get_filename()
        if filename:
            filename = decode_header_value(filename)
        if disposition == "attachment" or (filename and not ctype.startswith("text/")):
            attachments.append(filename or "(unnamed attachment)")
            continue
        if filename and disposition != "inline":
            # 带文件名的文本附件（.txt/.csv 等）也算附件，不混进正文
            attachments.append(filename)
            continue
        if ctype == "text/plain":
            plain.append(_part_text(part))
        elif ctype == "text/html":
            html.append(_part_text(part))
    if any(p.strip() for p in plain):
        text = "\n".join(plain)
    elif html:
        text = "\n".join(html_to_text(h) for h in html)
    else:
        text = ""
    return text.replace("\r\n", "\n").replace("\r", "\n"), attachments


def _norm_flags(flags: list[Any] | None) -> list[str]:
    out: list[str] = []
    for f in flags or []:
        out.append(f.decode("utf-8", errors="replace") if isinstance(f, bytes) else str(f))
    return out


def parse_message(
    raw: bytes,
    *,
    mailbox_id: str,
    uid: int,
    gm_msgid: str = "",
    gm_thrid: str = "",
    labels: list[str] | None = None,
    flags: list[str] | None = None,
    internal_date: datetime | None = None,
) -> MailMessage:
    """internal_date 是对契约的补充参数：Date 头解析失败时用它（IMAP INTERNALDATE）。"""
    msg = email.message_from_bytes(raw)  # compat32：头部自己解码，更可控

    from_pairs = header_addresses(msg, "From")
    from_name, from_addr = from_pairs[0] if from_pairs else ("", "")
    if not from_pairs:
        from_name = decode_header_value(msg.get("From"))
    reply_pairs = header_addresses(msg, "Reply-To")

    text, attachments = _extract_body(msg)
    body, quoted = split_quoted(text)

    norm_flags = _norm_flags(flags)
    return MailMessage(
        mailbox_id=mailbox_id,
        uid=uid,
        gm_msgid=gm_msgid,
        gm_thrid=gm_thrid,
        message_id=decode_header_value(msg.get("Message-ID")),
        in_reply_to=decode_header_value(msg.get("In-Reply-To")),
        references=" ".join(decode_header_value(msg.get("References")).split()),
        from_addr=from_addr.lower(),
        from_name=from_name,
        to_addrs=[a for _, a in header_addresses(msg, "To")],
        cc_addrs=[a for _, a in header_addresses(msg, "Cc")],
        subject=decode_header_value(msg.get("Subject")),
        date=_parse_date(msg, internal_date),
        body_text=body[:BODY_MAX],
        quoted_text=quoted[:QUOTED_MAX],
        attachments=attachments,
        labels=list(labels or []),
        is_unread="\\Seen" not in norm_flags,
        list_unsubscribe=msg.get("List-Unsubscribe") is not None,
        reply_to=reply_pairs[0][1] if reply_pairs else "",
    )


# ---- HTML 转纯文本 ----

_SKIP_TAGS = {"script", "style", "head", "title", "noscript", "template"}
_BLOCK_TAGS = {
    "p", "div", "tr", "li", "ul", "ol", "table", "blockquote", "section", "article",
    "header", "footer", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "pre", "center",
}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0
        self._pre = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "body":
            self._skip = 0  # 有的邮件 <head> 不闭合
        if tag in _SKIP_TAGS:
            self._skip += 1
            return
        if tag == "br":
            self.parts.append("\n")
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")
            if tag == "pre":
                self._pre += 1
        elif tag in ("td", "th"):
            self.parts.append(" ")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # <br/> 之类自闭合标签：不要触发 handle_endtag 的 skip 计数
        if tag in _SKIP_TAGS:
            return
        self.handle_starttag(tag, attrs)
        if tag == "pre":
            self._pre = max(0, self._pre - 1)

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
            return
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")
            if tag == "pre":
                self._pre = max(0, self._pre - 1)

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        if not self._pre:
            data = re.sub(r"\s+", " ", data)
        self.parts.append(data)


def html_to_text(html: str) -> str:
    html = html[:HTML_MAX]
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
        text = "".join(parser.parts)
    except Exception:
        log.debug("html.parser failed, falling back to regex tag stripping", exc_info=True)
        text = re.sub(r"(?is)<(script|style|head)\b.*?</\1>", " ", html)
        text = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|li)>", "\n", text)
        text = re.sub(r"<[^>]+>", " ", text)
        text = _html.unescape(text)
    text = text.replace("\xa0", " ").replace("​", "").replace("‌", "")
    lines = [ln.strip() for ln in text.split("\n")]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ---- 引用历史切分 ----

# "On Mon, Oct 30, 2026 at 10:00 AM John <a@b.com> wrote:"（Gmail/Apple Mail，常被折成 2~3 行）
_RE_ON_WROTE = re.compile(r"^On\s.+\swrote\s*:\s*$", re.IGNORECASE | re.DOTALL)
# "在 2026年10月30日 周五 10:00，张三 <a@b.com> 写道："（Gmail 中文界面等）
_RE_ZAI_WROTE = re.compile(r"^在\s*.+(写道|寫道)\s*[:：]\s*$", re.DOTALL)
# Outlook / QQ / Foxmail 分隔线
_RE_ORIGINAL = re.compile(
    r"^\s*-{2,}\s*(Original Message|原始邮件|原始郵件|原始信息|原邮件)\s*-{2,}\s*$",
    re.IGNORECASE,
)
_RE_FROM = re.compile(r"^\s*\*?\s*(From|发件人|寄件者|發件人)\s*\*?\s*[:：]", re.IGNORECASE)
_RE_SENT = re.compile(
    r"^\s*\*?\s*(Sent|Date|发送时间|发送日期|日期|时间|寄件日期|傳送時間)\s*\*?\s*[:：]",
    re.IGNORECASE,
)
_RE_UNDERSCORES = re.compile(r"^\s*_{8,}\s*$")
# 转发的正文不算引用历史（Gmail 英文/中文界面、Apple Mail）
_RE_FORWARDED = re.compile(r"Forwarded message|Begin forwarded message|转发的邮件|轉寄的郵件", re.IGNORECASE)


def _quote_header_at(lines: list[str], i: int) -> bool:
    line = lines[i].strip()
    if not line:
        return False
    if _RE_ORIGINAL.match(line):
        return True
    if line.startswith(("On ", "On\t", "在")):
        # 允许跨最多 3 行
        joined = line
        for j in range(i, min(i + 3, len(lines))):
            if j > i:
                joined += " " + lines[j].strip()
            if _RE_ON_WROTE.match(joined) or _RE_ZAI_WROTE.match(joined):
                return True
    if _RE_FROM.match(line):
        if any(_RE_FORWARDED.search(lines[j]) for j in range(max(0, i - 2), i)):
            return False
        # 只有后面几行里紧跟 Sent:/Date:/发送时间: 才算 Outlook 头部块
        for j in range(i + 1, min(i + 5, len(lines))):
            if _RE_SENT.match(lines[j]):
                return True
    return False


def _trailing_gt_block(lines: list[str]) -> int | None:
    """末尾连续的 ">" 引用块（中间允许空行）的起始行；中间夹着回复的不算。"""
    start: int | None = None
    for i in range(len(lines) - 1, -1, -1):
        s = lines[i].strip()
        if not s:
            continue
        if s.startswith(">"):
            start = i
        else:
            break
    return start


def split_quoted(text: str) -> tuple[str, str]:
    """(本次正文, 引用历史)。识别不到引用时引用历史为空。"""
    if not text:
        return "", ""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    cut: int | None = None
    for i in range(len(lines)):
        if _quote_header_at(lines, i):
            cut = i
            if i > 0 and _RE_UNDERSCORES.match(lines[i - 1]):
                cut = i - 1  # Outlook 头部块前的下划线分隔线
            break
    gt = _trailing_gt_block(lines)
    if gt is not None and (cut is None or gt < cut):
        cut = gt
    if cut is None:
        return text.strip(), ""
    body = "\n".join(lines[:cut]).strip()
    quoted = "\n".join(lines[cut:]).strip()
    if not body:
        # 整封都像引用（例如转发的 Outlook 邮件），不切，免得正文为空
        return text.strip(), ""
    return body, quoted
