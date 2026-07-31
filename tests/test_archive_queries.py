import sqlite3
from pathlib import Path

import pytest

from wechat_archive.models import Message
from wechat_archive.storage import ArchiveStore


def message(
    text: str,
    source: str = "page-001.png",
    occurred_at: str | None = None,
    visible_time: str | None = None,
) -> Message:
    return Message(
        speaker="联系人",
        text=text,
        confidence=0.95,
        source=source,
        x=0.1,
        y=0.2,
        width=0.3,
        height=0.04,
        visible_time=visible_time,
        occurred_at=occurred_at,
    )


def test_lists_multiple_chats_by_latest_time(tmp_path: Path) -> None:
    store = ArchiveStore(tmp_path / "archive.sqlite3")
    store.append_session(
        "联系人甲",
        [message("甲一", occurred_at="2026-07-28T09:00+08:00")],
        1,
        tmp_path / "session-a",
    )
    store.append_session(
        "联系人乙",
        [
            message("乙一", occurred_at="2026-07-29T08:00+08:00"),
            message("乙二", occurred_at="2026-07-30T20:30+08:00"),
        ],
        1,
        tmp_path / "session-b",
    )
    store.append_session("联系人丙", [], 0, tmp_path / "session-c")

    summaries = store.list_chats()

    assert [summary.partner_name for summary in summaries] == [
        "联系人乙",
        "联系人甲",
        "联系人丙",
    ]
    assert [summary.message_count for summary in summaries] == [2, 1, 0]
    assert summaries[0].latest_occurred_at == "2026-07-30T20:30+08:00"
    assert summaries[2].latest_occurred_at is None


def test_load_chat_messages_resolves_source_per_session(tmp_path: Path) -> None:
    store = ArchiveStore(tmp_path / "archive.sqlite3")
    first_session = tmp_path / "captures" / "session-1"
    second_session = tmp_path / "captures" / "session-2"
    first_session.mkdir(parents=True)
    second_session.mkdir(parents=True)
    (first_session / "page-001.png").touch()
    (second_session / "page-001.png").touch()

    store.append_session(
        "联系人",
        [message("第一条")],
        1,
        first_session,
    )
    store.append_session(
        "联系人",
        [message("第二条")],
        1,
        second_session,
    )

    records = store.load_chat_messages("联系人")

    assert [record.message.sequence for record in records] == [1, 2]
    assert [record.source_path for record in records] == [
        (first_session / "page-001.png").resolve(),
        (second_session / "page-001.png").resolve(),
    ]
    assert all(record.source_path.is_absolute() for record in records)
    assert store.load_chat_messages("不存在") == []


def test_chat_summary_derives_time_from_migrated_database(tmp_path: Path) -> None:
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
            INSERT INTO chats VALUES (1, '旧联系人', '2026-07-30T21:00:00+08:00');
            INSERT INTO sessions VALUES (
                1, 1, '2026-07-30T21:00:00+08:00', 1, 'captures/session-old'
            );
            INSERT INTO messages VALUES (
                1, 1, 1, 1, '旧联系人', '旧消息', 0.9, 'page-001.png',
                0.1, 0.2, 0.3, 0.04, 'Yesterday 20:15', 'text'
            );
            """
        )

    store = ArchiveStore(database)

    assert store.unreviewed_message_count() == 1
    assert store.list_chats()[0].message_count == 0
    pending = store.load_unreviewed_messages()
    store.set_message_visibility([(pending[0].message_id, True)])

    assert store.list_chats()[0].latest_occurred_at == "2026-07-29T20:15+08:00"
    assert store.load_messages("旧联系人")[0].text == "旧消息"


def test_visible_message_query_supports_search_speaker_and_pagination(
    tmp_path: Path,
) -> None:
    store = ArchiveStore(tmp_path / "archive.sqlite3")
    messages = [
        Message(
            speaker="我" if index % 2 else "联系人",
            text=f"第 {index:03d} 条" + (" 目标" if index in {5, 205} else ""),
            confidence=0.95,
            source=f"page-{index:03d}.png",
            x=0.1,
            y=0.2,
            width=0.3,
            height=0.04,
            occurred_at=f"2026-07-30T{index % 24:02d}:00+08:00",
        )
        for index in range(1, 211)
    ]
    store.append_session("联系人", messages, 210, tmp_path / "session")

    first_page = store.load_chat_messages("联系人", limit=200, offset=0, newest_first=True)
    second_page = store.load_chat_messages(
        "联系人", limit=200, offset=200, newest_first=True
    )

    assert len(first_page) == 200
    assert len(second_page) == 10
    assert first_page[0].message.sequence == 210
    assert second_page[-1].message.sequence == 1
    assert store.count_chat_messages("联系人", query="目标") == 2
    assert store.count_chat_messages("联系人", query="目标", speaker="我") == 2
    assert store.count_chat_messages("联系人", query="目标", speaker="联系人") == 0


def test_delete_chat_removes_related_database_rows_and_capture_files(
    tmp_path: Path,
) -> None:
    database = tmp_path / "archive.sqlite3"
    deleted_session = tmp_path / "captures" / "deleted"
    kept_session = tmp_path / "captures" / "kept"
    deleted_session.mkdir(parents=True)
    kept_session.mkdir(parents=True)
    (deleted_session / "page-001.png").touch()
    (deleted_session / "page-ocr.json").touch()
    (kept_session / "page-001.png").touch()
    export = tmp_path / "exports" / "联系人.json"
    export.parent.mkdir()
    export.write_text("[]", encoding="utf-8")

    store = ArchiveStore(database)
    store.append_session(
        "联系人",
        [
            message("第一条", source="page-001.png"),
            message("第二条", source="page-001.png"),
        ],
        1,
        deleted_session,
    )
    store.append_session(
        "保留联系人", [message("保留", source="page-001.png")], 1, kept_session
    )
    record = store.load_chat_messages("联系人")[0]
    store.update_message(
        record.message_id,
        text="已修改",
        speaker=record.message.speaker,
        occurred_date=None,
        occurred_time=None,
    )
    store.set_review_status([record.message_id], "confirmed")
    with sqlite3.connect(database) as connection:
        chat_id, session_id = connection.execute(
            """
            SELECT c.id, s.id FROM chats c JOIN sessions s ON s.chat_id = c.id
            WHERE c.partner_name = '联系人'
            """
        ).fetchone()
        connection.execute(
            """
            INSERT INTO capture_pages(
                session_id, page_number, source, sha256, perceptual_hash, created_at
            ) VALUES (?, 1, 'page-001.png', 'sha', 'hash', '2026-07-31T00:00:00')
            """,
            (session_id,),
        )
        connection.execute(
            """
            INSERT INTO capture_conflicts(
                chat_id, session_id, kind, details_json, status, created_at
            ) VALUES (?, ?, 'missing_anchor', '{}', 'pending', '2026-07-31T00:00:00')
            """,
            (chat_id, session_id),
        )

    preview = store.chat_deletion_preview("联系人")
    assert preview.message_count == 2
    assert preview.session_count == 1
    assert preview.screenshot_file_count == 2

    deleted = store.delete_chat("联系人")

    assert deleted == preview
    assert not deleted_session.exists()
    assert kept_session.is_dir()
    assert export.is_file()
    assert [chat.partner_name for chat in store.list_chats()] == ["保留联系人"]
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM message_revisions").fetchone()[0]
            == 0
        )
        assert connection.execute("SELECT COUNT(*) FROM message_reviews").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM capture_pages").fetchone()[0] == 0
        assert (
            connection.execute("SELECT COUNT(*) FROM capture_conflicts").fetchone()[0]
            == 0
        )
    assert not list(tmp_path.glob("archive-pre-v*-*.sqlite3"))


def test_delete_chat_rejects_session_directory_outside_captures(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "legacy-session"
    outside.mkdir()
    (outside / "page.png").touch()
    store = ArchiveStore(tmp_path / "archive.sqlite3")
    store.append_session("联系人", [message("文字")], 1, outside)

    with pytest.raises(ValueError, match="不在归档 captures 目录内"):
        store.delete_chat("联系人")

    assert outside.is_dir()
    assert store.list_chats()[0].partner_name == "联系人"
