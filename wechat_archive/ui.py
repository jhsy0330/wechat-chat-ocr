from __future__ import annotations

import tempfile
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QThread, QUrl, Signal
from PySide6.QtGui import QColor, QCloseEvent, QDesktopServices, QMouseEvent, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QSplitter,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import macos
from .models import CaptureSettings, Message, NormalizedRegion, WindowInfo
from .worker import ArchiveWorker


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"


class RegionCanvas(QWidget):
    selection_changed = Signal()

    def __init__(self, image: QPixmap, region: NormalizedRegion) -> None:
        super().__init__()
        self.image = image
        self.region = QRectF(region.x, region.y, region.width, region.height)
        self._start: QPointF | None = None
        self._current: QPointF | None = None
        self.setMinimumSize(QSize(760, 470))
        self.setCursor(Qt.CursorShape.CrossCursor)

    def _image_rect(self) -> QRectF:
        scaled = self.image.size().scaled(
            self.size(), Qt.AspectRatioMode.KeepAspectRatio
        )
        return QRectF(
            (self.width() - scaled.width()) / 2,
            (self.height() - scaled.height()) / 2,
            scaled.width(),
            scaled.height(),
        )

    def _normalized(self, point: QPointF) -> QPointF:
        rect = self._image_rect()
        x = min(1.0, max(0.0, (point.x() - rect.left()) / rect.width()))
        y = min(1.0, max(0.0, (point.y() - rect.top()) / rect.height()))
        return QPointF(x, y)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._start = self._normalized(event.position())
            self._current = self._start
            self.update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._start is not None:
            self._current = self._normalized(event.position())
            self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._start is None or self._current is None:
            return
        end = self._normalized(event.position())
        left, right = sorted((self._start.x(), end.x()))
        top, bottom = sorted((self._start.y(), end.y()))
        if right - left >= 0.08 and bottom - top >= 0.08:
            self.region = QRectF(left, top, right - left, bottom - top)
            self.selection_changed.emit()
        self._start = None
        self._current = None
        self.update()

    def selected_region(self) -> NormalizedRegion:
        return NormalizedRegion(
            self.region.x(), self.region.y(), self.region.width(), self.region.height()
        )

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#202124"))
        image_rect = self._image_rect()
        painter.drawPixmap(image_rect.toRect(), self.image)
        selection = self.region
        if self._start is not None and self._current is not None:
            selection = QRectF(self._start, self._current).normalized()
        selected_rect = QRectF(
            image_rect.left() + selection.x() * image_rect.width(),
            image_rect.top() + selection.y() * image_rect.height(),
            selection.width() * image_rect.width(),
            selection.height() * image_rect.height(),
        )
        painter.setPen(QPen(QColor("#18a058"), 3))
        painter.drawRect(selected_rect)


class RegionDialog(QDialog):
    def __init__(
        self, image_path: Path, current: NormalizedRegion, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("校准聊天区域")
        image = QPixmap(str(image_path))
        self.canvas = RegionCanvas(image, current)
        self.region_size_label = QLabel()
        self.region_size_label.setObjectName("status")
        self.canvas.selection_changed.connect(self._update_region_size)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.Ok
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(self.canvas, 1)
        layout.addWidget(self.region_size_label)
        layout.addWidget(buttons)
        self._update_region_size()
        self.resize(940, 660)

    def selected_region(self) -> NormalizedRegion:
        return self.canvas.selected_region()

    def region_pixel_size(self) -> tuple[int, int]:
        return self.selected_region().pixel_size(
            self.canvas.image.width(), self.canvas.image.height()
        )

    def _update_region_size(self) -> None:
        width, height = self.region_pixel_size()
        low = round(height * 0.60)
        high = round(height * 0.75)
        self.region_size_label.setText(
            f"区域尺寸：{width} x {height} px    建议每次向上滚动：{low}-{high} px"
        )


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("微信聊天归档")
        self.resize(1080, 760)
        self.region = NormalizedRegion()
        self.windows: list[WindowInfo] = []
        self.worker: ArchiveWorker | None = None
        self.thread: QThread | None = None
        self.paused = False
        self.last_export: Path | None = None
        self.calibrated_region_size: tuple[int, int] | None = None
        self._build_ui()
        self.refresh_windows()
        self.refresh_permissions()

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(12)

        title = QLabel("微信聊天归档")
        title.setObjectName("title")
        root.addWidget(title)

        setup_frame = QFrame()
        setup_frame.setObjectName("settings")
        setup_layout = QVBoxLayout(setup_frame)

        window_row = QHBoxLayout()
        window_row.addWidget(QLabel("微信窗口"))
        self.window_combo = QComboBox()
        self.window_combo.setMinimumWidth(380)
        window_row.addWidget(self.window_combo, 1)
        refresh_button = QPushButton("刷新")
        refresh_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload))
        refresh_button.clicked.connect(self.refresh_windows)
        window_row.addWidget(refresh_button)
        self.calibrate_button = QPushButton("校准区域")
        self.calibrate_button.clicked.connect(self.calibrate_region)
        window_row.addWidget(self.calibrate_button)
        setup_layout.addLayout(window_row)

        permission_row = QHBoxLayout()
        self.permission_label = QLabel()
        permission_row.addWidget(self.permission_label, 1)
        permission_button = QPushButton("检查权限")
        permission_button.clicked.connect(self.request_permissions)
        permission_row.addWidget(permission_button)
        setup_layout.addLayout(permission_row)

        form = QFormLayout()
        self.partner_input = QLineEdit("女朋友")
        self.partner_input.setMaximumWidth(220)
        form.addRow("对方称呼", self.partner_input)
        self.region_size_label = QLabel("尚未校准")
        form.addRow("聊天区域", self.region_size_label)
        settings_row = QHBoxLayout()
        self.max_pages = QSpinBox()
        self.max_pages.setRange(1, 1000)
        self.max_pages.setValue(50)
        self.max_pages.setSuffix(" 页")
        self.scroll_pixels = QSpinBox()
        self.scroll_pixels.setRange(150, 3000)
        self.scroll_pixels.setValue(650)
        self.scroll_pixels.setSingleStep(50)
        self.scroll_pixels.setSuffix(" px")
        settings_row.addWidget(QLabel("最多读取"))
        settings_row.addWidget(self.max_pages)
        settings_row.addSpacing(18)
        settings_row.addWidget(QLabel("每次向上滚动"))
        settings_row.addWidget(self.scroll_pixels)
        settings_row.addStretch()
        form.addRow("采集设置", settings_row)
        setup_layout.addLayout(form)
        root.addWidget(setup_frame)

        controls = QHBoxLayout()
        self.start_button = QPushButton("开始记录")
        self.start_button.setObjectName("primary")
        self.start_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay))
        self.start_button.clicked.connect(self.start_capture)
        controls.addWidget(self.start_button)
        self.pause_button = QPushButton("暂停")
        self.pause_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPause))
        self.pause_button.setEnabled(False)
        self.pause_button.clicked.connect(self.toggle_pause)
        controls.addWidget(self.pause_button)
        self.stop_button = QPushButton("停止")
        self.stop_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaStop))
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop_capture)
        controls.addWidget(self.stop_button)
        controls.addStretch()
        self.open_export_button = QPushButton("打开导出文件")
        self.open_export_button.setEnabled(False)
        self.open_export_button.clicked.connect(self.open_export)
        controls.addWidget(self.open_export_button)
        root.addLayout(controls)

        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        root.addWidget(self.progress)
        self.status_label = QLabel("等待开始")
        self.status_label.setObjectName("status")
        root.addWidget(self.status_label)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.preview = QLabel("暂无截图")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumWidth(340)
        self.preview.setObjectName("preview")
        splitter.addWidget(self.preview)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["序号", "发送方", "内容", "日期时间", "微信显示"]
        )
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.horizontalHeader().setSectionResizeMode(2, self.table.horizontalHeader().ResizeMode.Stretch)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        splitter.addWidget(self.table)
        splitter.setSizes([390, 650])
        root.addWidget(splitter, 1)

        self.setStyleSheet(
            """
            QMainWindow { background: #f5f6f7; color: #242629; }
            QLabel#title { font-size: 22px; font-weight: 650; }
            QFrame#settings { background: white; border: 1px solid #dfe2e5; border-radius: 6px; }
            QPushButton { min-height: 28px; padding: 2px 10px; }
            QPushButton#primary { background: #168f4e; color: white; border: 1px solid #11753f; border-radius: 4px; }
            QPushButton#primary:disabled { background: #aeb8b2; border-color: #aeb8b2; }
            QLabel#status { color: #5f666d; }
            QLabel#preview { background: #202124; color: #c7cbd0; border: 1px solid #d8dbde; }
            QTableWidget { background: white; border: 1px solid #d8dbde; gridline-color: #eceeef; }
            """
        )

    def selected_window(self) -> WindowInfo | None:
        index = self.window_combo.currentIndex()
        return self.windows[index] if 0 <= index < len(self.windows) else None

    def refresh_windows(self) -> None:
        self.windows = macos.list_wechat_windows()
        self.window_combo.clear()
        self.window_combo.addItems([window.label for window in self.windows])
        available = bool(self.windows) and self.worker is None
        self.start_button.setEnabled(available)
        self.calibrate_button.setEnabled(available)
        if not self.windows:
            self.window_combo.addItem("未找到微信窗口，请打开微信和目标聊天")

    def refresh_permissions(self) -> None:
        accessibility = macos.accessibility_granted()
        capture = macos.screen_capture_granted()
        access_text = "已授权" if accessibility else "未授权"
        capture_text = "已授权" if capture else "未授权"
        self.permission_label.setText(
            f"辅助功能：{access_text}    屏幕录制：{capture_text}"
        )

    def request_permissions(self) -> None:
        macos.request_accessibility()
        macos.request_screen_capture()
        self.refresh_permissions()
        if not macos.accessibility_granted() or not macos.screen_capture_granted():
            QMessageBox.information(
                self,
                "需要系统权限",
                "请在系统设置的“隐私与安全性”中允许当前终端或应用使用辅助功能和屏幕录制，然后重新启动本工具。",
            )

    def calibrate_region(self) -> None:
        window = self.selected_window()
        if window is None:
            return
        try:
            with tempfile.TemporaryDirectory(prefix="wechat-ocr-") as directory:
                screenshot = Path(directory) / "window.png"
                macos.capture_window(window.window_id, screenshot)
                dialog = RegionDialog(screenshot, self.region, self)
                if dialog.exec() == QDialog.DialogCode.Accepted:
                    self.region = dialog.selected_region()
                    self.calibrated_region_size = dialog.region_pixel_size()
                    width, height = self.calibrated_region_size
                    low = round(height * 0.60)
                    high = round(height * 0.75)
                    self.region_size_label.setText(
                        f"{width} x {height} px（建议滚动 {low}-{high} px）"
                    )
                    self._show_preview(screenshot)
                    self.status_label.setText(
                        f"聊天区域已校准：{width} x {height} px"
                    )
        except RuntimeError as error:
            QMessageBox.critical(self, "校准失败", str(error))

    def start_capture(self) -> None:
        window = self.selected_window()
        partner = self.partner_input.text().strip()
        if window is None or not partner:
            return
        if not macos.accessibility_granted() or not macos.screen_capture_granted():
            self.request_permissions()
            return

        settings = CaptureSettings(
            window=window,
            region=self.region,
            partner_name=partner,
            max_pages=self.max_pages.value(),
            scroll_pixels=self.scroll_pixels.value(),
            session_dir=DATA_DIR / "captures",
        )
        self.thread = QThread(self)
        self.worker = ArchiveWorker(settings, DATA_DIR)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.status.connect(self.status_label.setText)
        self.worker.page_captured.connect(self.on_page_captured)
        self.worker.messages_ready.connect(self.show_messages)
        self.worker.finished.connect(self.on_finished)
        self.worker.failed.connect(self.on_failed)
        self.worker.finished.connect(self.thread.quit)
        self.worker.failed.connect(self.thread.quit)
        self.thread.finished.connect(self._thread_finished)
        self.thread.start()

        self.start_button.setEnabled(False)
        self.calibrate_button.setEnabled(False)
        self.pause_button.setEnabled(True)
        self.stop_button.setEnabled(True)
        self.progress.setRange(0, 0)
        self.table.setRowCount(0)
        self.last_export = None
        self.open_export_button.setEnabled(False)

    def toggle_pause(self) -> None:
        if self.worker is None:
            return
        self.paused = not self.paused
        if self.paused:
            self.worker.pause()
            self.pause_button.setText("继续")
            self.pause_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay))
        else:
            self.worker.resume()
            self.pause_button.setText("暂停")
            self.pause_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPause))

    def stop_capture(self) -> None:
        if self.worker is not None:
            self.worker.stop()
            self.status_label.setText("正在安全停止")

    def on_page_captured(self, number: int, path: str) -> None:
        self.status_label.setText(f"已截取 {number} 页")
        self._show_preview(Path(path))

    def _show_preview(self, path: Path) -> None:
        pixmap = QPixmap(str(path))
        self.preview.setPixmap(
            pixmap.scaled(
                self.preview.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def show_messages(self, messages: list[Message]) -> None:
        self.table.setRowCount(len(messages))
        for row, message in enumerate(messages):
            values = (
                str(message.sequence),
                message.speaker,
                message.text,
                _display_datetime(message.occurred_at),
                message.visible_time or "",
            )
            for column, value in enumerate(values):
                self.table.setItem(row, column, QTableWidgetItem(value))
        self.table.resizeColumnsToContents()

    def on_finished(self, _messages: list[Message], export_path: str, added: int) -> None:
        self.last_export = Path(export_path)
        self.open_export_button.setEnabled(True)
        self.status_label.setText(f"完成，本次新增 {added} 条识别记录")

    def on_failed(self, message: str) -> None:
        self.status_label.setText("任务失败")
        QMessageBox.critical(self, "任务失败", message)

    def _thread_finished(self) -> None:
        if self.worker is not None:
            self.worker.deleteLater()
        if self.thread is not None:
            self.thread.deleteLater()
        self.worker = None
        self.thread = None
        self.paused = False
        self.pause_button.setText("暂停")
        self.pause_button.setEnabled(False)
        self.stop_button.setEnabled(False)
        self.progress.setRange(0, 1)
        self.progress.setValue(1)
        self.refresh_windows()

    def open_export(self) -> None:
        if self.last_export is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.last_export)))

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.worker is not None:
            self.worker.stop()
            event.ignore()
            return
        event.accept()


def configure_application(application: QApplication) -> None:
    application.setApplicationName("微信聊天归档")
    application.setOrganizationName("Local Archive")


def _display_datetime(value: str | None) -> str:
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return value
