from __future__ import annotations

from datetime import datetime
from pathlib import Path
from threading import Event

from PySide6.QtCore import QObject, QRectF, QSize, Qt, QThread, QUrl, Signal, Slot
from PySide6.QtGui import QColor, QDesktopServices, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSplitter,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .models import ArchivedMessage, ChatSummary, Message
from .storage import ArchiveStore
from .visibility import ArchiveVisibilityScanner


PAGE_SIZE = 200


def message_highlight_rect(message: Message, image_rect: QRectF) -> QRectF:
    """Map an OCR box normalized to its source image into a painted image rect."""
    return QRectF(
        image_rect.left() + message.x * image_rect.width(),
        image_rect.top() + message.y * image_rect.height(),
        message.width * image_rect.width(),
        message.height * image_rect.height(),
    )


class VisibilityWorker(QObject):
    progress = Signal(int, int)
    finished = Signal(int, int, bool)
    failed = Signal(str)

    def __init__(self, database_path: Path) -> None:
        super().__init__()
        self.database_path = database_path
        self._stop_event = Event()

    @Slot()
    def run(self) -> None:
        try:
            scanner = ArchiveVisibilityScanner(ArchiveStore(self.database_path))
            processed, total, complete = scanner.scan(
                on_progress=self.progress.emit,
                should_stop=self._stop_event.is_set,
            )
            self.finished.emit(processed, total, complete)
        except Exception as error:
            self.failed.emit(str(error))

    def stop(self) -> None:
        self._stop_event.set()


class ScreenshotViewer(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._pixmap = QPixmap()
        self._message: Message | None = None
        self._placeholder = "选择一条聊天记录查看来源截图"
        self.setMinimumSize(QSize(300, 360))
        self.setObjectName("screenshotViewer")

    def show_record(self, record: ArchivedMessage) -> bool:
        if not record.source_path.is_file():
            self.clear(f"来源截图不存在\n{record.source_path.name}")
            return False
        pixmap = QPixmap(str(record.source_path))
        if pixmap.isNull():
            self.clear(f"无法读取来源截图\n{record.source_path.name}")
            return False
        self._pixmap = pixmap
        self._message = record.message
        self.update()
        return True

    def clear(self, message: str = "选择一条聊天记录查看来源截图") -> None:
        self._pixmap = QPixmap()
        self._message = None
        self._placeholder = message
        self.update()

    def image_rect(self) -> QRectF:
        if self._pixmap.isNull():
            return QRectF()
        available = self.rect().adjusted(12, 12, -12, -12)
        scaled = self._pixmap.size().scaled(
            available.size(), Qt.AspectRatioMode.KeepAspectRatio
        )
        return QRectF(
            available.left() + (available.width() - scaled.width()) / 2,
            available.top() + (available.height() - scaled.height()) / 2,
            scaled.width(),
            scaled.height(),
        )

    def highlight_rect(self) -> QRectF:
        if self._message is None:
            return QRectF()
        return message_highlight_rect(self._message, self.image_rect())

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#202124"))
        if self._pixmap.isNull():
            painter.setPen(QColor("#c7cbd0"))
            painter.drawText(
                self.rect().adjusted(24, 24, -24, -24),
                Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                self._placeholder,
            )
            return

        image_rect = self.image_rect()
        painter.drawPixmap(image_rect.toRect(), self._pixmap)
        highlight = self.highlight_rect()
        painter.fillRect(highlight, QColor(255, 193, 7, 52))
        painter.setPen(QPen(QColor("#ffb300"), 3))
        painter.drawRect(highlight)


class ArchiveViewerPage(QWidget):
    maintenance_changed = Signal(bool)

    def __init__(self, database_path: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.database_path = database_path
        self.store = ArchiveStore(database_path)
        self.chats: list[ChatSummary] = []
        self.page_records: list[ArchivedMessage] = []
        self.current_page = 0
        self.total_messages = 0
        self.current_record: ArchivedMessage | None = None
        self.visibility_worker: VisibilityWorker | None = None
        self.visibility_thread: QThread | None = None
        self._build_ui()
        self.refresh_archive()
        self._start_visibility_scan()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)

        toolbar = QHBoxLayout()
        title = QLabel("聊天记录")
        title.setObjectName("sectionTitle")
        toolbar.addWidget(title)
        self.archive_status = QLabel()
        self.archive_status.setObjectName("status")
        toolbar.addWidget(self.archive_status)
        toolbar.addStretch()
        self.refresh_button = QPushButton("刷新档案")
        self.refresh_button.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload)
        )
        self.refresh_button.clicked.connect(self.refresh_archive)
        toolbar.addWidget(self.refresh_button)
        root.addLayout(toolbar)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._build_chat_panel())
        splitter.addWidget(self._build_message_panel())
        splitter.addWidget(self._build_screenshot_panel())
        splitter.setSizes([220, 540, 430])
        root.addWidget(splitter, 1)

    def _build_chat_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("archivePanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(10, 10, 10, 10)
        heading = QLabel("联系人")
        heading.setObjectName("panelTitle")
        layout.addWidget(heading)
        self.chat_search = QLineEdit()
        self.chat_search.setPlaceholderText("搜索联系人")
        self.chat_search.setClearButtonEnabled(True)
        self.chat_search.textChanged.connect(self._populate_chats)
        layout.addWidget(self.chat_search)
        self.chat_list = QListWidget()
        self.chat_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.chat_list.currentItemChanged.connect(self._chat_selected)
        layout.addWidget(self.chat_list, 1)
        self.chat_count_label = QLabel("0 个联系人")
        self.chat_count_label.setObjectName("status")
        layout.addWidget(self.chat_count_label)
        return panel

    def _build_message_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("archivePanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(10, 10, 10, 10)
        self.chat_title = QLabel("请选择联系人")
        self.chat_title.setObjectName("panelTitle")
        layout.addWidget(self.chat_title)

        filters = QHBoxLayout()
        self.message_search = QLineEdit()
        self.message_search.setPlaceholderText("搜索聊天文字")
        self.message_search.setClearButtonEnabled(True)
        self.message_search.textChanged.connect(self._apply_message_filters)
        filters.addWidget(self.message_search, 1)
        self.speaker_filter = QComboBox()
        self.speaker_filter.addItem("全部发送方", None)
        self.speaker_filter.currentIndexChanged.connect(self._apply_message_filters)
        filters.addWidget(self.speaker_filter)
        layout.addLayout(filters)

        self.message_table = QTableWidget(0, 4)
        self.message_table.setHorizontalHeaderLabels(["日期时间", "发送方", "内容", "来源截图"])
        self.message_table.verticalHeader().setVisible(False)
        self.message_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        self.message_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        self.message_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch
        )
        self.message_table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.ResizeToContents
        )
        self.message_table.setAlternatingRowColors(True)
        self.message_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.message_table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.message_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.message_table.currentCellChanged.connect(self._message_selected)
        layout.addWidget(self.message_table, 1)

        pagination = QHBoxLayout()
        self.message_count_label = QLabel("0 条记录")
        self.message_count_label.setObjectName("status")
        pagination.addWidget(self.message_count_label)
        pagination.addStretch()
        self.previous_button = QPushButton("上一页")
        self.previous_button.clicked.connect(self._previous_page)
        pagination.addWidget(self.previous_button)
        self.page_label = QLabel("0 / 0")
        self.page_label.setMinimumWidth(64)
        self.page_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pagination.addWidget(self.page_label)
        self.next_button = QPushButton("下一页")
        self.next_button.clicked.connect(self._next_page)
        pagination.addWidget(self.next_button)
        layout.addLayout(pagination)
        return panel

    def _build_screenshot_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("archivePanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(10, 10, 10, 10)
        header = QHBoxLayout()
        heading = QLabel("原始截图")
        heading.setObjectName("panelTitle")
        header.addWidget(heading)
        header.addStretch()
        self.open_source_button = QPushButton("打开原图")
        self.open_source_button.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_DialogOpenButton)
        )
        self.open_source_button.setEnabled(False)
        self.open_source_button.clicked.connect(self._open_current_source)
        header.addWidget(self.open_source_button)
        layout.addLayout(header)
        self.screenshot_viewer = ScreenshotViewer()
        layout.addWidget(self.screenshot_viewer, 1)
        self.source_detail = QLabel("未选择记录")
        self.source_detail.setObjectName("status")
        self.source_detail.setWordWrap(True)
        layout.addWidget(self.source_detail)
        return panel

    def refresh_archive(self) -> None:
        selected_name = self._selected_partner_name()
        self.store = ArchiveStore(self.database_path)
        self.chats = self.store.list_chats()
        self._populate_chats(preferred_name=selected_name)

    def _populate_chats(
        self, _text: str = "", preferred_name: str | None = None
    ) -> None:
        if preferred_name is None:
            preferred_name = self._selected_partner_name()
        query = self.chat_search.text().strip().casefold()
        visible = [chat for chat in self.chats if query in chat.partner_name.casefold()]
        self.chat_list.blockSignals(True)
        self.chat_list.clear()
        selected_row = -1
        for row, chat in enumerate(visible):
            latest = _display_datetime(chat.latest_occurred_at) or "无日期"
            item = QListWidgetItem(
                f"{chat.partner_name}\n{chat.message_count} 条 · {latest}"
            )
            item.setData(Qt.ItemDataRole.UserRole, chat.partner_name)
            item.setToolTip(chat.partner_name)
            item.setSizeHint(QSize(180, 54))
            self.chat_list.addItem(item)
            if chat.partner_name == preferred_name:
                selected_row = row
        self.chat_list.blockSignals(False)
        self.chat_count_label.setText(f"{len(visible)} 个联系人")
        if visible:
            self.chat_list.setCurrentRow(selected_row if selected_row >= 0 else 0)
        else:
            self._clear_messages("没有匹配的联系人")

    def _selected_partner_name(self) -> str | None:
        item = self.chat_list.currentItem()
        if item is None:
            return None
        value = item.data(Qt.ItemDataRole.UserRole)
        return str(value) if value else None

    def _chat_selected(
        self, current: QListWidgetItem | None, _previous: QListWidgetItem | None
    ) -> None:
        if current is None:
            self._clear_messages("请选择联系人")
            return
        partner_name = str(current.data(Qt.ItemDataRole.UserRole))
        self.chat_title.setText(partner_name)
        self.message_search.blockSignals(True)
        self.message_search.clear()
        self.message_search.blockSignals(False)
        self.speaker_filter.blockSignals(True)
        self.speaker_filter.clear()
        self.speaker_filter.addItem("全部发送方", None)
        for speaker in self.store.list_chat_speakers(partner_name):
            self.speaker_filter.addItem(speaker, speaker)
        self.speaker_filter.blockSignals(False)
        self._apply_message_filters()

    def _apply_message_filters(self, _value=None) -> None:
        self.current_page = 0
        self._load_message_page()

    def _load_message_page(self) -> None:
        partner_name = self._selected_partner_name()
        if partner_name is None:
            self._clear_messages("请选择联系人")
            return
        query = self.message_search.text().strip()
        speaker = self.speaker_filter.currentData()
        self.total_messages = self.store.count_chat_messages(
            partner_name, query=query, speaker=speaker
        )
        page_count = (self.total_messages + PAGE_SIZE - 1) // PAGE_SIZE
        if page_count:
            self.current_page = min(self.current_page, page_count - 1)
        else:
            self.current_page = 0
        self.page_records = self.store.load_chat_messages(
            partner_name,
            query=query,
            speaker=speaker,
            limit=PAGE_SIZE,
            offset=self.current_page * PAGE_SIZE,
            newest_first=True,
        )

        self.message_table.blockSignals(True)
        self.message_table.setRowCount(len(self.page_records))
        for row, record in enumerate(self.page_records):
            message = record.message
            values = (
                _display_datetime(message.occurred_at),
                message.speaker,
                message.text.replace("\n", " "),
                record.source_path.name if record.source_path.is_file() else "截图缺失",
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, record)
                self.message_table.setItem(row, column, item)
        self.message_table.blockSignals(False)

        self.message_count_label.setText(f"{self.total_messages} 条文字记录")
        self.page_label.setText(
            f"{self.current_page + 1} / {page_count}" if page_count else "0 / 0"
        )
        self.previous_button.setEnabled(self.current_page > 0)
        self.next_button.setEnabled(self.current_page + 1 < page_count)
        if self.page_records:
            self.message_table.setCurrentCell(0, 0)
            self._show_record(self.page_records[0])
        else:
            self.current_record = None
            self.screenshot_viewer.clear("没有匹配的文字记录")
            self.source_detail.setText("未选择记录")
            self.open_source_button.setEnabled(False)

    def _message_selected(
        self,
        current_row: int,
        _current_column: int,
        _previous_row: int,
        _previous_column: int,
    ) -> None:
        if current_row < 0:
            return
        item = self.message_table.item(current_row, 0)
        if item is None:
            return
        record = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(record, ArchivedMessage):
            self._show_record(record)

    def _show_record(self, record: ArchivedMessage) -> None:
        self.current_record = record
        available = self.screenshot_viewer.show_record(record)
        message = record.message
        confidence = round(message.confidence * 100)
        state = "已定位并高亮" if available else "截图文件缺失"
        self.source_detail.setText(
            f"{record.source_path.name} · OCR {confidence}% · {state}"
        )
        self.source_detail.setToolTip(str(record.source_path))
        self.open_source_button.setEnabled(available)

    def _clear_messages(self, title: str) -> None:
        self.chat_title.setText(title)
        self.page_records = []
        self.total_messages = 0
        self.message_table.setRowCount(0)
        self.message_count_label.setText("0 条文字记录")
        self.page_label.setText("0 / 0")
        self.previous_button.setEnabled(False)
        self.next_button.setEnabled(False)
        self.current_record = None
        self.screenshot_viewer.clear()
        self.source_detail.setText("未选择记录")
        self.open_source_button.setEnabled(False)

    def _previous_page(self) -> None:
        if self.current_page > 0:
            self.current_page -= 1
            self._load_message_page()

    def _next_page(self) -> None:
        if (self.current_page + 1) * PAGE_SIZE < self.total_messages:
            self.current_page += 1
            self._load_message_page()

    def _start_visibility_scan(self) -> None:
        total = self.store.unreviewed_message_count()
        if total == 0:
            self.archive_status.setText("档案已是最新")
            return
        self.archive_status.setText(f"正在整理旧档案：0/{total}")
        self.maintenance_changed.emit(True)
        self.visibility_thread = QThread(self)
        self.visibility_worker = VisibilityWorker(self.database_path)
        self.visibility_worker.moveToThread(self.visibility_thread)
        self.visibility_thread.started.connect(self.visibility_worker.run)
        self.visibility_worker.progress.connect(self._visibility_progress)
        self.visibility_worker.finished.connect(self._visibility_finished)
        self.visibility_worker.failed.connect(self._visibility_failed)
        self.visibility_worker.finished.connect(self.visibility_thread.quit)
        self.visibility_worker.failed.connect(self.visibility_thread.quit)
        self.visibility_thread.finished.connect(self._visibility_thread_finished)
        self.visibility_thread.start()

    def _visibility_progress(self, processed: int, total: int) -> None:
        self.archive_status.setText(f"正在整理旧档案：{processed}/{total}")

    def _visibility_finished(self, processed: int, _total: int, complete: bool) -> None:
        if complete:
            self.archive_status.setText(f"旧档案整理完成：{processed} 条")
            self.refresh_archive()
        else:
            self.archive_status.setText("旧档案整理已暂停")

    def _visibility_failed(self, message: str) -> None:
        self.archive_status.setText(f"旧档案整理失败：{message}")

    def _visibility_thread_finished(self) -> None:
        if self.visibility_worker is not None:
            self.visibility_worker.deleteLater()
        if self.visibility_thread is not None:
            self.visibility_thread.deleteLater()
        self.visibility_worker = None
        self.visibility_thread = None
        self.maintenance_changed.emit(False)

    def maintenance_running(self) -> bool:
        return self.visibility_thread is not None

    def request_close(self) -> bool:
        if self.visibility_thread is not None:
            if self.visibility_worker is not None:
                self.visibility_worker.stop()
            self.archive_status.setText("正在停止旧档案整理，停止后可关闭窗口")
            return False
        return True

    def _open_current_source(self) -> None:
        if (
            self.current_record is not None
            and self.current_record.source_path.is_file()
        ):
            QDesktopServices.openUrl(
                QUrl.fromLocalFile(str(self.current_record.source_path))
            )


def _display_datetime(value: str | None) -> str:
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return value
