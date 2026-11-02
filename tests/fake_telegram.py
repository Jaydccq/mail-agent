"""测试用的假 Telegram Bot API（标准库 http.server，跑在线程里）。

记录 bot 发出的所有消息；push_text / push_callback 模拟用户发消息、点按钮。
和真 API 一样拒收不合法的 parse_mode=HTML 文本（400 can't parse entities），见 tg_html_ok。
"""

from __future__ import annotations

import json
import re
import threading
import time
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

_TG_TAGS = {"b", "i", "s", "code", "pre"}  # mail-agent 只用这几个
_TG_ENTITY = re.compile(r"&(?!(?:amp|lt|gt|quot|#\d+|#x[0-9a-fA-F]+);)")


class _TgHtml(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag not in _TG_TAGS or attrs:
            raise ValueError(f"unsupported tag <{tag}>")
        # https://core.telegram.org/bots/api#formatting-options ：code/pre 不能和别的实体互相嵌套
        if (tag in ("code", "pre") and self.stack) or ({"code", "pre"} & set(self.stack)):
            raise ValueError(f"<{tag}> nested in {self.stack}")
        self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if not self.stack or self.stack[-1] != tag:
            raise ValueError(f"</{tag}> with open tags {self.stack}")
        self.stack.pop()

    def handle_data(self, data: str) -> None:
        self.text.append(data)


def tg_html_ok(s: str) -> str:
    """s 是 Telegram 能收的 HTML 就返回用户看到的文字，否则抛 ValueError。"""
    if not s.strip():
        raise ValueError("message text is empty")
    if len(s) > 4096:
        raise ValueError("message is too long")
    if _TG_ENTITY.search(s):
        raise ValueError("unsupported character entity")
    p = _TgHtml()
    p.feed(s)
    p.close()
    if p.stack:
        raise ValueError(f"unclosed tags {p.stack}")
    return "".join(p.text)


class FakeTelegram:
    def __init__(self, token: str = "TEST:TOKEN"):
        self.token = token
        self.sent: list[dict[str, Any]] = []  # sendMessage / editMessageText 调用参数
        self.calls: list[str] = []
        self.rejected: list[dict[str, Any]] = []  # 因 HTML 不合法被拒收的消息
        self.throttle_sends: set[int] = set()  # 第几次 sendMessage（从 1 数）回 429 retry_after=0
        self._updates: list[dict[str, Any]] = []
        self._next_update = 1
        self._next_msg = 100
        self._cond = threading.Condition()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def api_base(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def start(self) -> "FakeTelegram":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()

    # ---- 模拟用户 ----

    def _push(self, upd: dict[str, Any]) -> int:
        with self._cond:
            upd["update_id"] = self._next_update
            self._next_update += 1
            self._updates.append(upd)
            self._cond.notify_all()
            return upd["update_id"]

    def push_text(self, text: str, chat_id: int = 42, chat_type: str = "private") -> int:
        return self._push({"message": {"message_id": 1, "date": int(time.time()), "text": text,
                                       "chat": {"id": chat_id, "type": chat_type},
                                       "from": {"id": chat_id, "is_bot": False, "first_name": "Me"}}})

    def push_callback(self, data: str, message_id: int, chat_id: int = 42) -> int:
        return self._push({"callback_query": {"id": f"cb{self._next_update}", "data": data,
                                              "from": {"id": chat_id, "is_bot": False, "first_name": "Me"},
                                              "message": {"message_id": message_id, "chat": {"id": chat_id, "type": "private"},
                                                          "date": int(time.time()), "text": "x"}}})

    def wait_sent(self, pred, timeout: float = 30) -> dict[str, Any]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._cond:
                for s in self.sent:
                    if pred(s):
                        return s
                self._cond.wait(0.2)
        raise TimeoutError(f"没等到符合条件的消息；已发出：{[s.get('text', '')[:80] for s in self.sent]}")

    # ---- HTTP ----

    def _handler(self):
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # 安静
                pass

            def do_POST(self):
                self._handle()

            def do_GET(self):
                self._handle()

            def _handle(self):
                prefix = f"/bot{fake.token}/"
                if not self.path.startswith(prefix):
                    return self._reply(401, {"ok": False, "error_code": 401, "description": "Unauthorized"})
                method = self.path[len(prefix):].split("?")[0]
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                params: dict[str, Any] = {}
                if raw:
                    ctype = self.headers.get("Content-Type", "")
                    if "json" in ctype:
                        params = json.loads(raw)
                    else:
                        from urllib.parse import parse_qsl
                        params = dict(parse_qsl(raw.decode()))
                fake.calls.append(method)
                if method == "sendMessage" and fake.calls.count("sendMessage") in fake.throttle_sends:
                    return self._reply(429, {"ok": False, "error_code": 429, "parameters": {"retry_after": 0},
                                             "description": "Too Many Requests: retry after 0"})
                if method in ("sendMessage", "editMessageText") and params.get("parse_mode") == "HTML":
                    try:
                        tg_html_ok(str(params.get("text", "")))
                    except ValueError as e:
                        fake.rejected.append({"text": params.get("text"), "error": str(e)})
                        return self._reply(400, {"ok": False, "error_code": 400,
                                                 "description": f"Bad Request: can't parse entities: {e}"})
                return self._reply(200, {"ok": True, "result": fake._dispatch(method, params)})

            def _reply(self, code, body):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        return H

    def _dispatch(self, method: str, p: dict[str, Any]) -> Any:
        if method == "getMe":
            return {"id": 1, "is_bot": True, "first_name": "Mail", "username": "fake_mail_bot"}
        if method == "getWebhookInfo":
            return {"url": "", "pending_update_count": 0}
        if method == "deleteWebhook":
            return True
        if method == "getUpdates":
            offset = int(p.get("offset") or 0)
            timeout = min(float(p.get("timeout") or 0), 5)
            deadline = time.time() + timeout
            with self._cond:
                while True:
                    self._updates = [u for u in self._updates if u["update_id"] >= offset]
                    if self._updates or time.time() >= deadline:
                        return list(self._updates)
                    self._cond.wait(max(0.05, deadline - time.time()))
        if method in ("sendMessage", "editMessageText"):
            with self._cond:
                rec = dict(p)
                rec["_method"] = method
                if method == "sendMessage":
                    rec["message_id"] = self._next_msg
                    self._next_msg += 1
                self.sent.append(rec)
                self._cond.notify_all()
                return {"message_id": rec.get("message_id") or int(p.get("message_id", 0)),
                        "chat": {"id": int(p["chat_id"]), "type": "private"}, "date": int(time.time()),
                        "text": p.get("text", "")}
        if method in ("answerCallbackQuery", "sendChatAction"):
            return True
        return True
