"""生成模型：订阅 CLI（claude/codex/gemini）和 API（OpenAI 兼容 / Anthropic）。"""

from __future__ import annotations

import shutil

from mailagent.ai.llm.base import LLM, LLMError
from mailagent.config import LLMConfig

__all__ = ["LLM", "LLMError", "make_llm", "detect_clis"]

CLI_NAMES = ("claude", "codex", "gemini")


def detect_clis() -> dict[str, str]:
    """PATH 里能找到的官方 CLI：{"claude": "/path/to/claude", ...}。只查找，不运行。"""
    found: dict[str, str] = {}
    for name in CLI_NAMES:
        p = shutil.which(name)
        if p:
            found[name] = p
    return found


def make_llm(cfg: LLMConfig) -> LLM | None:
    """按配置创建生成模型；provider="none" 返回 None。"""
    p = cfg.provider
    if p == "none" or not p:
        return None
    if p == "claude-cli":
        from mailagent.ai.llm.claude_cli import ClaudeCLI
        return ClaudeCLI(cfg)
    if p == "codex-cli":
        from mailagent.ai.llm.codex_cli import CodexCLI
        return CodexCLI(cfg)
    if p == "gemini-cli":
        from mailagent.ai.llm.gemini_cli import GeminiCLI
        return GeminiCLI(cfg)
    if p == "openai":
        from mailagent.ai.llm.openai_compat import OpenAICompat
        return OpenAICompat(cfg)
    if p == "anthropic":
        from mailagent.ai.llm.anthropic_api import AnthropicAPI
        return AnthropicAPI(cfg)
    raise ValueError(f"unknown llm.provider: {p}")
