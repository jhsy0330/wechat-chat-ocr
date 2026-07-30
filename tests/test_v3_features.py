from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from openpyxl import load_workbook

from wechat_archive.exporter import export_records
from wechat_archive.models import ArchivedMessage, Message
from wechat_archive.processing import merge_capture_pages
from wechat_archive.storage import ArchiveStore


def message(
    text: str,
    *,
    source: str = "page-001.png",
    occurred_date: str | None = None,
    occurred_at: str | None = None,
    date_source: str = "unresolved",
) -> Message:
    return Message(
        speaker="我",
        text=text,
        confidence=0.91,
        source=source,
        x=0.5,
        y=0.2,
        width=0.2,
        height=0.08,
        occurred_date=occurred_date,
        occurred_at=occurred_at,
        date_source=date_source,
        original_text=text,
    )


def test_edit_delete_restore_and_revision_history(tmp_path: Path) -> None:
    root = tmp_path / "archive"
    session = root / "captures" / "one"
    session.mkdir(parents=True)
    store = ArchiveStore(root / "archive.sqlite3")
    store.append_session("小明", [message("OCR文字")], 1, session)
    record = store.load_chat_messages("小明")[0]

    store.update_message(
        record.message_id,
        text="修正文字",
        speaker="小明",
        occurred_date="2026-07-29",
        occurred_time="20:15",
    )
    edited = store.load_chat_messages("小明")[0]
    assert edited.message.text == "修正文字"
    assert edited.message.original_text == "OCR文字"
    assert edited.message.date_source == "manual"
    assert edited.message.occurred_at == "2026-07-29T20:15"

    store.set_message_deleted(record.message_id, True)
    assert store.count_chat_messages("小明") == 0
    deleted = store.load_chat_messages("小明", include_deleted=True)[0]
    assert deleted.message.is_deleted
    store.set_message_deleted(record.message_id, False)
    assert store.count_chat_messages("小明") == 1
    assert [revision["action"] for revision in store.load_message_revisions(record.message_id)] == [
        "edit",
        "delete",
        "restore",
    ]


def test_batch_soft_delete_is_atomic_and_records_each_revision(tmp_path: Path) -> None:
    store = ArchiveStore(tmp_path / "archive.sqlite3")
    store.append_session(
        "小明",
        [message("第一条"), message("第二条")],
        1,
        tmp_path / "captures" / "one",
    )
    records = store.load_chat_messages("小明")
    message_ids = [record.message_id for record in records]

    store.set_messages_deleted(message_ids, True)

    assert store.count_chat_messages("小明") == 0
    assert store.count_chat_messages("小明", include_deleted=True) == 2
    for message_id in message_ids:
        revisions = store.load_message_revisions(message_id)
        assert [revision["action"] for revision in revisions] == ["delete"]

    store.set_messages_deleted(message_ids, True)
    for message_id in message_ids:
        assert len(store.load_message_revisions(message_id)) == 1

    store.set_messages_deleted(message_ids, False)
    with pytest.raises(ValueError, match="聊天记录不存在"):
        store.set_messages_deleted([message_ids[0], 999999], True)
    assert store.count_chat_messages("小明") == 2


def test_legacy_schema_and_session_path_are_migrated(tmp_path: Path) -> None:
    root = tmp_path / "archive"
    root.mkdir()
    database = root / "archive.sqlite3"
    session = root / "captures" / "legacy"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE chats(id INTEGER PRIMARY KEY, partner_name TEXT UNIQUE, created_at TEXT);
            CREATE TABLE sessions(id INTEGER PRIMARY KEY, chat_id INTEGER, started_at TEXT, page_count INTEGER, session_dir TEXT);
            CREATE TABLE messages(
                id INTEGER PRIMARY KEY, chat_id INTEGER, session_id INTEGER,
                sequence INTEGER, speaker TEXT, text TEXT, confidence REAL,
                source TEXT, x REAL, y REAL, width REAL, height REAL,
                visible_time TEXT, occurred_at TEXT, kind TEXT, is_visible INTEGER,
                UNIQUE(chat_id, sequence)
            );
            """
        )
        connection.execute("INSERT INTO chats VALUES (1, '小明', '2026-07-30')")
        connection.execute(
            "INSERT INTO sessions VALUES (1, 1, '2026-07-30T12:00:00', 1, ?)",
            (str(session),),
        )
        connection.execute(
            "INSERT INTO messages VALUES (1,1,1,1,'我','旧文字',0.8,'page.png',0,0,1,1,NULL,'2026-07-29T12:00:00','text',1)"
        )

    store = ArchiveStore(database)
    migrated = store.load_chat_messages("小明")[0]
    assert migrated.message.original_text == "旧文字"
    assert migrated.message.occurred_date == "2026-07-29"
    assert migrated.message.date_source == "recognized"
    with sqlite3.connect(database) as connection:
        stored_session = connection.execute("SELECT session_dir FROM sessions").fetchone()[0]
        revision_table = connection.execute(
            "SELECT name FROM sqlite_master WHERE name = 'message_revisions'"
        ).fetchone()
    assert stored_session == "captures/legacy"
    assert revision_table is not None


def test_both_capture_orders_forward_fill_dates_without_inventing_time() -> None:
    oldest = [message("开头"), message("日期锚点", occurred_date="2026-07-28", date_source="recognized")]
    newest = [message("后续消息")]

    upward = merge_capture_pages([newest, oldest], direction="up")
    downward = merge_capture_pages([oldest, newest], direction="down")
    for merged in (upward, downward):
        assert [item.text for item in merged] == ["开头", "日期锚点", "后续消息"]
        assert merged[0].occurred_date is None
        assert merged[0].date_source == "unresolved"
        assert merged[2].occurred_date == "2026-07-28"
        assert merged[2].occurred_at is None
        assert merged[2].date_source == "inherited"


def test_selectable_export_fields_and_four_formats(tmp_path: Path) -> None:
    root = tmp_path / "portable"
    screenshot = root / "captures" / "one" / "page.png"
    screenshot.parent.mkdir(parents=True)
    screenshot.write_bytes(b"not copied")
    item = message(
        "你好",
        occurred_date="2026-07-30",
        occurred_at="2026-07-30T20:15",
        date_source="recognized",
    )
    item.sequence = 3
    record = ArchivedMessage(7, "小明", item, screenshot)
    fields = ["date", "speaker", "text", "screenshot_path"]

    outputs = export_records(
        [record],
        "小明",
        root / "exports" / "chat",
        formats={"json", "markdown", "xlsx", "html"},
        fields=fields,
        archive_root=root,
    )

    assert set(outputs) == {"json", "markdown", "xlsx", "html"}
    exported = json.loads(outputs["json"].read_text(encoding="utf-8"))
    assert exported == [{
        "date": "2026-07-30",
        "speaker": "我",
        "text": "你好",
        "screenshot_path": "captures/one/page.png",
    }]
    assert "OCR 原始文字" not in outputs["markdown"].read_text(encoding="utf-8")
    assert "data:image" not in outputs["html"].read_text(encoding="utf-8")
    workbook = load_workbook(outputs["xlsx"])
    assert [cell.value for cell in workbook.active[1]] == ["日期", "发送人", "修正后文字", "截图路径"]
