"""配置：一个 TOML 文件，存在 ~/.mail-agent/config.toml（权限 0600）。

设计目标是"少设置"：除了邮箱、渠道 token、Jev key 外全部有默认值。
数据目录可用环境变量 MAIL_AGENT_HOME 覆盖（测试和多实例用）。
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Callable


def data_dir() -> Path:
    return Path(os.environ.get("MAIL_AGENT_HOME", "~/.mail-agent")).expanduser()


def config_path() -> Path:
    return data_dir() / "config.toml"


LLM_PROVIDERS = ("none", "claude-cli", "codex-cli", "gemini-cli", "openai", "anthropic")


@dataclass
class MailboxConfig:
    id: str  # 短标识，例如 "personal"；数据库各表都用它区分邮箱
    address: str
    app_password: str
    imap_host: str = "imap.gmail.com"
    imap_port: int = 993
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 465


@dataclass
class JevConfig:
    api_key: str = ""
    model: str = "jev-latest"
    base_url: str = "https://api.typesafe.ai/v1"


@dataclass
class LLMConfig:
    # none | claude-cli | codex-cli | gemini-cli | openai | anthropic
    # *-cli 走用户本机已登录的官方 CLI（即用户自己的订阅额度）；
    # openai 是任意 OpenAI 兼容接口（豆包/火山方舟、DeepSeek、Kimi、Qwen、GLM、OpenRouter、Ollama…，见 ai/llm/presets.py）；
    # anthropic 是 Anthropic 或任意 Anthropic 兼容接口。base_url / api_key / model 都可以自己填。
    provider: str = "none"
    model: str = ""  # 留空 = 用 CLI / 服务端默认
    base_url: str = ""  # 仅 openai / anthropic
    api_key: str = ""  # 仅 openai / anthropic
    command: str = ""  # 自定义 CLI 路径，留空则在 PATH 里找
    timeout: int = 180


@dataclass
class TelegramConfig:
    bot_token: str = ""
    owner_chat_id: str = ""  # 配对后写入
    api_base: str = "https://api.telegram.org"


@dataclass
class LarkConfig:
    app_id: str = ""
    app_secret: str = ""
    domain: str = "lark"  # lark（国际版）| feishu（飞书）
    owner_open_id: str = ""  # 配对后写入


@dataclass
class Prefs:
    timezone: str = "America/Los_Angeles"
    language: str = "en"  # 通知、摘要、分析的语言；聊天回复跟随用户说的语言
    digest_times: list[str] = field(default_factory=lambda: ["08:30", "18:00"])
    quiet_start: str = "23:00"
    quiet_end: str = "08:00"
    primary_channel: str = "telegram"  # telegram | lark


@dataclass
class Config:
    jev: JevConfig = field(default_factory=JevConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    mailboxes: list[MailboxConfig] = field(default_factory=list)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    lark: LarkConfig = field(default_factory=LarkConfig)
    prefs: Prefs = field(default_factory=Prefs)

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram.bot_token)

    @property
    def lark_enabled(self) -> bool:
        return bool(self.lark.app_id and self.lark.app_secret)

    def mailbox(self, mailbox_id: str) -> MailboxConfig:
        for m in self.mailboxes:
            if m.id == mailbox_id:
                return m
        raise KeyError(mailbox_id)


class ConfigError(Exception):
    pass


def _build(cls: type, raw: dict[str, Any]) -> Any:
    known = {f.name for f in fields(cls)}
    unknown = set(raw) - known
    if unknown:
        raise ConfigError(f"unknown field(s) in {cls.__name__}: {', '.join(sorted(unknown))}")
    return cls(**raw)


def load_config(path: Path | None = None) -> Config:
    path = path or config_path()
    if not path.exists():
        raise ConfigError(f"config file {path} not found; run: mail-agent setup")
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    cfg = Config(
        jev=_build(JevConfig, raw.get("jev", {})),
        llm=_build(LLMConfig, raw.get("llm", {})),
        mailboxes=[_build(MailboxConfig, m) for m in raw.get("mailboxes", [])],
        telegram=_build(TelegramConfig, raw.get("telegram", {})),
        lark=_build(LarkConfig, raw.get("lark", {})),
        prefs=_build(Prefs, raw.get("prefs", {})),
    )
    validate(cfg)
    return cfg


def validate(cfg: Config) -> None:
    if cfg.llm.provider not in LLM_PROVIDERS:
        raise ConfigError(f"llm.provider must be one of {LLM_PROVIDERS}")
    ids = [m.id for m in cfg.mailboxes]
    if len(ids) != len(set(ids)):
        raise ConfigError("mailbox ids must be unique")
    if cfg.prefs.primary_channel not in ("telegram", "lark"):
        raise ConfigError("prefs.primary_channel must be telegram or lark")
    if cfg.lark.domain not in ("lark", "feishu"):
        raise ConfigError("lark.domain must be lark or feishu")


def _toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    raise TypeError(f"不支持写入 TOML 的类型: {type(v)}")


def dumps(cfg: Config) -> str:
    out: list[str] = ["# mail-agent config. Contains secrets: do not share or commit it.", ""]
    for section in ("jev", "llm", "telegram", "lark", "prefs"):
        out.append(f"[{section}]")
        for k, v in asdict(getattr(cfg, section)).items():
            out.append(f"{k} = {_toml_value(v)}")
        out.append("")
    for m in cfg.mailboxes:
        out.append("[[mailboxes]]")
        for k, v in asdict(m).items():
            out.append(f"{k} = {_toml_value(v)}")
        out.append("")
    return "\n".join(out)


def save_config(cfg: Config, path: Path | None = None) -> Path:
    validate(cfg)
    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(dumps(cfg))
    os.replace(tmp, path)
    os.chmod(path, 0o600)
    return path


def update_config(change: Callable[[Config], Any], path: Path | None = None) -> None:
    """改一项配置并保存：读磁盘上当前的文件、改、写回。运行中的 agent 改配置都走这里：
    直接保存内存里的 Config 会把用户手改过（还没 restart）的其他内容覆盖回旧值。"""
    cfg = load_config(path)
    change(cfg)
    save_config(cfg, path)


def new_mailbox_id(address: str, taken: set[str]) -> str:
    """邮箱 id = 地址 @ 前面的部分（只留字母数字，小写），重复时加数字。"""
    base = re.sub(r"[^A-Za-z0-9]", "", address.split("@", 1)[0]).lower() or "mail"
    mid, n = base, 2
    while mid in taken:
        mid, n = f"{base}{n}", n + 1
    return mid


def make_mailbox(address: str, app_password: str, existing: list[MailboxConfig]) -> MailboxConfig:
    """按地址新建（或替换同地址的）邮箱配置。Gmail 和 Google Workspace 都用默认的 imap/smtp.gmail.com。"""
    address = address.strip().lower()
    password = app_password.replace(" ", "").strip()
    prev = next((m for m in existing if m.address.lower() == address), None)
    if prev:
        return MailboxConfig(id=prev.id, address=address, app_password=password, imap_host=prev.imap_host,
                             imap_port=prev.imap_port, smtp_host=prev.smtp_host, smtp_port=prev.smtp_port)
    return MailboxConfig(id=new_mailbox_id(address, {m.id for m in existing}), address=address, app_password=password)


def upsert_mailbox(cfg: Config, mb: MailboxConfig) -> None:
    """加入或替换（按地址）一个邮箱，保持原有顺序。"""
    for i, m in enumerate(cfg.mailboxes):
        if m.address.lower() == mb.address.lower():
            cfg.mailboxes[i] = mb
            return
    cfg.mailboxes.append(mb)


def remove_mailbox(cfg: Config, key: str) -> MailboxConfig | None:
    """按 id 或地址删除邮箱，返回被删的配置。"""
    key = key.strip().lower()
    for i, m in enumerate(cfg.mailboxes):
        if key in (m.id.lower(), m.address.lower()):
            return cfg.mailboxes.pop(i)
    return None


def redacted(cfg: Config) -> dict[str, Any]:
    """给 status / doctor 展示用，隐藏所有密钥。"""

    def scrub(obj: Any) -> Any:
        if is_dataclass(obj) and not isinstance(obj, type):
            obj = asdict(obj)
        if isinstance(obj, dict):
            return {
                k: ("***" if v and any(s in k for s in ("password", "key", "token", "secret")) else scrub(v))
                for k, v in obj.items()
            }
        if isinstance(obj, list):
            return [scrub(x) for x in obj]
        return obj

    return scrub(cfg)
