"""各 provider 共用的小工具：JSON 提取、子进程调用、HTTP 错误归类。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import signal
from typing import Any

from mailagent.ai.llm.base import LLMError

log = logging.getLogger(__name__)

_FENCE = re.compile(r"```(?:json|JSON)?[ \t]*\n?(.*?)```", re.S)

# 传给 CLI 子进程的环境变量白名单。CLI 处理的是不可信的邮件内容，shell 里别的密钥（GITHUB_TOKEN、AWS_* 等）
# 不该被它看到。登录状态在 HOME 下的文件或 macOS 钥匙串里，不靠环境变量；各 CLI 自己的变量
# （CODEX_HOME、ANTHROPIC_API_KEY 等）由调用方用 env_prefixes 放行。
ENV_KEEP = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "LANG", "TZ",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS",
})
ENV_KEEP_PREFIXES = ("LC_", "XDG_")
MAX_OUTPUT = 4 * 1024 * 1024  # stdout、stderr 各自最多读这么多字节；正常回复只有几 KB，超了说明 CLI 失控


def extract_json(text: str) -> dict[str, Any]:
    """从模型回复里取出第一个 JSON 对象。

    处理 ```json 代码块、前后废话；从每个 "{" 位置用 raw_decode 尝试，
    这样字符串里的花括号不会干扰配平。失败抛 LLMError(retryable=False)。
    """
    if not isinstance(text, str) or not text.strip():
        raise LLMError("the model returned no content", retryable=False)
    candidates = [m.group(1) for m in _FENCE.finditer(text)] + [text]
    decoder = json.JSONDecoder()
    for cand in candidates:
        s = cand.strip()
        i = s.find("{")
        while i != -1:
            try:
                obj, _ = decoder.raw_decode(s, i)
            except json.JSONDecodeError:
                obj = None
            if isinstance(obj, dict):
                return obj
            i = s.find("{", i + 1)
    raise LLMError("could not parse JSON from the model reply", retryable=False)


def schema_instruction(schema: dict[str, Any]) -> str:
    """没有原生结构化输出时，追加在 prompt 末尾的格式要求。"""
    return (
        "\n\nOutput a single JSON object only, with no other text, explanation or code fences. "
        "The JSON must conform to this JSON Schema:\n" + json.dumps(schema, ensure_ascii=False)
    )


def strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """转换成 OpenAI strict structured outputs 能接受的形式（返回新对象，不改原 schema）。

    每个 object：additionalProperties=false，所有 properties 都放进 required；
    原来不是必填的字段改成可空（type 里加 "null"）。已经是 strict 形式的 schema 原样不变。
    """

    def conv(node: Any) -> Any:
        if isinstance(node, list):
            return [conv(x) for x in node]
        if not isinstance(node, dict):
            return node
        out = {k: conv(v) for k, v in node.items()}
        props = out.get("properties")
        if out.get("type") == "object" or isinstance(props, dict):
            props = props if isinstance(props, dict) else {}
            required = set(out.get("required") or [])
            for k, sub in props.items():
                if k not in required and isinstance(sub, dict):
                    t = sub.get("type")
                    if isinstance(t, str) and t != "null":
                        sub["type"] = [t, "null"]
                    elif isinstance(t, list) and "null" not in t:
                        sub["type"] = [*t, "null"]
                    if isinstance(sub.get("enum"), list) and None not in sub["enum"]:
                        sub["enum"] = [*sub["enum"], None]
            out["properties"] = props
            out["required"] = list(props.keys())
            out["additionalProperties"] = False
        return out

    return conv(schema)


def short(s: str | bytes, n: int = 300) -> str:
    """压成一行并截短，给日志/异常用。"""
    if isinstance(s, bytes):
        s = s.decode("utf-8", errors="replace")
    s = " ".join(s.split())
    return s if len(s) <= n else s[:n] + "…"


def looks_like_auth_error(text: str) -> bool:
    t = text.lower()
    return any(k in t for k in ("login", "log in", "logged in", "auth", "unauthorized", "credential",
                                "api key", "api_key", "sign in"))


def resolve_command(command: str, default: str, hint: str) -> str:
    """cfg.command 优先，否则在 PATH 里找；找不到抛 LLMError(retryable=False) 并附上安装/登录提示。"""
    name = command.strip() or default
    path = shutil.which(os.path.expanduser(name))
    if not path:
        raise LLMError(f"{name} not found. {hint}", retryable=False)
    return path


async def _kill(proc: asyncio.subprocess.Process) -> None:
    """杀掉整个进程组：codex 等 CLI 是 Node 外壳再起原生进程，SIGKILL 只发给外壳的话原生进程会留下来继续跑。"""
    try:
        os.killpg(proc.pid, signal.SIGKILL)  # start_new_session=True：进程组 id 就是子进程 pid
    except ProcessLookupError:
        pass
    await proc.wait()


def cli_env(prefixes: tuple[str, ...] = ()) -> dict[str, str]:
    """os.environ 里白名单内的变量，加上以 prefixes 开头的。"""
    keep = ENV_KEEP_PREFIXES + prefixes
    return {k: v for k, v in os.environ.items() if k in ENV_KEEP or k.startswith(keep)}


async def _feed(writer: asyncio.StreamWriter, data: bytes) -> None:
    try:
        writer.write(data)
        await writer.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass  # CLI 没读完 stdin 就退出了：照常看它的退出码和输出
    finally:
        writer.close()


async def _read_capped(reader: asyncio.StreamReader, name: str) -> bytes:
    buf = bytearray()
    while chunk := await reader.read(65536):
        buf += chunk
        if len(buf) > MAX_OUTPUT:
            raise LLMError(f"{name} printed more than {MAX_OUTPUT} bytes; stopped it", retryable=False)
    return bytes(buf)


async def run_cli(argv: list[str], stdin: str, *, timeout: float, cwd: str,
                  env_prefixes: tuple[str, ...] = ()) -> tuple[int, str, str]:
    """跑一次 CLI 子进程（不经过 shell），stdin 传入文本；超时 kill 并抛 retryable LLMError，
    输出超过 MAX_OUTPUT 也 kill 并抛 LLMError。

    返回 (退出码, stdout, stderr)。环境变量见 cli_env：只传白名单和 env_prefixes 开头的。
    """
    name = os.path.basename(argv[0])
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=cli_env(env_prefixes),
            start_new_session=True,  # 自成一个进程组，超时/取消时 _kill 连它起的子进程一起杀
        )
    except FileNotFoundError as e:
        raise LLMError(f"executable not found: {argv[0]}", retryable=False) from e
    except PermissionError as e:
        raise LLMError(f"cannot execute {argv[0]} (permission denied)", retryable=False) from e
    sin, sout, serr = proc.stdin, proc.stdout, proc.stderr
    assert sin is not None and sout is not None and serr is not None

    async def talk() -> tuple[bytes, bytes]:
        _, out, err = await asyncio.gather(_feed(sin, stdin.encode("utf-8")),
                                           _read_capped(sout, name), _read_capped(serr, name))
        await proc.wait()
        return out, err

    try:
        out, err = await asyncio.wait_for(talk(), timeout)
    except (asyncio.TimeoutError, TimeoutError):
        await _kill(proc)
        raise LLMError(f"{name} timed out ({timeout:.0f}s)", retryable=True) from None
    except BaseException:  # 输出超限，或者被取消
        await _kill(proc)
        raise
    return (proc.returncode or 0), out.decode("utf-8", errors="replace"), err.decode("utf-8", errors="replace")


def http_error(provider: str, status: int, body: str) -> LLMError:
    """把 HTTP 错误码归类成 LLMError。只取响应里的 error.message 一小段，不记录整个 body。"""
    msg = ""
    try:
        data = json.loads(body)
        e = data.get("error") if isinstance(data, dict) else None
        if isinstance(e, dict):
            msg = str(e.get("message") or e.get("type") or "")
        elif isinstance(e, str):
            msg = e
    except (ValueError, AttributeError):
        pass
    msg = short(msg, 200)
    tail = f" {msg}" if msg else ""
    if status in (401, 403):
        return LLMError(f"{provider} authentication failed (HTTP {status}); check api_key.{tail}", retryable=False)
    if status in (408, 409, 429) or status >= 500:
        return LLMError(f"{provider} temporarily unavailable (HTTP {status}).{tail}", retryable=True)
    return LLMError(f"{provider} request rejected (HTTP {status}).{tail}", retryable=False)
