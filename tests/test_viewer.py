import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image
from PySide6.QtCore import QRectF, Qt
from PySide6.QtWidgets import QApplication, QMessageBox

from wechat_archive.models import ArchivedMessage, Message
from wechat_archive.storage import ArchiveStore
from wechat_archive.ui import MainWindow
from wechat_archive.viewer import (
    ArchiveViewerPage,
    EditMessageDialog,
    ExportDialog,
    ReviewQueuePage,
    RevisionHistoryDialog,
    ScreenshotViewer,
    message_highlight_rect,
    _message_status,
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


def test_voice_message_text_is_read_only_and_status_shows_duration() -> None:
    application()
    message = make_message("[语音消息]", "voice.png", "2026-07-30T20:00+08:00")
    message.kind = "voice"
    message.voice_duration_seconds = 8
    record = ArchivedMessage(1, "联系人", message, Path("voice.png"))

    dialog = EditMessageDialog(record)

    assert dialog.text_edit.isReadOnly()
    assert dialog.text_edit.toPlainText() == "[语音消息]"
    assert _message_status(message).startswith("语音 · 8 秒")


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


def test_viewer_can_show_deleted_and_switch_archive_root(tmp_path: Path) -> None:
    application()
    first_database = tmp_path / "first" / "archive.sqlite3"
    first = ArchiveStore(first_database)
    first.append_session(
        "联系人甲",
        [make_message("待删除", "page.png", "2026-07-30T20:00+08:00")],
        1,
        tmp_path / "first" / "captures" / "one",
    )
    record = first.load_chat_messages("联系人甲")[0]
    first.set_message_deleted(record.message_id, True)
    second_database = tmp_path / "second" / "archive.sqlite3"
    second = ArchiveStore(second_database)
    second.append_session(
        "联系人乙",
        [make_message("另一份档案", "page.png", "2026-07-30T21:00+08:00")],
        1,
        tmp_path / "second" / "captures" / "one",
    )

    page = ArchiveViewerPage(first_database)
    assert page.message_table.rowCount() == 0
    page.deleted_filter.setChecked(True)
    assert page.message_table.rowCount() == 1
    assert page.message_table.item(0, 3).text().startswith("已删除")
    page.set_database_path(second_database)
    assert page.chat_list.item(0).data(Qt.ItemDataRole.UserRole) == "联系人乙"


def test_main_window_archive_switch_updates_review_page(
    tmp_path: Path, monkeypatch
) -> None:
    application()
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()

    class FakeSettings:
        values: dict[str, str] = {"archive/root": str(first_root)}

        def value(self, key: str, default=None):
            return self.values.get(key, default)

        def setValue(self, key: str, value) -> None:
            self.values[key] = str(value)

    monkeypatch.setattr("wechat_archive.ui.QSettings", FakeSettings)
    monkeypatch.setattr("wechat_archive.ui.macos.list_wechat_windows", lambda: [])
    monkeypatch.setattr(
        "wechat_archive.ui.QFileDialog.getExistingDirectory",
        lambda *_args: str(second_root),
    )
    window = MainWindow()

    window.choose_archive_root()

    expected = second_root / "archive.sqlite3"
    assert window.viewer_page.database_path == expected
    assert window.review_page.database_path == expected
    assert window.capture_page.data_dir == second_root


def test_export_dialog_disables_selected_scope_when_nothing_selected() -> None:
    application()
    dialog = ExportDialog(0)
    selected_item = dialog.scope_combo.model().item(2)
    assert not selected_item.isEnabled()
    assert dialog.formats()
    assert dialog.fields()


def test_export_dialog_scope_is_set_by_current_entry_point() -> None:
    application()
    dialog = ExportDialog(3, initial_scope="filtered")
    assert dialog.scope() == "filtered"
    assert dialog.scope_combo.isEnabled()

    contact_dialog = ExportDialog(0, initial_scope="all", fixed_scope="all")
    assert contact_dialog.scope() == "all"
    assert not contact_dialog.scope_combo.isEnabled()

    selected_dialog = ExportDialog(3, initial_scope="selected", fixed_scope="selected")
    assert selected_dialog.scope() == "selected"
    assert not selected_dialog.scope_combo.isEnabled()


def test_contact_context_menu_exports_all_visible_records(
    tmp_path: Path, monkeypatch
) -> None:
    application()
    store = ArchiveStore(tmp_path / "archive.sqlite3")
    store.append_session(
        "联系人",
        [make_message("文字", "page.png", "2026-07-30T20:00+08:00")],
        1,
        tmp_path / "captures" / "one",
    )
    page = ArchiveViewerPage(store.path)
    assert page.chat_list.contextMenuPolicy() == Qt.ContextMenuPolicy.CustomContextMenu
    assert (
        page.message_table.contextMenuPolicy() == Qt.ContextMenuPolicy.CustomContextMenu
    )
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(page, "_export_records", lambda **kwargs: calls.append(kwargs))

    menu = page._build_chat_context_menu("联系人")
    assert [action.text() for action in menu.actions()] == ["导出该联系人的全部聊天记录"]
    menu.actions()[0].trigger()
    assert calls == [
        {
            "forced_scope": "all",
            "partner_name": "联系人",
            "include_deleted": False,
        }
    ]


def test_message_context_menu_has_current_and_multi_select_actions(
    tmp_path: Path, monkeypatch
) -> None:
    application()
    store = ArchiveStore(tmp_path / "archive.sqlite3")
    store.append_session(
        "联系人",
        [
            make_message("第一条", "one.png", "2026-07-30T20:00+08:00"),
            make_message("第二条", "two.png", "2026-07-30T20:01+08:00"),
        ],
        1,
        tmp_path / "captures" / "one",
    )
    page = ArchiveViewerPage(store.path)
    records = page.page_records
    deleted_calls: list[tuple[list[int], bool]] = []
    monkeypatch.setattr(
        page,
        "_set_records_deleted",
        lambda selected, deleted: deleted_calls.append(
            ([record.message_id for record in selected], deleted)
        ),
    )

    menu = page._build_message_context_menu(records[0], records)
    labels = [action.text() for action in menu.actions() if not action.isSeparator()]
    assert labels == [
        "修改记录",
        "查看修订历史",
        "查看对应截图",
        "打开原始截图",
        "删除记录",
        "删除所选记录（2 条）",
        "导出当前记录",
        "导出所选记录（2 条）",
    ]
    next(
        action for action in menu.actions() if action.text().startswith("删除所选")
    ).trigger()
    assert deleted_calls == [([record.message_id for record in records], True)]


def test_export_all_ignores_message_filters_and_pagination(tmp_path: Path) -> None:
    application()
    store = ArchiveStore(tmp_path / "archive.sqlite3")
    store.append_session(
        "联系人",
        [
            make_message("苹果", "one.png", "2026-07-30T20:00+08:00"),
            make_message("香蕉", "two.png", "2026-07-30T20:01+08:00"),
        ],
        1,
        tmp_path / "captures" / "one",
    )
    page = ArchiveViewerPage(store.path)
    page.message_search.setText("苹果")

    filtered = page._records_for_export("联系人", "filtered", [], include_deleted=False)
    all_records = page._records_for_export("联系人", "all", [], include_deleted=False)
    assert [record.message.text for record in filtered] == ["苹果"]
    assert [record.message.text for record in all_records] == ["苹果", "香蕉"]


def test_review_queue_page_lists_abnormal_messages_and_conflicts(
    tmp_path: Path,
) -> None:
    application()
    database = tmp_path / "archive.sqlite3"
    store = ArchiveStore(database)
    abnormal = make_message("低置信度", "missing.png", "")
    abnormal.confidence = 0.4
    abnormal.occurred_at = None
    abnormal.occurred_date = None
    abnormal.date_source = "unresolved"
    abnormal.x = 0.45
    abnormal.width = 0.1
    store.append_session("联系人", [abnormal], 1, tmp_path / "captures" / "one")
    store.append_session_checked(
        "联系人",
        [make_message("没有锚点", "other.png", "2026-07-29T10:00")],
        1,
        tmp_path / "captures" / "two",
    )

    page = ReviewQueuePage(database)

    assert page.tabs.count() == 2
    assert page.review_table.rowCount() == 1
    assert "低置信度" in page.review_table.item(0, 1).text()
    assert page.conflict_table.rowCount() == 1
    assert not page.merge_conflict_button.isEnabled()


def test_revision_history_dialog_restores_selected_version(
    tmp_path: Path, monkeypatch
) -> None:
    application()
    store = ArchiveStore(tmp_path / "archive.sqlite3")
    store.append_session(
        "联系人",
        [make_message("OCR 原文", "page.png", "2026-07-30T20:00")],
        1,
        tmp_path / "captures" / "one",
    )
    record = store.load_chat_messages("联系人")[0]
    store.update_message(
        record.message_id,
        text="修改后",
        speaker="联系人",
        occurred_date="2026-07-30",
        occurred_time="20:10",
    )
    current = store.load_chat_messages("联系人")[0]
    dialog = RevisionHistoryDialog(store, current)
    monkeypatch.setattr(
        "wechat_archive.viewer.QMessageBox.question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes,
    )

    assert dialog.table.rowCount() == 2
    assert "语音时长" not in dialog.detail.toPlainText()
    dialog.table.setCurrentCell(0, 0)
    dialog._restore_version()

    assert dialog.restored
    assert store.load_chat_messages("联系人")[0].message.text == "OCR 原文"
    assert store.load_message_revisions(record.message_id)[-1]["action"] == "revert"
