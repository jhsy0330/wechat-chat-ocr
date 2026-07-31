from __future__ import annotations

from collections import OrderedDict
from dataclasses import asdict
from datetime import datetime
import json
from pathlib import Path

from PySide6.QtCore import QObject, Signal, Slot

from .capture import CaptureEngine, ResumeAnchorMismatch
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
    finished = Signal(object, str, int, int)
    interrupted = Signal(int)
    failed = Signal(str)

    def __init__(
        self,
        settings: CaptureSettings,
        data_dir: Path,
        resume_session_id: int | None = None,
    ) -> None:
        super().__init__()
        self.settings = settings
        self.data_dir = data_dir
        self.engine = CaptureEngine()
        self.resume_session_id = resume_session_id

    @Slot()
    def run(self) -> None:
        store = ArchiveStore(self.data_dir / "archive.sqlite3")
        session_id: int | None = self.resume_session_id
        stage = "capture"
        try:
            reference_time = datetime.now().astimezone()
            ocr = VisionOCR()
            self.status.emit("正在准备本地 OCR")
            ocr.ensure_helper()
            existing_page_info = []
            skip_capture = False
            if session_id is not None:
                session = store.load_capture_session(session_id)
                session_dir = session.session_dir
                existing_page_info = store.load_capture_pages(session_id)
                skip_capture = session.status == "ocr" or bool(
                    session.error_message
                    and session.error_message.startswith(("ocr:", "merge:"))
                )
                store.update_session_status(session_id, "capturing")
            else:
                session_name = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
                session_dir = self.settings.session_dir / session_name
                session_id = store.create_capture_session(self.settings, session_dir)

            def page_checkpoint(number: int, path: Path) -> None:
                store.record_capture_page(session_id, number, path)
                self.page_captured.emit(number, str(path))

            if skip_capture:
                pages = [page.source_path for page in existing_page_info]
            else:
                pages = self.engine.capture(
                    self.settings,
                    on_status=self.status.emit,
                    on_page=page_checkpoint,
                    session_dir=session_dir,
                    existing_pages=[page.source_path for page in existing_page_info],
                )
            if self.engine.was_stopped:
                store.update_session_status(session_id, "interrupted")
                self.interrupted.emit(session_id)
                return
            if not pages:
                raise RuntimeError("没有获得聊天截图")

            stage = "ocr"
            store.update_session_status(session_id, "ocr")
            page_info = {
                page.page_number: page for page in store.load_capture_pages(session_id)
            }
            parsed_pages: list[list[Message]] = []
            for index, page in enumerate(pages, 1):
                self.status.emit(f"OCR 识别第 {index}/{len(pages)} 页")
                checkpoint = page_info.get(index)
                if checkpoint is not None and checkpoint.ocr_json:
                    raw_lines = [
                        OCRLine(**payload)
                        for payload in json.loads(checkpoint.ocr_json)
                    ]
                else:
                    raw_lines = ocr.recognize(page)
                    store.save_page_ocr(
                        session_id,
                        index,
                        json.dumps(
                            [asdict(line) for line in raw_lines],
                            ensure_ascii=False,
                        ),
                    )
                text_filter = TextMessageFilter(page)
                voice_bubbles = text_filter.detect_voice_bubbles(raw_lines)
                parsed_pages.append(
                    parse_page(
                        raw_lines,
                        self.settings.partner_name,
                        text_filter=text_filter.accepts,
                        system_filter=text_filter.accepts_system,
                        reference_time=reference_time,
                        voice_bubbles=voice_bubbles,
                    )
                )
            current_messages = merge_capture_pages(
                parsed_pages, direction=self.settings.direction
            )

            stage = "merge"
            filter_cache: OrderedDict[Path, TextMessageFilter] = OrderedDict()

            def keep_existing(message: Message, source_path: Path) -> bool:
                if message.kind == "voice":
                    return True
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

            all_messages, added, conflict_id = store.append_session_checked(
                self.settings.partner_name,
                current_messages,
                len(pages),
                pages[0].parent,
                session_id=session_id,
                existing_message_filter=keep_existing,
            )
            if conflict_id is None:
                store.update_session_status(session_id, "completed")
            else:
                self.status.emit("发现跨次采集冲突，已转入待复核队列")
            export_name = datetime.now().strftime("chat-%Y%m%d-%H%M%S")
            output = self.data_dir / "exports" / export_name
            html_path, _, _ = export_archive(
                all_messages, self.settings.partner_name, output
            )
            self.messages_ready.emit(all_messages)
            self.finished.emit(all_messages, str(html_path), added, conflict_id or 0)
        except ResumeAnchorMismatch as error:
            if session_id is not None:
                store.update_session_status(session_id, "interrupted", str(error))
            self.failed.emit(str(error))
        except Exception as error:
            if session_id is not None:
                store.update_session_status(session_id, "failed", f"{stage}:{error}")
            self.failed.emit(str(error))

    def stop(self) -> None:
        self.engine.stop()

    def pause(self) -> None:
        self.engine.pause()

    def resume(self) -> None:
        self.engine.resume()
