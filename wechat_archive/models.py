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
