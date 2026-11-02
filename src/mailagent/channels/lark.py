"""Lark（飞书）渠道：lark-oapi 长连接收事件，不需要公网回调地址。

Lark/飞书开放平台后台需要做的配置：
1. 创建「企业自建应用」，记下 App ID / App Secret。
2. 「添加应用能力」里开启「机器人」。
3. 「权限管理」开通：im:message（接收/读取消息）、im:message:send_as_bot（以应用身份发消息）、
   im:message.p2p_msg:readonly（收到发给机器人的私聊）。群消息一律忽略，不需要 group_at_msg。
4. 「事件与回调」→「事件配置」：订阅方式选「使用长连接接收事件」，添加事件 im.message.receive_v1（接收消息）。
5. 「事件与回调」→「回调配置」：订阅方式同样选「使用长连接接收回调」，添加回调 card.action.trigger
   （卡片回传交互）。不要用旧版「消息卡片请求网址」，SDK 长连接会丢弃旧版卡片帧。
6. 「版本管理与发布」创建版本并发布（权限和事件订阅发布后才生效；只保存不算发布，搜不到机器人）。
setup 向导一步步带用户做（cli.LARK_STEPS），并用 bot_name 检查机器人已发布启用。

实现要点：
- lark_oapi.ws.Client.start() 阻塞，并且用的是 ws/client.py 里的模块级变量 loop。所以每个长连接在自己的
  daemon 线程里新建事件循环，并把模块级 loop 换成 _ThreadLoop：SDK 在哪个线程里用它，就是那个线程自己的循环。
  重建长连接时旧线程可能还卡在 SDK 不带超时的同步 HTTP 请求里，新旧线程同时存在也互不干扰。
  start 运行中调用 pair 会复用 start 的连接。
- SDK 在自己的循环里同步调用我们的回调：回调里只做解析，用 run_coroutine_threadsafe 交回主循环，立刻返回。
- SDK 没有 stop 接口：停止时关掉自动重连、断开连接、再 stop 线程里的事件循环。
- 发送一律用 interactive 卡片（config.update_multi=true 才能 PATCH 更新），HTTP 调用是同步的，放 to_thread。
- 国际版还是飞书由 detect_domain 自动识别：token 接口两个域名都认同一个应用，分不出来；长连接地址接口
  在错的域名上返回 1000040351 Incorrect domain name（2026-11-01 用真实飞书应用确认）。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import threading
import time
from typing import Any, Callable

import httpx

from mailagent.channels import chat_format
from mailagent.channels.base import ChannelError, InboundHandler, send_parts
from mailagent.config import LarkConfig
from mailagent.models import Inbound, OutMessage

log = logging.getLogger(__name__)

CARD_TEXT = 5000  # 单张卡片正文最多字符数（卡片总大小上限约 30KB）
RATE_LIMIT_CODES = {99991400, 230020}  # 接口限流 / 消息频控
BACKOFF_START = 2.0
BACKOFF_MAX = 60.0
WATCH_INTERVAL = 30.0
DISCONNECT_RESTART = 600.0  # 断线超过这么久（SDK 重连放弃）就整个重建长连接
HEALTHY_RESET = 300.0  # 长连接稳定运行这么久后，退避重新从头算
TOAST_TEXT = "Received"
WS_ENDPOINT = "/callback/ws/endpoint"  # lark_oapi.ws.const.GEN_ENDPOINT_URI：SDK 建长连接前先要地址


async def detect_domain(app_id: str, app_secret: str) -> str:
    """应用属于飞书还是国际版 Lark：返回长连接地址接口接受它的那个（"feishu" | "lark"）。"""
    import lark_oapi as lark

    errors: list[str] = []
    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as http:
        for name, base in (("feishu", lark.FEISHU_DOMAIN), ("lark", lark.LARK_DOMAIN)):
            try:
                r = await http.post(base + WS_ENDPOINT, json={"AppID": app_id, "AppSecret": app_secret})
                data = r.json()
            except (httpx.HTTPError, ValueError) as e:
                errors.append(f"{name}: {type(e).__name__}")
                continue
            if data.get("code") == 0:
                return name
            errors.append(f"{name}: {data.get('code')} {data.get('msg') or ''}".rstrip())
    raise ChannelError("neither Feishu nor Lark accepted this app for a persistent connection; check the "
                       f"App ID / App Secret and that the app is published ({'; '.join(errors)})", retryable=False)


async def bot_name(domain: str, app_id: str, app_secret: str) -> str:
    """机器人的名字。应用没加机器人能力、或者还没发布启用（搜不到机器人，配对不了）时抛 ChannelError 说清楚缺哪步。"""
    import lark_oapi as lark

    base = lark.FEISHU_DOMAIN if domain == "feishu" else lark.LARK_DOMAIN
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as http:
            tok = (await http.post(base + "/open-apis/auth/v3/tenant_access_token/internal",
                                   json={"app_id": app_id, "app_secret": app_secret})).json()
            if tok.get("code") != 0:
                raise ChannelError(f"check the App ID / App Secret ({tok.get('code')} {tok.get('msg')})",
                                   retryable=False)
            info = (await http.get(base + "/open-apis/bot/v3/info",
                                   headers={"Authorization": f"Bearer {tok.get('tenant_access_token', '')}"})).json()
    except (httpx.HTTPError, ValueError) as e:
        raise ChannelError(f"could not reach {base}: {type(e).__name__}", retryable=True) from None
    if info.get("code") != 0:
        raise ChannelError("the app has no bot yet: Add Features → Bot, then create a version and Publish it "
                           f"({info.get('code')} {info.get('msg')})", retryable=False)
    bot = info.get("bot") or {}
    if bot.get("activate_status") != 2:  # 2 = 已启用；没发布时搜不到机器人
        raise ChannelError("the app isn't published yet: Create Version with you in its availability → Save → "
                           f"Publish (saving alone doesn't publish; activate_status {bot.get('activate_status')})",
                           retryable=False)
    return str(bot.get("app_name") or "")


def _body(msg: OutMessage) -> str:
    return chat_format.lark_markdown(msg.text or "") if msg.markdown else (msg.text or "")


def _split_text(text: str, limit: int = CARD_TEXT) -> list[str]:
    """按行拆分成 <= limit 的段；单行过长时硬切。"""
    parts: list[str] = []
    cur = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(line[:limit])
            line = line[limit:]
        if not cur:
            cur = line
        elif len(cur) + 1 + len(line) <= limit:
            cur += "\n" + line
        else:
            parts.append(cur)
            cur = line
    parts.append(cur)
    return parts


def _build_card(title: str | None, text: str, buttons: list[list[Any]], markdown: bool) -> dict[str, Any]:
    """markdown=True：text 是 chat_format.lark_markdown 的结果，用 markdown 组件；否则 plain_text 原样显示
    （邮件原文、草稿预览）。"""
    elements: list[dict[str, Any]] = []
    if text.strip() or not title:
        content = text if text.strip() else " "
        elements.append({"tag": "markdown", "content": content} if markdown
                        else {"tag": "div", "text": {"tag": "plain_text", "content": content}})
    for row in buttons:
        if not row:
            continue
        elements.append({
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": b.label},
                    "type": "default",
                    "value": {"d": b.data},
                }
                for b in row
            ],
        })
    card: dict[str, Any] = {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "elements": elements,
    }
    if title:
        card["header"] = {"title": {"tag": "plain_text", "content": title}, "template": "blue"}
    return card


class _ThreadLoop:
    """替换 lark_oapi.ws.client 的模块级 loop：属性访问转给当前线程的事件循环（_WsRunner 线程开头设置）。"""

    _local = threading.local()

    def set(self, loop: asyncio.AbstractEventLoop) -> None:
        self._local.loop = loop

    def __getattr__(self, name: str) -> Any:
        loop = getattr(self._local, "loop", None)
        if loop is None:
            raise RuntimeError("lark SDK used outside a long-connection thread")
        return getattr(loop, name)


_SDK_LOOP = _ThreadLoop()


class _WsRunner:
    """在 daemon 线程里跑一个 lark ws.Client。done 在线程结束时完成（异常 = 启动失败）。"""

    def __init__(self, cfg: LarkConfig, domain: str, event_handler: Any, name: str):
        self.cfg = cfg
        self.domain = domain
        self.event_handler = event_handler
        self.done: concurrent.futures.Future[None] = concurrent.futures.Future()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client: Any = None
        self._stopping = False
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)

    @property
    def connected(self) -> bool:
        return self._client is not None and getattr(self._client, "_conn", None) is not None

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        import lark_oapi as lark
        import lark_oapi.ws.client as ws_client

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        _SDK_LOOP.set(loop)
        ws_client.loop = _SDK_LOOP  # SDK 内部全部用这个模块级 loop
        self._loop = loop
        err: BaseException | None = None
        try:
            # Client 构造时会在当前事件循环上建定时任务，必须在换完 loop 之后构造
            self._client = lark.ws.Client(
                self.cfg.app_id,
                self.cfg.app_secret,
                log_level=lark.LogLevel.WARNING,
                event_handler=self.event_handler,
                domain=self.domain,
            )
            if not self._stopping:
                self._client.start()
        except RuntimeError as e:
            if not self._stopping:  # stop() 让 run_until_complete 抛 "Event loop stopped..."
                err = e
        except BaseException as e:
            err = e
        finally:
            try:
                pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
                for t in pending:
                    t.cancel()
                if pending:
                    loop.run_until_complete(
                        asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), 5))
                loop.close()
            except Exception:
                pass
            if err is not None:
                self.done.set_exception(err)
            else:
                self.done.set_result(None)

    async def _shutdown(self) -> None:
        cli = self._client
        if cli is not None:
            cli._auto_reconnect = False
            try:
                await cli._disconnect()
            except Exception:
                pass
        asyncio.get_running_loop().stop()

    def stop(self) -> None:
        """尽力停止，不阻塞。"""
        self._stopping = True
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            asyncio.run_coroutine_threadsafe(self._shutdown(), loop)
        except RuntimeError:
            pass


class LarkChannel:
    name = "lark"

    def __init__(self, cfg: LarkConfig):
        self.cfg = cfg
        self._client: Any = None  # lark.Client，懒加载（import lark_oapi 很慢）
        self._main_loop: asyncio.AbstractEventLoop | None = None
        self._on_inbound: InboundHandler | None = None
        self._runner: _WsRunner | None = None
        self._closed = False
        # pair() 在 start() 运行时复用它的连接：(code, future)
        self._pair_waiter: tuple[str, asyncio.Future[Inbound]] | None = None

    # ---- SDK 准备 ----

    def _domain(self) -> str:
        import lark_oapi as lark

        return lark.FEISHU_DOMAIN if self.cfg.domain == "feishu" else lark.LARK_DOMAIN

    def _api(self) -> Any:
        if self._client is None:
            import lark_oapi as lark

            self._client = (
                lark.Client.builder()
                .app_id(self.cfg.app_id)
                .app_secret(self.cfg.app_secret)
                .domain(self._domain())
                .log_level(lark.LogLevel.WARNING)
                .build()
            )
        return self._client

    def _event_handler(self, on_message: Callable[[Any], None],
                       on_card: Callable[[Any], Any] | None) -> Any:
        import lark_oapi as lark

        b = lark.EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(on_message)
        if on_card is not None:
            b = b.register_p2_card_action_trigger(on_card)
        return b.build()

    # ---- 解析（在 SDK 线程里调用） ----

    def _parse_message(self, data: Any) -> Inbound | None:
        ev = data.event
        msg = ev.message if ev else None
        sender = ev.sender if ev else None
        if msg is None or msg.message_type != "text":
            return None
        if sender is not None and sender.sender_type == "app":
            return None
        try:
            text = str(json.loads(msg.content or "{}").get("text", ""))
        except (ValueError, AttributeError):
            return None
        for m in msg.mentions or []:
            if m.key:
                text = text.replace(m.key, "")
        open_id = (sender.sender_id.open_id if sender and sender.sender_id else None) or ""
        return Inbound(
            channel=self.name,
            chat_id=open_id,
            user_id=open_id,
            event_id=(data.header.event_id if data.header else None) or str(msg.message_id),
            text=text.strip(),
            message_id=msg.message_id,
            is_private=msg.chat_type == "p2p",
        )

    def _parse_card(self, data: Any) -> Inbound | None:
        ev = data.event
        if ev is None or ev.action is None:
            return None
        value = ev.action.value or {}
        d = value.get("d") if isinstance(value, dict) else None
        if not isinstance(d, str):
            return None
        open_id = (ev.operator.open_id if ev.operator else None) or ""
        msg_id = ev.context.open_message_id if ev.context else None
        event_id = (data.header.event_id if data.header else None) or f"card:{msg_id}:{d}:{ev.token}"
        return Inbound(
            channel=self.name,
            chat_id=open_id,
            user_id=open_id,
            event_id=event_id,
            callback_data=d,
            message_id=msg_id,
            is_private=True,  # 卡片只发到私聊；是否主人由 agent 层判断
        )

    # ---- 接收 ----

    def _dispatch(self, inbound: Inbound) -> None:
        """SDK 线程 -> 主循环。不等待结果，避免卡住 SDK 的收包循环。"""
        loop = self._main_loop
        if loop is None or loop.is_closed():
            return
        waiter = self._pair_waiter
        if waiter is not None and inbound.is_private and inbound.callback_data is None \
                and inbound.text == waiter[0]:
            fut = waiter[1]

            def resolve() -> None:
                if not fut.done():
                    fut.set_result(inbound)

            loop.call_soon_threadsafe(resolve)
            return
        asyncio.run_coroutine_threadsafe(self._deliver(inbound), loop)

    async def _deliver(self, inbound: Inbound) -> None:
        if self._on_inbound is None:
            return
        try:
            await self._on_inbound(inbound)
        except Exception:
            log.exception("error handling lark event %s", inbound.event_id)

    def _on_message(self, data: Any) -> None:
        try:
            inbound = self._parse_message(data)
        except Exception:
            log.exception("failed to parse lark message")
            return
        if inbound is not None:
            self._dispatch(inbound)

    def _on_card(self, data: Any) -> Any:
        from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse

        try:
            inbound = self._parse_card(data)
        except Exception:
            log.exception("failed to parse lark card callback")
            inbound = None
        if inbound is not None:
            self._dispatch(inbound)
        # 回调必须 3 秒内返回，toast 在这里直接给
        return P2CardActionTriggerResponse({"toast": {"type": "info", "content": TOAST_TEXT}})

    async def start(self, on_inbound: InboundHandler) -> None:
        self._main_loop = asyncio.get_running_loop()
        self._on_inbound = on_inbound
        self._closed = False
        backoff = BACKOFF_START
        domain = self._domain()
        while True:
            runner = _WsRunner(self.cfg, domain, self._event_handler(self._on_message, self._on_card),
                               name="lark-ws")
            self._runner = runner
            started = time.monotonic()
            runner.start()
            try:
                await self._watch(runner)
            except asyncio.CancelledError:
                runner.stop()
                self._runner = None
                raise
            runner.stop()
            self._runner = None
            if self._closed:
                return  # close() 主动停止
            if time.monotonic() - started > HEALTHY_RESET:
                backoff = BACKOFF_START
            log.warning("rebuilding lark long connection in %.0fs", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX)

    async def _watch(self, runner: _WsRunner) -> None:
        """线程退出或长时间断线时返回。"""
        done = asyncio.wrap_future(runner.done)
        lost_since: float | None = None
        while True:
            finished, _ = await asyncio.wait({done}, timeout=WATCH_INTERVAL)
            if finished:
                exc = done.exception()
                if exc is not None:
                    log.error("lark long connection exited: %s: %s", type(exc).__name__, exc)
                else:
                    log.warning("lark long-connection thread exited unexpectedly")
                return
            if runner.connected:
                lost_since = None
            elif lost_since is None:
                lost_since = time.monotonic()
            elif time.monotonic() - lost_since > DISCONNECT_RESTART:
                log.error("lark long connection down for more than %.0fs, rebuilding", DISCONNECT_RESTART)
                return

    # ---- 发送 ----

    async def _request(self, fn: Callable[[Any], Any], req: Any, what: str) -> Any:
        try:
            resp = await asyncio.to_thread(fn, req)
        except Exception as e:
            raise ChannelError(f"lark {what} network error: {type(e).__name__}: {e}", retryable=True) from None
        if not resp.success():
            status = getattr(resp.raw, "status_code", 0) or 0
            retryable = resp.code in RATE_LIMIT_CODES or status >= 500 or status == 429
            raise ChannelError(
                f"lark {what} failed code={resp.code} msg={resp.msg} log_id={resp.get_log_id()}",
                retryable=retryable,
            )
        return resp

    async def _create_card(self, open_id: str, card: dict[str, Any]) -> str:
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

        req = (
            CreateMessageRequest.builder()
            .receive_id_type("open_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(open_id)
                .msg_type("interactive")
                .content(json.dumps(card, ensure_ascii=False))
                .build()
            )
            .build()
        )
        resp = await self._request(self._api().im.v1.message.create, req, "send message")
        return str(resp.data.message_id) if resp.data else ""

    async def send(self, chat_id: str, msg: OutMessage) -> str:
        parts = _split_text(_body(msg))

        async def send_part(i: int) -> str:
            last = i == len(parts) - 1
            card = _build_card(msg.title if i == 0 else None, parts[i], msg.buttons if last else [], msg.markdown)
            return await self._create_card(chat_id, card)

        return await send_parts(len(parts), send_part)

    async def edit(self, chat_id: str, message_id: str, msg: OutMessage) -> None:
        from lark_oapi.api.im.v1 import PatchMessageRequest, PatchMessageRequestBody

        text = _body(msg)
        if len(text) > CARD_TEXT:
            text = text[: CARD_TEXT - 1] + "…"  # 只能改一张卡片
        card = _build_card(msg.title, text, msg.buttons, msg.markdown)
        req = (
            PatchMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                PatchMessageRequestBody.builder().content(json.dumps(card, ensure_ascii=False)).build()
            )
            .build()
        )
        await self._request(self._api().im.v1.message.patch, req, "update card")

    async def delete_message(self, chat_id: str, message_id: str) -> bool:
        # im v1 message delete = recall. A bot can generally only recall messages it sent itself
        # (a user's message in a p2p chat usually can't be recalled), so failure is expected.
        try:
            from lark_oapi.api.im.v1 import DeleteMessageRequest

            req = DeleteMessageRequest.builder().message_id(message_id).build()
            await self._request(self._api().im.v1.message.delete, req, "delete message")
            return True
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.debug("lark message %s could not be deleted: %s", message_id, e)
            return False

    async def ack_callback(self, inbound: Inbound, toast: str = "") -> None:
        return None  # toast 已在卡片回调返回值里给出

    async def typing(self, chat_id: str) -> None:
        return None  # Lark 没有"正在输入"

    # ---- setup ----

    async def check(self) -> str:
        """凭据、版本（国际版/飞书）、机器人已启用；返回 'Feishu bot "名字"'（配对时让用户搜这个名字）。"""
        # token 两个域名都认，长连接只认应用所属的那个：配错了这里报出来（doctor 也走这里）
        actual = await detect_domain(self.cfg.app_id, self.cfg.app_secret)
        if actual != self.cfg.domain:
            raise ChannelError(f"this is a {actual} app but lark.domain is {self.cfg.domain}; "
                               "run mail-agent setup again (it detects the edition)", retryable=False)
        name = await bot_name(actual, self.cfg.app_id, self.cfg.app_secret)
        return f'{"Feishu" if actual == "feishu" else "Lark"} bot "{name}"'

    async def pair(self, code: str, timeout: float = 300) -> tuple[str, str]:
        code = code.strip()
        main = asyncio.get_running_loop()
        fut: asyncio.Future[Inbound] = main.create_future()
        self._pair_waiter = (code, fut)
        runner: _WsRunner | None = None
        try:
            if self._runner is None:
                # start() 没在跑：自己起一个临时长连接，只收消息
                self._main_loop = main
                runner = _WsRunner(self.cfg, self._domain(), self._event_handler(self._on_message, None),
                                   name="lark-ws-pair")
                runner.start()
                done = asyncio.wrap_future(runner.done)
                waiting: set[asyncio.Future[Any]] = {fut, done}
                finished, _ = await asyncio.wait(waiting, timeout=timeout,
                                                 return_when=asyncio.FIRST_COMPLETED)
                if not finished:
                    raise TimeoutError("timed out waiting for the pairing code")
                if fut not in finished:
                    exc = done.exception()
                    raise ChannelError(f"lark long connection failed: {exc}", retryable=False)
                inbound = fut.result()
            else:
                inbound = await asyncio.wait_for(fut, timeout)
        finally:
            self._pair_waiter = None
            if runner is not None:
                runner.stop()
        try:
            await self.send(inbound.chat_id, OutMessage(text="Paired ✅"))
        except ChannelError as e:
            log.warning("lark pairing reply failed: %s", e)
        return inbound.user_id, inbound.user_id

    async def close(self) -> None:
        self._closed = True
        if self._runner is not None:
            self._runner.stop()
            self._runner = None
