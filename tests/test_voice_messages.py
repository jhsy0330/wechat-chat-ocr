from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw

from wechat_archive.content_filter import (
    TextMessageFilter,
    parse_voice_duration,
)
from wechat_archive.exporter import export_records
from wechat_archive.fingerprints import message_fingerprint
from wechat_archive.models import ArchivedMessage, Message, OCRLine
from wechat_archive.processing import parse_page
from wechat_archive.storage import ArchiveStore


def draw_voice_page(path: Path) -> None:
    image = Image.new("RGB", (800, 600), (245, 245, 245))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((500, 180, 680, 230), radius=12, fill=(149, 236, 105))
    for inset in (0, 6, 12):
        draw.arc(
            (632 + inset, 188 + inset // 2, 674 - inset, 222 - inset // 2),
            start=118,
            end=242,
            fill=(35, 40, 35),
            width=3,
        )
    image.save(path)


def draw_incoming_voice_page(path: Path) -> None:
    image = Image.new("RGB", (800, 600), (245, 245, 245))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((110, 260, 300, 310), radius=12, fill=(230, 230, 230))
    for inset in (0, 6, 12):
        draw.arc(
            (120 + inset, 268 + inset // 2, 162 - inset, 302 - inset // 2),
            start=298,
            end=62,
            fill=(35, 40, 35),
            width=3,
        )
    image.save(path)


def duration_line(source: str = "page.png") -> OCRLine:
    return OCRLine('5"', 0.94, 0.55, 0.315, 0.04, 0.045, source)


def test_voice_duration_parser_accepts_wechat_variants() -> None:
    assert parse_voice_duration('5"') == 5
    assert parse_voice_duration("12秒") == 12
    assert parse_voice_duration("8″") == 8
    assert parse_voice_duration('$ 8"') == 8
    assert parse_voice_duration('10"（') == 10
    assert parse_voice_duration('25"(') == 25
    assert parse_voice_duration("60s") == 60
    assert parse_voice_duration("61秒") is None
    assert parse_voice_duration("12:30") is None
    assert parse_voice_duration("5") is None
    assert parse_voice_duration("5", allow_bare=True) == 5


def test_detects_voice_bubble_and_suppresses_duration_ocr(tmp_path: Path) -> None:
    page = tmp_path / "page.png"
    draw_voice_page(page)
    line = duration_line(page.name)

    detections = TextMessageFilter(page).detect_voice_bubbles([line])

    assert len(detections) == 1
    voice = detections[0]
    assert voice.duration_seconds == 5
    assert voice.confidence >= 0.9
    assert line in voice.suppressed_lines
    assert voice.x == 0.625
    assert voice.source == page.name


def test_voice_icon_fallback_is_sent_to_review(tmp_path: Path) -> None:
    page = tmp_path / "page.png"
    draw_voice_page(page)

    detections = TextMessageFilter(page).detect_voice_bubbles([])

    assert len(detections) == 1
    assert detections[0].duration_seconds is None
    assert detections[0].confidence < 0.75


def test_detects_incoming_voice_and_assigns_partner(tmp_path: Path) -> None:
    page = tmp_path / "incoming.png"
    draw_incoming_voice_page(page)
    duration = OCRLine('11"', 0.96, 0.39, 0.442, 0.05, 0.045, page.name)
    detector = TextMessageFilter(page)
    voices = detector.detect_voice_bubbles([duration])

    messages = parse_page([duration], "联系人", voice_bubbles=voices)

    assert len(voices) == 1
    assert voices[0].duration_seconds == 11
    assert len(messages) == 1
    assert messages[0].speaker == "联系人"
    assert messages[0].text == "[语音消息]"


def test_short_text_bubble_is_not_misclassified_as_voice(tmp_path: Path) -> None:
    page = tmp_path / "text.png"
    image = Image.new("RGB", (800, 600), (245, 245, 245))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((520, 260, 650, 310), radius=12, fill=(149, 236, 105))
    draw.text((555, 274), "OK", fill=(30, 30, 30))
    image.save(page)
    text = OCRLine("OK", 0.98, 0.69, 0.455, 0.06, 0.035, page.name)

    assert not TextMessageFilter(page).detect_voice_bubbles([text])


def test_parse_page_emits_placeholder_with_time_and_not_duration_text(
    tmp_path: Path,
) -> None:
    page = tmp_path / "page.png"
    draw_voice_page(page)
    duration = duration_line(page.name)
    visible_time = OCRLine("Yesterday 20:15", 0.99, 0.42, 0.20, 0.16, 0.03, page.name)
    detector = TextMessageFilter(page)
    voices = detector.detect_voice_bubbles([visible_time, duration])

    messages = parse_page(
        [visible_time, duration],
        "联系人",
        reference_time=datetime.fromisoformat("2026-07-30T10:00:00+08:00"),
        voice_bubbles=voices,
    )

    voice_messages = [message for message in messages if message.kind == "voice"]
    assert len(voice_messages) == 1
    message = voice_messages[0]
    assert message.text == "[语音消息]"
    assert message.original_text == "[语音消息]"
    assert message.speaker == "我"
    assert message.occurred_date == "2026-07-29"
    assert message.voice_duration_seconds == 5
    assert duration.text not in [item.text for item in messages]


def test_voice_metadata_round_trips_and_exports_as_optional_field(
    tmp_path: Path,
) -> None:
    database = tmp_path / "archive.sqlite3"
    store = ArchiveStore(database)
    session = tmp_path / "captures" / "voice"
    session.mkdir(parents=True)
    screenshot = session / "page.png"
    draw_voice_page(screenshot)
    message = Message(
        speaker="我",
        text="[语音消息]",
        original_text="[语音消息]",
        confidence=0.96,
        source=screenshot.name,
        x=0.625,
        y=0.3,
        width=0.225,
        height=0.083,
        occurred_date="2026-07-30",
        date_source="recognized",
        kind="voice",
        voice_duration_seconds=5,
        voice_visual_hash="0123456789abcdef",
    )
    message.fingerprint = message_fingerprint(message)
    store.append_session("联系人", [message], 1, session)

    record = store.load_chat_messages("联系人")[0]
    loaded = record.message
    assert loaded.text == "[语音消息]"
    assert loaded.kind == "voice"
    assert loaded.voice_duration_seconds == 5
    assert loaded.voice_visual_hash == "0123456789abcdef"

    store.update_message(
        record.message_id,
        text="不应覆盖语音占位符",
        speaker="我",
        occurred_date="2026-07-30",
        occurred_time=None,
    )
    loaded = store.load_chat_messages("联系人")[0].message
    assert loaded.text == "[语音消息]"

    outputs = export_records(
        [ArchivedMessage(record.message_id, "联系人", loaded, screenshot)],
        "联系人",
        tmp_path / "exports" / "voice",
        formats={"json"},
        fields=["text", "voice_duration", "screenshot_path"],
        archive_root=tmp_path,
    )
    payload = json.loads(outputs["json"].read_text(encoding="utf-8"))
    assert payload == [
        {
            "text": "[语音消息]",
            "voice_duration": 5,
            "screenshot_path": "captures/voice/page.png",
        }
    ]


def test_uncertain_voice_without_duration_enters_review_queue(
    tmp_path: Path,
) -> None:
    store = ArchiveStore(tmp_path / "archive.sqlite3")
    session = tmp_path / "captures" / "uncertain"
    session.mkdir(parents=True)
    screenshot = session / "page.png"
    draw_voice_page(screenshot)
    message = Message(
        speaker="我",
        text="[语音消息]",
        confidence=0.70,
        source=screenshot.name,
        x=0.625,
        y=0.3,
        width=0.225,
        height=0.083,
        kind="voice",
        voice_visual_hash="0123456789abcdef",
    )
    store.append_session("联系人", [message], 1, session)

    review = store.list_review_records()[0]

    assert "uncertain_voice" in review.reasons
    assert "missing_voice_duration" in review.reasons


def test_voice_fingerprint_uses_duration_and_visual_hash() -> None:
    first = Message("我", "[语音消息]", 0.9, "a.png", 0.6, 0.2, 0.2, 0.05)
    first.kind = "voice"
    first.voice_duration_seconds = 3
    first.voice_visual_hash = "0000000000000000"
    second = Message("我", "[语音消息]", 0.9, "b.png", 0.6, 0.3, 0.2, 0.05)
    second.kind = "voice"
    second.voice_duration_seconds = 7
    second.voice_visual_hash = "ffffffffffffffff"

    assert message_fingerprint(first) != message_fingerprint(second)
