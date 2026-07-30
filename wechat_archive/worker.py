from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, Signal, Slot

from .capture import CaptureEngine
from .exporter import export_archive
from .models import CaptureSettings, Message
from .ocr import VisionOCR
from .processing import merge_capture_pages, parse_page
from .storage import ArchiveStore


class ArchiveWorker(QObject):
    status = Signal(str)
    page_captured = Signal(int, str)
    messages_ready = Signal(object)
    finished = Signal(object, str, int)
    failed = Signal(str)

    def __init__(self, settings: CaptureSettings, data_dir: Path) -> None:
        super().__init__()
        self.settings = settings
        self.data_dir = data_dir
        self.engine = CaptureEngine()

    @Slot()
    def run(self) -> None:
        try:
            ocr = VisionOCR()
            self.status.emit("正在准备本地 OCR")
            ocr.ensure_helper()
            pages = self.engine.capture(
                self.settings,
                on_status=self.status.emit,
                on_page=lambda number, path: self.page_captured.emit(number, str(path)),
            )
            if not pages:
                raise RuntimeError("没有获得聊天截图")

            parsed_pages: list[list[Message]] = []
            for index, page in enumerate(pages, 1):
                self.status.emit(f"OCR 识别第 {index}/{len(pages)} 页")
                parsed_pages.append(
                    parse_page(ocr.recognize(page), self.settings.partner_name)
                )
            current_messages = merge_capture_pages(parsed_pages)

            store = ArchiveStore(self.data_dir / "archive.sqlite3")
            all_messages, added = store.append_session(
                self.settings.partner_name,
                current_messages,
                len(pages),
                pages[0].parent,
            )
            export_name = datetime.now().strftime("chat-%Y%m%d-%H%M%S")
            output = self.data_dir / "exports" / export_name
            html_path, _, _ = export_archive(
                all_messages, self.settings.partner_name, output
            )
            self.messages_ready.emit(all_messages)
            self.finished.emit(all_messages, str(html_path), added)
        except Exception as error:
            self.failed.emit(str(error))

    def stop(self) -> None:
        self.engine.stop()

    def pause(self) -> None:
        self.engine.pause()

    def resume(self) -> None:
        self.engine.resume()
