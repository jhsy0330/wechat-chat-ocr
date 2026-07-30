from __future__ import annotations

import re
from collections.abc import Callable
from collections.abc import Sequence
from datetime import datetime

from rapidfuzz.fuzz import ratio

from .models import Message, OCRLine
from .time_parser import is_wechat_time_label, parse_wechat_timestamp


SYSTEM_PATTERN = re.compile(r"以下为新消息|撤回了一条消息|你已添加了|以上是打招呼的内容")


def normalize_text(text: str) -> str:
    return re.sub(r"[\s\u200b]+", "", text).lower()


def is_system_line(line: OCRLine) -> bool:
    text = line.text.strip()
    center = line.x + line.width / 2
    centered = abs(center - 0.5) <= 0.13
    return bool(SYSTEM_PATTERN.search(text) or (centered and is_wechat_time_label(text)))


def parse_page(
    lines: Sequence[OCRLine],
    partner_name: str,
    minimum_confidence: float = 0.25,
    text_filter: Callable[[OCRLine], bool] | None = None,
    system_filter: Callable[[OCRLine], bool] | None = None,
    reference_time: datetime | None = None,
) -> list[Message]:
    messages: list[Message] = []
    visible_time: str | None = None
    occurred_at: str | None = None
    reference_time = reference_time or datetime.now().astimezone()
    for line in sorted(lines, key=lambda item: (item.y, item.x)):
        if line.confidence < minimum_confidence or not line.text.strip():
            continue
        system_line = is_system_line(line)
        if system_line and system_filter is not None and not system_filter(line):
            continue
        if not system_line and text_filter is not None and not text_filter(line):
            continue
        if system_line:
            speaker = "系统"
            parsed_time = parse_wechat_timestamp(line.text, reference_time)
            if parsed_time is not None:
                visible_time = line.text.strip()
                occurred_at = parsed_time.isoformat(timespec="minutes")
        else:
            center = line.x + line.width / 2
            speaker = "我" if center >= 0.5 else partner_name

        message = Message(
            speaker=speaker,
            text=line.text.strip(),
            confidence=line.confidence,
            source=line.source,
            x=line.x,
            y=line.y,
            width=line.width,
            height=line.height,
            visible_time=visible_time,
            occurred_at=occurred_at,
            kind="system" if speaker == "系统" else "text",
        )

        if messages and _should_join(messages[-1], message):
            previous = messages[-1]
            previous.text = f"{previous.text}\n{message.text}"
            previous.confidence = min(previous.confidence, message.confidence)
            previous.height = max(
                previous.height, message.y + message.height - previous.y
            )
        else:
            messages.append(message)
    return messages


def _should_join(previous: Message, current: Message) -> bool:
    if previous.speaker != current.speaker or current.speaker == "系统":
        return False
    vertical_gap = current.y - (previous.y + previous.height)
    same_anchor = abs(previous.x - current.x) <= 0.08
    return -0.005 <= vertical_gap <= 0.014 and same_anchor


def message_similarity(left: Message, right: Message) -> float:
    if left.speaker != right.speaker:
        return 0.0
    return ratio(normalize_text(left.text), normalize_text(right.text))


def overlap_length(
    left: Sequence[Message],
    right: Sequence[Message],
    *,
    allow_short_single: bool = False,
) -> int:
    maximum = min(len(left), len(right), 30)
    for size in range(maximum, 0, -1):
        comparisons = [
            message_similarity(a, b)
            for a, b in zip(left[-size:], right[:size], strict=True)
        ]
        if all(score >= 88 for score in comparisons):
            text_size = sum(len(normalize_text(item.text)) for item in right[:size])
            if size >= 2 or text_size >= 4 or allow_short_single:
                return size
    return 0


def merge_capture_pages(pages_newest_first: Sequence[Sequence[Message]]) -> list[Message]:
    merged: list[Message] = []
    for page in reversed(pages_newest_first):
        overlap = overlap_length(merged, page, allow_short_single=True)
        merged.extend(page[overlap:])
    current_visible_time: str | None = None
    current_occurred_at: str | None = None
    for sequence, message in enumerate(merged, 1):
        message.sequence = sequence
        if message.kind == "system" and message.occurred_at:
            current_visible_time = message.visible_time
            current_occurred_at = message.occurred_at
        elif message.kind != "system" and current_occurred_at:
            message.visible_time = current_visible_time
            message.occurred_at = current_occurred_at
    return merged
