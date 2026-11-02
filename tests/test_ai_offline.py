import asyncio
from datetime import datetime, timezone

import pytest

from mailagent.ai.analyze import analyze_mail, validate_analysis, write_digest
from mailagent.ai.jev import fallback_triage
from mailagent.ai.llm import LLMError
from mailagent.ai.llm._util import extract_json, strict_schema
from mailagent.models import MailMessage


def _mail(subject="s", body="b", frm="a@x.example", unsub=False) -> MailMessage:
    return MailMessage("t", 1, "", "", "<m@x>", "", "", frm, "A", ["me@x"], [], subject,
                       datetime.now(timezone.utc), body, list_unsubscribe=unsub)


def test_extract_json_variants():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('好的，结果如下：{"a": "x}y", "b": [1]} 以上') == {"a": "x}y", "b": [1]}
    with pytest.raises(LLMError) as e:
        extract_json("没有 JSON")
    assert not e.value.retryable


def test_strict_schema_makes_all_required():
    s = strict_schema({"type": "object", "properties": {"a": {"type": "string"}, "b": {"type": "integer"}},
                       "required": ["a"]})
    assert s["additionalProperties"] is False and set(s["required"]) == {"a", "b"}


def test_validate_analysis_drops_bad_dates():
    a = validate_analysis({"summary": "s", "action": None, "reply_suggested": False, "deadlines": [
        {"what": "ok", "date": "2026-11-06", "time": "12:00", "kind": "deadline", "timezone": None},
        {"what": "bad", "date": "2026-02-30", "time": None, "kind": "deadline", "timezone": None},
        {"what": "bad time", "date": "2026-11-07", "time": "25:00", "kind": "weird", "timezone": "Mars/Base"},
    ]})
    assert [d.what for d in a.deadlines] == ["ok", "bad time"]
    assert a.deadlines[1].time is None and a.deadlines[1].kind == "other" and a.deadlines[1].timezone is None


class _BrokenLLM:
    name = "broken"

    async def complete(self, *a, **k):
        raise LLMError("boom")

    async def check(self):
        return "x"


def test_analyze_never_raises_and_digest_template():
    a = asyncio.run(analyze_mail(_BrokenLLM(), _mail(body="hello world"), now_local=datetime.now(timezone.utc),
                                 tz="UTC", language="zh", memories=[]))
    assert a.source == "template" and "hello" in a.summary
    rows = [{"id": 1, "from_name": "A", "from_addr": "a@x.example", "subject": "主题一", "summary": "摘要", "body_text": "",
             "importance": 1.5, "needs_action": 0.8, "has_time": 0.1, "category": "work", "action": "回复",
             "date": datetime.now(timezone.utc).isoformat(), "mailbox_id": "t"}]
    text = asyncio.run(write_digest(None, rows, [], language="zh", tz="UTC"))
    assert "主题一" in text


def test_fallback_triage_never_skips_security():
    t = fallback_triage(_mail(subject="安全提醒：新设备登录", body="如果不是你本人请修改密码"))
    assert t.source == "fallback" and t.importance >= 1.5 and t.confidence < 0.35
