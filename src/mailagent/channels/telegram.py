"""Telegram 渠道：httpx 直连 Bot API，getUpdates 长轮询，不需要公网地址。

- 每个 update 交给 on_inbound，返回后才推进 offset；崩溃后可能重复投递，上层用 event_id（= update_id）去重。
- 发送用 parse_mode=HTML：title 加粗作首行，正文 html.escape（msg.markdown 时先转换 Markdown，见 chat_format）；
  超长按行拆分，按钮只放最后一段。
- token 绝不进日志/异常：自己的消息统一过 _scrub，另外给 httpx 的 logger 挂了脱敏过滤器
  （httpx 在 INFO 级别会打印带 token 的请求 URL）。
"""

from __future__ import annotations

import asyncio
import html
import logging
from collections import OrderedDict
from typing import Any

import httpx

from mailagent.channels import chat_format
from mailagent.channels.base import ChannelError, InboundHandler, send_parts
from mailagent.config import TelegramConfig
from mailagent.models import Inbound, OutMessage

log = logging.getLogger(__name__)

MAX_TEXT = 4096  # Telegram 单条消息上限
TITLE_MAX = 256  # 标题那一段不参与拆分：转义后最多约 1.5k，留足正文的位置
POLL_TIMEOUT = 50  # getUpdates 长轮询秒数
ALLOWED_UPDATES = ["message", "callback_query"]
BACKOFF_START = 2.0
BACKOFF_MAX = 60.0
CB_ID_CACHE = 500  # event_id -> callback_query.id 最多缓存条数


class _TgError(ChannelError):
    """带 Bot API error_code 的 ChannelError。"""

    def __init__(self, message: str, code: int, retryable: bool = True, retry_after: float | None = None):
        super().__init__(message, retryable=retryable, retry_after=retry_after)
        self.code = code


class _RedactFilter(logging.Filter):
    """把日志记录里的 token 换成 ***（作用于 httpx 的 logger）。"""

    def __init__(self, token: str):
        super().__init__()
        self.token = token

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        if self.token in msg:
            record.msg = msg.replace(self.token, "***")
            record.args = None
        return True


_redact_filters: dict[str, _RedactFilter] = {}


def _install_redact(token: str) -> None:
    if not token or token in _redact_filters:
        return
    f = _RedactFilter(token)
    _redact_filters[token] = f
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).addFilter(f)


def _is_pair_text(text: str, code: str) -> bool:
    """配对码本身，或点 t.me/<bot>?start=<code> 链接后 Telegram 发来的 "/start <code>"。"""
    parts = text.split()
    if len(parts) == 2 and parts[0].split("@", 1)[0].lower() == "/start":
        parts = parts[1:]
    return parts == [code]


class TelegramChannel:
    name = "telegram"

    def __init__(self, cfg: TelegramConfig, http: httpx.AsyncClient | None = None):
        self.cfg = cfg
        self._token = cfg.bot_token.strip()
        self._base = f"{cfg.api_base.rstrip('/')}/bot{self._token}"
        self._own_http = http is None
        self._http = http or httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))
        self._offset: int | None = None
        self._cb_ids: OrderedDict[str, str] = OrderedDict()
        _install_redact(self._token)

    # ---- 基础请求 ----

    def _scrub(self, s: str) -> str:
        return s.replace(self._token, "***") if self._token else s

    async def _call(self, method: str, params: dict[str, Any] | None = None,
                    timeout: float | None = None) -> Any:
        """调 Bot API，返回 result 字段。失败抛 ChannelError（信息里不含 token）。"""
        kw: dict[str, Any] = {"json": params or {}}
        if timeout is not None:
            kw["timeout"] = httpx.Timeout(timeout, connect=10.0)
        try:
            r = await self._http.post(f"{self._base}/{method}", **kw)
        except httpx.HTTPError as e:
            raise ChannelError(f"telegram {method} network error: {type(e).__name__}: {self._scrub(str(e))}",
                               retryable=True) from None
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code == 200 and data.get("ok"):
            return data.get("result")

        code = int(data.get("error_code") or r.status_code)
        desc = self._scrub(str(data.get("description") or r.reason_phrase or ""))
        msg = f"telegram {method} failed {code}: {desc}"
        if code == 429:
            retry_after = (data.get("parameters") or {}).get("retry_after")
            raise _TgError(msg, code, retryable=True,
                           retry_after=float(retry_after) if retry_after is not None else None)
        if code >= 500 or code == 409:
            # 409：另一个进程在 getUpdates 或又被设了 webhook，稍后重试
            raise _TgError(msg, code, retryable=True)
        raise _TgError(msg, code, retryable=False)

    # ---- 接收 ----

    async def _ensure_no_webhook(self) -> None:
        info = await self._call("getWebhookInfo")
        if info and info.get("url"):
            log.warning("telegram bot has a webhook set; deleting it before long polling (pending updates are kept)")
            await self._call("deleteWebhook", {"drop_pending_updates": False})

    async def _get_updates(self, offset: int | None, timeout: int) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"timeout": timeout, "allowed_updates": ALLOWED_UPDATES}
        if offset is not None:
            params["offset"] = offset
        return await self._call("getUpdates", params, timeout=timeout + 15) or []

    async def start(self, on_inbound: InboundHandler) -> None:
        backoff = BACKOFF_START
        webhook_checked = False
        while True:
            try:
                if not webhook_checked:
                    await self._ensure_no_webhook()
                    webhook_checked = True
                updates = await self._get_updates(self._offset, POLL_TIMEOUT)
                backoff = BACKOFF_START
            except asyncio.CancelledError:
                raise
            except ChannelError as e:
                if e.retryable:
                    wait = max(backoff, e.retry_after or 0)
                    log.warning("telegram polling failed, retrying in %.0fs: %s", wait, e)
                else:
                    wait = BACKOFF_MAX
                    log.error("telegram polling failed (not retryable), trying again in %.0fs: %s", wait, e)
                await asyncio.sleep(wait)
                backoff = min(backoff * 2, BACKOFF_MAX)
                continue

            for upd in updates:
                inbound = self._parse_update(upd)
                if inbound is not None:
                    try:
                        await on_inbound(inbound)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        log.exception("error handling telegram update %s", upd.get("update_id"))
                self._offset = int(upd["update_id"]) + 1

    def _parse_update(self, upd: dict[str, Any]) -> Inbound | None:
        event_id = str(upd.get("update_id"))
        if "message" in upd:
            m = upd["message"]
            text = m.get("text")
            if not isinstance(text, str):
                return None  # 只处理文本
            chat = m.get("chat") or {}
            return Inbound(
                channel=self.name,
                chat_id=str(chat.get("id")),
                user_id=str((m.get("from") or {}).get("id", "")),
                event_id=event_id,
                text=text,
                message_id=str(m.get("message_id")),
                is_private=chat.get("type") == "private",
            )
        if "callback_query" in upd:
            cq = upd["callback_query"]
            m = cq.get("message") or {}
            chat = m.get("chat") or {}
            self._cb_ids[event_id] = str(cq.get("id"))
            while len(self._cb_ids) > CB_ID_CACHE:
                self._cb_ids.popitem(last=False)
            return Inbound(
                channel=self.name,
                chat_id=str(chat.get("id", "")),
                user_id=str((cq.get("from") or {}).get("id", "")),
                event_id=event_id,
                callback_data=cq.get("data"),
                message_id=str(m.get("message_id")) if m.get("message_id") is not None else None,
                is_private=chat.get("type", "private") == "private",
            )
        return None

    # ---- 发送 ----

    @staticmethod
    def _render_chunks(msg: OutMessage) -> list[str]:
        """渲染成 HTML（msg.markdown 时转换 Markdown，见 chat_format）并拆成 <= MAX_TEXT 的段。
        每段标签自身平衡，转义后再计长度，不会切断实体。"""
        pieces: list[str] = []
        if msg.title:
            title = msg.title if len(msg.title) <= TITLE_MAX else msg.title[:TITLE_MAX - 1] + "…"
            pieces.append(f"<b>{html.escape(title)}</b>")
        for esc, raw in chat_format.telegram_pieces(msg.text or "", msg.markdown):
            if len(esc) <= MAX_TEXT:
                pieces.append(esc)
                continue
            # 单段过长：按原文切小块再转义（放弃格式）
            buf = ""
            for ch in raw:
                e = html.escape(ch)
                if len(buf) + len(e) > MAX_TEXT:
                    pieces.append(buf)
                    buf = ""
                buf += e
            if buf:
                pieces.append(buf)

        chunks: list[str] = []
        cur: str | None = None
        for p in pieces:
            if cur is None:
                cur = p
            elif len(cur) + 1 + len(p) <= MAX_TEXT:
                cur += "\n" + p
            else:
                chunks.append(cur)
                cur = p
        if cur is not None:
            chunks.append(cur)
        chunks = [c for c in chunks if c.strip()]
        return chunks or ["​"]  # 空消息 Telegram 不收

    @staticmethod
    def _markup(msg: OutMessage) -> dict[str, Any] | None:
        if not msg.buttons:
            return None
        rows = [[{"text": b.label, "callback_data": b.data} for b in row] for row in msg.buttons if row]
        return {"inline_keyboard": rows} if rows else None

    async def send(self, chat_id: str, msg: OutMessage) -> str:
        chunks = self._render_chunks(msg)
        markup = self._markup(msg)

        async def send_part(i: int) -> str:
            params: dict[str, Any] = {
                "chat_id": chat_id,
                "text": chunks[i],
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
            if markup and i == len(chunks) - 1:
                params["reply_markup"] = markup
            res = await self._call("sendMessage", params)
            return str(res.get("message_id"))

        return await send_parts(len(chunks), send_part)

    async def edit(self, chat_id: str, message_id: str, msg: OutMessage) -> None:
        chunks = self._render_chunks(msg)
        text = chunks[0]
        if len(chunks) > 1 and len(text) + 2 <= MAX_TEXT:
            text += "\n…"  # 编辑只能改一条：超长时只保留第一段
        params: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": int(message_id),
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        markup = self._markup(msg)
        if markup:
            params["reply_markup"] = markup  # 不传则去掉按钮
        try:
            await self._call("editMessageText", params)
        except ChannelError as e:
            if "message is not modified" in str(e):
                return
            raise

    async def ack_callback(self, inbound: Inbound, toast: str = "") -> None:
        cb_id = self._cb_ids.pop(inbound.event_id, None)
        if not cb_id:
            return
        params: dict[str, Any] = {"callback_query_id": cb_id}
        if toast:
            params["text"] = toast[:200]
        try:
            await self._call("answerCallbackQuery", params)
        except ChannelError as e:
            # 回调超时（>15 分钟）等情况下会失败，不影响业务
            log.warning("telegram answerCallbackQuery failed: %s", e)

    async def delete_message(self, chat_id: str, message_id: str) -> bool:
        try:
            await self._call("deleteMessage", {"chat_id": chat_id, "message_id": int(message_id)})
            return True
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # _call already scrubs the token; scrub again for non-ChannelError exceptions
            log.warning("telegram deleteMessage failed: %s", self._scrub(str(e)))
            return False

    async def typing(self, chat_id: str) -> None:
        try:
            await self._call("sendChatAction", {"chat_id": chat_id, "action": "typing"})
        except Exception as e:
            log.debug("telegram typing failed: %s", e)

    # ---- setup ----

    async def check(self) -> str:
        try:
            me = await self._call("getMe")
        except ChannelError as e:
            if isinstance(e, _TgError) and e.code in (401, 404):
                raise ChannelError("invalid bot token", retryable=False) from None
            raise ChannelError(f"could not connect to Telegram: {e}", retryable=False) from None
        return f"@{me.get('username', '')}"

    async def pair(self, code: str, timeout: float = 300) -> tuple[str, str]:
        code = code.strip()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        await self._ensure_no_webhook()
        offset = self._offset
        backoff = BACKOFF_START
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError("timed out waiting for the pairing code")
            try:
                updates = await self._get_updates(offset, max(1, min(POLL_TIMEOUT, int(remaining))))
                backoff = BACKOFF_START
            except ChannelError as e:
                if not e.retryable:
                    raise
                wait = min(max(backoff, e.retry_after or 0), max(0.0, deadline - loop.time()))
                log.warning("telegram pairing poll failed, retrying in %.0fs: %s", wait, e)
                await asyncio.sleep(wait)
                backoff = min(backoff * 2, BACKOFF_MAX)
                continue
            for upd in updates:
                uid = int(upd["update_id"])
                offset = uid + 1
                m = upd.get("message") or {}
                chat = m.get("chat") or {}
                text = m.get("text")
                if chat.get("type") != "private" or not isinstance(text, str) or not _is_pair_text(text, code):
                    continue
                chat_id = str(chat.get("id"))
                user_id = str((m.get("from") or {}).get("id", ""))
                # 确认到这条之后，避免 start() 再收到它
                await self._call("getUpdates", {"offset": uid + 1, "timeout": 0, "limit": 1,
                                                "allowed_updates": ALLOWED_UPDATES})
                self._offset = uid + 1
                try:
                    await self.send(chat_id, OutMessage(text="Paired ✅"))
                except ChannelError as e:
                    log.warning("telegram pairing reply failed: %s", e)
                return chat_id, user_id

    async def close(self) -> None:
        if self._own_http:
            await self._http.aclose()
