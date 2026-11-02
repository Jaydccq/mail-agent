"""SMTP 发信（Gmail：smtp.gmail.com:465 + 应用专用密码）。

错误分两类，调用方据此决定要不要重试：
- SendFailed：肯定没发出去（连不上、登录失败、服务器明确拒收），可以改正后重试；
- SendUnknown：DATA 过程中断开/超时，可能已经发出，不要自动重发。
"""

from __future__ import annotations

import logging
import re
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, getaddresses, make_msgid

from mailagent.config import MailboxConfig
from mailagent.models import MailMessage

log = logging.getLogger(__name__)

_APP_PASSWORD_HINT = ("check the app password (2-Step Verification must be turned on first): "
                      "https://myaccount.google.com/apppasswords")
_RE_REPLY_PREFIX = re.compile(r"^\s*(re|回复|答复|回覆)\s*[:：]", re.IGNORECASE)


class SendFailed(Exception):
    """肯定没发出去。"""


class SendUnknown(Exception):
    """发送中断，结果未知（可能已发出）。"""


def _domain(address: str) -> str:
    return address.rsplit("@", 1)[-1].strip() or "localhost"


def _bracket(msgid: str) -> str:
    msgid = msgid.strip()
    if msgid and not msgid.startswith("<"):
        msgid = f"<{msgid}>"
    return msgid


def build_reply(
    mailbox: MailboxConfig,
    original: MailMessage,
    body: str,
    *,
    to: list[str] | None = None,
    cc: list[str] | None = None,
) -> EmailMessage:
    msg = EmailMessage()
    recipients = to or [original.reply_to or original.from_addr]
    recipients = [r for r in recipients if r]
    if not recipients:
        raise ValueError("the original email has no address to reply to")

    subject = original.subject.strip()
    if not _RE_REPLY_PREFIX.match(subject):
        subject = f"Re: {subject}" if subject else "Re:"

    msg["From"] = mailbox.address
    msg["To"] = ", ".join(recipients)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=_domain(mailbox.address))

    parent = _bracket(original.message_id)
    if parent:
        msg["In-Reply-To"] = parent
        refs = original.references.split()
        if parent not in refs:
            refs.append(parent)
        msg["References"] = " ".join(refs)

    # 非 ASCII 正文用 base64：不依赖服务器的 8BITMIME
    if body.isascii():
        msg.set_content(body)
    else:
        msg.set_content(body, cte="base64")
    return msg


def send(mailbox: MailboxConfig, msg: EmailMessage, timeout: float = 60) -> tuple[str, list[str]]:
    """登录并发送，返回 (Message-ID, 被服务器拒收的收件人)。全部拒收抛 SendFailed；部分拒收照常返回，
    调用方要告诉用户哪些人没收到。"""
    if not msg["From"]:
        msg["From"] = mailbox.address
    if not msg["Date"]:
        msg["Date"] = formatdate(localtime=True)
    if not msg["Message-ID"]:
        msg["Message-ID"] = make_msgid(domain=_domain(mailbox.address))
    message_id = str(msg["Message-ID"])

    rcpts = [a for _, a in getaddresses([str(v) for k in ("To", "Cc", "Bcc") for v in (msg.get_all(k) or [])]) if a]
    if not rcpts:
        raise SendFailed("no recipients")

    password = mailbox.app_password.replace(" ", "")
    context = ssl.create_default_context()

    # 连接 + 登录：这里失败肯定没发出去
    try:
        smtp = smtplib.SMTP_SSL(mailbox.smtp_host, mailbox.smtp_port, timeout=timeout, context=context)
    except (OSError, smtplib.SMTPException) as e:
        raise SendFailed(f"could not connect to {mailbox.smtp_host}:{mailbox.smtp_port}: {e}") from e

    try:
        try:
            smtp.login(mailbox.address, password)
        except smtplib.SMTPAuthenticationError as e:
            raise SendFailed(f"SMTP login failed ({mailbox.address}): {_APP_PASSWORD_HINT}") from e
        except (OSError, smtplib.SMTPException) as e:
            raise SendFailed(f"SMTP login failed ({mailbox.address}): {e}") from e

        # 发送：服务器明确拒收 = 没发出；中途断开/超时 = 结果未知
        try:
            refused = smtp.send_message(msg, from_addr=mailbox.address, to_addrs=rcpts)
        except (
            smtplib.SMTPRecipientsRefused,
            smtplib.SMTPSenderRefused,
            smtplib.SMTPDataError,
            smtplib.SMTPHeloError,
            smtplib.SMTPNotSupportedError,
        ) as e:
            raise SendFailed(f"rejected by the server: {e}") from e
        except (smtplib.SMTPServerDisconnected, OSError) as e:
            raise SendUnknown(f"connection dropped while sending; the email may have been sent: {e}") from e
        except smtplib.SMTPException as e:
            raise SendUnknown(f"send result unknown: {e}") from e
        refused_addrs = sorted(refused)
        if refused_addrs:
            log.warning("some recipients were refused %s: %s", message_id, refused_addrs)
    finally:
        # QUIT 失败不影响已经成功的发送
        try:
            smtp.quit()
        except Exception:
            try:
                smtp.close()
            except Exception:
                pass

    log.info("sent %s -> %s", message_id, rcpts)
    return message_id, refused_addrs
