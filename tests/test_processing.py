from datetime import datetime
from zoneinfo import ZoneInfo

from wechat_archive.models import Message, OCRLine
from wechat_archive.processing import merge_capture_pages, overlap_length, parse_page


def ocr(text: str, x: float, y: float, width: float = 0.15) -> OCRLine:
    return OCRLine(text, 0.95, x, y, width, 0.03, "page.png")


def message(text: str, speaker: str = "女朋友") -> Message:
    return Message(speaker, text, 0.95, "page.png", 0.1, 0.1, 0.2, 0.03)


def test_parse_sides_and_visible_time() -> None:
    reference = datetime(2026, 7, 30, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    messages = parse_page(
        [
            ocr("昨天 20:15", 0.42, 0.05),
            ocr("到家了吗", 0.10, 0.20),
            ocr("刚到", 0.72, 0.30),
        ],
        "女朋友",
        reference_time=reference,
    )
    assert [item.speaker for item in messages] == ["系统", "女朋友", "我"]
    assert messages[1].visible_time == "昨天 20:15"
    assert messages[1].occurred_at == "2026-07-29T20:15+08:00"


def test_multiline_message_uses_union_of_ocr_boxes() -> None:
    messages = parse_page(
        [
            ocr("第一行", 0.12, 0.20, 0.18),
            OCRLine("第二行更长", 0.95, 0.10, 0.232, 0.32, 0.03, "page.png"),
        ],
        "女朋友",
    )

    assert len(messages) == 1
    assert messages[0].text == "第一行\n第二行更长"
    assert messages[0].x == 0.10
    assert messages[0].y == 0.20
    assert round(messages[0].width, 3) == 0.32
    assert round(messages[0].height, 3) == 0.062


def test_overlap_uses_speaker_and_text() -> None:
    left = [message("早上好"), message("吃饭了吗"), message("吃了", "我")]
    right = [message("吃饭了吗"), message("吃了", "我"), message("早点休息")]
    assert overlap_length(left, right) == 2


def test_merge_pages_captured_newest_first() -> None:
    newest = [message("三"), message("四"), message("五")]
    oldest = [message("一"), message("二"), message("三")]
    merged = merge_capture_pages([newest, oldest])
    assert [item.text for item in merged] == ["一", "二", "三", "四", "五"]
    assert [item.sequence for item in merged] == [1, 2, 3, 4, 5]
