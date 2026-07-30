import json
import sqlite3
from pathlib import Path

from wechat_archive.exporter import export_archive
from wechat_archive.models import Message
from wechat_archive.storage import ArchiveStore


def message(text: str, sequence: int = 0) -> Message:
    return Message(
        "女朋友",
        text,
        0.9,
        "page.png",
        0.1,
        0.1,
        0.2,
        0.03,
        visible_time="20:00",
        sequence=sequence,
    )


def test_incremental_storage_and_export(tmp_path: Path) -> None:
    store = ArchiveStore(tmp_path / "archive.sqlite3")
    all_messages, added = store.append_session(
        "女朋友", [message("一"), message("二")], 1, tmp_path / "session-1"
    )
    assert added == 2
    assert len(all_messages) == 2

    all_messages, added = store.append_session(
        "女朋友",
        [message("一"), message("二"), message("三")],
        2,
        tmp_path / "session-2",
    )
    assert added == 1
    assert [item.text for item in all_messages] == ["一", "二", "三"]

    all_messages[0].occurred_at = "2026-07-30T20:00+08:00"
    html, markdown, data = export_archive(
        all_messages, "女朋友", tmp_path / "exports" / "chat"
    )
    assert "2026-07-30 20:00" in html.read_text(encoding="utf-8")
    assert "2026-07-30 20:00" in markdown.read_text(encoding="utf-8")
    exported = json.loads(data.read_text(encoding="utf-8"))
    assert len(exported) == 3
    assert exported[0]["occurred_at"] == "2026-07-30T20:00+08:00"


def test_existing_image_ocr_can_be_hidden_without_deleting_database(
    tmp_path: Path,
) -> None:
    store = ArchiveStore(tmp_path / "archive.sqlite3")
    store.append_session(
        "女朋友", [message("正常文字"), message("图片文字")], 1, tmp_path / "session-1"
    )
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE messages SET is_visible = NULL")
    visible, _added = store.append_session(
        "女朋友",
        [],
        0,
        tmp_path / "session-2",
        existing_message_filter=lambda item, _path: item.text != "图片文字",
    )
    assert [item.text for item in visible] == ["正常文字"]
    assert len(store.load_messages("女朋友")) == 2
    assert [record.message.text for record in store.load_chat_messages("女朋友")] == [
        "正常文字"
    ]
    assert store.list_chats()[0].message_count == 1

    def must_not_revalidate(_item: Message, _path: Path) -> bool:
        raise AssertionError("persisted visibility was revalidated")

    visible, _added = store.append_session(
        "女朋友",
        [],
        0,
        tmp_path / "session-3",
        existing_message_filter=must_not_revalidate,
    )
    assert [item.text for item in visible] == ["正常文字"]


def test_existing_database_adds_occurred_at_column(tmp_path: Path) -> None:
    database = tmp_path / "old.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE chats (
                id INTEGER PRIMARY KEY,
                partner_name TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            );
            CREATE TABLE sessions (
                id INTEGER PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                started_at TEXT NOT NULL,
                page_count INTEGER NOT NULL,
                session_dir TEXT NOT NULL
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                session_id INTEGER NOT NULL,
                sequence INTEGER NOT NULL,
                speaker TEXT NOT NULL,
                text TEXT NOT NULL,
                confidence REAL NOT NULL,
                source TEXT NOT NULL,
                x REAL NOT NULL,
                y REAL NOT NULL,
                width REAL NOT NULL,
                height REAL NOT NULL,
                visible_time TEXT,
                kind TEXT NOT NULL,
                UNIQUE(chat_id, sequence)
            );
            """
        )
    ArchiveStore(database)
    with sqlite3.connect(database) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(messages)")}
    assert {"occurred_at", "is_visible"} <= columns
