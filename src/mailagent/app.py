"""应用装配：进程锁 → 数据库 → Jev / 生成模型 / 渠道 → 后台任务（watchers、pipeline、outbox、scheduler、渠道接收）。

邮箱可以在运行中通过聊天热增删（App.add_mailbox / App.remove_mailbox），时区也可以热改（App.set_timezone）：
- 配置文件只在主循环线程里写（update_config：读文件、改这一项、写回，不覆盖用户手改的内容），
  用 _mb_lock 串行化，并和 stop() 互斥；
- 被替换/删除的 watcher 先从 self.watchers 移除，再在线程里 stop；它之后经桥接发回的回调一律作废，
  不会再写数据库（见 _make_watcher）。
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import signal
from datetime import timedelta
from functools import partial
from typing import IO, Any, Awaitable, Callable

from mailagent.agent.chat import HELP_TEXT, ChatAgent
from mailagent.agent.pipeline import Pipeline, primary_target
from mailagent.agent import render
from mailagent.agent.scheduler import Scheduler, retime_open_todos
from mailagent.agent.timeutil import utcnow
from mailagent.ai.jev import JevClient
from mailagent.ai.llm import make_llm
from mailagent.channels.base import Channel, ChannelError
from mailagent.config import Config, MailboxConfig, data_dir, update_config, upsert_mailbox
from mailagent.config import remove_mailbox as cfg_remove_mailbox
from mailagent.db import Database
from mailagent.mail.watcher import MailWatcher
from mailagent.models import Button, Inbound, MailMessage, OutMessage

log = logging.getLogger(__name__)

OUTBOX_INTERVAL = 2.0
RESTART_DELAY = 10.0


# ---- 进程锁 ----


def acquire_lock() -> IO[str] | None:
    d = data_dir()
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    f = open(d / "agent.lock", "a+", encoding="utf-8")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    f.seek(0)
    f.truncate()
    f.write(str(os.getpid()))
    f.flush()
    return f


def release_lock(f: IO[str]) -> None:
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    finally:
        f.close()


# ---- 渠道 ----


def make_channels(cfg: Config) -> dict[str, Channel]:
    channels: dict[str, Channel] = {}
    if cfg.telegram_enabled:
        from mailagent.channels.telegram import TelegramChannel

        channels["telegram"] = TelegramChannel(cfg.telegram)
    if cfg.lark_enabled:
        from mailagent.channels.lark import LarkChannel  # lark-oapi 较重，只在用到时导入

        channels["lark"] = LarkChannel(cfg.lark)
    return channels


def is_owner(cfg: Config, ib: Inbound) -> bool:
    if not ib.is_private:
        return False
    if ib.channel == "telegram":
        return bool(cfg.telegram.owner_chat_id) and str(ib.chat_id) == str(cfg.telegram.owner_chat_id)
    if ib.channel == "lark":
        return bool(cfg.lark.owner_open_id) and str(ib.user_id) == str(cfg.lark.owner_open_id)
    return False


def _out_from_payload(payload: str) -> OutMessage:
    d = json.loads(payload)
    return OutMessage(text=d.get("text", ""), title=d.get("title"),
                      buttons=[[Button(b["label"], b["data"]) for b in row] for row in d.get("buttons") or []],
                      markdown=bool(d.get("markdown")))


async def outbox_worker(db: Database, channels: dict[str, Channel]) -> None:
    """每 2 秒发送 outbox 里到时间的消息；可重试的失败指数退避（封顶 1 小时，一直重试直到送达）。"""
    db.reset_outbox_claims()
    while True:
        for row in db.ready_outbox(utcnow()):
            claimed = db.claim_outbox(row["id"])
            if claimed is None:
                continue
            row = claimed
            ch = channels.get(row["channel"])
            if ch is None:
                db.outbox_failed(row["id"], f"channel {row['channel']} is not enabled", retry_at=None)
                continue
            try:
                provider_id = await ch.send(row["chat_id"], _out_from_payload(row["payload"]))
            except asyncio.CancelledError:
                db.outbox_failed(row["id"], "Delivery interrupted", retry_at=utcnow() + timedelta(seconds=30))
                raise
            except ChannelError as e:
                if e.retryable:
                    delay = max(e.retry_after or 0, min(30 * 2 ** row["attempts"], 3600))
                    db.outbox_failed(row["id"], str(e), retry_at=utcnow() + timedelta(seconds=delay))
                    log.warning("Sending notification #%s failed, retrying in %ss: %s", row["id"], int(delay), e)
                else:
                    db.outbox_failed(row["id"], str(e), retry_at=None)
                    log.error("Sending notification #%s failed (not retryable): %s", row["id"], e)
                continue
            except Exception as e:  # noqa: BLE001
                delay = min(30 * 2 ** row["attempts"], 3600)
                db.outbox_failed(row["id"], f"{type(e).__name__}: {e}", retry_at=utcnow() + timedelta(seconds=delay))
                log.exception("Error sending notification #%s", row["id"])
                continue
            db.outbox_sent(row["id"], str(provider_id or ""))
            key = row.get("dedupe_key") or ""
            if key.startswith("notify:") and key[7:].isdigit():
                db.mark_notified(int(key[7:]))
        await asyncio.sleep(OUTBOX_INTERVAL)


async def supervise(name: str, factory: Callable[[], Awaitable[Any]]) -> None:
    """任务异常或意外退出：记日志，10 秒后重启；不让整个进程退出。"""
    while True:
        try:
            await factory()
            log.error("Task %s exited unexpectedly, restarting in %ss", name, RESTART_DELAY)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Task %s crashed, restarting in %ss", name, RESTART_DELAY)
        await asyncio.sleep(RESTART_DELAY)


class RetiredWatcher(Exception):
    """已被替换/删除的 watcher 发回的回调：不处理。"""


class App:
    def __init__(self, cfg: Config, db: Database):
        self.cfg = cfg
        self.db = db
        self.loop = asyncio.get_running_loop()
        self.jev = JevClient(cfg.jev)
        self.llm = make_llm(cfg.llm)
        self.channels = make_channels(cfg)
        self.pipeline = Pipeline(cfg, db, self.jev, self.llm)
        self.scheduler = Scheduler(cfg, db, self.llm)
        moved = retime_open_todos(db, cfg.prefs.timezone)  # 时区在配置文件里改过：把待办挪过去
        if moved:
            log.info("Time zone is %s: moved %s open todo(s) to it", cfg.prefs.timezone, moved)
        self.chat = ChatAgent(cfg, db, self.llm, self.channels, self.scheduler, mailboxes=self)
        self.watchers: list[MailWatcher] = []
        self.tasks: list[asyncio.Task[Any]] = []
        self._mb_lock = asyncio.Lock()  # 串行化邮箱增删、配置写入和 stop()
        self._stopping = False

    # ---- IMAP 线程 → 主循环 ----

    def _bridge(self, fn: Callable[..., Any], *args: Any) -> Any:
        async def call() -> Any:
            return fn(*args)

        return asyncio.run_coroutine_threadsafe(call(), self.loop).result(timeout=60)

    def _make_watcher(self, mb: MailboxConfig) -> MailWatcher:
        mb_id = mb.id
        me: list[MailWatcher] = []

        def live() -> bool:
            # 在主循环里判断（和增删邮箱在同一线程，没有竞争）：不在 self.watchers 里 = 已退役
            return bool(me) and any(w is me[0] for w in self.watchers)

        def guarded(fn: Callable[..., Any]) -> Callable[..., Any]:
            def run(*args: Any) -> Any:
                if not live():
                    raise RetiredWatcher(mb_id)
                return fn(*args)

            return run

        def load_cursor() -> tuple[int | None, int | None]:
            st = self._bridge(self.db.get_mailbox_state, mb_id) or {}
            return st.get("uidvalidity"), st.get("last_uid")

        def baseline(uidvalidity: int, last_uid: int) -> None:
            self.db.set_cursor(mb_id, uidvalidity, last_uid)

        def on_baseline(uidvalidity: int, last_uid: int) -> None:
            self._bridge(guarded(baseline), uidvalidity, last_uid)
            log.info("%s: baseline at uid=%s (older mail is not replayed)", mb_id, last_uid)

        def on_messages(uidvalidity: int, msgs: list[MailMessage]) -> None:
            ids = self._bridge(guarded(self.db.ingest_messages), mb_id, uidvalidity, msgs)
            if ids:
                log.info("%s: %s new email(s)", mb_id, len(ids))
                self.loop.call_soon_threadsafe(self.pipeline.wake.set)

        def status(st: str, error: str | None) -> None:
            if live():
                self._set_status(mb_id, st, error)

        def on_status(st: str, error: str | None) -> None:
            self._bridge(status, st, error)

        w = MailWatcher(mb, load_cursor, on_baseline, on_messages, on_status)
        me.append(w)
        return w

    def _set_status(self, mb_id: str, status: str, error: str | None) -> None:
        prev = (self.db.get_mailbox_state(mb_id) or {}).get("status")
        self.db.set_mailbox_status(mb_id, status, error)
        if status == prev:
            return
        if status == "auth_failed":
            log.error("%s: login failed: %s", mb_id, error)
            try:
                address = self.cfg.mailbox(mb_id).address
            except KeyError:
                address = mb_id
            self._notify_owner(OutMessage(
                title=f"🔑 Can't log in to {address}",
                text=(f"{error or ''}\n\n"
                      "The app password was probably revoked, or the Google account password changed. "
                      "Create a new app password at https://myaccount.google.com/apppasswords and send:\n"
                      f"/addmailbox {address} <new app password>\n\n"
                      "Until then I can't fetch new mail for this mailbox.").strip()))
        elif status == "ok" and prev in ("error", "auth_failed"):
            log.info("%s: back to normal", mb_id)

    def _notify_owner(self, msg: OutMessage, dedupe_key: str | None = None) -> None:
        target = primary_target(self.cfg)
        if target is None:
            log.warning("No paired channel to notify: %s", msg.title)
            return
        self.db.enqueue(target[0], target[1], msg, dedupe_key=dedupe_key)

    # ---- 运行中增删邮箱（ChatAgent 通过 MailboxManager 接口调用；只在主循环里调用） ----

    async def _retire_watchers(self, mb_id: str) -> None:
        """把这个邮箱的 watcher 移出列表（之后它的回调作废），再在线程里停掉（最多约 10 秒）。"""
        old = [w for w in self.watchers if w.cfg.id == mb_id]
        if not old:
            return
        self.watchers = [w for w in self.watchers if w.cfg.id != mb_id]
        await asyncio.gather(*(asyncio.to_thread(w.stop) for w in old), return_exceptions=True)

    async def add_mailbox(self, mb: MailboxConfig) -> bool:
        """加入一个邮箱，或替换同地址邮箱的配置（例如换了应用专用密码）：写配置 → 停掉同 id 的旧 watcher →
        启动新 watcher。登录验证由调用方先做。返回 True 表示替换了已有邮箱。"""
        async with self._mb_lock:
            if self._stopping:
                raise RuntimeError("the agent is shutting down")
            replaced = any(m.address.lower() == mb.address.lower() for m in self.cfg.mailboxes)
            update_config(lambda c: upsert_mailbox(c, mb))  # 先写文件；失败就抛，内存里什么都没改
            upsert_mailbox(self.cfg, mb)
            await self._retire_watchers(mb.id)
            if self._stopping:  # 等旧 watcher 退出时开始了退出流程：配置已保存，下次启动会监控它
                log.info("%s: saved; not starting a watcher because the agent is shutting down", mb.id)
                return replaced
            w = self._make_watcher(mb)
            self.watchers.append(w)
            w.start()
            log.info("%s: %s, watcher started", mb.id, "app password updated" if replaced else "mailbox added")
            return replaced

    async def set_timezone(self, tz: str) -> int:
        """换时区：写配置 → 把未完成待办挪到新时区。摘要、静默时段、提醒都直接读 cfg.prefs，立即生效。
        返回挪了几条待办。"""
        async with self._mb_lock:
            if self._stopping:
                raise RuntimeError("the agent is shutting down")
            def change(c: Config) -> None:
                c.prefs.timezone = tz

            update_config(change)
            old = self.cfg.prefs.timezone
            change(self.cfg)
            moved = retime_open_todos(self.db, tz)
            log.info("Time zone %s → %s; moved %s open todo(s)", old, tz, moved)
            return moved

    async def remove_mailbox(self, mailbox_id: str) -> MailboxConfig | None:
        """删除一个邮箱：写配置 → 停掉它的 watcher → 清掉它的游标（已收邮件和待办保留）。
        找不到返回 None；只剩这一个邮箱时抛 ValueError。"""
        async with self._mb_lock:
            if self._stopping:
                raise RuntimeError("the agent is shutting down")
            if not any(m.id == mailbox_id for m in self.cfg.mailboxes):
                return None
            if len(self.cfg.mailboxes) <= 1:
                raise ValueError("can't remove the only mailbox")
            update_config(lambda c: cfg_remove_mailbox(c, mailbox_id))
            removed = cfg_remove_mailbox(self.cfg, mailbox_id)
            if removed is None:
                return None
            await self._retire_watchers(removed.id)
            self.db.delete_mailbox_state(removed.id)
            log.info("%s: mailbox removed", removed.id)
            return removed

    # ---- 入站 ----

    async def on_inbound(self, ib: Inbound) -> None:
        if not is_owner(self.cfg, ib):
            log.info("Ignoring a message from someone other than the owner (%s, private=%s)", ib.channel, ib.is_private)
            return
        await self.chat.handle(ib)

    # ---- 生命周期 ----

    def _startup_messages(self) -> None:
        for action in self.db.interrupted_actions():
            log.warning("Action #%s was interrupted while sending; outcome unknown", action["id"])
            self._notify_owner(render.interrupted_action(action), dedupe_key=f"interrupted:{action['id']}")
        if self.db.kv_get("welcomed") is None and primary_target(self.cfg) is not None:
            n = len(self.cfg.mailboxes)
            boxes = ", ".join(m.address for m in self.cfg.mailboxes)
            p = self.cfg.prefs
            text = (f"{boxes}\n\n"
                    f"I'll alert you right away about important mail and send a digest of the rest at "
                    f"{', '.join(p.digest_times)} every day. Quiet hours are {p.quiet_start}–{p.quiet_end}.\n"
                    "Buttons on each alert let you mark it done, mute a sender, or draft a reply "
                    "(nothing is ever sent without your confirmation).\n\n"
                    + HELP_TEXT)
            title = f"✅ Now watching {n} mailbox{'es' if n != 1 else ''}"
            self._notify_owner(OutMessage(title=title, text=text), dedupe_key="welcome")
            self.db.kv_set("welcomed", "1")

    def start(self) -> None:
        self._startup_messages()
        for mb in self.cfg.mailboxes:
            w = self._make_watcher(mb)
            self.watchers.append(w)
            w.start()
        jobs: list[tuple[str, Callable[[], Awaitable[Any]]]] = [
            ("pipeline", self.pipeline.run),
            ("analysis-recovery", self.pipeline.recovery_worker),
            ("outbox", lambda: outbox_worker(self.db, self.channels)),
            ("scheduler", self.scheduler.run),
        ]
        for name, ch in self.channels.items():
            jobs.append((f"channel:{name}", partial(ch.start, self.on_inbound)))
        for name, factory in jobs:
            self.tasks.append(asyncio.create_task(supervise(name, factory), name=name))
        log.info("Started: %s mailbox(es), channels %s, model %s", len(self.watchers),
                 ",".join(self.channels) or "none", self.llm.name if self.llm else "not configured")

    async def stop(self) -> None:
        log.info("Shutting down…")
        self._stopping = True  # 之后的 add/remove_mailbox 直接拒绝；正在进行的那个不会再启动新 watcher
        # 先停 watcher（主循环还在跑，它们阻塞中的桥接调用才能完成）。拿锁：等正在进行的增删邮箱做完（最多约 10 秒）
        async with self._mb_lock:
            watchers = list(self.watchers)
            await asyncio.gather(*(asyncio.to_thread(w.stop) for w in watchers), return_exceptions=True)
        for t in self.tasks:
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        for ch in self.channels.values():
            try:
                await ch.close()
            except Exception as e:  # noqa: BLE001
                log.warning("Closing channel failed: %s", e)
        try:
            await self.jev.aclose()
        except Exception:  # noqa: BLE001
            pass


async def run(cfg: Config) -> int:
    lock = acquire_lock()
    if lock is None:
        print("Another instance is already running (if you installed the background service, check: mail-agent status).")
        return 1
    db: Database | None = None
    app: App | None = None
    try:
        db = Database(data_dir() / "agent.db")
        app = App(cfg, db)
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except (NotImplementedError, RuntimeError):
                pass
        app.start()
        await stop.wait()
        return 0
    finally:
        if app is not None:
            await app.stop()
        if db is not None:
            db.close()
        release_lock(lock)
        log.info("Stopped")
