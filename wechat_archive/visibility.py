from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from .content_filter import TextMessageFilter
from .models import ArchivedMessage, OCRLine
from .storage import ArchiveStore


ProgressCallback = Callable[[int, int], None]
StopCallback = Callable[[], bool]


class ArchiveVisibilityScanner:
    """Classify legacy rows once and persist their text-bubble visibility."""

    def __init__(self, store: ArchiveStore, batch_size: int = 500) -> None:
        self.store = store
        self.batch_size = batch_size

    def scan(
        self,
        *,
        on_progress: ProgressCallback | None = None,
        should_stop: StopCallback | None = None,
    ) -> tuple[int, int, bool]:
        on_progress = on_progress or (lambda _processed, _total: None)
        should_stop = should_stop or (lambda: False)
        total = self.store.unreviewed_message_count()
        processed = 0
        on_progress(processed, total)

        while not should_stop():
            records = self.store.load_unreviewed_messages(self.batch_size)
            if not records:
                return processed, total, True

            results: list[tuple[int, bool]] = []
            source_path: Path | None = None
            content_filter: TextMessageFilter | None = None
            for record in records:
                if should_stop():
                    self.store.set_message_visibility(results)
                    processed += len(results)
                    on_progress(processed, total)
                    return processed, total, False
                if record.source_path != source_path:
                    source_path = record.source_path
                    content_filter = self._load_filter(source_path)
                results.append(
                    (record.message_id, self._is_visible(record, content_filter))
                )

            self.store.set_message_visibility(results)
            processed += len(results)
            on_progress(processed, total)

        return processed, total, False

    @staticmethod
    def _load_filter(source_path: Path) -> TextMessageFilter | None:
        if not source_path.is_file():
            return None
        try:
            return TextMessageFilter(source_path)
        except (OSError, ValueError):
            return None

    @staticmethod
    def _is_visible(
        record: ArchivedMessage, content_filter: TextMessageFilter | None
    ) -> bool:
        # Missing or unreadable evidence is retained for manual review.
        if content_filter is None:
            return True
        message = record.message
        line = OCRLine(
            text=message.text,
            confidence=message.confidence,
            x=message.x,
            y=message.y,
            width=message.width,
            height=message.height,
            source=message.source,
        )
        try:
            if message.kind == "system":
                return content_filter.accepts_system(line)
            return content_filter.accepts(line)
        except (IndexError, ValueError):
            return True
