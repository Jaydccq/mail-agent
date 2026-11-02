"""IMAP 收信（imapclient，同步代码，跑在线程里）。

一个 ImapMailbox = 一条连接，只能在一个线程里用（interrupt() 除外）。
search / sent_since / append_draft 会切换当前文件夹，fetch_new / idle_wait 会自动切回 INBOX；
但 IDLE 线程和聊天搜索不要共用一个实例。

抓邮件一律 BODY.PEEK[]，文件夹也用只读方式 SELECT（EXAMINE），绝不改变已读状态。
"""

from __future__ import annotations

import email
import logging
import threading
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from email.policy import SMTP as SMTP_POLICY
from email.utils import parsedate_to_datetime
from typing import Any

from imapclient import IMAPClient
from imapclient import exceptions as imap_exc
from imapclient.imap_utf7 import decode as utf7_decode
from imapclient.imapclient import ALL, DELETED, DRAFT, DRAFTS, SENT, TRASH

from mailagent.config import MailboxConfig
from mailagent.mail.parse import decode_header_value, header_addresses, parse_message
from mailagent.models import MailMessage

log = logging.getLogger(__name__)

INBOX = "INBOX"
MAX_FULL_SIZE = 8 * 1024 * 1024  # 超过只取头部
BODY_BATCH_BYTES = 20 * 1024 * 1024  # 一次 FETCH 正文的总大小上限
IDLE_CHECK_INTERVAL = 5.0  # idle_check 间隔，决定 stop() 的响应速度
TOO_LARGE_BODY = "(email too large; body not downloaded)"
UNFETCHABLE_SUBJECT = "(Gmail could not return this email)"
SENT_MAX_PER_CALL = 200

_APP_PASSWORD_HINT = ("check the app password (2-Step Verification must be turned on first): "
                      "https://myaccount.google.com/apppasswords")
# 这些登录失败是服务器临时问题，不算密码错（RFC 5530 的 UNAVAILABLE/SERVERBUG/LIMIT/INUSE，
# 以及 Gmail 的 "Too many simultaneous connections"、"exceeded command or bandwidth limits"、THROTTLED）
_TRANSIENT_LOGIN = ("UNAVAILABLE", "SERVERBUG", "TRY AGAIN", "TEMPORARY", "SYSTEM ERROR",
                    "[LIMIT]", "[INUSE]", "SIMULTANEOUS", "BANDWIDTH", "THROTTLED")


class AuthError(Exception):
    """登录失败（密码错/未开两步验证），不要重试轰炸。"""


def _s(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _body_key(data: dict[bytes, Any]) -> bytes | None:
    """FETCH 返回的键里没有 .PEEK，例如 b'BODY[]'、b'BODY[HEADER]'；按前缀找。"""
    for key in data:
        if isinstance(key, bytes) and key.upper().startswith(b"BODY["):
            return key
    return None


def _labels(value: Any) -> list[str]:
    out: list[str] = []
    for item in value or ():
        if isinstance(item, bytes):
            out.append(utf7_decode(item))
        else:
            out.append(str(item))  # 纯数字标签会被解析成 int
    return out


def _flags(value: Any) -> list[str]:
    return [_s(f) for f in (value or ())]


def _utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class ImapMailbox:
    def __init__(self, cfg: MailboxConfig, timeout: float = 60) -> None:
        self.cfg = cfg
        self.timeout = timeout
        self._client: IMAPClient | None = None
        self._gmail = False
        self._has_idle = False
        self._selected: str | None = None
        self._folders: dict[bytes, str | None] = {}
        self._interrupt = threading.Event()

    # ---- 连接 ----

    @property
    def is_gmail(self) -> bool:
        return self._gmail

    def connect(self) -> None:
        client = IMAPClient(self.cfg.imap_host, port=self.cfg.imap_port, ssl=True, timeout=self.timeout)
        client.normalise_times = False  # INTERNALDATE 返回带时区的 datetime
        password = self.cfg.app_password.replace(" ", "")
        try:
            client.login(self.cfg.address, password)
        except imap_exc.LoginError as e:
            try:
                client.shutdown()
            except Exception:
                pass
            text = str(e)
            if any(t in text.upper() for t in _TRANSIENT_LOGIN):
                raise ConnectionError(f"IMAP server temporarily unavailable: {text}") from e
            raise AuthError(f"IMAP login failed ({self.cfg.address}): {_APP_PASSWORD_HINT}. "
                            f"Server said: {text}") from e
        self._client = client
        self._gmail = client.has_capability("X-GM-EXT-1")
        self._has_idle = client.has_capability("IDLE")
        self._selected = None
        self._folders = {}
        log.info("IMAP logged in %s (gmail=%s idle=%s)", self.cfg.id, self._gmail, self._has_idle)

    def close(self) -> None:
        client, self._client = self._client, None
        self._selected = None
        if client is None:
            return
        try:
            client.logout()
        except Exception:
            try:
                client.shutdown()
            except Exception:
                pass

    def interrupt(self) -> None:
        """契约外的补充：让正在进行（或之后）的 idle_wait 尽快返回 False。可跨线程调用。"""
        self._interrupt.set()

    @property
    def client(self) -> IMAPClient:
        if self._client is None:
            raise ConnectionError("IMAP not connected")
        return self._client

    # ---- 文件夹 ----

    def _select(self, folder: str) -> dict[bytes, Any]:
        info = self.client.select_folder(folder, readonly=True)
        self._selected = folder
        return info

    def _ensure_inbox(self) -> None:
        if self._selected != INBOX:
            self._select(INBOX)

    def _special(self, flag: bytes) -> str:
        if flag not in self._folders:
            self._folders[flag] = self.client.find_special_folder(flag)
        name = self._folders[flag]
        if not name:
            raise RuntimeError(f"special folder {flag.decode()} not found (server lacks SPECIAL-USE?)")
        return name

    def _uid_info(self, info: dict[bytes, Any]) -> tuple[int, int]:
        uidvalidity = int(info.get(b"UIDVALIDITY") or 0)
        uidnext = info.get(b"UIDNEXT")
        if uidnext is None:
            # RFC 3501 不保证 SELECT 返回 UIDNEXT
            uids = self.client.search("ALL")
            uidnext = (max(uids) + 1) if uids else 1
        return uidvalidity, int(uidnext)

    def select_inbox(self) -> tuple[int, int]:
        return self._uid_info(self._select(INBOX))

    # ---- 抓取 ----

    def _meta_fields(self) -> list[str]:
        fields = ["FLAGS", "INTERNALDATE", "RFC822.SIZE"]
        if self._gmail:
            fields += ["X-GM-MSGID", "X-GM-THRID", "X-GM-LABELS"]
        return fields

    def _fetch_messages(self, uids: list[int], *, keep_uid: bool) -> list[MailMessage]:
        """在当前文件夹里抓 uids，返回顺序与 uids 一致；keep_uid=False 时 uid 记为 0（非 INBOX）。"""
        if not uids:
            return []
        meta = self.client.fetch(uids, self._meta_fields())
        small = [u for u in uids if u in meta and int(meta[u].get(b"RFC822.SIZE") or 0) <= MAX_FULL_SIZE]
        large = [u for u in uids if u in meta and u not in small]

        raw: dict[int, bytes] = {}
        batch: list[int] = []
        batch_bytes = 0
        for u in small:
            size = int(meta[u].get(b"RFC822.SIZE") or 0)
            if batch and batch_bytes + size > BODY_BATCH_BYTES:
                raw.update(self._fetch_bodies(batch, "BODY.PEEK[]"))
                batch, batch_bytes = [], 0
            batch.append(u)
            batch_bytes += size
        if batch:
            raw.update(self._fetch_bodies(batch, "BODY.PEEK[]"))
        if large:
            raw.update(self._fetch_bodies(large, "BODY.PEEK[HEADER]"))

        out: list[MailMessage] = []
        for u in uids:
            if u not in meta or u not in raw:
                continue  # 抓取之间被删了
            m = meta[u]
            kwargs: dict[str, Any] = dict(
                mailbox_id=self.cfg.id,
                uid=u if keep_uid else 0,
                gm_msgid=_s(m.get(b"X-GM-MSGID")),
                gm_thrid=_s(m.get(b"X-GM-THRID")),
                labels=_labels(m.get(b"X-GM-LABELS")),
                flags=_flags(m.get(b"FLAGS")),
                internal_date=m.get(b"INTERNALDATE"),
            )
            try:
                msg = parse_message(raw[u], **kwargs)
            except Exception:
                # 解析不了也不能丢：留一个占位，至少能通知到
                log.exception("failed to parse email %s uid=%s", self.cfg.id, u)
                msg = parse_message(b"Subject: (could not parse this email)\r\n\r\n", **kwargs)
            if u in large:
                msg.body_text = TOO_LARGE_BODY
                msg.quoted_text = ""
            out.append(msg)
        return out

    def _fetch_bodies(self, uids: list[int], item: str) -> dict[int, bytes]:
        """一封邮件让服务器回错（NO/协议错误）时逐封重抓，抓不出来的那封换成占位：
        不然同一批永远失败，游标不动，这个邮箱之后的邮件全卡住。连接错误（abort）照常往上抛。"""
        out: dict[int, bytes] = {}
        try:
            fetched = self.client.fetch(uids, [item])
        except imap_exc.IMAPClientAbortError:
            raise
        except imap_exc.IMAPClientError as e:
            if len(uids) == 1:
                log.warning("could not fetch %s uid=%s, recording a placeholder: %s", self.cfg.id, uids[0], e)
                return {uids[0]: f"Subject: {UNFETCHABLE_SUBJECT}\r\n\r\n".encode()}
            for u in uids:
                out.update(self._fetch_bodies([u], item))
            return out
        for u, data in fetched.items():
            key = _body_key(data)
            if key is not None and isinstance(data[key], bytes):
                out[u] = data[key]
        return out

    def fetch_new(self, after_uid: int, limit: int = 50) -> list[MailMessage]:
        self._ensure_inbox()
        # "UID n:*" 在没有新邮件时也会返回最后一封，必须再过滤
        uids = sorted(u for u in self.client.search(["UID", f"{after_uid + 1}:*"]) if u > after_uid)
        return self._fetch_messages(uids[:limit], keep_uid=True)

    # ---- IDLE ----

    def idle_wait(self, timeout: float) -> bool:
        """timeout 内收到 EXISTS 返回 True；超时或被 interrupt() 打断返回 False。"""
        if self._interrupt.is_set():
            return False
        if not self._has_idle:
            # 退化成轮询：睡 timeout 后让调用方去 fetch_new
            return not self._interrupt.wait(timeout)
        self._ensure_inbox()
        client = self.client
        deadline = time.monotonic() + timeout
        got_new = self._start_idle(client)
        quick_empty = 0
        ok = False
        try:
            while not got_new and not self._interrupt.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                wait = min(IDLE_CHECK_INTERVAL, remaining)
                started = time.monotonic()
                resps = client.idle_check(timeout=wait)
                if self._has_exists(resps):
                    got_new = True
                    break
                if not resps and time.monotonic() - started < wait * 0.5:
                    # idle_check 吞掉了 EOF：socket 可读却读不到东西 = 连接已断
                    quick_empty += 1
                    if quick_empty >= 3:
                        raise ConnectionError("IMAP connection dropped during IDLE")
                else:
                    quick_empty = 0
            ok = True
        finally:
            try:
                _, resps = client.idle_done()
                if self._has_exists(resps):
                    got_new = True
            except Exception:
                if ok:
                    raise
                log.debug("idle_done failed (another exception is already propagating)", exc_info=True)
        return got_new

    @staticmethod
    def _start_idle(client: IMAPClient) -> bool:
        """发 IDLE 并等到 "+" 续行。imapclient.idle() 遇到服务器在 "+" 之前先推的未经请求响应
        （发 IDLE 时正好来了新邮件："* 9 EXISTS"）会直接抛错；这里把它们读掉，见到 EXISTS 返回 True。"""
        client._idle_tag = client._imap._command("IDLE")
        saw_exists = False
        while True:
            resp = client._imap._get_response()
            if resp is None:  # 续行 "+ idling"
                return saw_exists
            if resp.startswith(client._idle_tag):
                raise ConnectionError(f"IDLE rejected: {_s(resp)}")
            if b"EXISTS" in resp:
                saw_exists = True

    @staticmethod
    def _has_exists(resps: list[Any]) -> bool:
        for r in resps or ():
            if not isinstance(r, tuple) or len(r) < 2:
                continue
            if r[0] == b"BYE":
                raise ConnectionError(f"IMAP server closed the connection: {_s(r[1])}")
            if r[1] == b"EXISTS":
                return True
        return False

    # ---- 搜索 ----

    def search(self, query: str, limit: int = 10) -> list[MailMessage]:
        """Gmail 用 X-GM-RAW 在 All Mail 里搜；其他服务器在 INBOX 里 TEXT 搜。最新的在前。"""
        if self._gmail:
            self._select(self._special(ALL))
            uids = self.client.gmail_search(query)
            keep_uid = False
        else:
            self._select(INBOX)
            uids = self.client.search(["TEXT", query], charset="UTF-8")
            keep_uid = True
        picked = sorted(uids, reverse=True)[:limit]
        msgs = self._fetch_messages(picked, keep_uid=keep_uid)
        msgs.sort(key=lambda m: m.date, reverse=True)
        return msgs

    def fetch_by_gm_msgid(self, gm_msgid: str) -> MailMessage | None:
        if not self._gmail or not gm_msgid.isdigit():
            return None
        self._select(self._special(ALL))
        uids = self.client.search(["X-GM-MSGID", int(gm_msgid)])
        if not uids:
            return None
        msgs = self._fetch_messages([max(uids)], keep_uid=False)
        return msgs[0] if msgs else None

    # ---- 已发送 / 草稿 ----

    def sent_since(self, after_uid: int | None) -> tuple[int, int, list[dict]]:
        """返回 (UIDVALIDITY, max_uid, 列表)。列表项：uid、gm_thrid、in_reply_to、message_id、
        to（To+Cc 地址，小写）、date（aware UTC datetime）。一次最多 200 封，max_uid 是本次处理到的位置。"""
        uidvalidity, uidnext = self._uid_info(self._select(self._special(SENT)))
        max_uid = uidnext - 1
        if after_uid is None:
            return uidvalidity, max_uid, []
        uids = sorted(u for u in self.client.search(["UID", f"{after_uid + 1}:*"]) if u > after_uid)
        if not uids:
            return uidvalidity, max(after_uid, max_uid), []
        uids = uids[:SENT_MAX_PER_CALL]
        fields = ["INTERNALDATE", "BODY.PEEK[HEADER.FIELDS (IN-REPLY-TO TO CC DATE MESSAGE-ID)]"]
        if self._gmail:
            fields.append("X-GM-THRID")
        data = self.client.fetch(uids, fields)
        items: list[dict] = []
        for u in uids:
            d = data.get(u)
            if d is None:
                continue
            key = _body_key(d)
            hdr = email.message_from_bytes(d[key] if key and isinstance(d[key], bytes) else b"")
            date = None
            try:
                if hdr.get("Date"):
                    date = _utc(parsedate_to_datetime(str(hdr["Date"])))
            except (TypeError, ValueError, IndexError, OverflowError):
                date = None
            items.append(
                {
                    "uid": u,
                    "gm_thrid": _s(d.get(b"X-GM-THRID")),
                    "in_reply_to": decode_header_value(hdr.get("In-Reply-To")),
                    "message_id": decode_header_value(hdr.get("Message-ID")).strip(),
                    "to": [a.lower() for _, a in header_addresses(hdr, "To") + header_addresses(hdr, "Cc")],
                    "date": date or _utc(d.get(b"INTERNALDATE")) or datetime.now(timezone.utc),
                }
            )
        return uidvalidity, uids[-1], items

    def fetch_sent(self, uids: list[int]) -> list[MailMessage]:
        """按 UID 抓 \\Sent 里的邮件全文（比对用户在 Gmail 里改过再发的草稿）。返回的 uid 是 Sent 文件夹的 UID。"""
        self._select(self._special(SENT))
        return self._fetch_messages(uids, keep_uid=True)

    def append_draft(self, msg: EmailMessage) -> None:
        folder = self._special(DRAFTS)
        self.client.append(folder, msg.as_bytes(policy=SMTP_POLICY), flags=(DRAFT,), msg_time=datetime.now(timezone.utc))
        log.info("saved draft %s: %s", self.cfg.id, msg.get("Message-ID", ""))

    def delete_draft(self, message_id: str) -> bool:
        """发送成功后清理 mail-agent 自己存的草稿：在 \\Drafts 里按 Message-ID 精确找，移到 \\Trash。
        找不到（用户已在 Gmail 里改过/删掉）返回 False。"""
        if not message_id:
            return False
        folder = self._special(DRAFTS)
        self.client.select_folder(folder, readonly=False)
        self._selected = None  # 下次 _ensure_inbox 重新只读选中 INBOX
        uids = self.client.search(["HEADER", "Message-ID", message_id])
        if not uids:
            return False
        if self.client.has_capability("MOVE"):
            self.client.move(uids, self._special(TRASH))
        else:
            self.client.add_flags(uids, [DELETED])
            self.client.expunge()
        log.info("removed sent draft %s: %s", self.cfg.id, message_id)
        return True
