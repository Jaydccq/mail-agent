"""API 预设：常见服务商的接口地址和默认模型。setup 里都能改，也可以完全自己填网址。

只预填官方文档里确认过、或长期有效的模型名；拿不准的留空，给一个"去哪查"的提示，
setup 的验证请求会把服务端的报错（比如模型名不对）原样告诉用户。

豆包：豆包 App 会员没有 API；火山方舟 Coding Plan / Agent Plan 的条款不允许在 AI 编程工具以外调用
（可能停用订阅或封号），所以这里只预设按量付费的方舟 API。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ApiPreset:
    key: str
    label: str
    provider: str  # openai（OpenAI 兼容）| anthropic（Anthropic 兼容）
    base_url: str
    model: str = ""  # 预填的模型名；空 = 用户自己填
    model_hint: str = ""
    key_url: str = ""  # 去哪拿 API key
    key_required: bool = True


DOUBAO = ApiPreset(
    "doubao", "Doubao API (Volcengine Ark, pay as you go)", "openai",
    "https://ark.cn-beijing.volces.com/api/v3", "doubao-seed-2-1-pro-260915",
    "copy the exact model ID from the Ark console's model list; IDs carry a date (e.g. doubao-seed-2-1-pro-260915)",
    "https://console.volcengine.com/ark (API Key 管理)")
DOUBAO_INTL = ApiPreset(
    "doubao-intl", "Doubao / Seed API, international (BytePlus ModelArk)", "openai",
    "https://ark.ap-southeast.bytepluses.com/api/v3", "",
    "copy the model ID from the ModelArk console's model list",
    "https://console.byteplus.com/ark (API Keys)")

OTHER_PRESETS: tuple[ApiPreset, ...] = (
    ApiPreset("deepseek", "DeepSeek", "openai", "https://api.deepseek.com", "",
              "see https://api-docs.deepseek.com for current model names", "https://platform.deepseek.com/api_keys"),
    ApiPreset("kimi", "Kimi (Moonshot)", "openai", "https://api.moonshot.cn/v1", "",
              "e.g. kimi-k3.1-pro; international accounts use https://api.moonshot.ai/v1",
              "https://platform.moonshot.cn/console/api-keys"),
    ApiPreset("qwen", "Qwen (Alibaba Cloud Model Studio)", "openai",
              "https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus",
              "international accounts use https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
              "https://help.aliyun.com/zh/model-studio/get-api-key"),
    ApiPreset("glm", "GLM (Zhipu)", "openai", "https://open.bigmodel.cn/api/paas/v4", "glm-5.3",
              "z.ai accounts use https://api.z.ai/api/paas/v4", "https://open.bigmodel.cn/usercenter/apikeys"),
    ApiPreset("openai", "OpenAI", "openai", "https://api.openai.com/v1", "",
              "see https://platform.openai.com/docs/models", "https://platform.openai.com/api-keys"),
    ApiPreset("anthropic", "Anthropic (Claude API)", "anthropic", "https://api.anthropic.com", "claude-sonnet-5-5",
              "", "https://console.anthropic.com/settings/keys"),
    ApiPreset("openrouter", "OpenRouter", "openai", "https://openrouter.ai/api/v1", "",
              "e.g. anthropic/claude-sonnet-5.5; see https://openrouter.ai/models", "https://openrouter.ai/keys"),
    ApiPreset("ollama", "Ollama (local models, no key)", "openai", "http://localhost:11434/v1", "",
              "a model you have pulled (see: ollama list)", "", key_required=False),
)

PRESETS: tuple[ApiPreset, ...] = (DOUBAO, DOUBAO_INTL, *OTHER_PRESETS)


def custom_preset(provider: str) -> ApiPreset:
    label = "Anthropic-compatible API" if provider == "anthropic" else "OpenAI-compatible API"
    return ApiPreset("custom", label, provider, "", "", "", "", key_required=False)


def norm_url(url: str) -> str:
    return (url or "").strip().rstrip("/").lower()


def preset_for(provider: str, base_url: str) -> ApiPreset | None:
    """已保存的配置对应哪个预设（按协议 + 网址认）；认不出返回 None（= 自己填的网址）。"""
    u = norm_url(base_url)
    return next((p for p in PRESETS if p.provider == provider and norm_url(p.base_url) == u), None)
