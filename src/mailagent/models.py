"""各模块之间传递的数据结构。改字段前先看 CONTRACTS.md。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class MailMessage:
    mailbox_id: str
    uid: int  # INBOX 内的 IMAP UID（搜索结果里可能来自 All Mail，此时 uid=0）
    gm_msgid: str  # Gmail X-GM-MSGID 十进制字符串；非 Gmail 服务器为 ""
    gm_thrid: str  # Gmail X-GM-THRID；非 Gmail 为 ""
    message_id: str  # RFC 5322 Message-ID 头
    in_reply_to: str
    references: str
    from_addr: str  # 小写邮箱地址
    from_name: str
    to_addrs: list[str]
    cc_addrs: list[str]
    subject: str
    date: datetime  # 带时区（UTC）
    body_text: str  # 本次新写的正文（已去掉引用历史），最多约 20k 字符
    quoted_text: str = ""  # 引用的旧邮件，最多约 5k 字符
    attachments: list[str] = field(default_factory=list)  # 附件文件名
    labels: list[str] = field(default_factory=list)  # Gmail 标签，例如 "\\Important"
    is_unread: bool = True
    list_unsubscribe: bool = False  # 有 List-Unsubscribe 头（订阅/营销信号）
    reply_to: str = ""  # Reply-To 头里的地址（回复时优先用）

    @property
    def snippet(self) -> str:
        return " ".join(self.body_text.split())[:200]

    @property
    def dedupe_key(self) -> str:
        return f"{self.mailbox_id}:{self.gm_msgid or self.message_id or self.uid}"


@dataclass
class Triage:
    """Jev 的分拣结果。数值都是 Jev 返回的概率/期望分，不是正确率。"""

    importance: float  # 0..3：0 噪音/营销，1 普通知会，2 今天值得知道，3 需要立刻关注
    needs_action: float  # 0..1：需要收件人本人做事（回复/付款/签字/出席/提交）
    has_time: float  # 0..1：提到与收件人相关的截止日期/会议/时间
    category: str  # 见 ai/jev.py 的 CATEGORIES
    confidence: float  # importance 题的 confidence
    source: str = "jev"  # jev | fallback（Jev 不可用时的启发式）


@dataclass
class Deadline:
    what: str  # 一句话说明，例如 "提交报销单"
    date: str  # 本地日期 YYYY-MM-DD
    time: str | None = None  # 本地 HH:MM；邮件只给日期时为 None，不要编造 23:59
    kind: str = "deadline"  # deadline | meeting | event | renewal | reply_by | other
    timezone: str | None = None  # 邮件明确写了时区才填 IANA 名


@dataclass
class Analysis:
    summary: str  # 2-3 句话，用户语言
    action: str | None  # 用户需要做什么；不需要则 None
    deadlines: list[Deadline] = field(default_factory=list)
    reply_suggested: bool = False
    source: str = "llm"  # llm | template


# ---- 渠道 ----


@dataclass
class Button:
    label: str
    data: str  # 不透明短引用，<= 60 字节 ASCII，格式 "verb:arg[:arg2]"，语义见 CONTRACTS.md


@dataclass
class OutMessage:
    text: str  # 纯文本（可含换行和 emoji），渠道适配器负责转义
    title: str | None = None  # Telegram 渲染成粗体首行；Lark 渲染成卡片标题
    buttons: list[list[Button]] = field(default_factory=list)  # 按行排列
    markdown: bool = False  # text 是模型写的 Markdown（聊天回复、摘要），渠道按格式渲染；否则原样显示（草稿预览必须原样）


@dataclass
class Inbound:
    channel: str  # "telegram" | "lark"
    chat_id: str  # 回复时传给 Channel.send 的 id（Lark 私聊 = 发送者 open_id）
    user_id: str  # 发送者稳定 id（Telegram user id / Lark open_id）
    event_id: str  # 平台事件 id，用于去重
    text: str = ""
    callback_data: str | None = None  # 按钮回调时为 Button.data
    message_id: str | None = None  # 按钮所在消息 id（回调时）或用户消息 id
    is_private: bool = True  # 群聊消息一律 False，app 会丢弃
