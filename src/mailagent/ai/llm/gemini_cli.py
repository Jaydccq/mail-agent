"""Google Gemini CLI（`gemini -p`），走用户本机已登录的 Google 账号额度。

【未实测】本机没装 gemini，按官方文档实现：
- `gemini -p <指令> --output-format json [-m <m>]`，-p 的内容会追加在 stdin 正文之后；
- stdout 是一个 JSON：{"response": "<文本>", "stats": {...}, "error": {...}?}。
gemini 没有 system prompt 参数：system 拼在 stdin 正文最前面。

安全：无法用命令行参数可靠地关闭全部工具。这里做了尽力而为的限制：cwd 是空临时目录，
并在其中写 .gemini/settings.json 排除内置工具、清空 MCP（非受信目录下 gemini 可能忽略工作区设置）。
非交互模式下需要确认的工具会被拒绝，但只读工具（读文件、搜索、联网）不保证被禁用。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from typing import Any

from mailagent.ai.llm._util import extract_json, looks_like_auth_error, resolve_command, run_cli, schema_instruction, short
from mailagent.ai.llm.base import LLMError
from mailagent.config import LLMConfig

log = logging.getLogger(__name__)

ENV = ("GEMINI_", "GOOGLE_")  # 这个 CLI 自己的环境变量（配置目录、API key），其他见 _util.cli_env

_HINT = "Install the Gemini CLI (npm i -g @google/gemini-cli) and run `gemini` once in a terminal to log in."

_EXCLUDE_TOOLS = [
    "run_shell_command", "write_file", "replace", "read_file", "read_many_files", "glob",
    "search_file_content", "list_directory", "web_fetch", "google_web_search", "save_memory", "write_todos",
]
_SETTINGS = {"tools": {"exclude": _EXCLUDE_TOOLS}, "excludeTools": _EXCLUDE_TOOLS, "mcpServers": {}}

_INSTRUCTION = "Answer as instructed above. Output only the answer itself and do not use any tools."
_SEP = "\n\n==========\n\n"


class GeminiCLI:
    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg
        self.name = "gemini-cli" + (f":{cfg.model}" if cfg.model else "")
        self._sem = asyncio.Semaphore(2)

    async def complete(self, system: str, prompt: str, schema: dict[str, Any] | None = None,
                       timeout: float | None = None) -> str | dict[str, Any]:
        exe = resolve_command(self.cfg.command, "gemini", _HINT)
        t = float(timeout or self.cfg.timeout or 180)
        body = f"{system.strip()}{_SEP}{prompt}" if system.strip() else prompt
        if schema is not None:
            body += schema_instruction(schema)
        argv = [exe, "-p", _INSTRUCTION, "--output-format", "json", "-e", "none"]
        if self.cfg.model:
            argv += ["-m", self.cfg.model]
        async with self._sem:
            with tempfile.TemporaryDirectory(prefix="mailagent-gemini-") as tmp:
                os.mkdir(os.path.join(tmp, ".gemini"))
                with open(os.path.join(tmp, ".gemini", "settings.json"), "w", encoding="utf-8") as fh:
                    json.dump(_SETTINGS, fh)
                code, out, err = await run_cli(argv, body, timeout=t, cwd=tmp, env_prefixes=ENV)

        data: dict[str, Any] | None = None
        try:
            parsed = json.loads(out) if out.strip() else None
            if isinstance(parsed, dict):
                data = parsed
        except ValueError:
            data = None
        error = data.get("error") if data else None
        text = data.get("response") if data else None
        if code != 0 or error or not isinstance(text, str) or not text.strip():
            if isinstance(error, dict):
                detail = short(str(error.get("message") or error))
            else:
                detail = short(err or out)
            log.warning("gemini-cli failed code=%s: %s", code, detail)
            if looks_like_auth_error(detail):
                raise LLMError(f"gemini CLI is not logged in or authentication failed: {detail}. "
                               "Run `gemini` in a terminal to log in.", retryable=False)
            raise LLMError(f"gemini CLI failed (exit code {code}): {detail or 'no output'}", retryable=True)
        if schema is None:
            return text.strip()
        return extract_json(text)

    async def check(self) -> str:
        exe = resolve_command(self.cfg.command, "gemini", _HINT)
        version = ""
        try:
            with tempfile.TemporaryDirectory(prefix="mailagent-gemini-") as tmp:
                code, out, _ = await run_cli([exe, "--version"], "", timeout=30, cwd=tmp, env_prefixes=ENV)
            if code == 0 and out.strip():
                version = out.strip().split()[-1]
        except LLMError:
            pass
        reply = await self.complete("Reply with just the two letters ok.", "ok?", timeout=120)
        if not reply:
            raise LLMError("gemini CLI returned an empty result", retryable=True)
        return f"gemini-cli {version} ok".replace("  ", " ")
