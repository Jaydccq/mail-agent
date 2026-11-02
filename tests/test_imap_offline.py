from datetime import datetime, timezone

import pytest
from imapclient import exceptions as imap_exc

from mailagent.config import MailboxConfig
from mailagent.mail import imap as imap_mod
from mailagent.mail import watcher as watcher_mod
from mailagent.mail.imap import AuthError, ImapMailbox
from mailagent.mail.watcher import MailWatcher


class _FakeImap:
    def __init__(self, responses):
        self.responses = list(responses)

    def _command(self, name):
        assert name == "IDLE"
        return b"A001"

    def _get_response(self):
        return self.responses.pop(0)


class _FakeClient:
    def __init__(self, responses):
        self._imap = _FakeImap(responses)
        self._idle_tag = None


def test_start_idle_tolerates_exists_before_continuation():
    # 发 IDLE 时正好来了新邮件：服务器先推 "* 9 EXISTS" 再给 "+"
    c = _FakeClient([b"* 9 EXISTS", b"* 1 RECENT", None])
    assert ImapMailbox._start_idle(c) is True
    assert c._idle_tag == b"A001"
    assert ImapMailbox._start_idle(_FakeClient([None])) is False


def test_start_idle_rejected():
    with pytest.raises(ConnectionError):
        ImapMailbox._start_idle(_FakeClient([b"A001 BAD IDLE not allowed"]))


# ---- 登录失败分类、重连退避、坏邮件 ----

MB = MailboxConfig(id="me", address="me@gmail.com", app_password="abcd efgh ijkl mnop")


def _login_fails_with(monkeypatch, text):
    class Client:
        def __init__(self, *a, **k):
            pass

        def login(self, user, password):
            raise imap_exc.LoginError(text)

        def shutdown(self):
            pass

    monkeypatch.setattr(imap_mod, "IMAPClient", Client)


@pytest.mark.parametrize("text", [
    "[ALERT] Too many simultaneous connections. (Failure)",
    "[ALERT] Account exceeded command or bandwidth limits. (Failure)",
    "[THROTTLED] Please try again later",
])
def test_gmail_connection_limits_are_not_a_bad_password(monkeypatch, text):
    _login_fails_with(monkeypatch, text)
    with pytest.raises(ConnectionError):
        ImapMailbox(MB).connect()


def test_wrong_app_password_is_an_auth_error(monkeypatch):
    _login_fails_with(monkeypatch, "[AUTHENTICATIONFAILED] Invalid credentials (Failure)")
    with pytest.raises(AuthError):
        ImapMailbox(MB).connect()


def test_reconnect_backoff_grows_when_idle_keeps_failing(monkeypatch):
    sessions: list[object] = []
    w = MailWatcher(MB, lambda: (1, 10), lambda *a: None, lambda *a: None, lambda *a: None)

    class Box:  # 登录、补齐都成功，但每次一进 IDLE 就断
        def __init__(self, cfg):
            sessions.append(self)

        def connect(self):
            pass

        def select_inbox(self):
            return 1, 11

        def fetch_new(self, after_uid, limit=50):
            return []

        def idle_wait(self, timeout):
            if len(sessions) >= 5:
                w._stop.set()
            raise ConnectionError("IMAP connection dropped during IDLE")

        def interrupt(self):
            pass

        def close(self):
            pass

    waits: list[float] = []
    monkeypatch.setattr(watcher_mod, "ImapMailbox", Box)
    monkeypatch.setattr(w._stop, "wait", waits.append)
    w._run()
    assert waits == [5, 10, 20, 40]


class _BatchClient:
    """FETCH 里只要带上 uid 7 就报协议错误（一封服务器吐不出来的坏邮件）。"""

    def __init__(self, error=imap_exc.ProtocolError):
        self.error = error

    def fetch(self, uids, fields):
        if "RFC822.SIZE" in fields:
            return {u: {b"RFC822.SIZE": 100, b"FLAGS": (), b"INTERNALDATE": datetime.now(timezone.utc)} for u in uids}
        if 7 in uids:
            raise self.error("unexpected response for uid 7")
        return {u: {b"BODY[]": f"From: a@x.example\r\nSubject: mail {u}\r\n\r\nhi\r\n".encode()} for u in uids}


def _box(client) -> ImapMailbox:
    box = ImapMailbox(MB)
    box._client = client
    box._gmail = False
    return box


def test_one_unfetchable_email_does_not_block_the_batch():
    msgs = _box(_BatchClient())._fetch_messages([5, 6, 7, 8], keep_uid=True)
    assert [(m.uid, m.subject) for m in msgs] == [
        (5, "mail 5"), (6, "mail 6"), (7, imap_mod.UNFETCHABLE_SUBJECT), (8, "mail 8")]


def test_connection_errors_during_fetch_are_not_swallowed():
    with pytest.raises(imap_exc.IMAPClientAbortError):
        _box(_BatchClient(imap_exc.IMAPClientAbortError))._fetch_messages([5, 7], keep_uid=True)
