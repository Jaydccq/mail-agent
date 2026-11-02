"""时间工具：时区、静默时段、提醒时间、摘要时段。

约定：入参和返回的"时刻"都是 aware datetime（UTC 存库）；"本地日期/时间"是 YYYY-MM-DD / HH:MM 字符串。
"""

from __future__ import annotations

import difflib
import os
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def get_tz(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def to_local(dt: datetime, tz: str) -> datetime:
    return dt.astimezone(get_tz(tz))


def parse_hhmm(s: str | None) -> time | None:
    """'08:30' -> time(8, 30)；非法返回 None。"""
    if not s:
        return None
    try:
        h, m = s.strip().split(":", 1)
        return time(int(h), int(m))
    except (ValueError, TypeError):
        return None


def parse_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(s.strip())
    except ValueError:
        return None


def parse_iso_safe(s: str | None) -> datetime | None:
    """库里的 UTC ISO 字符串 -> aware datetime；空或非法返回 None。"""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def local_at(d: date, t: time, tz: str) -> datetime:
    """本地日期+时间 -> UTC 时刻。"""
    return datetime.combine(d, t, tzinfo=get_tz(tz)).astimezone(timezone.utc)


def today_local(now: datetime, tz: str) -> date:
    return to_local(now, tz).date()


# ---- 展示 ----


def fmt_date(d: date) -> str:
    """date(2026, 11, 6) -> 'Fri, Nov 6'（不依赖 locale）。"""
    return f"{WEEKDAYS[d.weekday()]}, {MONTHS[d.month - 1]} {d.day}"


def fmt_due(due_date: str | None, due_time: str | None = None) -> str:
    """'2026-11-06', '14:00' -> 'Fri, Nov 6 14:00'。"""
    d = parse_date(due_date)
    if not d:
        return due_date or ""
    out = fmt_date(d)
    if due_time:
        out += f" {due_time}"
    return out


def fmt_local(dt: datetime | None, tz: str, with_date: bool = True) -> str:
    """aware 时刻 -> 本地 'Fri, Nov 6 12:00'（with_date=False 时只要 '12:00'）。"""
    if dt is None:
        return ""
    loc = to_local(dt, tz)
    hm = loc.strftime("%H:%M")
    return f"{fmt_date(loc.date())} {hm}" if with_date else hm


# ---- 静默时段 ----


def in_quiet(now: datetime, quiet_start: str, quiet_end: str, tz: str) -> bool:
    start, end = parse_hhmm(quiet_start), parse_hhmm(quiet_end)
    if start is None or end is None or start == end:
        return False
    t = to_local(now, tz).time()
    if start < end:
        return start <= t < end
    return t >= start or t < end  # 跨午夜，例如 23:00~08:00


def quiet_deferral(now: datetime, quiet_start: str, quiet_end: str, tz: str) -> datetime:
    """不在静默时段返回 now；否则返回下一个 quiet_end（UTC）。"""
    if not in_quiet(now, quiet_start, quiet_end, tz):
        return now
    end = parse_hhmm(quiet_end)
    assert end is not None
    loc = to_local(now, tz)
    target = local_at(loc.date(), end, tz)
    if target <= now:
        target = local_at(loc.date() + timedelta(days=1), end, tz)
    return target


# ---- 提醒 ----


def reminder_times(due_date: str | None, due_time: str | None, tz: str) -> list[datetime]:
    """有时间：[前一天 20:00, 当天 due_time-2h]；只有日期：[前一天 20:00, 当天 09:00]。返回排序去重的 UTC 时刻。"""
    d = parse_date(due_date)
    if not d:
        return []
    out = [local_at(d - timedelta(days=1), time(20, 0), tz)]
    t = parse_hhmm(due_time)
    if t:
        out.append(local_at(d, t, tz) - timedelta(hours=2))
    else:
        out.append(local_at(d, time(9, 0), tz))
    return sorted(set(out))


def next_reminder(todo_row: dict[str, Any], now: datetime, default_tz: str = "UTC") -> datetime | None:
    """第一个晚于 now 的提醒时刻；没有返回 None。todo_row 需要 due_date / due_time / timezone 键。"""
    tz = todo_row.get("timezone") or default_tz
    for t in reminder_times(todo_row.get("due_date"), todo_row.get("due_time"), tz):
        if t > now:
            return t
    return None


def shift_due(due_date: str | None, due_time: str | None, old_tz: str, new_tz: str) -> tuple[str | None, str | None]:
    """把本地截止日期/时间换到另一个时区：有时间的保持同一时刻（可能跨日），只有日期的日期不变。"""
    d, t = parse_date(due_date), parse_hhmm(due_time)
    if not d or not t:
        return due_date, due_time
    loc = to_local(local_at(d, t, old_tz), new_tz)
    return loc.date().isoformat(), loc.strftime("%H:%M")


def tomorrow_at(now: datetime, tz: str, hhmm: str = "09:00") -> datetime:
    t = parse_hhmm(hhmm) or time(9, 0)
    return local_at(today_local(now, tz) + timedelta(days=1), t, tz)


def parse_local_datetime(s: str, tz: str) -> datetime | None:
    """'YYYY-MM-DD HH:MM' / 'YYYY-MM-DD' / ISO -> UTC 时刻。只有日期时按 09:00。"""
    s = (s or "").strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("T", " "))
    except ValueError:
        return None
    if len(s) <= 10:
        dt = dt.replace(hour=9, minute=0)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=get_tz(tz))
    return dt.astimezone(timezone.utc)


# ---- 摘要时段 ----


def latest_slot(now: datetime, digest_times: list[str], tz: str) -> tuple[str, datetime] | None:
    """所有摘要时段里最近一个 <= now 的（今天或昨天），返回 (本地 "YYYY-MM-DD HH:MM", UTC 时刻)。
    错过多个时段时只返回最近一个 -> 只补发一次。"""
    today = today_local(now, tz)
    best: datetime | None = None
    for s in digest_times:
        t = parse_hhmm(s)
        if t is None:
            continue
        for d in (today, today - timedelta(days=1)):
            at = local_at(d, t, tz)
            if at <= now and (best is None or at > best):
                best = at
    if best is None:
        return None
    return to_local(best, tz).strftime("%Y-%m-%d %H:%M"), best


# ---- 时区查找 ----


_TZ_REGIONS = ("Africa/", "America/", "Antarctica/", "Asia/", "Atlantic/", "Australia/", "Europe/", "Indian/", "Pacific/")
# 不是 IANA 城市名、但常有人这么写的
_TZ_ALIASES = {
    "beijing": "Asia/Shanghai", "china": "Asia/Shanghai", "hangzhou": "Asia/Shanghai", "shenzhen": "Asia/Shanghai",
    "guangzhou": "Asia/Shanghai", "india": "Asia/Kolkata", "japan": "Asia/Tokyo", "korea": "Asia/Seoul",
    "uk": "Europe/London", "pt": "America/Los_Angeles", "pst": "America/Los_Angeles", "pdt": "America/Los_Angeles",
    "pacific": "America/Los_Angeles", "san_francisco": "America/Los_Angeles", "sf": "America/Los_Angeles",
    "seattle": "America/Los_Angeles", "et": "America/New_York", "est": "America/New_York",
    "edt": "America/New_York", "eastern": "America/New_York", "boston": "America/New_York",
    "ct": "America/Chicago", "cdt": "America/Chicago", "central": "America/Chicago",
    "mt": "America/Denver", "mst": "America/Denver", "mdt": "America/Denver", "mountain": "America/Denver",
}


def find_timezone(query: str) -> tuple[str | None, list[str]]:
    """用户输入 -> (IANA 名, 候选)。认完整 IANA 名（不分大小写）、UTC、城市名（"shanghai"、"new york"）
    和少量常用别名（beijing、PST、ET…）。没认出来时返回 (None, 最多 5 个相近的 IANA 名)。"""
    q = "_".join((query or "").strip().split()).lower()
    if not q:
        return None, []
    if q in ("utc", "gmt", "z"):
        return "UTC", []
    if q in _TZ_ALIASES:
        return _TZ_ALIASES[q], []
    all_zones = available_timezones()
    by_city: dict[str, list[str]] = {}
    for z in sorted(z for z in all_zones if z.startswith(_TZ_REGIONS)):
        by_city.setdefault(z.rsplit("/", 1)[-1].lower(), []).append(z)
    if len(by_city.get(q, [])) == 1:  # 城市名优先于同名的旧式别名（"singapore" -> Asia/Singapore）
        return by_city[q][0], []
    exact = {z.lower(): z for z in all_zones}
    if q in exact:
        return exact[q], []
    if q in by_city:
        return None, by_city[q][:5]
    out: list[str] = []
    for k in difflib.get_close_matches(q, list(by_city), n=5, cutoff=0.75):
        out += [z for z in by_city[k] if z not in out]
    return None, out[:5]


# ---- 时区检测 ----


def detect_timezone() -> str:
    """/etc/localtime 软链接 -> IANA 名；其次 TZ 环境变量；都不行用 UTC。"""
    try:
        target = os.path.realpath("/etc/localtime")
        if "zoneinfo/" in target:
            name = target.split("zoneinfo/", 1)[1]
            ZoneInfo(name)
            return name
    except (OSError, ZoneInfoNotFoundError, ValueError):
        pass
    tz_env = os.environ.get("TZ", "").lstrip(":")
    if tz_env:
        try:
            ZoneInfo(tz_env)
            return tz_env
        except (ZoneInfoNotFoundError, ValueError):
            pass
    try:
        name = Path("/etc/timezone").read_text(encoding="utf-8").strip()
        if name:
            ZoneInfo(name)
            return name
    except (OSError, ZoneInfoNotFoundError, ValueError):
        pass
    return "UTC"
