"""把邮件、待办、摘要、待确认动作渲染成 OutMessage。按钮 data 约定见 CONTRACTS.md。

界面文字统一用英文；摘要/分析内容本身的语言由 ai 层按 prefs.language 生成。
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from mailagent.agent.timeutil import fmt_due, fmt_local, parse_iso_safe
from mailagent.models import Analysis, Button, Deadline, MailMessage, OutMessage


SENDER_MAX = 80


def sender_label(addr: str, name: str = "") -> str:
    name = (name or "").strip().strip('"')
    if len(name) > SENDER_MAX:  # 显示名由发件人随便写；放进标题后不参与拆分，太长会让整条通知被拒收
        name = name[:SENDER_MAX - 1] + "…"
    return name or addr or "(unknown sender)"


def deadline_line(d: Deadline) -> str:
    return f"🗓 {fmt_due(d.date, d.time)} {d.what}".rstrip()


def notification(msg_id: int, mail: MailMessage, analysis: Analysis | None, *, tz: str,
                 show_draft: bool, with_buttons: bool = True) -> OutMessage:
    """重要邮件即时通知。"""
    lines = [f"Subject: {mail.subject or '(no subject)'}"]
    summary = (analysis.summary if analysis else "") or mail.snippet
    if summary:
        lines += ["", summary]
    if analysis and analysis.action:
        lines += ["", f"👉 You need to: {analysis.action}"]
    if analysis and analysis.deadlines:
        lines.append("")
        lines += [deadline_line(d) for d in analysis.deadlines]
    lines += ["", f"{mail.mailbox_id} · {fmt_local(mail.date, tz)}"]
    buttons: list[list[Button]] = []
    if with_buttons:
        buttons.append([Button("✅ Done", f"seen:{msg_id}"), Button("🔕 Mute this sender", f"mute:{msg_id}")])
        if show_draft:
            buttons.append([Button("✍️ Draft a reply", f"draft:{msg_id}")])
    return OutMessage(text="\n".join(lines), title=f"📬 {sender_label(mail.from_addr, mail.from_name)}",
                      buttons=buttons)


def with_result(msg: OutMessage, line: str) -> OutMessage:
    """按钮点完后：去掉按钮，末尾追加一行结果。"""
    return replace(msg, text=f"{msg.text}\n\n{line}", buttons=[])


def todo_due_text(todo: dict[str, Any]) -> str:
    if not todo.get("due_date"):
        return ""
    return fmt_due(todo["due_date"], todo.get("due_time"))


def todo_reminder(todo: dict[str, Any], source_subject: str | None = None, *,
                  with_buttons: bool = True) -> OutMessage:
    lines: list[str] = []
    due = todo_due_text(todo)
    if due:
        lines.append(f"Due: {due}")
    if source_subject:
        lines.append(f"From email: {source_subject}")
    buttons = [[Button("✅ Done", f"done:{todo['id']}"), Button("💤 Tomorrow", f"snooze:{todo['id']}")]] \
        if with_buttons else []
    return OutMessage(text="\n".join(lines) or "(no due date)", title=f"⏰ {todo['title']}", buttons=buttons)


def todo_list(todos: list[dict[str, Any]], max_buttons: int = 5) -> OutMessage:
    if not todos:
        return OutMessage(text="Nothing on your list 🎉", title="📝 Todos")
    lines = []
    for t in todos:
        due = todo_due_text(t)
        lines.append(f"#{t['id']} {t['title']}" + (f" ({due})" if due else ""))
    buttons = [[Button(f"✅ #{t['id']}", f"done:{t['id']}"), Button("💤 Tomorrow", f"snooze:{t['id']}")]
               for t in todos[:max_buttons]]
    return OutMessage(text="\n".join(lines), title=f"📝 Todos ({len(todos)})", buttons=buttons)


def action_preview(action: dict[str, Any], note: str = "") -> OutMessage:
    """发信预览：完整展示，按钮带确认码。"""
    p = json.loads(action["payload"]) if isinstance(action["payload"], str) else action["payload"]
    lines = [
        f"From: {p.get('from', '')}",
        f"To: {', '.join(p.get('to') or []) or '(none)'}",
    ]
    if p.get("cc"):
        lines.append(f"Cc: {', '.join(p['cc'])}")
    lines += [f"Subject: {p.get('subject', '')}", "", "———— Message ————", p.get("body", ""), "————————————", ""]
    if note:
        lines += [note, ""]
    lines.append(f"Code {action['code']}. You can also reply: confirm {action['id']} {action['code']}")
    buttons = [[Button("📤 Send", f"approve:{action['id']}:{action['code']}"), Button("Cancel", f"cancel:{action['id']}")]]
    return OutMessage(text="\n".join(lines), title="✉️ Reply draft (not sent)", buttons=buttons)


def memory_suggestion(s: dict[str, Any], *, with_buttons: bool = True) -> OutMessage:
    """记忆建议：用户点 Save 才存，点 No 以后不再提。"""
    lines = [f"“{s['text']}”", ""]
    if s.get("reason"):
        lines.append(s["reason"])
    lines.append("Should I remember this? I'd use it when sorting your mail and drafting replies. "
                 "/memory lists and deletes memories.")
    buttons = [[Button("💾 Save", f"sug:{s['id']}"), Button("No", f"sugno:{s['id']}")]] if with_buttons else []
    return OutMessage(text="\n".join(lines), title="💡 Remember this?", buttons=buttons)


def write_confirmation(write_id: int, summary: str, *, with_buttons: bool = True) -> OutMessage:
    """聊天里读过邮件的那一轮想改规则/记忆/待办：用户点 Confirm 才执行。"""
    text = (f"{summary}\n\n"
            "I read email content while handling your request, so I need your OK before changing this. "
            "If you didn't ask for it, tap Cancel.")
    buttons = [[Button("✅ Confirm", f"wok:{write_id}"), Button("Cancel", f"wno:{write_id}")]] if with_buttons else []
    return OutMessage(text=text, title="🛡 Confirm this change", buttons=buttons)


def digest(body: str, *, slot_label: str, problems: list[str], paused: bool) -> OutMessage:
    parts: list[str] = []
    if paused:
        parts.append("⏸ Instant alerts are paused (/resume to turn them back on), so everything goes to the digest.")
    if problems:
        parts.append("⚠️ " + "\n⚠️ ".join(problems))
    if body.strip():
        parts.append(body.strip())
    return OutMessage(text="\n\n".join(parts) or "Nothing new.", title=f"📋 Digest · {slot_label}", markdown=True)


def interrupted_action(action: dict[str, Any]) -> OutMessage:
    p = json.loads(action["payload"]) if isinstance(action["payload"], str) else action["payload"]
    return OutMessage(
        title="⚠️ A reply may or may not have been sent",
        text=(f"I was shut down while sending action #{action['id']}:\nSubject: {p.get('subject', '')}\n"
              f"To: {', '.join(p.get('to') or [])}\n\n"
              "Please check Gmail's Sent folder to see whether it went out. "
              "To avoid a duplicate, I won't resend it automatically."),
    )


def mailbox_problem(state: dict[str, Any], tz: str) -> str:
    """邮箱同步异常的一行说明。"""
    if state.get("status") == "auth_failed":
        what = "can't log in (the app password may have been revoked)"
    else:
        what = "sync failed"
    err = (state.get("error") or "").strip()
    last = parse_iso_safe(state.get("last_sync_at"))
    tail = f"; last synced {fmt_local(last, tz)}" if last else ""
    return f"{state['mailbox_id']} {what}: {err[:200] or 'unknown reason'}{tail}"
