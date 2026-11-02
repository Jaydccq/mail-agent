"""API 适配器对着假服务器（httpx.MockTransport）：OpenAI 兼容和 Anthropic 兼容，自定义网址、key、模型名。"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from mailagent.ai.llm.anthropic_api import AnthropicAPI
from mailagent.ai.llm.base import LLMError
from mailagent.ai.llm.openai_compat import OpenAICompat
from mailagent.config import LLMConfig

SCHEMA = {"type": "object", "properties": {"a": {"type": "integer"}}, "required": ["a"]}


def _openai(handler, **cfg) -> OpenAICompat:
    c = LLMConfig(provider="openai", base_url="https://llm.example/api/v3/", api_key="sk-test", model="m-1", **cfg)
    return OpenAICompat(c, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def _chat(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": content}}]})


def test_openai_custom_url_key_model_and_structured_fallback():
    seen: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        seen.append({"url": str(req.url), "auth": req.headers.get("authorization"), "body": body})
        fmt = (body.get("response_format") or {}).get("type")
        if fmt == "json_schema":
            return httpx.Response(400, json={"error": {"message": "json_schema not supported"}})
        if fmt == "json_object":
            return _chat('{"a": 1}')
        return _chat("plain answer")

    async def go():
        llm = _openai(handler)
        assert await llm.complete("sys", "hi", schema=SCHEMA) == {"a": 1}
        assert await llm.complete("sys", "again", schema=SCHEMA) == {"a": 1}  # 记住了降级，不再先试 json_schema
        assert await llm.complete("sys", "text please") == "plain answer"
        await llm.aclose()

    asyncio.run(go())
    assert all(s["url"] == "https://llm.example/api/v3/chat/completions" for s in seen)
    assert all(s["auth"] == "Bearer sk-test" and s["body"]["model"] == "m-1" for s in seen)
    assert [(s["body"].get("response_format") or {}).get("type") for s in seen] == \
        ["json_schema", "json_object", "json_object", None]
    assert seen[0]["body"]["messages"][0] == {"role": "system", "content": "sys"}


def test_openai_errors_are_classified():
    async def call(status: int) -> LLMError:
        llm = _openai(lambda req: httpx.Response(status, json={"error": {"message": "nope"}}))
        with pytest.raises(LLMError) as e:
            await llm.complete("", "hi")
        await llm.aclose()
        return e.value

    assert asyncio.run(call(401)).retryable is False
    assert asyncio.run(call(429)).retryable is True
    assert asyncio.run(call(503)).retryable is True


def test_anthropic_compatible_custom_url():
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"content": [{"type": "text", "text": 'Sure: {"a": 2}'}],
                                         "stop_reason": "end_turn"})

    async def go():
        cfg = LLMConfig(provider="anthropic", base_url="https://llm.example/api/coding/v1", api_key="k-1",
                        model="some-model")
        llm = AnthropicAPI(cfg, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        assert await llm.complete("sys", "hi", schema=SCHEMA) == {"a": 2}
        await llm.aclose()

    asyncio.run(go())
    req = seen[0]
    body = json.loads(req.content)
    assert str(req.url) == "https://llm.example/api/coding/v1/messages"
    assert req.headers["x-api-key"] == "k-1" and body["model"] == "some-model" and body["system"] == "sys"


def test_anthropic_auth_headers_official_vs_compatible():
    seen: dict[str, httpx.Request] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen[req.url.host] = req
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn"})

    async def go():
        for url in ("https://api.anthropic.com", "https://api.deepseek.com/anthropic"):
            llm = AnthropicAPI(LLMConfig(provider="anthropic", base_url=url, api_key="k", model="m"),
                               http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
            assert await llm.complete("", "hi") == "ok"
            await llm.aclose()

    asyncio.run(go())
    assert "authorization" not in seen["api.anthropic.com"].headers  # 官方只用 x-api-key
    assert str(seen["api.deepseek.com"].url) == "https://api.deepseek.com/anthropic/v1/messages"
    assert seen["api.deepseek.com"].headers["authorization"] == "Bearer k"
    assert seen["api.deepseek.com"].headers["x-api-key"] == "k"


def test_openai_strips_leading_think_block():
    async def go():
        llm = _openai(lambda req: _chat('<think>maybe {"a": 9}?</think>\n{"a": 3}'))
        assert await llm.complete("", "hi", schema=SCHEMA) == {"a": 3}
        await llm.aclose()

    asyncio.run(go())


def test_openai_400_about_the_input_does_not_downgrade_structured_output():
    formats: list[str | None] = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        formats.append((body.get("response_format") or {}).get("type"))
        if "too long" in body["messages"][-1]["content"]:
            return httpx.Response(400, json={"error": {"message": "This model's maximum context length is 8192"}})
        return _chat('{"a": 1}')

    async def go():
        llm = _openai(handler)
        with pytest.raises(LLMError) as e:
            await llm.complete("sys", "too long", schema=SCHEMA)
        assert e.value.retryable is False
        assert await llm.complete("sys", "fine", schema=SCHEMA) == {"a": 1}
        await llm.aclose()

    asyncio.run(go())
    assert formats == ["json_schema", "json_schema"]  # 第二封照样用 json_schema，没有被一封超长邮件降级


def test_openai_concurrent_fallbacks_step_down_one_mode_at_a_time():
    formats: list[str | None] = []

    async def handler(req: httpx.Request) -> httpx.Response:
        fmt = (json.loads(req.content).get("response_format") or {}).get("type")
        formats.append(fmt)
        await asyncio.sleep(0.01)  # 两个请求同时在路上
        if fmt == "json_schema":
            return httpx.Response(400, json={"error": {"message": "response_format json_schema is not supported"}})
        return _chat('{"a": 1}')

    async def go():
        llm = _openai(handler)
        assert await asyncio.gather(llm.complete("s", "x", schema=SCHEMA), llm.complete("s", "y", schema=SCHEMA)) == \
            [{"a": 1}, {"a": 1}]
        await llm.aclose()

    asyncio.run(go())
    assert "prompt" not in formats and None not in formats  # 两个都只降到 json_object，没有跳过它
