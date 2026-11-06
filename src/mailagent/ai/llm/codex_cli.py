"""OpenAI Codex CLI（`codex exec`），走用户本机已登录的 ChatGPT 订阅额度。

codex 没有 system prompt 参数：system 拼在 prompt 最前面，用分隔线隔开。prompt 从 stdin 传（参数 "-"）。
最终回复从 -o 文件读取，stdout 的进度输出忽略。

安全：邮件内容不可信。--sandbox read-only 只禁止写，模型仍可能跑只读命令读本机文件，
所以再用 --disable 关掉 shell / 浏览器 / 插件 / 多 agent 等工具类特性（名字取自 codex 0.164 的
`codex features list`；其他版本若不认识某个特性名可能直接报错，需要时调整 DISABLED_FEATURES）。
注意：用户 ~/.codex/config.toml 里配置的 MCP 服务器无法用参数完全关闭。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
from typing import Any

from mailagent.ai.llm._util import extract_json, looks_like_auth_error, resolve_command, run_cli, strict_schema
from mailagent.ai.llm.base import LLMError
from mailagent.config import LLMConfig

log = logging.getLogger(__name__)

ENV = ("CODEX_", "OPENAI_")  # 这个 CLI 自己的环境变量（配置目录、API key），其他见 _util.cli_env

_HINT = "Install the Codex CLI (npm i -g @openai/codex) and run `codex login` to log in."

DISABLED_FEATURES = (
    "shell_tool", "unified_exec", "apps", "plugins", "multi_agent", "browser_use",
    "computer_use", "in_app_browser", "view_image", "image_generation",
)

_SEP = "\n\n==========\n\n"


def failure_detail(output: str) -> str:
    """Classify the full diagnostic stream without logging echoed prompts, URLs or secrets."""
    statuses = re.findall(r"\bHTTP(?:/\d(?:\.\d)?)?\s+(?:error[: ]*|status[: =]*)?([45]\d\d)\b", output, re.I)
    if statuses:
        return f"HTTP {statuses[-1]}"
    lower = output.lower()
    if looks_like_auth_error(lower):
        return "authentication failed"
    if "timed out" in lower or "timeout" in lower:
        return "request timed out"
    if "websocket" in lower:
        return "WebSocket transport failed"
    if "connection" in lower or "connect" in lower:
        return "connection failed"
    return "CLI exited without a usable response"


class CodexCLI:
    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg
        self.name = "codex-cli" + (f":{cfg.model}" if cfg.model else "")
        self._sem = asyncio.Semaphore(2)

    async def complete(self, system: str, prompt: str, schema: dict[str, Any] | None = None,
                       timeout: float | None = None) -> str | dict[str, Any]:
        exe = resolve_command(self.cfg.command, "codex", _HINT)
        t = float(timeout or self.cfg.timeout or 180)
        text = f"{system.strip()}{_SEP}{prompt}" if system.strip() else prompt
        async with self._sem:
            with tempfile.TemporaryDirectory(prefix="mailagent-codex-") as tmp:
                work = os.path.join(tmp, "work")  # 模型看到的工作目录，保持为空
                os.mkdir(work)
                out_file = os.path.join(tmp, "last.txt")
                argv = [exe, "exec", "--skip-git-repo-check", "--ephemeral", "--sandbox", "read-only",
                        "--ignore-rules", "-C", work, "-c", 'web_search="disabled"']
                for f in DISABLED_FEATURES:
                    argv += ["--disable", f]
                if schema is not None:
                    schema_file = os.path.join(tmp, "schema.json")
                    with open(schema_file, "w", encoding="utf-8") as fh:
                        json.dump(strict_schema(schema), fh, ensure_ascii=False)
                    argv += ["--output-schema", schema_file]
                argv += ["-o", out_file]
                if self.cfg.model:
                    argv += ["-m", self.cfg.model]
                argv.append("-")
                code, out, err = await run_cli(argv, text, timeout=t, cwd=work, env_prefixes=ENV)
                try:
                    with open(out_file, encoding="utf-8", errors="replace") as fh:
                        result = fh.read().strip()
                except FileNotFoundError:
                    result = ""

        if code != 0 or not result:
            detail = failure_detail(err or out)
            log.warning("codex-cli failed code=%s: %s", code, detail)
            if detail == "authentication failed" or detail in ("HTTP 401", "HTTP 403"):
                raise LLMError(f"codex CLI is not logged in or authentication failed: {detail}. "
                               "Run `codex login`.", retryable=False)
            raise LLMError(f"codex CLI failed (exit code {code}): {detail or 'no output'}", retryable=True)
        if schema is None:
            return result
        return extract_json(result)

    async def check(self) -> str:
        exe = resolve_command(self.cfg.command, "codex", _HINT)
        version = ""
        try:
            with tempfile.TemporaryDirectory(prefix="mailagent-codex-") as tmp:
                code, out, _ = await run_cli([exe, "--version"], "", timeout=30, cwd=tmp, env_prefixes=ENV)
            if code == 0 and out.strip():
                version = out.strip().split()[-1]
        except LLMError:
            pass
        reply = await self.complete("Reply with just the two letters ok.", "ok?", timeout=120)
        if not reply:
            raise LLMError("codex CLI returned an empty result", retryable=True)
        return f"codex-cli {version} ok".replace("  ", " ")
