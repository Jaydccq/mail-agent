"""Anthropic Messages API：POST {base_url}/v1/messages（x-api-key、anthropic-version: 2023-06-01）。
也用于第三方 Anthropic 兼容接口（自己填 base_url）；这时 key 同时放在 Authorization: Bearer 里。

结构化输出：提示词要求只输出 JSON，再从回复里提取第一个 JSON 对象。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from mailagent.ai.llm._util import extract_json, http_error, schema_instruction
from mailagent.ai.llm.base import LLMError
from mailagent.config import LLMConfig

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.anthropic.com"
DEFAULT_MODEL = "claude-sonnet-5-5"
MAX_TOKENS = 4096


class AnthropicAPI:
    def __init__(self, cfg: LLMConfig, http: httpx.AsyncClient | None = None):
        self.cfg = cfg
        base = (cfg.base_url or DEFAULT_BASE_URL).rstrip("/")
        if base.endswith("/v1"):  # 兼容用户把 /v1 也写进 base_url
            base = base[:-3]
        self.base_url = base
        self.official = httpx.URL(base).host == "api.anthropic.com"
        self.model = cfg.model or DEFAULT_MODEL
        self.name = f"anthropic:{self.model}"
        self._sem = asyncio.Semaphore(4)
        self._own_http = http is None
        self._http = http or httpx.AsyncClient()

    async def complete(self, system: str, prompt: str, schema: dict[str, Any] | None = None,
                       timeout: float | None = None) -> str | dict[str, Any]:
        if not self.cfg.api_key:
            raise LLMError("the anthropic provider requires llm.api_key", retryable=False)
        t = float(timeout or self.cfg.timeout or 180)
        user = prompt + schema_instruction(schema) if schema is not None else prompt
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": MAX_TOKENS,
            "messages": [{"role": "user", "content": user}],
        }
        if system:
            body["system"] = system
        headers = {"x-api-key": self.cfg.api_key, "anthropic-version": "2023-06-01",
                   "content-type": "application/json"}
        if not self.official:
            # 第三方 Anthropic 兼容接口多数按 Claude Code 的 ANTHROPIC_AUTH_TOKEN 收 key，也就是 Bearer
            headers["authorization"] = f"Bearer {self.cfg.api_key}"
        async with self._sem:
            try:
                r = await self._http.post(f"{self.base_url}/v1/messages", json=body, headers=headers, timeout=t)
            except httpx.TimeoutException as e:
                raise LLMError(f"{self.name} request timed out", retryable=True) from e
            except httpx.HTTPError as e:
                raise LLMError(f"{self.name} network error: {type(e).__name__}", retryable=True) from e
        if r.status_code != 200:
            # 529 = overloaded，也算 5xx
            err = http_error(self.name, r.status_code, r.text)
            log.warning("%s request failed: %s", self.name, err)
            raise err
        try:
            data = r.json()
        except ValueError:
            raise LLMError(f"{self.name} returned a malformed response", retryable=True) from None
        if data.get("stop_reason") == "refusal":
            raise LLMError(f"{self.name} refused to answer", retryable=False)
        blocks = data.get("content") or []
        text = "".join(b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text").strip()
        if not text:
            raise LLMError(f"{self.name} returned empty content", retryable=True)
        if data.get("stop_reason") == "max_tokens":
            log.warning("%s output was truncated at max_tokens", self.name)
        if schema is None:
            return text
        return extract_json(text)

    async def check(self) -> str:
        reply = await self.complete("Reply with just the two letters ok.", "ok?", timeout=60)
        if not reply:
            raise LLMError(f"{self.name} returned an empty result", retryable=True)
        return f"{self.name} ok"

    async def aclose(self) -> None:
        if self._own_http:
            await self._http.aclose()
