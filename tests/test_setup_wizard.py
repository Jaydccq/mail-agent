"""setup 向导走一遍：脚本化输入，Jev / IMAP / Telegram / 模型检查都替换成假的，确认最后写出的配置。"""

from __future__ import annotations

import getpass

import pytest

from mailagent import cli
from mailagent.config import load_config


class FakeChannel:
    def __init__(self, codes: list[str]):
        self.codes = codes

    async def check(self) -> str:
        return "@test_bot"

    async def pair(self, code: str, timeout: float = 300) -> tuple[str, str]:
        self.codes.append(code)
        return "42", "42"

    async def close(self) -> None:
        pass


def _script(monkeypatch: pytest.MonkeyPatch, inputs: list[str], secrets_: list[str]) -> None:
    it, sec = iter(inputs), iter(secrets_)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(it))
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": next(sec))


def test_first_run_wizard(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MAIL_AGENT_HOME", str(tmp_path))
    codes: list[str] = []

    async def jev_models(jc):
        assert jc.api_key == "jev-key"
        return ["jev-latest"]

    async def llm_check(lc):
        return f"{lc.provider} ok"

    monkeypatch.setattr(cli, "_jev_models", jev_models)
    monkeypatch.setattr(cli, "_imap_check", lambda mb: None)
    monkeypatch.setattr(cli, "_make_channel", lambda name, cfg: FakeChannel(codes))
    monkeypatch.setattr(cli, "_llm_check", llm_check)
    monkeypatch.setattr("mailagent.ai.llm.detect_clis", lambda: {"codex": "/usr/bin/codex"})
    monkeypatch.setattr("mailagent.agent.timeutil.detect_timezone", lambda: "Europe/London")
    _script(monkeypatch,
            inputs=["Me@Gmail.com",  # 第一个邮箱
                    "",               # 不再加邮箱
                    "",               # 渠道：默认 Telegram
                    "",               # 模型：默认第一个找到的 CLI（codex）
                    "tokyo",          # 时区
                    "n"],             # 不装后台服务
            secrets_=["jev-key", "abcd efgh ijkl mnop", "123:bot-token"])

    assert cli.main(["setup"]) == 0
    out = capsys.readouterr().out
    assert "Have these ready" in out
    assert f"https://t.me/test_bot?start={codes[0]}" in out
    cfg = load_config()
    assert cfg.jev.api_key == "jev-key"
    assert [(m.address, m.app_password) for m in cfg.mailboxes] == [("me@gmail.com", "abcdefghijklmnop")]
    assert (cfg.telegram.bot_token, cfg.telegram.owner_chat_id) == ("123:bot-token", "42")
    assert cfg.prefs.primary_channel == "telegram"
    assert cfg.llm.provider == "codex-cli"
    assert cfg.prefs.timezone == "Asia/Tokyo"
    assert (tmp_path / "config.toml").stat().st_mode & 0o777 == 0o600


def test_rerun_keeps_everything_with_enter(tmp_path, monkeypatch, capsys):
    test_first_run_wizard(tmp_path, monkeypatch, capsys)
    before = (tmp_path / "config.toml").read_text()
    _script(monkeypatch,
            inputs=["",   # 保留已有邮箱
                    "",   # 不再加邮箱
                    "",   # 渠道不变
                    "",   # 已配对，不重新配对（默认 No）
                    "",   # 模型不变
                    "",   # 时区不变
                    "n"],
            secrets_=["", ""])  # Jev key、bot token 回车保留
    codes: list[str] = []
    monkeypatch.setattr(cli, "_make_channel", lambda name, cfg: FakeChannel(codes))
    assert cli.main(["setup"]) == 0
    assert codes == []  # 已配对的不再配对
    assert "Have these ready" not in capsys.readouterr().out
    assert (tmp_path / "config.toml").read_text() == before


def test_feishu_wizard_detects_edition_and_pairs(tmp_path, monkeypatch, capsys):
    """飞书：第一次一步步带着建应用；不问国际版还是飞书（自动识别）；SDK 日志不往终端打；配对提示写出要搜的机器人名。"""
    import logging

    monkeypatch.setenv("MAIL_AGENT_HOME", str(tmp_path))
    codes: list[str] = []
    detected: list[tuple[str, str]] = []

    async def jev_models(jc):
        return ["jev-latest"]

    async def llm_check(lc):
        return "ok"

    async def detect_domain(app_id, app_secret):
        detected.append((app_id, app_secret))
        return "feishu"

    monkeypatch.setattr(cli, "_jev_models", jev_models)
    monkeypatch.setattr(cli, "_imap_check", lambda mb: None)
    class FakeLark(FakeChannel):
        async def check(self) -> str:
            return 'Feishu bot "Mail Agent"'

    monkeypatch.setattr(cli, "_make_channel", lambda name, cfg: FakeLark(codes))
    monkeypatch.setattr(cli, "_llm_check", llm_check)
    monkeypatch.setattr("mailagent.channels.lark.detect_domain", detect_domain)
    monkeypatch.setattr("mailagent.ai.llm.detect_clis", lambda: {"codex": "/usr/bin/codex"})
    monkeypatch.setattr("mailagent.agent.timeutil.detect_timezone", lambda: "Asia/Shanghai")
    monkeypatch.setattr(logging.getLogger("Lark"), "disabled", False)
    _script(monkeypatch,
            inputs=["me@gmail.com", "",  # 邮箱
                    "2",                 # 渠道：Lark / Feishu
                    "",                  # 还没有应用 → 一步步带着做
                    "", "", "", "", "",  # 前 5 步各按一次回车（第 6 步复制凭据，接着就问 App ID）
                    "cli_abc",           # App ID（后面直接是模型，没有"哪个版本"的问题）
                    "", "", "n"],        # 模型、时区回车，不装后台服务
            secrets_=["jev-key", "abcd efgh ijkl mnop", "app-secret"])

    assert cli.main(["setup"]) == 0
    out = capsys.readouterr().out
    assert "Which edition" not in out
    assert '"im:message.p2p_msg:readonly"' in out and "then click Publish" in out
    walkthrough = out[out.index("Create the bot app"):out.index("App ID")]
    assert all(len(line) <= 80 for line in walkthrough.splitlines() if '"scopes"' not in line)  # 默认终端宽度
    assert '✅ Feishu bot "Mail Agent"' in out
    assert 'search for the Feishu bot "Mail Agent",' in out and "open the chat marked BOT" in out
    assert codes[0] in out
    assert detected == [("cli_abc", "app-secret")]
    assert logging.getLogger("Lark").disabled
    cfg = load_config()
    assert (cfg.lark.app_id, cfg.lark.domain, cfg.lark.owner_open_id) == ("cli_abc", "feishu", "42")
    assert cfg.prefs.primary_channel == "lark"

# ---- 生成模型这一步：预设、自己填网址、换服务商不带旧 key ----

DOUBAO_URL = "https://ark.cn-beijing.volces.com/api/v3"


def _model_env(tmp_path, monkeypatch, llm, checks: list[str], fail_models: tuple[str, ...] = ()):
    """存一份只差模型的配置，假的 CLI 检测和模型验证；返回 cli.main(["model"]) 前的准备。"""
    from mailagent.config import Config, JevConfig, MailboxConfig, save_config

    monkeypatch.setenv("MAIL_AGENT_HOME", str(tmp_path))
    save_config(Config(jev=JevConfig(api_key="j"), mailboxes=[MailboxConfig(id="me", address="me@gmail.com",
                                                                               app_password="x")], llm=llm))

    async def llm_check(lc):
        checks.append(f"{lc.provider}|{lc.base_url}|{lc.api_key}|{lc.model}")
        if lc.model in fail_models:
            raise RuntimeError(f"model {lc.model} not found")
        return "ok"

    monkeypatch.setattr(cli, "_llm_check", llm_check)
    monkeypatch.setattr(cli, "restart_service_if_installed", lambda: True)
    monkeypatch.setattr("mailagent.ai.llm.detect_clis", lambda: {})


def test_model_doubao_preset_and_retry_only_the_model(tmp_path, monkeypatch, capsys):
    from mailagent.config import LLMConfig

    checks: list[str] = []
    _model_env(tmp_path, monkeypatch, LLMConfig(), checks, fail_models=("doubao-seed-2-1-pro-260915",))
    # 菜单：1-3 三个 CLI（都没装），4 豆包，5 豆包国际，6 其他 API，7 自己填网址，8 不用
    _script(monkeypatch, inputs=["4", "",           # 豆包；网址回车用预设
                                 "",                # 模型名回车用预设 → 验证失败
                                 "y", "doubao-seed-2-1-lite-260915"],  # 只重填模型名
            secrets_=["ark-key"])
    assert cli.main(["model"]) == 0
    out = capsys.readouterr().out
    assert "console.volcengine.com/ark" in out
    llm = load_config().llm
    assert (llm.provider, llm.base_url, llm.api_key, llm.model) == \
        ("openai", DOUBAO_URL, "ark-key", "doubao-seed-2-1-lite-260915")
    assert checks == [f"openai|{DOUBAO_URL}|ark-key|doubao-seed-2-1-pro-260915",
                      f"openai|{DOUBAO_URL}|ark-key|doubao-seed-2-1-lite-260915"]


def test_switching_provider_does_not_carry_the_old_key(tmp_path, monkeypatch):
    from mailagent.config import LLMConfig

    checks: list[str] = []
    _model_env(tmp_path, monkeypatch, LLMConfig(provider="openai", base_url=DOUBAO_URL, api_key="ark-key",
                                                model="doubao-x"), checks)
    _script(monkeypatch, inputs=["6", "1", "",      # 其他 API → DeepSeek → 网址回车
                                 "deepseek-chat"],
            secrets_=["", "ds-key"])  # 第一次回车：没有旧 key 可沿用 → "Required." → 再输入
    assert cli.main(["model"]) == 0
    llm = load_config().llm
    assert (llm.base_url, llm.api_key, llm.model) == ("https://api.deepseek.com", "ds-key", "deepseek-chat")


def test_rerun_model_keeps_doubao_with_enter(tmp_path, monkeypatch, capsys):
    from mailagent.config import LLMConfig

    checks: list[str] = []
    old = LLMConfig(provider="openai", base_url=DOUBAO_URL, api_key="ark-key", model="doubao-x")
    _model_env(tmp_path, monkeypatch, old, checks)
    _script(monkeypatch, inputs=["", "", ""], secrets_=[""])  # 菜单默认豆包、网址、模型都回车，key 回车沿用
    assert cli.main(["model"]) == 0
    assert "No change." in capsys.readouterr().out
    assert load_config().llm == old


def test_model_custom_anthropic_compatible_url(tmp_path, monkeypatch):
    from mailagent.config import LLMConfig

    checks: list[str] = []
    _model_env(tmp_path, monkeypatch, LLMConfig(provider="codex-cli"), checks)
    _script(monkeypatch, inputs=["7", "2",                              # 自己填网址 → Anthropic 兼容
                                 "https://llm.example/anthropic/", "my-model"],
            secrets_=["sk-x"])
    assert cli.main(["model"]) == 0
    llm = load_config().llm
    assert (llm.provider, llm.base_url, llm.api_key, llm.model) == \
        ("anthropic", "https://llm.example/anthropic", "sk-x", "my-model")


def test_pairing_pauses_the_service_for_this_config_only(tmp_path, monkeypatch):
    """配对时后台服务也连着同一个机器人，会把配对码收走（飞书长连接一条消息只推给一个连接；Telegram 只能
    一个 getUpdates）。所以配对期间停掉它，之后再启动；只动读同一份配置的服务（别的 MAIL_AGENT_HOME 不碰）。"""
    monkeypatch.setenv("MAIL_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    plist = tmp_path / "com.mailagent.agent.plist"
    monkeypatch.setattr(cli, "_launchd_path", lambda: plist)
    monkeypatch.setattr(cli, "service_status", lambda: "running")
    events: list[str] = []
    monkeypatch.setattr(cli, "_sh", lambda cmd: events.append(cmd[1]) or (0, ""))

    class Recording(FakeChannel):
        async def pair(self, code, timeout=300):
            events.append("pair")
            return "42", "42"

    monkeypatch.setattr(cli, "_make_channel", lambda name, cfg: Recording([]))
    from mailagent.config import TelegramConfig

    plist.write_text(f"<string>MAIL_AGENT_HOME</string><string>{tmp_path / 'home'}</string>")
    assert cli._pair("telegram", TelegramConfig(bot_token="t"), "@b") == ("42", "42")
    assert events == ["unload", "pair", "load"]

    events.clear()
    plist.write_text(f"<string>MAIL_AGENT_HOME</string><string>{tmp_path / 'other'}</string>")
    cli._pair("telegram", TelegramConfig(bot_token="t"), "@b")
    assert events == ["pair"]
