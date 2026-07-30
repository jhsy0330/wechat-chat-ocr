import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image
from PySide6.QtCore import QRectF
from PySide6.QtWidgets import QApplication

from wechat_archive.models import ArchivedMessage, Message
from wechat_archive.storage import ArchiveStore
from wechat_archive.viewer import (
    ArchiveViewerPage,
    ScreenshotViewer,
    message_highlight_rect,
)


def make_message(text: str, source: str, occurred_at: str) -> Message:
    return Message(
        speaker="联系人",
        text=text,
        confidence=0.93,
        source=source,
        x=0.25,
        y=0.20,
        width=0.50,
        height=0.10,
        occurred_at=occurred_at,
    )


def application() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_maps_normalized_ocr_box_to_painted_image() -> None:
    message = make_message("文字", "page.png", "2026-07-30T20:00+08:00")

    result = message_highlight_rect(message, QRectF(10, 20, 800, 600))

    assert result == QRectF(210, 140, 400, 60)


def test_screenshot_viewer_loads_source_and_highlights_message(tmp_path: Path) -> None:
    application()
    source = tmp_path / "page.png"
    Image.new("RGB", (800, 600), "white").save(source)
    message = make_message("文字", source.name, "2026-07-30T20:00+08:00")
    record = ArchivedMessage(1, "联系人", message, source)
    viewer = ScreenshotViewer()
    viewer.resize(424, 324)

    assert viewer.show_record(record)
    image_rect = viewer.image_rect()
    highlight = viewer.highlight_rect()
    assert not image_rect.isEmpty()
    assert highlight == message_highlight_rect(message, image_rect)


def test_viewer_search_selects_the_matching_source(tmp_path: Path) -> None:
    application()
    database = tmp_path / "archive.sqlite3"
    session = tmp_path / "captures" / "session-1"
    session.mkdir(parents=True)
    Image.new("RGB", (800, 600), "white").save(session / "page-001.png")
    Image.new("RGB", (800, 600), "gray").save(session / "page-002.png")
    store = ArchiveStore(database)
    store.append_session(
        "联系人",
        [
            make_message("普通消息", "page-001.png", "2026-07-30T19:00+08:00"),
            make_message("需要核对", "page-002.png", "2026-07-30T20:00+08:00"),
        ],
        2,
        session,
    )
    page = ArchiveViewerPage(database)
    page.message_search.setText("核对")

    assert page.chat_list.count() == 1
    assert page.message_table.rowCount() == 1
    assert page.current_record is not None
    assert page.current_record.message.text == "需要核对"
    assert page.current_record.source_path == (session / "page-002.png").resolve()
    assert page.open_source_button.isEnabled()


def test_viewer_uses_database_pagination(tmp_path: Path, monkeypatch) -> None:
    application()
    monkeypatch.setattr("wechat_archive.viewer.PAGE_SIZE", 1)
    database = tmp_path / "archive.sqlite3"
    store = ArchiveStore(database)
    store.append_session(
        "联系人",
        [
            make_message("较早", "page-001.png", "2026-07-30T19:00+08:00"),
            make_message("较新", "page-002.png", "2026-07-30T20:00+08:00"),
        ],
        2,
        tmp_path / "missing-session",
    )

    page = ArchiveViewerPage(database)

    assert page.total_messages == 2
    assert page.message_table.rowCount() == 1
    assert page.current_record is not None
    assert page.current_record.message.text == "较新"
    assert not page.open_source_button.isEnabled()
    page._next_page()
    assert page.current_record is not None
    assert page.current_record.message.text == "较早"
