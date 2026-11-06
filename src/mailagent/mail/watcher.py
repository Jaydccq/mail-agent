"""每个邮箱一个守护线程：连接 → 补齐 → IDLE 循环。

游标（uidvalidity, last_uid）只在 on_messages / on_baseline 成功返回后才在内存里推进；
回调抛异常 = 没存上，退避后重试同一批。
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

from mailagent.config import MailboxConfig
from mailagent.mail.imap import AuthError, ImapMailbox
from mailagent.models import MailMessage

log = logging.getLogger(__name__)

IDLE_TIMEOUT = 9 * 60  # Gmail 约 10~29 分钟踢掉 IDLE，提前重新进入
BATCH = 50
BACKOFF_START = 5.0
BACKOFF_MAX = 300.0
AUTH_RETRY = 30 * 60
STOP_JOIN = 10.0


class _CallbackFailed(Exception):
    """on_messages / on_baseline 抛错：数据没存上。"""


class MailWatcher:
    """每个邮箱一个守护线程：连接 → 补齐 → IDLE 循环；断线指数退避重连（5s 起，最多 5 分钟）；
    Gmail 会在约 10~29 分钟踢掉 IDLE，idle_wait 超时设 9 分钟后重新 IDLE。"""

    def __init__(
        self,
        cfg: MailboxConfig,
        load_cursor: Callable[[], tuple[int | None, int | None]],
        on_baseline: Callable[[int, int], None],
        on_messages: Callable[[int, list[MailMessage]], None],
        on_status: Callable[[str, str | None], None],
    ) -> None:
        self.cfg = cfg
        self._load_cursor = load_cursor
        self._on_baseline = on_baseline
        self._on_messages = on_messages
        self._on_status = on_status
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._box: ImapMailbox | None = None
        self._box_lock = threading.Lock()
        self._uidvalidity: int | None = None
        self._last_uid: int | None = None
        self._cursor_loaded = False
        self._backoff = BACKOFF_START

    # ---- 生命周期 ----

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=f"imap-{self.cfg.id}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._box_lock:
            box = self._box
        if box is not None:
            box.interrupt()
        if self._thread is not None:
            self._thread.join(STOP_JOIN)
            if self._thread.is_alive():
                log.warning("IMAP thread %s did not exit within %ss (daemon thread, ends with the process)",
                            self.cfg.id, STOP_JOIN)

    # ---- 主循环 ----

    def _status(self, status: str, error: str | None) -> None:
        try:
            self._on_status(status, error)
        except Exception:
            log.exception("on_status callback failed %s", self.cfg.id)

    def _sleep_backoff(self) -> None:
        self._stop.wait(self._backoff)
        self._backoff = min(self._backoff * 2, BACKOFF_MAX)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._session()
            except AuthError as e:
                log.error("IMAP login failed %s, retrying in %d minutes", self.cfg.id, AUTH_RETRY // 60)
                self._status("auth_failed", str(e))
                self._stop.wait(AUTH_RETRY)
            except _CallbackFailed as e:
                log.warning("failed to save emails %s, retrying in %.0fs: %s", self.cfg.id, self._backoff,
                            e.__cause__)
                self._status("error", f"failed to save emails: {e.__cause__}")
                self._sleep_backoff()
            except Exception as e:
                if self._stop.is_set():
                    break
                log.warning("IMAP error %s, reconnecting in %.0fs: %s", self.cfg.id, self._backoff, e)
                self._status("error", f"{type(e).__name__}: {e}")
                self._sleep_backoff()
            finally:
                self._close_box()
        log.info("IMAP thread %s exited", self.cfg.id)

    def _close_box(self) -> None:
        with self._box_lock:
            box, self._box = self._box, None
        if box is not None:
            box.close()

    def _session(self) -> None:
        box = ImapMailbox(self.cfg)
        with self._box_lock:
            self._box = box
        if self._stop.is_set():
            return
        box.connect()
        uidvalidity, uidnext = box.select_inbox()

        if not self._cursor_loaded:
            try:
                self._uidvalidity, self._last_uid = self._load_cursor()
            except Exception as e:
                raise _CallbackFailed("load_cursor") from e
            self._cursor_loaded = True

        if self._uidvalidity is None or self._last_uid is None or self._uidvalidity != uidvalidity:
            # 首次运行或 UIDVALIDITY 变了：记基线，不回溯通知旧邮件
            baseline = max(uidnext - 1, 0)
            log.info("IMAP baseline %s uidvalidity=%s last_uid=%s", self.cfg.id, uidvalidity, baseline)
            try:
                self._on_baseline(uidvalidity, baseline)
            except Exception as e:
                raise _CallbackFailed("on_baseline") from e
            self._uidvalidity, self._last_uid = uidvalidity, baseline

        self._catch_up(box, uidvalidity)
        self._status("ok", None)

        while not self._stop.is_set():
            box.idle_wait(IDLE_TIMEOUT)
            # IDLE 正常返回过一次才算连接稳定：每次登录后 IDLE 立刻断的话，退避要一直加倍，不能固定 5 秒重连
            self._backoff = BACKOFF_START
            if self._stop.is_set():
                break
            # 不管有没有收到 EXISTS 都补一次，防止漏推送
            self._catch_up(box, uidvalidity)
            self._status("ok", None)  # 无新邮件也更新成功检查时间，不能把空收件箱误判为停摆

    def _catch_up(self, box: ImapMailbox, uidvalidity: int) -> None:
        while not self._stop.is_set():
            assert self._last_uid is not None
            msgs = box.fetch_new(self._last_uid, limit=BATCH)
            if not msgs:
                return
            try:
                self._on_messages(uidvalidity, msgs)
            except Exception as e:
                raise _CallbackFailed("on_messages") from e
            self._last_uid = max(m.uid for m in msgs)
            log.info("IMAP %s %d new email(s), last_uid=%s", self.cfg.id, len(msgs), self._last_uid)
            # 不用 len < BATCH 判断结束：中途被删的邮件会让一批变少，继续搜到空为止
