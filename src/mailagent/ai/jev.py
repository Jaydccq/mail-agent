"""Jev（TypeSafe System One）分拣客户端。

Jev 是分类模型：只回答选择/打分/是非题，不生成文本。
POST {base_url}/systemone，body {"model","state","questions"}，返回 {"answers": {qid: ...}}。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from mailagent.config import JevConfig
from mailagent.models import MailMessage, Triage

log = logging.getLogger(__name__)

CATEGORIES: dict[str, str] = {
    "personal": "亲友或认识的人写给收件人本人的私人邮件",
    "work": "工作、学业、项目、同事/客户/老师之间的往来",
    "finance": "银行、信用卡、账单、付款、报税、投资、保险",
    "travel": "机票、酒店、行程、签证、出行提醒",
    "shopping": "订单确认、发货、物流、退款、售后（不是促销）",
    "newsletter": "订阅的资讯、周报、博客、社区摘要",
    "promotion": "营销、广告、优惠券、促销活动",
    "social": "社交网络通知（点赞、关注、消息提醒、论坛）",
    "notification": "系统或服务的自动通知（账户变动、提醒、报告、验证码以外的系统消息）",
    "security": "登录告警、验证码、密码重置、账户安全、可疑活动",
    "other": "以上都不是",
}

QUESTIONS: dict[str, dict[str, Any]] = {
    "importance": {
        "type": "score",
        "instructions": (
            "这封邮件对收件人本人有多重要、多紧急？state.user_context 里是收件人的偏好，优先遵循。"
            "state.quoted 是引用的旧邮件历史，只作背景，按本次新内容判断。"
        ),
        "criteria": [
            "0：营销、广告、促销、群发资讯、自动通知，收件人无需关注",
            "1：普通知会，知道一下即可，不急",
            "2：今天值得知道：与收件人相关的重要信息、真实的人写来的邮件、近期安排",
            "3：需要收件人尽快本人处理：账单逾期或即将扣款失败、账户安全告警、重要的人在等回复、临近的截止日期",
        ],
    },
    "needs_action": {
        "type": "noul",
        "instructions": "这封邮件是否需要收件人本人去做某件事（回复、付款、签字、确认、出席、提交材料、改密码等）？",
        "criteria": {
            "true": "明确要求或期待收件人本人采取行动，不做会有后果或对方在等",
            "false": "只是知会、营销、自动通知，或者行动只是可选的推广（例如“立即购买”“查看优惠”）",
        },
    },
    "has_time": {
        "type": "noul",
        "instructions": "邮件本次新内容里是否提到与收件人本人相关的具体日期或时间（截止日期、会议、活动、到期、续费、预约）？",
        "criteria": {
            "true": "有与收件人相关的具体日期/时间：截止、会议、航班、预约、账单到期、订阅续费等",
            "false": "没有，或只有促销截止、版权年份、退订说明、引用历史中的旧日期",
        },
    },
    "category": {
        "type": "choice",
        "instructions": "这封邮件属于哪一类？",
        "criteria": CATEGORIES,
    },
}


class JevError(Exception):
    def __init__(self, message: str, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


def _clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n] + "…"


def build_state(mail: MailMessage, user_context: str = "") -> dict[str, Any]:
    sender = f"{mail.from_name} <{mail.from_addr}>" if mail.from_name else mail.from_addr
    return {
        "from": sender,
        "subject": mail.subject,
        "date": mail.date.isoformat(),
        "labels": list(mail.labels),
        "has_list_unsubscribe": mail.list_unsubscribe,
        "attachments": list(mail.attachments),
        "body": _clip(mail.body_text, 6000),
        "quoted": _clip(mail.quoted_text, 1500),
        "user_context": user_context,
    }


class JevClient:
    def __init__(self, cfg: JevConfig, http: httpx.AsyncClient | None = None):
        self.cfg = cfg
        self.base_url = cfg.base_url.rstrip("/")
        self._own_http = http is None
        self._http = http or httpx.AsyncClient(timeout=60)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.cfg.api_key}", "Content-Type": "application/json"}

    async def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.cfg.api_key:
            raise JevError("Jev api_key is not configured", retryable=False)
        try:
            r = await self._http.request(method, f"{self.base_url}{path}", json=body, headers=self._headers())
        except httpx.TimeoutException as e:
            raise JevError("Jev request timed out", retryable=True) from e
        except httpx.HTTPError as e:
            raise JevError(f"Jev network error: {type(e).__name__}", retryable=True) from e
        if r.status_code != 200:
            detail = ""
            try:
                d = r.json().get("detail")
                if isinstance(d, list):  # 422：只留 loc/msg，不带回显的输入（可能含邮件内容）
                    d = [{"loc": x.get("loc"), "msg": x.get("msg")} for x in d if isinstance(x, dict)]
                detail = json.dumps(d, ensure_ascii=False)[:300] if not isinstance(d, str) else d[:300]
            except (ValueError, AttributeError):
                pass
            retryable = r.status_code == 429 or r.status_code >= 500
            if r.status_code in (401, 403):
                msg = f"Jev authentication failed (HTTP {r.status_code}); check api_key"
            else:
                msg = f"Jev request failed (HTTP {r.status_code}) {detail}".rstrip()
            log.warning("%s", msg)
            raise JevError(msg, retryable=retryable)
        try:
            data = r.json()
        except ValueError:
            raise JevError("Jev did not return JSON", retryable=True) from None
        if not isinstance(data, dict):
            raise JevError("Jev returned a malformed response", retryable=True)
        return data

    async def ask(self, state: str | dict, questions: dict) -> dict:
        """问一组题，返回原始 answers（qid → 答案）。"""
        data = await self._request("POST", "/systemone",
                                   {"model": self.cfg.model, "state": state, "questions": questions})
        answers = data.get("answers")
        if not isinstance(answers, dict):
            raise JevError("Jev response has no answers", retryable=False)
        return answers

    async def triage(self, mail: MailMessage, user_context: str = "") -> Triage:
        answers = await self.ask(build_state(mail, user_context), QUESTIONS)
        try:
            imp = answers["importance"]
            importance = float(imp["score"])
            confidence = float(imp.get("confidence", 0.0))
            needs_action = float(answers["needs_action"]["noul"])
            has_time = float(answers["has_time"]["noul"])
            category = str(answers["category"]["choice"])
        except (KeyError, TypeError, ValueError) as e:
            raise JevError(f"Jev answers missing or malformed: {type(e).__name__} {e}", retryable=False) from None
        if category not in CATEGORIES:
            category = "other"
        return Triage(
            importance=min(max(importance, 0.0), 3.0),
            needs_action=min(max(needs_action, 0.0), 1.0),
            has_time=min(max(has_time, 0.0), 1.0),
            category=category,
            confidence=min(max(confidence, 0.0), 1.0),
            source="jev",
        )

    async def models(self) -> list[str]:
        data = await self._request("GET", "/models")
        out: list[str] = []
        for m in data.get("models") or []:
            if isinstance(m, dict) and m.get("name"):
                out.append(str(m["name"]))
            elif isinstance(m, str):
                out.append(m)
        return out

    async def aclose(self) -> None:
        if self._own_http:
            await self._http.aclose()


# ---- Jev 不可用时的启发式 ----

_SECURITY = re.compile(r"(安全|登录|登入|验证码|密码|可疑|异常活动|security|sign[- ]?in|log[- ]?in|password|"
                       r"verification code|suspicious|unusual activity|2fa|two-factor)", re.I)
_FINANCE = re.compile(r"(账单|付款|扣款|逾期|欠费|发票|invoice|payment|overdue|past due|bill|receipt|statement)", re.I)
_ACTION = re.compile(r"(请回复|请确认|请尽快|截止|到期|逾期|需要你|请提交|请签|action required|please reply|"
                     r"please confirm|respond|rsvp|deadline|due|overdue|expires?|sign|submit)", re.I)
_TIME = re.compile(r"(\d{4}[-/年]\d{1,2}[-/月]\d{1,2}|\d{1,2}月\d{1,2}[日号]|\d{1,2}:\d{2}|明天|后天|下周|周[一二三四五六日天]|"
                   r"星期[一二三四五六日天]|tomorrow|next week|monday|tuesday|wednesday|thursday|friday|saturday|"
                   r"sunday|\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.? \d{1,2}\b)", re.I)
_NOREPLY = re.compile(r"(no-?reply|do-?not-?reply|notifications?@|mailer|news(letter)?@|marketing@)", re.I)


def fallback_triage(mail: MailMessage) -> Triage:
    """Jev 不可用时的粗略判断。宁可多进摘要，不能静默丢：
    confidence 固定 0.3（pipeline 对 confidence<0.35 的邮件永远不 skip），默认 importance=1.0。"""
    text = f"{mail.subject}\n{mail.body_text[:3000]}"
    automated = mail.list_unsubscribe or bool(_NOREPLY.search(mail.from_addr))
    security = bool(_SECURITY.search(text))
    finance = bool(_FINANCE.search(text))
    action = bool(_ACTION.search(text))
    has_time = bool(_TIME.search(text))

    importance = 1.0
    category = "other"
    if security:
        category, importance = "security", 2.0
    elif finance:
        category, importance = "finance", 1.5
    elif mail.list_unsubscribe:
        category = "newsletter"
    elif automated:
        category = "notification"
    else:
        category = "personal"
        importance = 1.5
    if "\\Important" in mail.labels:
        importance += 0.5
    if action and not automated:
        importance += 0.5
    if mail.list_unsubscribe and not (action or security or finance):
        importance = 0.5
    return Triage(
        importance=min(importance, 3.0),
        needs_action=0.6 if action else 0.2,
        has_time=0.6 if has_time else 0.2,
        category=category,
        confidence=0.3,
        source="fallback",
    )
