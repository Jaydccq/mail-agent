"""任意 OpenAI 兼容接口：POST {base_url}/chat/completions。

base_url 例：https://api.openai.com/v1、https://ark.cn-beijing.volces.com/api/v3（豆包/火山方舟）、
https://api.deepseek.com、https://openrouter.ai/api/v1、http://localhost:11434/v1（Ollama）；常用的见 presets.py。
结构化输出：先用 response_format=json_schema(strict)；服务端返回说不支持它的 400 时退回 json_object + 提示词，
再不行就只靠提示词。降级结果按实例记住，避免每次都多打一次 400。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

import httpx

from mailagent.ai.llm._util import extract_json, http_error, schema_instruction, strict_schema
from mailagent.ai.llm.base import LLMError
from mailagent.config import LLMConfig

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.openai.com/v1"

_THINK = re.compile(r"^\s*<think>.*?</think>", re.S)
# 400 里说的是结构化输出不支持，才降级；上下文超长、内容审核之类的 400 是这一封邮件的问题，不能让之后的请求都降级
_FORMAT_ERROR = re.compile(r"response_format|json_schema|json_object|structured output", re.I)

# 结构化输出的几种方式，按优先级
_MODES = ("json_schema", "json_object", "prompt")


class OpenAICompat:
    def __init__(self, cfg: LLMConfig, http: httpx.AsyncClient | None = None):
        self.cfg = cfg
        self.base_url = (cfg.base_url or DEFAULT_BASE_URL).rstrip("/")
        self.name = f"openai:{cfg.model or '?'}"
        self._sem = asyncio.Semaphore(4)
        self._own_http = http is None
        self._http = http or httpx.AsyncClient()
        self._mode_idx = 0  # 当前可用的结构化输出方式

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.cfg.api_key:
            h["Authorization"] = f"Bearer {self.cfg.api_key}"
        return h

    async def _post(self, body: dict[str, Any], timeout: float) -> tuple[int, dict[str, Any] | None, str]:
        try:
            r = await self._http.post(f"{self.base_url}/chat/completions", json=body,
                                      headers=self._headers(), timeout=timeout)
        except httpx.TimeoutException as e:
            raise LLMError(f"{self.name} request timed out", retryable=True) from e
        except httpx.HTTPError as e:
            raise LLMError(f"{self.name} network error: {type(e).__name__}", retryable=True) from e
        try:
            data = r.json()
        except ValueError:
            data = None
        return r.status_code, data if isinstance(data, dict) else None, r.text

    async def complete(self, system: str, prompt: str, schema: dict[str, Any] | None = None,
                       timeout: float | None = None) -> str | dict[str, Any]:
        if not self.cfg.model:
            raise LLMError("the openai provider requires llm.model", retryable=False)
        t = float(timeout or self.cfg.timeout or 180)
        async with self._sem:
            if schema is None:
                return await self._call(system, prompt, None, t)
            idx = self._mode_idx
            while True:
                try:
                    return await self._call(system, prompt, (_MODES[idx], schema), t)
                except _Unsupported:
                    if idx >= len(_MODES) - 1:
                        raise LLMError(f"{self.name} does not accept structured output requests",
                                       retryable=False) from None
                    idx += 1
                    # 并发的请求各自从自己的起点往下走一档；共享的只记最低已证实可用的那档，不会一次跳两档
                    if self._mode_idx < idx:
                        self._mode_idx = idx
                        log.info("%s does not support response_format=%s, switching to %s",
                                 self.name, _MODES[idx - 1], _MODES[idx])

    async def _call(self, system: str, prompt: str, structured: tuple[str, dict[str, Any]] | None,
                    timeout: float) -> str | dict[str, Any]:
        user = prompt
        body: dict[str, Any] = {"model": self.cfg.model}
        if structured:
            mode, schema = structured
            if mode == "json_schema":
                body["response_format"] = {"type": "json_schema", "json_schema": {
                    "name": "out", "schema": strict_schema(schema), "strict": True}}
            else:
                user = prompt + schema_instruction(schema)
                if mode == "json_object":
                    body["response_format"] = {"type": "json_object"}
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user})
        body["messages"] = messages

        status, data, text = await self._post(body, timeout)
        if status == 400 and structured and structured[0] != "prompt" and _FORMAT_ERROR.search(text):
            raise _Unsupported()
        if status != 200 or data is None:
            err = http_error(self.name, status, text)
            log.warning("%s request failed: %s", self.name, err)
            raise err
        try:
            choice = data["choices"][0]
            content = choice["message"].get("content")
        except (KeyError, IndexError, TypeError, AttributeError):
            raise LLMError(f"{self.name} returned a malformed response", retryable=True) from None
        if isinstance(content, list):  # 少数兼容实现返回分段内容
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        if isinstance(content, str):  # 推理模型（如 Ollama 上的 qwen3）把思考过程放在 <think>…</think> 里
            content = _THINK.sub("", content, count=1)
        if not isinstance(content, str) or not content.strip():
            refusal = choice.get("message", {}).get("refusal") if isinstance(choice, dict) else None
            if refusal:
                raise LLMError(f"{self.name} refused to answer", retryable=False)
            raise LLMError(f"{self.name} returned empty content", retryable=True)
        if structured is None:
            return content.strip()
        if structured[0] == "json_schema":
            try:
                obj = json.loads(content)
                if isinstance(obj, dict):
                    return obj
            except ValueError:
                pass
        return extract_json(content)

    async def check(self) -> str:
        reply = await self.complete("Reply with just the two letters ok.", "ok?", timeout=60)
        if not reply:
            raise LLMError(f"{self.name} returned an empty result", retryable=True)
        return f"{self.name} ok ({self.base_url})"

    async def aclose(self) -> None:
        if self._own_http:
            await self._http.aclose()


class _Unsupported(Exception):
    """服务端拒绝当前 response_format（HTTP 400），换下一种方式。"""
