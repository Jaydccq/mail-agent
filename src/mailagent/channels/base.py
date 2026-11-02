"""聊天渠道接口。Telegram 和 Lark 各实现一个。

适配器只负责平台协议：收消息 -> Inbound，发 OutMessage -> 平台消息 id。
"谁是主人"、去重、业务逻辑都在 agent 层，适配器把所有入站都交上去（群聊标 is_private=False）。
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Protocol

from mailagent.models import Inbound, OutMessage

InboundHandler = Callable[[Inbound], Awaitable[None]]


class ChannelError(Exception):
    """发送失败。retryable=True 表示可稍后重试（网络/429/5xx）。"""

    def __init__(self, message: str, retryable: bool = True, retry_after: float | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


class Channel(Protocol):
    name: str  # "telegram" | "lark"

    async def start(self, on_inbound: InboundHandler) -> None:
        """一直运行到被 cancel：接收消息和按钮回调，逐个 await on_inbound。
        on_inbound 抛异常要记日志并继续，不能让接收循环退出。断线自动重连（带退避）。"""
        ...

    async def send(self, chat_id: str, msg: OutMessage) -> str:
        """发消息，返回平台 message id。失败抛 ChannelError。超长文本自己拆分，按钮放最后一段。"""
        ...

    async def edit(self, chat_id: str, message_id: str, msg: OutMessage) -> None:
        """改写已发出的消息（用于"思考中…" -> 最终答案、按钮点完后去掉按钮）。失败抛 ChannelError。"""
        ...

    async def delete_message(self, chat_id: str, message_id: str) -> bool:
        """删除一条消息（用于删掉用户发来的含应用专用密码的消息）。尽力而为：成功 True，失败 False，绝不抛异常。"""
        ...

    async def ack_callback(self, inbound: Inbound, toast: str = "") -> None:
        """确认按钮回调（Telegram answerCallbackQuery）。没有这个概念的平台可以什么都不做。"""
        ...

    async def typing(self, chat_id: str) -> None:
        """显示"正在输入"。不支持就什么都不做，绝不抛异常。"""
        ...

    async def check(self) -> str:
        """验证凭据可用，返回 bot 名称等可读信息；失败抛 ChannelError(retryable=False)。"""
        ...

    async def pair(self, code: str, timeout: float = 300) -> tuple[str, str]:
        """setup 向导用：等待某个用户私聊发来 code，返回 (chat_id, user_id)。超时抛 TimeoutError。
        不依赖 start() 是否在运行。"""
        ...

    async def close(self) -> None:
        ...


PART_RETRIES = 3


async def send_parts(n: int, send_part: Callable[[int], Awaitable[str]]) -> str:
    """按顺序发一条长消息的 n 段，返回最后一段的平台 message id。

    第一段失败照常抛：整条消息交回 outbox 稍后重发，没有重复。后面的段遇到可重试的错误（限流/网络/5xx）就地重试
    最多 PART_RETRIES 次：交回 outbox 的话，已经发出去的前几段会再发一遍。重试用完仍失败才抛（宁可重复，不能丢）。"""
    last_id = ""
    for i in range(n):
        for attempt in range(PART_RETRIES):
            try:
                last_id = await send_part(i)
                break
            except ChannelError as e:
                if i == 0 or not e.retryable or attempt == PART_RETRIES - 1:
                    raise
                await asyncio.sleep(min(e.retry_after if e.retry_after is not None else 2 ** attempt, 30))
    return last_id
