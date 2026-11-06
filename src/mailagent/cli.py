"""命令行入口：setup / run / status / doctor / add-mailbox / remove-mailbox / mailboxes /
install-service / uninstall-service / test-notify。"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import copy
import getpass
import json
import logging
import os
import plistlib
import secrets
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterator, TypeVar

from mailagent.config import (
    Config, ConfigError, JevConfig, LarkConfig, LLMConfig, MailboxConfig, TelegramConfig, config_path, data_dir,
    load_config, make_mailbox, redacted, remove_mailbox, save_config, upsert_mailbox,
)

T = TypeVar("T")

SYSTEMD_UNIT = "mail-agent.service"
LAUNCHD_LABEL = "com.mailagent.agent"

APP_PASSWORD_STEPS = (
    "  You need a Google app password:\n"
    "  1. Turn on 2-Step Verification for your Google account\n"
    "  2. Open https://myaccount.google.com/apppasswords\n"
    "  3. Create one with any name (e.g. mail-agent) and paste the 16 characters here"
)
CHAT_MAILBOX_HINT = ("Tip: you can also do this from Telegram/Lark: /addmailbox you@gmail.com <app password>, "
                     "/removemailbox <address>, /mailboxes")


# ---- 交互小工具 ----


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    v = input(f"{prompt}{suffix}: ").strip()
    return v or default


def ask_secret(prompt: str, old: str = "") -> str:
    suffix = " (Enter to keep current)" if old else ""
    v = getpass.getpass(f"{prompt}{suffix}: ").strip()
    return v or old


def yes(prompt: str, default: bool) -> bool:
    hint = "Y/n" if default else "y/N"
    while True:
        v = input(f"{prompt} ({hint}) ").strip().lower()
        if not v:
            return default
        if v in ("y", "yes"):
            return True
        if v in ("n", "no"):
            return False


def choose(prompt: str, options: list[str], default: int = 0) -> int:
    print(prompt)
    for i, o in enumerate(options, 1):
        print(f"  {i}) {o}")
    while True:
        v = input(f"Choose [{default + 1}]: ").strip()
        if not v:
            return default
        if v.isdigit() and 1 <= int(v) <= len(options):
            return int(v) - 1


def _err(e: BaseException) -> str:
    return f"{type(e).__name__}: {e}" if str(e) else type(e).__name__


# ---- 异步验证小工具 ----


async def _jev_models(jc: JevConfig) -> list[str]:
    from mailagent.ai.jev import JevClient

    c = JevClient(jc)
    try:
        return await c.models()
    finally:
        await c.aclose()


def _imap_check(mb: MailboxConfig) -> None:
    from mailagent.mail.imap import ImapMailbox

    box = ImapMailbox(mb, timeout=30)
    box.connect()
    box.close()


def _verify_mailbox(mb: MailboxConfig) -> bool:
    """登录 IMAP 验证并打印结果。"""
    print("  Logging in to IMAP to verify…")
    try:
        _imap_check(mb)
        print(f"  ✅ Login OK (mailbox id: {mb.id})")
        return True
    except Exception as e:  # noqa: BLE001
        from mailagent.mail.imap import AuthError

        if isinstance(e, AuthError):
            print("  ❌ Login rejected. Make sure 2-Step Verification is on and you used an app password, "
                  "not your Google account password.")
        else:
            print(f"  ❌ Connection failed: {_err(e)}")
        return False


def _make_channel(name: str, cfg: TelegramConfig | LarkConfig) -> Any:
    if name == "telegram":
        from mailagent.channels.telegram import TelegramChannel

        return TelegramChannel(cfg)  # type: ignore[arg-type]
    from mailagent.channels.lark import LarkChannel

    return LarkChannel(cfg)  # type: ignore[arg-type]


async def _with_channel(name: str, cfg: TelegramConfig | LarkConfig, fn: Callable[[Any], Awaitable[T]]) -> T:
    ch = _make_channel(name, cfg)
    try:
        return await fn(ch)
    finally:
        try:
            await ch.close()
        except Exception:  # noqa: BLE001
            pass


async def _llm_check(lc: LLMConfig) -> str:
    from mailagent.ai.llm import make_llm

    llm = make_llm(lc)
    if llm is None:
        return "not configured"
    return await llm.check()


# ---- setup 向导 ----


SETUP_CHECKLIST = """Have these ready (about 5 minutes in total):
  1. Jev API key — decides which emails matter: https://docs.typesafe.ai
  2. Gmail app password — https://myaccount.google.com/apppasswords (needs 2-Step Verification on)
  3. Telegram bot token — in Telegram, message @BotFather, send /newbot, copy the token
     (or a Lark / Feishu app; the wizard walks you through it)
  Optional: a logged-in claude, codex or gemini CLI on this machine for summaries, chat and reply drafts.
  Every step is checked on the spot, and pressing Enter keeps the value shown in [brackets]."""


def _setup_jev(cfg: Config) -> None:
    print("\n[1/5] Jev email triage (decides which emails are important; key from https://docs.typesafe.ai)")
    while True:
        key = ask_secret("Jev API key", cfg.jev.api_key)
        if not key:
            print("  Required.")
            continue
        jc = JevConfig(api_key=key, model=cfg.jev.model, base_url=cfg.jev.base_url)
        try:
            models = asyncio.run(_jev_models(jc))
            print(f"  ✅ Verified ({len(models)} models available)")
            if models and jc.model not in models:
                print(f"  ⚠️ The configured model {jc.model} is not in the list; change it under [jev] model "
                      "in config.toml")
            cfg.jev = jc
            return
        except Exception as e:  # noqa: BLE001
            print(f"  ❌ Verification failed: {_err(e)}")
            if not yes("  Try again?", True):
                cfg.jev = jc
                return


def _ask_mailbox(cfg: Config, old: Config, required: bool) -> MailboxConfig | None:
    """问一个邮箱（回车结束），验证登录；返回 None 表示用户结束输入。"""
    while True:
        prompt = "Gmail address" if required else "Gmail address (press Enter when done)"
        address = ask(prompt).strip().lower()
        if not address:
            if required:
                print("  At least one mailbox is required.")
                continue
            return None
        if "@" not in address:
            print("  Please enter the full email address.")
            continue
        prev = next((m for m in cfg.mailboxes + old.mailboxes if m.address.lower() == address), None)
        while True:
            password = ask_secret("App password (16 characters)", prev.app_password if prev else "")
            if not password.replace(" ", ""):
                print("  Required.")
                continue
            mb = make_mailbox(address, password, cfg.mailboxes)
            if _verify_mailbox(mb):
                return mb
            if not yes("  Enter the password again?", True):
                if yes("  Save this mailbox anyway?", False):
                    return mb
                break


def _setup_mailboxes(cfg: Config, old: Config) -> None:
    print("\n[2/5] Gmail mailboxes (Google Workspace addresses work the same way)")
    print(APP_PASSWORD_STEPS)
    cfg.mailboxes = []
    if old.mailboxes:
        print("  Configured: " + ", ".join(f"{m.address} ({m.id})" for m in old.mailboxes))
        if yes("  Keep these mailboxes?", True):
            cfg.mailboxes = list(old.mailboxes)
    while True:
        mb = _ask_mailbox(cfg, old, required=not cfg.mailboxes)
        if mb is None:
            break
        upsert_mailbox(cfg, mb)
        print("  Added. Enter another address, or press Enter to continue.")


def _pair(name: str, ch_cfg: TelegramConfig | LarkConfig, bot: str = "") -> tuple[str, str] | None:
    app = "Telegram" if name == "telegram" else "Lark / Feishu"
    with _service_paused():
        while True:
            code = f"{secrets.randbelow(10 ** 6):06d}"
            if name == "telegram" and bot.startswith("@") and len(bot) > 1:
                print(f"\n  👉 Open this link and tap Start: https://t.me/{bot[1:]}?start={code}")
                print(f"     (or send {bot} the code {code}; valid for 5 minutes, waiting…)")
            elif name == "lark":
                print(f"\n  👉 In {app}, press ⌘K (Ctrl+K) and search for the {bot},")
                print(f"     open the chat marked BOT and send just this code: {code}")
                print("     (valid for 5 minutes, waiting…; if the console said no connection was")
                print("     detected, save that setting again now)")
            else:
                print(f"\n  👉 Now send this code to your bot in a private chat on {app}: {code}")
                print("     (valid for 5 minutes, waiting…)")
            try:
                # B023 是误报：asyncio.run 当场跑完，lambda 不会拿到之后循环里的 code
                chat_id, user_id = asyncio.run(_with_channel(name, ch_cfg, lambda ch: ch.pair(code, 300)))  # noqa: B023
                print("  ✅ Paired")
                return chat_id, user_id
            except TimeoutError:
                print("  ⏱ No pairing code received within 5 minutes.")
                if name == "lark":
                    print("     Can't find the bot? You must be in the published version's availability.")
            except Exception as e:  # noqa: BLE001
                print(f"  ❌ Pairing error: {_err(e)}")
            if not yes("  Try again?", True):
                return None


def _setup_telegram(cfg: Config, old: Config) -> None:
    print("\n  Telegram: open @BotFather in Telegram → send /newbot → pick a name → copy the token it gives you")
    while True:
        token = ask_secret("  Bot token", old.telegram.bot_token)
        if not token:
            print("  Required.")
            continue
        tc = TelegramConfig(bot_token=token, api_base=old.telegram.api_base,
                            owner_chat_id=old.telegram.owner_chat_id if token == old.telegram.bot_token else "")
        try:
            bot = asyncio.run(_with_channel("telegram", tc, lambda ch: ch.check()))
            print(f"  ✅ {bot}")
            break
        except Exception as e:  # noqa: BLE001
            print(f"  ❌ Verification failed: {_err(e)}")
            if not yes("  Try again?", True):
                cfg.telegram = tc
                return
    if tc.owner_chat_id and not yes(f"  Already paired with chat {tc.owner_chat_id}. Pair again?", False):
        cfg.telegram = tc
        return
    got = _pair("telegram", tc, bot)
    if got:
        tc.owner_chat_id = got[0]
    cfg.telegram = tc


LARK_STEPS = [  # 每行不超过 80 列（默认终端宽度）；JSON 那行要整行复制，例外
    "Open the developer console → Create Custom App. Pick a name you can\n"
    "     search for, e.g. Mail Agent. Consoles: Feishu https://open.feishu.cn/app\n"
    "     · Lark https://open.larksuite.com/app · or your company's own address",
    "Add Features → Bot",
    "Permissions & Scopes → Batch import/export scopes → paste this, confirm:\n"
    '     {"scopes":{"tenant":["im:message","im:message:send_as_bot","im:message.p2p_msg:readonly"],"user":[]}}',
    "Events & Callbacks: set both subscription modes to persistent connection\n"
    "     Event configuration → Add Events → im.message.receive_v1\n"
    "     Callback configuration → Add callback → card.action.trigger\n"
    "     (Card callback communication, not the legacy one). If saving says no\n"
    "     connection was detected, go on: save again while pairing is waiting",
    "Create Version → Availability: only you (no approval needed) → Save,\n"
    "     then click Publish. Saving alone doesn't publish: until the version\n"
    "     is Released you can't find the bot",
    "Credentials & Basic Info → copy the App ID and App Secret",
]


def _lark_walkthrough() -> None:
    print("\n  Create the bot app in your browser (about 10 minutes).\n  Press Enter after each step.")
    for i, step in enumerate(LARK_STEPS, 1):
        print(f"\n  {i}. {step}")
        if i < len(LARK_STEPS):
            input("     Done? Enter ")


def _setup_lark(cfg: Config, old: Config) -> None:
    from mailagent.channels import lark as lark_channel

    # SDK 自己往 stdout 打日志（正常断开也是 ERROR），会把提示冲乱；向导自己报错误原因
    logging.getLogger("Lark").disabled = True
    if not old.lark.app_id and not yes("\n  Do you already have a Lark / Feishu bot app and its App Secret?", False):
        _lark_walkthrough()
    while True:
        app_id = ask("  App ID", old.lark.app_id)
        secret = ask_secret("  App Secret", old.lark.app_secret if app_id == old.lark.app_id else "")
        lc = LarkConfig(app_id=app_id, app_secret=secret, domain=old.lark.domain,
                        owner_open_id=old.lark.owner_open_id if app_id == old.lark.app_id else "")
        if not app_id or not secret:
            print("  Both App ID and App Secret are required.")
            continue
        try:
            lc.domain = asyncio.run(lark_channel.detect_domain(app_id, secret))  # 国际版还是飞书，不用问
            bot = asyncio.run(_with_channel("lark", lc, lambda ch: ch.check()))  # 还查机器人是否已发布
            print(f"  ✅ {bot}")
            break
        except Exception as e:  # noqa: BLE001
            print(f"  ❌ Verification failed: {_err(e)}")
            if not yes("  Try again?", True):
                cfg.lark = lc
                return
    if lc.owner_open_id and not yes("  Already paired. Pair again?", False):
        cfg.lark = lc
        return
    got = _pair("lark", lc, bot)
    if got:
        lc.owner_open_id = got[1]  # Lark 用发送者 open_id 识别主人
    cfg.lark = lc


def _setup_channels(cfg: Config, old: Config) -> None:
    print("\n[3/5] Notification and chat channel")
    default = 2 if (old.telegram_enabled and old.lark_enabled) else 1 if old.lark_enabled else 0
    idx = choose("Where should notifications go and where do you chat with the agent?",
                 ["Telegram", "Lark / Feishu", "Both"], default)
    if idx in (0, 2):
        _setup_telegram(cfg, old)
    else:
        cfg.telegram = TelegramConfig()
    if idx in (1, 2):
        _setup_lark(cfg, old)
    else:
        cfg.lark = LarkConfig()
    if idx == 2:
        p = choose("Which one gets instant notifications (primary channel)?", ["Telegram", "Lark / Feishu"],
                   1 if old.prefs.primary_channel == "lark" else 0)
        cfg.prefs.primary_channel = "lark" if p == 1 else "telegram"
    else:
        cfg.prefs.primary_channel = "telegram" if idx == 0 else "lark"


CLI_CHOICES = [("claude-cli", "claude", "Claude subscription"), ("codex-cli", "codex", "Codex subscription"),
               ("gemini-cli", "gemini", "Gemini subscription")]


def _ollama_models(base_url: str) -> list[str]:
    """本机 Ollama 已拉取的模型（GET /api/tags）；连不上返回 []。"""
    import httpx

    root = base_url.rstrip("/").removesuffix("/v1")
    try:
        r = httpx.get(f"{root}/api/tags", timeout=3)
        return [m["name"] for m in r.json().get("models", []) if m.get("name")]
    except Exception:  # noqa: BLE001
        return []


def _verify_llm(lc: LLMConfig) -> bool:
    print("  Verifying…")
    try:
        print(f"  ✅ {asyncio.run(_llm_check(lc))}")
        return True
    except Exception as e:  # noqa: BLE001
        print(f"  ❌ Verification failed: {_err(e)}")
        return False


def _ask_api(preset: Any, old: LLMConfig) -> LLMConfig | None:
    """API 方式：网址（预设可改）→ key → 模型名 → 验证。验证失败只重问模型名。返回 None = 回到上一级菜单。"""
    from mailagent.ai.llm.presets import norm_url

    print(f"\n  {preset.label}")
    while True:
        base_url = ask("  API base URL", preset.base_url or (old.base_url if old.provider == preset.provider else ""))
        if base_url.startswith(("http://", "https://")):
            break
        print("  Enter the full URL, starting with https:// (or http:// for a local server).")
    # 只有协议和网址都没变时才沿用旧的 key / 模型名（换了服务商不能带着旧 key 过去）
    same = old.provider == preset.provider and norm_url(old.base_url) == norm_url(base_url)
    if preset.key_url:
        print(f"  Get an API key at {preset.key_url}")
    while True:
        key = ask_secret("  API key" if preset.key_required else "  API key (Enter if it needs none)",
                         old.api_key if same else "")
        if key or not preset.key_required:
            break
        print("  Required.")
    default_model = old.model if same and old.model else preset.model
    if preset.key == "ollama":
        local = _ollama_models(base_url)
        if local:
            print(f"  Models on this machine: {', '.join(local[:10])}")
            default_model = default_model or local[0]
        else:
            print("  ⚠️ No models found at that address. Is Ollama running, and have you pulled a model "
                  "(ollama pull <name>)?")
    if preset.model_hint:
        print(f"  Model name: {preset.model_hint}")
    while True:
        model = ""
        while not model:
            model = ask("  Model name", default_model)
        lc = LLMConfig(provider=preset.provider, base_url=base_url.rstrip("/"), api_key=key, model=model,
                       timeout=old.timeout)
        if _verify_llm(lc):
            return lc
        if yes("  Try a different model name?", True):
            default_model = model
            continue
        if yes("  Save this setting anyway?", False):
            return lc
        return None


def _setup_llm(cfg: Config, old: Config, had_old: bool, header: str = "[4/5] ") -> None:
    from mailagent.ai.llm import detect_clis
    from mailagent.ai.llm.presets import DOUBAO, DOUBAO_INTL, OTHER_PRESETS, custom_preset, preset_for

    print(f"\n{header}Generative model (summaries, date extraction, chat, reply drafts; optional, but without it "
          "summaries are simpler and chat is commands only)")
    found = detect_clis()
    entries: list[tuple[str, str]] = []  # (kind, label)
    for provider, binary, label in CLI_CHOICES:
        if binary in found:
            entries.append((provider, f"{label} ({binary} CLI, installed)"))
    for provider, binary, label in CLI_CHOICES:
        if binary not in found:
            entries.append((provider, f"{label} ({binary} CLI, not found)"))
    entries += [
        ("doubao", f"{DOUBAO.label}"),
        ("doubao-intl", f"{DOUBAO_INTL.label}"),
        ("other", "Other API: " + ", ".join(p.label.split(" (")[0] for p in OTHER_PRESETS)),
        ("custom", "Your own API URL (OpenAI- or Anthropic-compatible), with your key and model name"),
        ("none", "None for now"),
    ]
    kinds = [k for k, _ in entries]
    old_preset = preset_for(old.llm.provider, old.llm.base_url) if old.llm.provider in ("openai", "anthropic") else None
    if had_old and (old.llm.provider.endswith("-cli") or old.llm.provider == "none"):
        default = kinds.index(old.llm.provider)
    elif had_old and old.llm.provider in ("openai", "anthropic"):
        default = kinds.index(old_preset.key if old_preset in (DOUBAO, DOUBAO_INTL) else
                              "other" if old_preset else "custom")
    elif found:
        default = 0
    else:
        default = kinds.index("none")
    while True:
        idx = choose("Choose:", [label for _, label in entries], default)
        kind = kinds[idx]
        default = idx
        if kind == "none":
            cfg.llm = LLMConfig(provider="none", timeout=old.llm.timeout)
            print("  OK, no generative model for now. You can add one later with: mail-agent model")
            return
        if kind.endswith("-cli"):
            binary = kind.split("-", 1)[0]
            same = kind == old.llm.provider
            lc = LLMConfig(provider=kind, timeout=old.llm.timeout, command=old.llm.command if same else "",
                           model=old.llm.model if same else "")
            if binary not in found and not lc.command:
                print(f"  ⚠️ {binary} was not found on PATH. Install it and log in first "
                      "(it uses your own subscription).")
            if _verify_llm(lc) or yes("  Save this setting anyway?", False):
                cfg.llm = lc
                return
            continue
        if kind == "doubao":
            preset = DOUBAO
        elif kind == "doubao-intl":
            preset = DOUBAO_INTL
        elif kind == "other":
            sub = choose("Which provider?", [p.label for p in OTHER_PRESETS],
                         OTHER_PRESETS.index(old_preset) if old_preset in OTHER_PRESETS else 0)
            preset = OTHER_PRESETS[sub]
        else:
            proto = choose("Which kind of API?", ["OpenAI-compatible (most providers)", "Anthropic-compatible"],
                           1 if old.llm.provider == "anthropic" and old_preset is None else 0)
            preset = custom_preset("anthropic" if proto == 1 else "openai")
        api = _ask_api(preset, old.llm)
        if api is not None:
            cfg.llm = api
            return


def cmd_setup(args: argparse.Namespace) -> int:
    from mailagent.agent.timeutil import detect_timezone, find_timezone

    print("mail-agent setup (Ctrl+C to quit at any time; nothing is changed until the final save)")
    had_old = config_path().exists()
    old = Config()
    if had_old:
        try:
            old = load_config()
            print(f"Found existing config {config_path()}; press Enter to keep current values.")
        except (ConfigError, ValueError) as e:
            print(f"⚠️ Could not read the existing config ({e}); starting from scratch.")
            had_old = False
    cfg = copy.deepcopy(old)
    if not had_old:
        print("\n" + SETUP_CHECKLIST)
    try:
        _setup_jev(cfg)
        _setup_mailboxes(cfg, old)
        _setup_channels(cfg, old)
        _setup_llm(cfg, old, had_old)
        print("\n[5/5] Time zone")
        default_tz = cfg.prefs.timezone if had_old else detect_timezone()
        while True:
            zone, close = find_timezone(ask("  Your time zone (name or city, e.g. Asia/Shanghai, New York)", default_tz))
            if zone:
                cfg.prefs.timezone = zone
                break
            print("  ❌ Unknown time zone" + (f". Did you mean: {', '.join(close)}?" if close else
                                              "; use a name like Europe/London or a city like Tokyo."))
        print(f"  Language {cfg.prefs.language}, digests at {', '.join(cfg.prefs.digest_times)}, "
              f"quiet hours {cfg.prefs.quiet_start}–{cfg.prefs.quiet_end}")
        print(f"  (change these under [prefs] in {config_path()}; the time zone any time with "
              "mail-agent timezone or /timezone in chat)")
        path = save_config(cfg)
        print(f"\n✅ Config saved: {path}")
        if not (cfg.telegram.owner_chat_id or cfg.lark.owner_open_id):
            print("⚠️ No notification channel is paired yet: mail will still be processed, but you won't get any "
                  "notifications. Run mail-agent setup again once you can pair one.")
        if yes("Install as a background service that starts automatically?", True):
            install_service()
        else:
            print("To run in the foreground: mail-agent run")
    except (KeyboardInterrupt, EOFError):
        print("\nQuit. Config was not saved.")
        return 130
    if cfg.telegram.owner_chat_id or cfg.lark.owner_open_id:
        print("Next: say hi to your bot, or send /help to see what it can do.")
    print("Check status: mail-agent status    Check everything: mail-agent doctor")
    return 0


# ---- 后台服务 ----


def _service_env() -> dict[str, str]:
    env = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")}  # 让服务找得到 claude/codex CLI
    if os.environ.get("MAIL_AGENT_HOME"):
        env["MAIL_AGENT_HOME"] = os.environ["MAIL_AGENT_HOME"]
    return env


def _systemd_path() -> Path:
    return Path("~/.config/systemd/user").expanduser() / SYSTEMD_UNIT


def _launchd_path() -> Path:
    return Path("~/Library/LaunchAgents").expanduser() / f"{LAUNCHD_LABEL}.plist"


def _sh(cmd: list[str]) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return p.returncode, (p.stdout + p.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 127, str(e)


def _systemd_quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def install_service() -> int:
    if sys.platform == "darwin":
        return _install_launchd()
    if sys.platform.startswith("linux"):
        return _install_systemd()
    print(f"Installing a background service is not supported on {sys.platform}; use mail-agent run instead.")
    return 1


def _install_systemd() -> int:
    if not shutil.which("systemctl"):
        print("systemctl not found. You can run it with nohup / tmux instead: mail-agent run")
        return 1
    env_lines = "\n".join(f"Environment={_systemd_quote(f'{k}={v}')}" for k, v in _service_env().items())
    unit = f"""[Unit]
Description=mail-agent personal email agent

[Service]
ExecStart={_systemd_quote(sys.executable)} -m mailagent run
Restart=always
RestartSec=10
{env_lines}

[Install]
WantedBy=default.target
"""
    path = _systemd_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(unit, encoding="utf-8")
    print(f"Wrote {path}")
    for cmd in (["systemctl", "--user", "daemon-reload"], ["systemctl", "--user", "enable", SYSTEMD_UNIT],
                ["systemctl", "--user", "restart", SYSTEMD_UNIT]):
        rc, out = _sh(cmd)
        if rc != 0:
            print(f"❌ {' '.join(cmd)} failed: {out}")
            return 1
    print("✅ Background service started and enabled at boot. Logs: journalctl --user -u mail-agent -f"
          f" (or {data_dir() / 'agent.log'})")
    user = os.environ.get("USER") or getpass.getuser()
    rc, out = _sh(["loginctl", "show-user", user, "-p", "Linger"])
    if "Linger=yes" not in out:
        print(f"Tip: run `loginctl enable-linger {user}` (may need sudo) so the service keeps running "
              "while you are logged out.")
    return 0


def _install_launchd() -> int:
    path = _launchd_path()
    plist = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": [sys.executable, "-m", "mailagent", "run"],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "EnvironmentVariables": _service_env(),
        "StandardOutPath": str(data_dir() / "launchd.log"),
        "StandardErrorPath": str(data_dir() / "launchd.log"),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    data_dir().mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists():
        _sh(["launchctl", "unload", "-w", str(path)])
    with open(path, "wb") as f:
        plistlib.dump(plist, f)
    print(f"Wrote {path}")
    rc, out = _sh(["launchctl", "load", "-w", str(path)])
    if rc != 0:
        print(f"❌ launchctl load failed: {out}")
        return 1
    print(f"✅ Background service started and set to run at login. Logs: {data_dir() / 'agent.log'}")
    return 0


def uninstall_service() -> int:
    if sys.platform == "darwin":
        path = _launchd_path()
        if path.exists():
            _sh(["launchctl", "unload", "-w", str(path)])
            path.unlink()
            print(f"Stopped and removed {path}")
        else:
            print("No background service is installed.")
        return 0
    path = _systemd_path()
    if not path.exists():
        print("No background service is installed.")
        return 0
    _sh(["systemctl", "--user", "disable", "--now", SYSTEMD_UNIT])
    path.unlink()
    _sh(["systemctl", "--user", "daemon-reload"])
    print(f"Stopped and removed {path}")
    return 0


def restart_service_if_installed() -> bool:
    """后台服务装了就重启（让它读到新配置），返回是否重启成功。"""
    if sys.platform == "darwin":
        if not _launchd_path().exists():
            return False
        cmd = ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"]
    else:
        if not _systemd_path().exists():
            return False
        cmd = ["systemctl", "--user", "restart", SYSTEMD_UNIT]
    rc, out = _sh(cmd)
    if rc != 0:
        print(f"⚠️ Could not restart the background service ({' '.join(cmd)}): {out}")
        return False
    print("✅ Background service restarted.")
    return True


def _service_uses_this_config() -> bool:
    """装好的后台服务读的是不是当前这份配置（同一个 MAIL_AGENT_HOME）。"""
    path = _launchd_path() if sys.platform == "darwin" else _systemd_path()
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return False
    if "MAIL_AGENT_HOME" in text:
        return str(data_dir()) in text
    return not os.environ.get("MAIL_AGENT_HOME")


@contextlib.contextmanager
def _service_paused() -> Iterator[None]:
    """配对期间停掉后台服务：它也连着同一个机器人，会把配对码收走（飞书长连接一条消息只推给其中一个连接，
    Telegram 同时只能有一个 getUpdates）。之后再启动；新配置由向导最后的安装/重启生效。"""
    if not _service_uses_this_config() or service_status() != "running":
        yield
        return
    if sys.platform == "darwin":
        stop, start = ["launchctl", "unload", str(_launchd_path())], ["launchctl", "load", str(_launchd_path())]
    else:
        stop, start = ["systemctl", "--user", "stop", SYSTEMD_UNIT], ["systemctl", "--user", "start", SYSTEMD_UNIT]
    _sh(stop)
    print("  (The background service is paused while pairing, so it can't take the code)")
    try:
        yield
    finally:
        _sh(start)


def service_status() -> str:
    if sys.platform == "darwin":
        if not _launchd_path().exists():
            return "not installed"
        rc, out = _sh(["launchctl", "list", LAUNCHD_LABEL])
        return "running" if rc == 0 and '"PID"' in out else "installed, not running"
    if not _systemd_path().exists():
        return "not installed"
    rc, out = _sh(["systemctl", "--user", "is-active", SYSTEMD_UNIT])
    return {"active": "running", "activating": "starting",
            "failed": "installed, failed to start (journalctl --user -u mail-agent)"}.get(
        out.splitlines()[-1] if out else "", f"installed, {out or 'not running'}")


# ---- 其他命令 ----


def _load() -> Config | None:
    try:
        return load_config()
    except (ConfigError, ValueError) as e:
        print(f"❌ {e}")
        return None


def _db_path() -> Path:
    return data_dir() / "agent.db"


def cmd_run(args: argparse.Namespace) -> int:
    from mailagent.app import run
    from mailagent.log import setup_logging

    setup_logging(args.verbose)
    cfg = _load()
    if cfg is None:
        return 2
    if not cfg.mailboxes:
        print("No mailbox configured yet. Run: mail-agent setup (or mail-agent add-mailbox)")
        return 2
    from mailagent.agent.pipeline import primary_target

    if primary_target(cfg) is None:
        logging.getLogger(__name__).warning("no paired notification channel: mail is processed but no "
                                            "notifications are sent (run mail-agent setup to pair one)")
    try:
        return asyncio.run(run(cfg))
    except KeyboardInterrupt:
        return 130


def cmd_status(args: argparse.Namespace) -> int:
    cfg = _load()
    if cfg is None:
        return 2
    print(f"Config file: {config_path()}")
    print(json.dumps(redacted(cfg), ensure_ascii=False, indent=2))
    db_path = _db_path()
    if db_path.exists():
        from mailagent.db import Database

        db = Database(db_path)
        try:
            s = db.stats()
            paused = db.kv_get("paused") == "1"
        finally:
            db.close()
        print("\nMailboxes:")
        for m in s.pop("mailboxes"):
            line = (f"  {m['mailbox_id']}: {m['status']}, last inbox check {m.get('last_sync_at') or '-'}, "
                    f"last mail received {m.get('last_message_at') or '-'}")
            if m.get("error") and m["status"] != "ok":
                line += f", error: {str(m['error'])[:200]}"
            print(line)
        labels = {"messages": "emails", "pending": "pending", "failed": "failed", "notified_24h": "notified (24h)",
                  "open_todos": "open todos", "outbox_queued": "queued notifications",
                  "outbox_failed": "failed notifications"}
        print("Stats: " + ", ".join(f"{labels.get(k, k)} {v}" for k, v in s.items()))
        if paused:
            print("⏸ Instant notifications are paused")
    else:
        print("\nNever run yet (no database).")
    print(f"\nBackground service: {service_status()}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    failures = 0

    def report(ok: bool, name: str, detail: str = "") -> None:
        nonlocal failures
        failures += 0 if ok else 1
        print(f"{'✅' if ok else '❌'} {name}" + (f": {detail}" if detail else ""))

    try:
        cfg = load_config()
        report(True, "Config", str(config_path()))
    except (ConfigError, ValueError) as e:
        report(False, "Config", str(e))
        return 1

    try:
        from mailagent.db import Database

        Database(":memory:").close()
        report(True, "SQLite FTS5 trigram")
    except Exception as e:  # noqa: BLE001
        report(False, "SQLite FTS5 trigram", f"{_err(e)} (needs SQLite >= 3.34 with FTS5 enabled)")

    try:
        models = asyncio.run(_jev_models(cfg.jev))
        report(True, "Jev", f"{len(models)} models" + ("" if not models or cfg.jev.model in models
                                                       else f", ⚠️ {cfg.jev.model} is missing"))
    except Exception as e:  # noqa: BLE001
        report(False, "Jev", _err(e))

    if not cfg.mailboxes:
        report(False, "Mailboxes", "none configured (mail-agent add-mailbox)")
    for mb in cfg.mailboxes:
        try:
            _imap_check(mb)
            report(True, f"IMAP {mb.id}", mb.address)
        except Exception as e:  # noqa: BLE001
            report(False, f"IMAP {mb.id}", f"{mb.address} {_err(e)}")

    channels: list[tuple[str, Any, str]] = []
    if cfg.telegram_enabled:
        channels.append(("telegram", cfg.telegram, cfg.telegram.owner_chat_id))
    if cfg.lark_enabled:
        channels.append(("lark", cfg.lark, cfg.lark.owner_open_id))
    if not channels:
        report(False, "Channels", "neither Telegram nor Lark is configured")
    for name, ch_cfg, owner in channels:
        try:
            info = asyncio.run(_with_channel(name, ch_cfg, lambda ch: ch.check()))
            report(bool(owner), f"Channel {name}",
                   info + ("" if owner else ", ⚠️ no owner paired yet (run mail-agent setup)"))
        except Exception as e:  # noqa: BLE001
            report(False, f"Channel {name}", _err(e))

    if cfg.llm.provider == "none":
        print("ℹ️ Generative model: not configured (template summaries, chat supports commands only)")
    else:
        try:
            report(True, f"Generative model {cfg.llm.provider}", asyncio.run(_llm_check(cfg.llm)))
        except Exception as e:  # noqa: BLE001
            report(False, f"Generative model {cfg.llm.provider}", _err(e))

    service = service_status()
    report(service == "running", "Background service", service)
    print("\nAll good ✅" if not failures else f"\n{failures} problem(s) found")
    return 0 if not failures else 1


def cmd_retry_analysis(args: argparse.Namespace) -> int:
    from mailagent.agent.timeutil import utcnow
    from mailagent.db import Database

    cfg = _load()
    if cfg is None:
        return 2
    if not _db_path().exists():
        print("No database yet.")
        return 1
    db = Database(_db_path())
    try:
        count = db.retry_failed_analyses([m.id for m in cfg.mailboxes], utcnow())
    finally:
        db.close()
    print(f"Queued {count} failed analyses for another bounded recovery cycle. Old notifications are not replayed.")
    return 0


def cmd_backup(args: argparse.Namespace) -> int:
    import sqlite3

    from mailagent.backup import create_backup, verify_backup

    try:
        if args.verify:
            counts = verify_backup(Path(args.verify))
            print("✅ Isolated restore and integrity checks passed: " + json.dumps(counts))
        else:
            print(f"✅ Verified backup: {create_backup(_db_path())}")
    except (OSError, ValueError, sqlite3.Error) as e:
        print(f"❌ Backup failed: {type(e).__name__}")
        return 1
    return 0


def cmd_test_notify(args: argparse.Namespace) -> int:
    from mailagent.agent.pipeline import primary_target
    from mailagent.models import OutMessage

    cfg = _load()
    if cfg is None:
        return 2
    target = primary_target(cfg)
    if target is None:
        print("No paired channel. Run mail-agent setup first.")
        return 1
    name, chat_id = target
    ch_cfg = cfg.telegram if name == "telegram" else cfg.lark
    msg = OutMessage(title="🔔 mail-agent test", text="This is a test message. If you can see it, notifications work.")
    try:
        asyncio.run(_with_channel(name, ch_cfg, lambda ch: ch.send(chat_id, msg)))
    except Exception as e:  # noqa: BLE001
        print(f"❌ Send failed: {_err(e)}")
        return 1
    print(f"✅ Sent to {name}")
    return 0


# ---- 邮箱管理 ----


def cmd_add_mailbox(args: argparse.Namespace) -> int:
    cfg = _load()
    if cfg is None:
        return 2
    try:
        address = (args.address or "").strip().lower()
        while "@" not in address:
            if address:
                print("  Please enter the full email address.")
            address = ask("Gmail address").strip().lower()
        existing = next((m for m in cfg.mailboxes if m.address.lower() == address), None)
        if existing:
            print(f"{address} is already configured (id {existing.id}); this will update its app password.")
        print(APP_PASSWORD_STEPS)
        while True:
            password = getpass.getpass("App password (spaces are fine): ").strip()
            if not password.replace(" ", ""):
                print("  Required.")
                continue
            mb = make_mailbox(address, password, cfg.mailboxes)
            if _verify_mailbox(mb):
                break
            if not yes("  Try another password?", True):
                print("Nothing was saved.")
                return 1
    except (KeyboardInterrupt, EOFError):
        print("\nQuit. Nothing was saved.")
        return 130
    upsert_mailbox(cfg, mb)
    path = save_config(cfg)
    print(f"✅ {'Updated' if existing else 'Added'} {mb.address} (id {mb.id}) in {path}")
    if not restart_service_if_installed():
        print("If mail-agent is running in the foreground, restart it to pick up the change.")
    if not existing:
        print("New mail in this mailbox is watched from now on. Older mail is not back-filled, "
              "but search in chat still covers its full history.")
    print(CHAT_MAILBOX_HINT)
    return 0


def cmd_remove_mailbox(args: argparse.Namespace) -> int:
    cfg = _load()
    if cfg is None:
        return 2
    key = args.mailbox.strip().lower()
    target = next((m for m in cfg.mailboxes if key in (m.id.lower(), m.address.lower())), None)
    if target is None:
        print(f"❌ No mailbox matches {args.mailbox}. Configured: "
              + (", ".join(f"{m.address} ({m.id})" for m in cfg.mailboxes) or "none"))
        return 1
    try:
        if not yes(f"Stop watching {target.address} (id {target.id})?", False):
            print("Cancelled.")
            return 0
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled.")
        return 130
    removed = remove_mailbox(cfg, key)
    assert removed is not None
    path = save_config(cfg)
    db_path = _db_path()
    if db_path.exists():
        from mailagent.db import Database

        db = Database(db_path)
        try:
            db.delete_mailbox_state(removed.id)
        finally:
            db.close()
    print(f"✅ Removed {removed.address} from {path}. Mail already stored stays in the local database.")
    if not cfg.mailboxes:
        print("⚠️ No mailboxes left. Add one with: mail-agent add-mailbox")
    if not restart_service_if_installed():
        print("If mail-agent is running in the foreground, restart it to pick up the change.")
    print(CHAT_MAILBOX_HINT)
    return 0


def cmd_mailboxes(args: argparse.Namespace) -> int:
    cfg = _load()
    if cfg is None:
        return 2
    if not cfg.mailboxes:
        print("No mailboxes configured. Add one with: mail-agent add-mailbox")
        return 0
    db = None
    if _db_path().exists():
        from mailagent.db import Database

        db = Database(_db_path())
    try:
        for m in cfg.mailboxes:
            line = f"  {m.address} (id {m.id}): "
            st = db.get_mailbox_state(m.id) if db else None
            if st is None:
                line += "never run"
            else:
                line += f"{st['status']}, last sync {st.get('last_sync_at') or '-'}"
                if st.get("error") and st["status"] != "ok":
                    line += f", error: {str(st['error'])[:200]}"
            print(line)
    finally:
        if db:
            db.close()
    print("Add: mail-agent add-mailbox    Remove: mail-agent remove-mailbox ADDRESS_OR_ID")
    return 0


def cmd_model(args: argparse.Namespace) -> int:
    """只改生成模型（setup 的第 4 步），保存并重启后台服务。"""
    cfg = _load()
    if cfg is None:
        return 2
    old = copy.deepcopy(cfg)
    try:
        _setup_llm(cfg, old, had_old=True, header="")
    except (KeyboardInterrupt, EOFError):
        print("\nQuit. Nothing was saved.")
        return 130
    if cfg.llm == old.llm:
        print("No change.")
        return 0
    path = save_config(cfg)
    print(f"✅ Saved the model setting in {path}")
    if not restart_service_if_installed():
        print("If mail-agent is running in the foreground, restart it to pick up the change.")
    return 0


def cmd_restart(args: argparse.Namespace) -> int:
    if restart_service_if_installed():
        return 0
    print("No background service is installed (mail-agent install-service sets one up). "
          "If mail-agent is running in the foreground, stop it and start it again.")
    return 1


def cmd_timezone(args: argparse.Namespace) -> int:
    from mailagent.agent.timeutil import find_timezone, fmt_local, utcnow

    cfg = _load()
    if cfg is None:
        return 2
    if not args.zone:
        print(f"Time zone: {cfg.prefs.timezone} (now {fmt_local(utcnow(), cfg.prefs.timezone)})")
        print('Change it: mail-agent timezone ZONE   (e.g. Asia/Shanghai, Europe/London, "New York")')
        return 0
    query = " ".join(args.zone)
    zone, close = find_timezone(query)
    if zone is None:
        print(f"❌ Unknown time zone: {query}" + (f". Did you mean: {', '.join(close)}?" if close else ""))
        return 1
    if zone == cfg.prefs.timezone:
        print(f"Already using {zone}.")
        return 0
    cfg.prefs.timezone = zone
    path = save_config(cfg)
    print(f"✅ Time zone set to {zone} (now {fmt_local(utcnow(), zone)}) in {path}")
    if not restart_service_if_installed():
        print("If mail-agent is running in the foreground, restart it to pick up the change "
              "(open to-dos move to the new zone when it starts).")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mail-agent", description="Always-on personal email agent")
    sub = parser.add_subparsers(dest="cmd", metavar="COMMAND")
    sub.add_parser("setup", help="interactive setup wizard")
    p_run = sub.add_parser("run", help="run in the foreground")
    p_run.add_argument("-v", "--verbose", action="store_true", help="print debug logs")
    sub.add_parser("status", help="show config (secrets hidden) and runtime status")
    sub.add_parser("doctor", help="check config and every connection")
    sub.add_parser("retry-analysis", help="retry exhausted analysis jobs from the last seven days")
    p_backup = sub.add_parser("backup", help="create a private, verified database snapshot (retains seven)")
    p_backup.add_argument("--verify", metavar="PATH", help="verify an existing backup by an isolated restore")
    p_add = sub.add_parser("add-mailbox", help="add a Gmail mailbox (or update its app password)")
    p_add.add_argument("address", nargs="?", metavar="ADDRESS", help="Gmail or Google Workspace address")
    p_rm = sub.add_parser("remove-mailbox", help="stop watching a mailbox")
    p_rm.add_argument("mailbox", metavar="ADDRESS_OR_ID", help="mailbox address or id")
    sub.add_parser("mailboxes", help="list mailboxes and their sync status")
    p_tz = sub.add_parser("timezone", help="show or change your time zone")
    p_tz.add_argument("zone", nargs="*", metavar="ZONE", help='IANA name or city, e.g. Asia/Shanghai or "New York"')
    sub.add_parser("model", help="choose the generative model (CLI subscription, Doubao, other APIs, your own URL)")
    sub.add_parser("restart", help="restart the background service (e.g. after editing config.toml)")
    sub.add_parser("install-service", help="install as a background service (starts at boot/login)")
    sub.add_parser("uninstall-service", help="stop and remove the background service")
    sub.add_parser("test-notify", help="send yourself a test message")
    args = parser.parse_args(argv)
    if not args.cmd:
        parser.print_help()
        return 0
    if args.cmd != "run":
        logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
        for name in ("httpx", "httpcore"):
            logging.getLogger(name).setLevel(logging.WARNING)
    handlers: dict[str, Callable[[argparse.Namespace], int]] = {
        "setup": cmd_setup,
        "run": cmd_run,
        "status": cmd_status,
        "doctor": cmd_doctor,
        "backup": cmd_backup,
        "retry-analysis": cmd_retry_analysis,
        "add-mailbox": cmd_add_mailbox,
        "remove-mailbox": cmd_remove_mailbox,
        "mailboxes": cmd_mailboxes,
        "timezone": cmd_timezone,
        "restart": cmd_restart,
        "model": cmd_model,
        "install-service": lambda a: install_service(),
        "uninstall-service": lambda a: uninstall_service(),
        "test-notify": cmd_test_notify,
    }
    try:
        return handlers[args.cmd](args)
    except KeyboardInterrupt:
        print()
        return 130
