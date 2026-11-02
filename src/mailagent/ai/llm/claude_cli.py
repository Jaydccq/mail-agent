"""Claude Code CLI（`claude -p`），走用户本机已登录的订阅额度。

安全：邮件内容不可信，关闭全部工具（--tools ""）、不加载任何 MCP（--strict-mcp-config + 空配置）、
不读用户/项目设置（--setting-sources ""）、禁用 slash 命令、不落盘会话，cwd 是空临时目录。
不用 --bare：--bare 不读订阅登录状态。
"""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
from typing import Any

from mailagent.ai.llm._util import extract_json, looks_like_auth_error, resolve_command, run_cli, short
from mailagent.ai.llm.base import LLMError
from mailagent.config import LLMConfig

log = logging.getLogger(__name__)

ENV = ("CLAUDE_", "ANTHROPIC_")  # 这个 CLI 自己的环境变量（配置目录、API key），其他见 _util.cli_env

_HINT = "Install Claude Code (npm i -g @anthropic-ai/claude-code) and run `claude` once in a terminal to log in."


class ClaudeCLI:
    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg
        self.name = "claude-cli" + (f":{cfg.model}" if cfg.model else "")
        self._sem = asyncio.Semaphore(2)

    def _argv(self, exe: str, system: str, schema: dict[str, Any] | None) -> list[str]:
        argv = [
            exe, "-p",
            "--output-format", "json",
            "--tools", "",
            "--strict-mcp-config",
            "--mcp-config", '{"mcpServers":{}}',
            "--setting-sources", "",
            "--no-session-persistence",
            "--disable-slash-commands",
            "--system-prompt", system or "You are a helpful assistant.",
        ]
        if schema is not None:
            argv += ["--json-schema", json.dumps(schema, ensure_ascii=False)]
        if self.cfg.model:
            argv += ["--model", self.cfg.model]
        return argv

    async def complete(self, system: str, prompt: str, schema: dict[str, Any] | None = None,
                       timeout: float | None = None) -> str | dict[str, Any]:
        exe = resolve_command(self.cfg.command, "claude", _HINT)
        t = float(timeout or self.cfg.timeout or 180)
        async with self._sem:
            with tempfile.TemporaryDirectory(prefix="mailagent-claude-") as tmp:
                code, out, err = await run_cli(self._argv(exe, system, schema), prompt, timeout=t, cwd=tmp,
                                               env_prefixes=ENV)
        return self._parse(code, out, err, schema)

    def _parse(self, code: int, out: str, err: str, schema: dict[str, Any] | None) -> str | dict[str, Any]:
        data: dict[str, Any] | None = None
        try:
            parsed = json.loads(out) if out.strip() else None
            if isinstance(parsed, list):
                # ~/.claude.json 里 "verbose": true 时输出全部事件的数组（--setting-sources 管不到），取最后的 result
                parsed = next((e for e in reversed(parsed) if isinstance(e, dict) and e.get("type") == "result"), None)
            if isinstance(parsed, dict):
                data = parsed
        except ValueError:
            data = None
        if data is None:
            detail = short(err or out)
            log.warning("claude-cli output could not be parsed code=%s: %s", code, detail)
            if looks_like_auth_error(err or out):
                raise LLMError(f"claude CLI is not logged in or authentication failed: {detail}. "
                               "Run `claude` in a terminal to log in.", retryable=False)
            raise LLMError(f"claude CLI failed (exit code {code}): {detail}", retryable=code != 0)

        result = data.get("result")
        if data.get("is_error") or code != 0 or data.get("subtype") not in (None, "success"):
            detail = short(str(result or "") or err or str(data.get("subtype") or ""))
            if looks_like_auth_error(detail) or looks_like_auth_error(err):
                raise LLMError(f"claude CLI is not logged in or authentication failed: {detail}. "
                               "Run `claude` in a terminal to log in.", retryable=False)
            raise LLMError(f"claude CLI returned an error (exit code {code}): {detail}", retryable=True)

        if schema is None:
            if not isinstance(result, str):
                raise LLMError("claude CLI returned no text result", retryable=False)
            return result.strip()
        so = data.get("structured_output")
        if isinstance(so, dict):
            return so
        if isinstance(so, str):
            return extract_json(so)
        return extract_json(result if isinstance(result, str) else "")

    async def check(self) -> str:
        exe = resolve_command(self.cfg.command, "claude", _HINT)
        version = ""
        try:
            with tempfile.TemporaryDirectory(prefix="mailagent-claude-") as tmp:
                code, out, _ = await run_cli([exe, "--version"], "", timeout=30, cwd=tmp, env_prefixes=ENV)
            if code == 0:
                version = out.strip().split()[0] if out.strip() else ""
        except LLMError:
            pass
        reply = await self.complete("Reply with just the two letters ok.", "ok?", timeout=90)
        if not isinstance(reply, str) or not reply:
            raise LLMError("claude CLI returned an empty result", retryable=True)
        return f"claude-cli {version} ok".replace("  ", " ")
