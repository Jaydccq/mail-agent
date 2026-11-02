"""日志：~/.mail-agent/agent.log（滚动 2MB × 3），在终端里运行时也打到 stderr。不记录密钥和完整邮件正文。

后台服务（launchd/systemd）的 stderr 不是终端（launchd 里是 launchd.log）：不往那里再写一份，
否则就是一个不轮转、无限增长的副本；那里只留下 Python 自己打印的崩溃信息。"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from mailagent.config import data_dir

FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def setup_logging(verbose: bool = False) -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    for h in list(root.handlers):
        root.removeHandler(h)
    fmt = logging.Formatter(FORMAT, datefmt="%Y-%m-%d %H:%M:%S")
    if sys.stderr.isatty():
        err = logging.StreamHandler(sys.stderr)
        err.setFormatter(fmt)
        root.addHandler(err)
    try:
        d = data_dir()
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        fh = RotatingFileHandler(d / "agent.log", maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError as e:
        root.warning("Cannot write the log file: %s", e)
    # 第三方库太吵；httpx 的 INFO 日志会带完整 URL（Telegram token 在 URL 里），必须压掉
    for name in ("httpx", "httpcore", "imapclient", "asyncio", "Lark"):
        logging.getLogger(name).setLevel(logging.WARNING)
