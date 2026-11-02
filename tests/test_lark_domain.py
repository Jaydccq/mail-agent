"""Lark 国际版 / 飞书自动识别。token 接口两个域名都认（分不出来），只有长连接地址接口
（lark_oapi 的 /callback/ws/endpoint）在错的域名上返回 1000040351 Incorrect domain name。
下面的响应码是 2026-11-01 对真实飞书应用观察到的。"""

import asyncio
import json

import httpx
import pytest

from mailagent.channels import lark as lark_mod
from mailagent.channels.base import ChannelError

FEISHU, LARK = "open.feishu.cn", "open.larksuite.com"


def _fake(monkeypatch, codes: dict[str, int]) -> list[str]:
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/callback/ws/endpoint"
        body = json.loads(req.content)
        assert body["AppID"] == "cli_x" and body["AppSecret"] == "s"
        seen.append(req.url.host)
        code = codes[req.url.host]
        return httpx.Response(200, json={"code": code, "msg": "Incorrect domain name" if code == 1000040351 else "",
                                         "data": {"URL": "wss://x"} if code == 0 else {}})

    real = httpx.AsyncClient
    monkeypatch.setattr(lark_mod.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    return seen


@pytest.mark.parametrize("codes, want", [
    ({FEISHU: 0, LARK: 1000040351}, "feishu"),
    ({FEISHU: 1000040351, LARK: 0}, "lark"),
])
def test_detect_domain(monkeypatch, codes, want):
    _fake(monkeypatch, codes)
    assert asyncio.run(lark_mod.detect_domain("cli_x", "s")) == want


def test_detect_domain_wrong_secret(monkeypatch):
    _fake(monkeypatch, {FEISHU: 1000040345, LARK: 1000040345})
    with pytest.raises(ChannelError, match="App ID / App Secret") as e:
        asyncio.run(lark_mod.detect_domain("cli_x", "s"))
    assert not e.value.retryable and "1000040345" in str(e.value)


def _fake_api(monkeypatch, bot: dict) -> None:
    """飞书 token 接口 + 机器人信息接口（/open-apis/bot/v3/info）。"""

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.host == FEISHU
        if req.url.path == "/open-apis/auth/v3/tenant_access_token/internal":
            return httpx.Response(200, json={"code": 0, "msg": "ok", "tenant_access_token": "t-1"})
        assert req.url.path == "/open-apis/bot/v3/info" and req.headers["Authorization"] == "Bearer t-1"
        return httpx.Response(200, json=bot)

    real = httpx.AsyncClient
    monkeypatch.setattr(lark_mod.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))


def test_bot_name_when_published(monkeypatch):
    # 2026-11-01 真实已发布的飞书应用返回的形状
    _fake_api(monkeypatch, {"code": 0, "msg": "ok", "bot": {"activate_status": 2, "app_name": "Mail Agent"}})
    assert asyncio.run(lark_mod.bot_name("feishu", "cli_x", "s")) == "Mail Agent"


@pytest.mark.parametrize("bot, hint", [
    ({"code": 0, "msg": "ok", "bot": {"activate_status": 0, "app_name": "Mail Agent"}}, "Publish"),
    ({"code": 230002, "msg": "bot not found"}, "Add Features → Bot"),  # 码是假设的：任何非 0 都这样提示
])
def test_bot_name_explains_what_is_missing(monkeypatch, bot, hint):
    _fake_api(monkeypatch, bot)
    with pytest.raises(ChannelError, match=hint) as e:
        asyncio.run(lark_mod.bot_name("feishu", "cli_x", "s"))
    assert not e.value.retryable
