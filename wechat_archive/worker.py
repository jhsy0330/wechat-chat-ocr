from __future__ import annotations

from collections import OrderedDict
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, Signal, Slot

from .capture import CaptureEngine
from .content_filter import TextMessageFilter
from .exporter import export_archive
from .models import CaptureSettings, Message, OCRLine
from .ocr import VisionOCR
from .processing import is_system_line, merge_capture_pages, parse_page
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
            reference_time = datetime.now().astimezone()
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
                text_filter = TextMessageFilter(page)
                parsed_pages.append(
                    parse_page(
                        ocr.recognize(page),
                        self.settings.partner_name,
                        text_filter=text_filter.accepts,
                        system_filter=text_filter.accepts_system,
                        reference_time=reference_time,
                    )
                )
            current_messages = merge_capture_pages(
                parsed_pages, direction=self.settings.direction
            )

            store = ArchiveStore(self.data_dir / "archive.sqlite3")
            filter_cache: OrderedDict[Path, TextMessageFilter] = OrderedDict()

            def keep_existing(message: Message, source_path: Path) -> bool:
                if not source_path.exists():
                    return True
                content_filter = filter_cache.get(source_path)
                if content_filter is None:
                    content_filter = TextMessageFilter(source_path)
                    filter_cache[source_path] = content_filter
                    if len(filter_cache) > 2:
                        filter_cache.popitem(last=False)
                else:
                    filter_cache.move_to_end(source_path)
                line = OCRLine(
                    text=message.text,
                    confidence=message.confidence,
                    x=message.x,
                    y=message.y,
                    width=message.width,
                    height=message.height,
                    source=message.source,
                )
                if is_system_line(line):
                    return content_filter.accepts_system(line)
                return content_filter.accepts(line)

            all_messages, added = store.append_session(
                self.settings.partner_name,
                current_messages,
                len(pages),
                pages[0].parent,
                existing_message_filter=keep_existing,
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
