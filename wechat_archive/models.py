from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from PIL import Image


@dataclass(frozen=True)
class WindowInfo:
    window_id: int
    pid: int
    owner: str
    title: str
    x: float
    y: float
    width: float
    height: float

    @property
    def label(self) -> str:
        title = self.title.strip() or "微信主窗口"
        return f"{title}  ({int(self.width)} x {int(self.height)})"


@dataclass(frozen=True)
class NormalizedRegion:
    x: float = 0.32
    y: float = 0.07
    width: float = 0.67
    height: float = 0.84

    def validate(self) -> None:
        values = (self.x, self.y, self.width, self.height)
        if any(value < 0.0 or value > 1.0 for value in values):
            raise ValueError("聊天区域坐标必须在 0 到 1 之间")
        if self.width <= 0 or self.height <= 0:
            raise ValueError("聊天区域必须有宽度和高度")
        if self.x + self.width > 1.0 or self.y + self.height > 1.0:
            raise ValueError("聊天区域超出窗口范围")

    def pixel_box(self, image: Image.Image) -> tuple[int, int, int, int]:
        self.validate()
        left = round(image.width * self.x)
        top = round(image.height * self.y)
        right = round(image.width * (self.x + self.width))
        bottom = round(image.height * (self.y + self.height))
        return left, top, right, bottom

    def pixel_size(self, image_width: int, image_height: int) -> tuple[int, int]:
        self.validate()
        return round(image_width * self.width), round(image_height * self.height)

    def screen_point(self, window: WindowInfo) -> tuple[float, float]:
        return (
            window.x + window.width * (self.x + self.width / 2),
            window.y + window.height * (self.y + self.height / 2),
        )


@dataclass(frozen=True)
class OCRLine:
    text: str
    confidence: float
    x: float
    y: float
    width: float
    height: float
    source: str


@dataclass(frozen=True)
class DetectedVoiceBubble:
    source: str
    x: float
    y: float
    width: float
    height: float
    duration_seconds: int | None
    confidence: float
    visual_hash: str
    suppressed_lines: tuple[OCRLine, ...] = ()


@dataclass
class Message:
    speaker: str
    text: str
    confidence: float
    source: str
    x: float
    y: float
    width: float
    height: float
    visible_time: str | None = None
    occurred_at: str | None = None
    kind: str = "text"
    sequence: int = 0
    original_text: str | None = None
    occurred_date: str | None = None
    date_source: str = "unresolved"
    is_deleted: bool = False
    edited_at: str | None = None
    fingerprint: str | None = None
    previous_fingerprint: str | None = None
    next_fingerprint: str | None = None
    voice_duration_seconds: int | None = None
    voice_visual_hash: str | None = None
    review_status: str = "pending"
    reviewed_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ChatSummary:
    partner_name: str
    message_count: int
    latest_occurred_at: str | None


@dataclass(frozen=True)
class ArchivedMessage:
    message_id: int
    partner_name: str
    message: Message
    source_path: Path


@dataclass(frozen=True)
class CaptureSettings:
    window: WindowInfo
    region: NormalizedRegion
    partner_name: str
    max_pages: int = 50
    scroll_pixels: int = 650
    stability_interval: float = 0.18
    stability_timeout: float = 3.0
    unchanged_limit: int = 3
    session_dir: Path = Path("data/captures")
    direction: str = "up"


@dataclass(frozen=True)
class CapturePageInfo:
    page_number: int
    source_path: Path
    sha256: str
    perceptual_hash: str
    ocr_status: str
    ocr_json: str | None = None


@dataclass(frozen=True)
class CaptureSessionSummary:
    session_id: int
    partner_name: str
    status: str
    direction: str
    page_count: int
    ocr_page_count: int
    session_dir: Path
    settings: dict[str, Any]
    started_at: str
    error_message: str | None = None


@dataclass(frozen=True)
class ReviewRecord:
    record: ArchivedMessage
    reasons: tuple[str, ...]
    status: str
    reviewed_at: str | None


@dataclass(frozen=True)
class CaptureConflict:
    conflict_id: int
    partner_name: str
    session_id: int
    kind: str
    details: dict[str, Any]
    status: str
    created_at: str
