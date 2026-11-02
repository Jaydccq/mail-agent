"""生成模型接口。

所有供应商（订阅 CLI 和 API）只需实现一个单轮 complete()。
多轮对话、工具调用都由 agent 层把上下文拼进 prompt、用 JSON schema 约束输出来实现，
这样 claude/codex/gemini CLI 和各种 API 走同一套逻辑。
"""

from __future__ import annotations

from typing import Any, Protocol


class LLMError(Exception):
    """调用失败。retryable=True：超时/限流/5xx/CLI 临时错误；False：未登录、key 错、CLI 不存在、输出无法解析。"""

    def __init__(self, message: str, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


class LLM(Protocol):
    name: str  # 例如 "claude-cli"、"openai:gpt-x"，给日志和 status 看

    async def complete(self, system: str, prompt: str, schema: dict[str, Any] | None = None,
                       timeout: float | None = None) -> str | dict[str, Any]:
        """单轮生成。
        - schema=None：返回纯文本 str。
        - schema 给定（JSON Schema object）：返回解析好的 dict。优先用供应商原生结构化输出
          （claude --json-schema、codex --output-schema、OpenAI response_format json_schema），
          否则在 prompt 末尾要求只输出 JSON 并从回复里提取第一个 JSON 对象。解析失败抛 LLMError(retryable=False)。
        - 不允许模型使用任何工具（不读文件、不跑命令、不联网）：邮件内容不可信。
        """
        ...

    async def check(self) -> str:
        """发一个极小的请求验证可用，返回可读描述（例如 "claude-cli 2.1.309 ok"）。失败抛 LLMError。"""
        ...
