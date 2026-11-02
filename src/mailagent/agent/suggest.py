"""记忆建议：agent 从用户的行为里发现长期偏好，提议存成记忆；用户点 Save 才存，点 No 永远不再提。

信号来源：
- 按钮：同一个发件人（或同一个机构域名下几个发件人）的提醒点了好几次 ✅ Done 却没让我起草回复 → 多半只是知会；
  同一个机构域名下静音了好几个发件人 → 整个域名都不急。规则只管被点的那个发件人，记忆会进 Jev 分拣的 user_context。
- 聊天：用户顺口说了长期事实（"我是博士生""我经理是 X"）但没让记住 —— 聊天模型在回复时顺带给出 suggest_memory。
- 草稿：用户把我起草的回复在 Gmail 里改过再发出去（Sent 轮询里比对）—— 落款变了直接看出来；
  正文改了再问一次模型，看是不是语气/长度这类长期习惯。

闸门 offer()：同一个 key 只提一次（memory_suggestions 永久保留，拒绝过的就在那里）；和已有记忆或以前提过的意思重复
（词重合）的不提；每天最多一条（上一条没回应就等 3 天），名额用完的排队，由调度器在下一个名额发出，排 7 天还没轮到就丢掉。
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from mailagent.agent.timeutil import parse_iso_safe, utcnow
from mailagent.ai import analyze
from mailagent.ai.llm.base import LLM
from mailagent.config import Config
from mailagent.db import Database

log = logging.getLogger(__name__)

CAP = timedelta(hours=20)  # "大约每天一条"：留几个小时余量，同一时段每天都能轮到
CAP_UNANSWERED = timedelta(hours=72)  # 上一条还没点 Save/No：别催
QUEUE_TTL = timedelta(days=7)
DONE_TAPS_SENDER = 3
DONE_TAPS_DOMAIN = 4  # 且至少 2 个发件人
MUTED_SENDERS_DOMAIN = 2

# 个人邮箱服务的域名：静音了两个 gmail 用户不代表所有 gmail 来信都不重要
FREE_MAIL_DOMAINS = frozenset("""
gmail.com googlemail.com outlook.com hotmail.com live.com msn.com yahoo.com ymail.com icloud.com me.com mac.com
aol.com proton.me protonmail.com pm.me gmx.com gmx.de gmx.net mail.com yandex.com yandex.ru zoho.com fastmail.com
qq.com foxmail.com 163.com 126.com yeah.net sina.com sina.cn sohu.com aliyun.com 139.com naver.com hanmail.net
""".split())

# 优先级：用户亲口说的 > 草稿改动 > 按钮
_PRIORITY = {"chat": 0, "draft_signoff": 1, "draft_style": 1, "mute_domain": 2, "seen_domain": 3, "seen_sender": 3}

_STOP = frozenset("""
the and for with that this from are was were is am be been my me mine i im i'm a an to of in on at it its as by or
but not no so if then than do does did have has had will would can could should our we you your he she her his they
their them about just very also really usually always often email emails mail mails
""".split())
_WORD_RE = re.compile(r"[a-z0-9][a-z0-9._@+-]*[a-z0-9]|[a-z0-9]")
_CJK_RE = re.compile(r"[㐀-鿿]+")


@dataclass
class Candidate:
    kind: str  # 见 memory_suggestions.kind
    key: str  # 去重键
    text: str  # 存成记忆的那句话（用户口吻）
    reason: str  # 给用户看的依据


def _tokens(text: str) -> set[str]:
    low = text.lower()
    out = {w for w in _WORD_RE.findall(low) if len(w) > 1 and w not in _STOP}
    for run in _CJK_RE.findall(low):
        out |= {run[i:i + 2] for i in range(len(run) - 1)} if len(run) > 1 else {run}
    return out


def similar(a: str, b: str) -> bool:
    """意思重复（粗略）：较短一句的实词有 3/4 以上出现在另一句里（至少 2 个，只有 1 个实词时要求相同）。"""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return False
    small = min(len(ta), len(tb))
    inter = len(ta & tb)
    return inter >= min(2, small) and inter / small >= 0.75


def _digest(text: str) -> str:
    return hashlib.sha1(" ".join(sorted(_tokens(text))).encode()).hexdigest()[:16]


def _norm(text: str) -> str:
    return " ".join((text or "").split())


# ---- 落款 ----

_CLOSING_RE = re.compile(
    r"^(best( regards| wishes)?|kind regards|warm regards|warmly|regards|cheers|thanks( again| so much| a lot)?|"
    r"thank you|many thanks|thx|sincerely|yours( sincerely| truly)?|all the best|talk soon|take care|cordially|"
    r"祝好|此致|谢谢|多谢|感谢|顺祝商祺|祝商祺|敬礼|顺颂商祺)"
    r"(?:\s*[,，!！.。~]+\s*|\s+[-—–]+\s*)?([^\s.!?。！？]+(?: [^\s.!?。！？]+){0,2})?$",
    re.I,
)


def signoff(body: str) -> str | None:
    """回复末尾的落款（"Best,\\nJamie" → "Best, Jamie"）；看不出来返回 None。'-- ' 之后的签名档不算。"""
    lines = [ln.strip() for ln in (body or "").strip().splitlines()]
    for i, ln in enumerate(lines):
        if ln in ("--", "—", "-- "):
            lines = lines[:i]
            break
    tail = [ln for ln in lines if ln][-3:]
    for i in range(len(tail) - 1, -1, -1):
        if _CLOSING_RE.match(tail[i]) and all(len(x) <= 40 for x in tail[i:]):
            return " ".join(tail[i:])
    return None


class Suggester:
    """状态都在数据库里；cap / cap_unanswered 可以改（测试、评测模拟"过了一天"）。"""

    def __init__(self, cfg: Config, db: Database):
        self.cfg = cfg
        self.db = db
        self.cap = CAP
        self.cap_unanswered = CAP_UNANSWERED

    # ---- 闸门 ----

    def slot_free(self, now: datetime | None = None) -> bool:
        last = self.db.last_shown_suggestion()
        if last is None:
            return True
        at = parse_iso_safe(last["shown_at"])
        if at is None:
            return True
        wait = self.cap_unanswered if last["status"] == "shown" else self.cap
        return (now or utcnow()) - at >= wait

    def duplicate_of(self, text: str, *, memories_only: bool = False) -> str | None:
        """和已有记忆或以前提过的建议（任何状态，含拒绝过的）意思重复：返回那句话。"""
        for m in self.db.list_memories(limit=500):
            if similar(text, m["text"]):
                return m["text"]
        if not memories_only:
            for s in self.db.list_suggestions(limit=1000):
                if similar(text, s["text"]):
                    return s["text"]
        return None

    def offer(self, cand: Candidate | None, now: datetime | None = None) -> dict[str, Any] | None:
        """过闸并入库。返回现在就该发给用户的那条（status=shown）；重复/提过/拒绝过返回 None；
        今天的名额用完了就排队（status=queued）并返回 None，调度器以后发。"""
        if cand is None:
            return None
        text = _norm(cand.text)[:300]
        if not text:
            return None
        if any(s["key"] == cand.key for s in self.db.list_suggestions(limit=1000)):
            log.info("Memory suggestion %s skipped: already suggested", cand.key)
            return None
        # 模型措辞的（chat/draft_style）key 不可靠，靠词重合去重；模板生成的按 key 就够了（模板句子彼此很像，不能比）
        if cand.kind in ("chat", "draft_style", "draft_signoff"):
            dup = self.duplicate_of(text, memories_only=cand.kind == "draft_signoff")
            if dup is not None:
                log.info("Memory suggestion %s skipped: same as an existing memory or earlier suggestion", cand.key)
                return None
        status = "shown" if self.slot_free(now) else "queued"
        sid = self.db.add_suggestion(cand.key, cand.kind, text, cand.reason, status)
        if sid is None:
            return None
        log.info("Memory suggestion #%s (%s) %s", sid, cand.kind, status)
        return self.db.get_suggestion(sid) if status == "shown" else None

    def next_due(self, now: datetime | None = None) -> dict[str, Any] | None:
        """调度器用：名额空出来时，取一条排队的标成 shown 返回（调用方负责发出去）。"""
        now = now or utcnow()
        self.db.drop_stale_suggestions(now - QUEUE_TTL)
        if not self.slot_free(now):
            return None
        queued = self.db.list_suggestions(("queued",))
        for s in sorted(queued, key=lambda s: (_PRIORITY.get(s["kind"], 9), -s["id"])):
            if self.db.show_suggestion(s["id"]):
                return self.db.get_suggestion(s["id"])
        return None

    # ---- 信号 → 候选 ----

    def _org_domain(self, domain: str) -> bool:
        """机构域名：不是个人邮箱服务，也不是用户自己邮箱的域名（同事不能一锅端）。"""
        own = {m.address.rsplit("@", 1)[-1].lower() for m in self.cfg.mailboxes if "@" in m.address}
        return bool(domain) and domain not in FREE_MAIL_DOMAINS and domain not in own

    def _mentioned(self, needle: str) -> bool:
        return any(needle in m["text"].lower() for m in self.db.list_memories(limit=500))

    def from_done_tap(self, row: dict[str, Any] | None) -> Candidate | None:
        """点了 ✅ Done 之后：同一发件人（或同一机构域名）的提醒常常点完就算，没让我起草回复。"""
        sender = ((row or {}).get("from_addr") or "").lower()
        if "@" not in sender:
            return None
        domain = sender.rsplit("@", 1)[1]
        if self.db.match_rules(sender, "") or self._mentioned(sender):
            return None  # 已经有规则（静音/VIP）或记忆管着它
        n = self.db.done_taps_by_sender(sender)
        if n >= DONE_TAPS_SENDER:
            return Candidate(
                "seen_sender", f"seen_sender:{sender}",
                f"Mail from {sender} is usually just FYI for me; it doesn't need an instant alert unless it's urgent.",
                f"You tapped ✅ Done on {n} alerts from {sender} without asking me to draft a reply.")
        if not self._org_domain(domain) or self._mentioned(domain):
            return None
        if any(s["key"].startswith("seen_sender:") and s["key"].endswith("@" + domain)
               for s in self.db.list_suggestions(("declined",))):
            return None  # 用户拒绝过这个域名下某个发件人的同类建议，更宽的版本也别提
        by_sender = self.db.done_taps_by_domain(domain)
        total = sum(by_sender.values())
        if len(by_sender) >= 2 and total >= DONE_TAPS_DOMAIN:
            return Candidate(
                "seen_domain", f"seen_domain:{domain}",
                f"Mail from {domain} is usually just FYI for me; it doesn't need an instant alert unless it's urgent.",
                f"You tapped ✅ Done on {total} alerts from {len(by_sender)} senders at {domain} "
                "without asking me to draft a reply.")
        return None

    def from_mute_tap(self, row: dict[str, Any] | None) -> Candidate | None:
        """点了 🔕 静音之后：同一机构域名下已经静音了好几个发件人。"""
        sender = ((row or {}).get("from_addr") or "").lower()
        if "@" not in sender:
            return None
        domain = sender.rsplit("@", 1)[1]
        if not self._org_domain(domain) or self._mentioned(domain):
            return None
        rules = self.db.list_rules()
        if any(r["field"] == "domain" and (domain == r["pattern"] or domain.endswith("." + r["pattern"])) for r in rules):
            return None  # 已经有域名规则
        if any(r["kind"] == "vip" and r["field"] == "from" and r["pattern"].endswith("@" + domain) for r in rules):
            return None  # 这个域名下有用户特别关心的人
        muted = sorted({r["pattern"] for r in rules
                        if r["kind"] == "mute" and r["field"] == "from" and r["pattern"].endswith("@" + domain)})
        if len(muted) < MUTED_SENDERS_DOMAIN:
            return None
        shown = ", ".join(muted[:4]) + (f" and {len(muted) - 4} more" if len(muted) > 4 else "")
        return Candidate(
            "mute_domain", f"mute_domain:{domain}",
            f"Mail from anyone at {domain} is low priority for me; the digest is enough unless it's urgent.",
            f"You've muted {len(muted)} senders at {domain}: {shown}.")

    def from_chat(self, text: str | None) -> Candidate | None:
        """聊天模型给的 suggest_memory。"""
        text = _norm(str(text or ""))[:300]
        if not text:
            return None
        return Candidate("chat", "chat:" + _digest(text), text, "You mentioned this in our chat.")

    async def from_draft_edit(self, draft: str, sent: str, *, subject: str = "", llm: LLM | None = None,
                              language: str = "en") -> Candidate | None:
        """用户改过我的草稿再发出去：落款变了直接提；正文改了再问模型是不是长期习惯（不是就 None）。"""
        if not sent.strip() or _norm(draft) == _norm(sent):
            return None
        about = f' to "{subject}"' if subject else ""
        before, after = signoff(draft), signoff(sent)
        if after and _norm(before or "").lower() != _norm(after).lower():
            return Candidate(
                "draft_signoff", "draft_signoff:" + _norm(after).lower(), f'I sign off my emails with "{after}".',
                f"You changed the sign-off of the reply I drafted{about} from "
                f"{'“' + before + '”' if before else 'nothing'} to “{after}” before sending it.")
        if llm is None:
            return None
        pref = await analyze.writing_preference(llm, draft, sent, language=language)
        if not pref:
            return None
        return Candidate("draft_style", "draft_style:" + _digest(pref), pref,
                         f"You edited the reply I drafted{about} before sending it.")
