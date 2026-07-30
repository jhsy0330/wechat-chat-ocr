from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta


TIME_TOKEN = (
    r"(?:(?P<prefix>上午|下午|晚上|凌晨|中午|am|pm)\s*)?"
    r"(?P<hour>\d{1,2})[:：](?P<minute>\d{2})"
    r"\s*(?P<suffix>am|pm)?"
)
RELATIVE_TOKEN = r"(?:today|yesterda[yt]|今天|昨天|前天)"
WEEKDAY_TOKEN = (
    r"(?:星期[一二三四五六日天]|周[一二三四五六日天]|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
)
DATE_TOKEN = (
    r"(?:\d{4}\s*[年./-]\s*\d{1,2}\s*[月./-]\s*\d{1,2}\s*日?|"
    r"\d{1,2}\s*[月./-]\s*\d{1,2}\s*日?)"
)
LABEL_PATTERN = re.compile(
    rf"^\s*(?:(?P<label>{RELATIVE_TOKEN}|{WEEKDAY_TOKEN}|{DATE_TOKEN})\s*(?:at\s*)?)?"
    rf"{TIME_TOKEN}\s*$",
    re.IGNORECASE,
)

WEEKDAYS = {
    "一": 0,
    "二": 1,
    "三": 2,
    "四": 3,
    "五": 4,
    "六": 5,
    "日": 6,
    "天": 6,
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


def is_wechat_time_label(text: str) -> bool:
    return LABEL_PATTERN.fullmatch(text.strip()) is not None


def parse_wechat_timestamp(
    text: str, reference: datetime | None = None
) -> datetime | None:
    match = LABEL_PATTERN.fullmatch(text.strip())
    if match is None:
        return None
    reference = reference or datetime.now().astimezone()
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=datetime.now().astimezone().tzinfo)

    hour = int(match.group("hour"))
    minute = int(match.group("minute"))
    period = (match.group("suffix") or match.group("prefix") or "").lower()
    hour = _normalize_hour(hour, period)
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        return None

    target_date = _resolve_date(match.group("label"), reference, hour, minute)
    if target_date is None:
        return None
    return datetime.combine(
        target_date, time(hour=hour, minute=minute), tzinfo=reference.tzinfo
    )


def _normalize_hour(hour: int, period: str) -> int:
    if period in {"pm", "下午", "晚上", "中午"} and hour < 12:
        return hour + 12
    if period in {"am", "上午", "凌晨"} and hour == 12:
        return 0
    return hour


def _resolve_date(
    label: str | None, reference: datetime, hour: int, minute: int
) -> date | None:
    if not label:
        return reference.date()
    normalized = re.sub(r"\s+", "", label).lower()
    if normalized in {"today", "今天"}:
        return reference.date()
    if normalized in {"yesterday", "yesterdat", "昨天"}:
        return reference.date() - timedelta(days=1)
    if normalized == "前天":
        return reference.date() - timedelta(days=2)

    weekday = _weekday_number(normalized)
    if weekday is not None:
        days_back = (reference.weekday() - weekday) % 7
        candidate = reference.date() - timedelta(days=days_back)
        if days_back == 0 and time(hour, minute) > reference.timetz().replace(tzinfo=None):
            candidate -= timedelta(days=7)
        return candidate

    numbers = [int(value) for value in re.findall(r"\d+", normalized)]
    try:
        if len(numbers) == 3:
            return date(numbers[0], numbers[1], numbers[2])
        if len(numbers) == 2:
            candidate = date(reference.year, numbers[0], numbers[1])
            if candidate > reference.date():
                candidate = date(reference.year - 1, numbers[0], numbers[1])
            return candidate
    except ValueError:
        return None
    return None


def _weekday_number(label: str) -> int | None:
    if label in WEEKDAYS:
        return WEEKDAYS[label]
    for prefix in ("星期", "周"):
        if label.startswith(prefix) and label[len(prefix) :] in WEEKDAYS:
            return WEEKDAYS[label[len(prefix) :]]
    return None
