"""订阅 CLI 输出解析（不运行真 CLI），以及子进程超时处理。"""

import asyncio
import json
import os
import sys
import time

import pytest

from mailagent.ai.llm import _util
from mailagent.ai.llm._util import run_cli
from mailagent.ai.llm.base import LLMError
from mailagent.ai.llm.claude_cli import ClaudeCLI
from mailagent.config import LLMConfig

RESULT = {"type": "result", "subtype": "success", "is_error": False, "result": '{"reply":"hi"}',
          "structured_output": {"reply": "hi"}}


OBJECT_SCHEMA = {"type": "object"}


def _parse(out: object, schema: dict | None = OBJECT_SCHEMA, code: int = 0):
    return ClaudeCLI(LLMConfig(provider="claude-cli"))._parse(code, json.dumps(out), "", schema)


def test_claude_single_result_object():
    assert _parse(RESULT) == {"reply": "hi"}
    assert _parse({**RESULT, "result": " ok "}, schema=None) == "ok"


def test_claude_verbose_event_array():
    """~/.claude.json 里 "verbose": true 时，-p --output-format json 输出全部事件的数组（--setting-sources 管不到）。"""
    events = [{"type": "system", "subtype": "init"}, {"type": "assistant", "message": {}},
              {"type": "rate_limit_event"}, RESULT]
    assert _parse(events) == {"reply": "hi"}
    assert _parse(events[:-1] + [{**RESULT, "result": "ok"}], schema=None) == "ok"


def test_claude_error_result_in_array():
    events = [{"type": "system", "subtype": "init"},
              {"type": "result", "subtype": "success", "is_error": True, "result": "Invalid API key · Please run /login"}]
    with pytest.raises(LLMError, match="not logged in"):
        _parse(events, code=1)


def test_claude_array_without_result_is_an_error():
    with pytest.raises(LLMError, match="could not be parsed|failed"):
        _parse([{"type": "system", "subtype": "init"}], code=0)


def test_timeout_kills_the_whole_process_group(tmp_path):
    # codex 是 Node 外壳再起原生进程；只杀外壳的话，原生进程会继续跑、继续耗额度
    pidfile = tmp_path / "child.pid"
    script = tmp_path / "wrapper.sh"
    script.write_text('sleep 30 &\necho $! > "$1"\nwait\n')
    with pytest.raises(LLMError):
        asyncio.run(run_cli(["/bin/sh", str(script), str(pidfile)], "", timeout=1, cwd=str(tmp_path)))
    child = int(pidfile.read_text())
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    os.kill(child, 9)
    pytest.fail("the wrapper's child process survived the timeout")


def test_cli_gets_only_allowlisted_env(tmp_path, monkeypatch):
    # CLI 处理不可信的邮件内容：shell 里别的密钥不能传给它；它自己的变量（配置目录、API key）要传
    monkeypatch.setenv("GITHUB_TOKEN", "s3cret")
    monkeypatch.setenv("CODEX_HOME", "/somewhere")
    code, out, _ = asyncio.run(run_cli(["/usr/bin/env"], "", timeout=10, cwd=str(tmp_path),
                                       env_prefixes=("CODEX_",)))
    names = {line.split("=", 1)[0] for line in out.splitlines()}
    assert code == 0
    assert "GITHUB_TOKEN" not in names
    assert {"CODEX_HOME", "PATH", "HOME"} <= names


def test_runaway_output_is_cut_off(tmp_path, monkeypatch):
    # 以前 communicate() 把全部输出读进内存，失控的 CLI 能一直写到超时
    monkeypatch.setattr(_util, "MAX_OUTPUT", 1000)
    script = "import sys, time; sys.stdout.write('x' * 5000); sys.stdout.flush(); time.sleep(30)"
    started = time.monotonic()
    with pytest.raises(LLMError, match="printed more than 1000 bytes"):
        asyncio.run(run_cli([sys.executable, "-c", script], "", timeout=20, cwd=str(tmp_path)))
    assert time.monotonic() - started < 10


def test_large_stdin_and_output_still_work(tmp_path):
    # 不用 communicate() 之后，stdin 和 stdout 要同时进行，否则两边管道都满了会互相等
    data = "y" * 300_000
    code, out, _ = asyncio.run(run_cli([sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read())"],
                                       data, timeout=20, cwd=str(tmp_path)))
    assert code == 0 and out == data
