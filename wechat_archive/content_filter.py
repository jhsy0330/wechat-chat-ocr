from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image
from scipy import ndimage

from .fingerprints import image_object_dhash
from .models import DetectedVoiceBubble, OCRLine


VOICE_DURATION_PATTERN = re.compile(
    r"^[（(]?\s*[^\d\s]{0,2}\s*(?P<seconds>\d{1,2})"
    r"\s*(?:秒|[sS]|[\"'’”″]{1,2})\s*[（）()]?$"
)
BARE_DURATION_PATTERN = re.compile(r"^\s*(?P<seconds>\d{1,2})\s*$")


@dataclass(frozen=True)
class ComponentMetrics:
    width: float
    height: float
    fill_ratio: float


@dataclass(frozen=True)
class BubbleComponent:
    left: int
    top: int
    right: int
    bottom: int
    color: tuple[int, int, int]
    outgoing: bool

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top


def parse_voice_duration(text: str, *, allow_bare: bool = False) -> int | None:
    normalized = text.strip().replace("：", ":")
    match = VOICE_DURATION_PATTERN.fullmatch(normalized)
    if match is None and allow_bare:
        match = BARE_DURATION_PATTERN.fullmatch(normalized)
    if match is None:
        return None
    seconds = int(match.group("seconds"))
    return seconds if 1 <= seconds <= 60 else None


class TextMessageFilter:
    """Accept OCR only when it is backed by a WeChat text-bubble surface."""

    def __init__(self, image_path: Path) -> None:
        self.image_path = image_path
        with Image.open(image_path) as image:
            self.pixels = np.asarray(image.convert("RGB"), dtype=np.int16)
        self.image_height, self.image_width = self.pixels.shape[:2]
        self.background = self._estimate_background()
        self._component_cache: dict[
            tuple[int, int, int], tuple[np.ndarray, list[slice | None], np.ndarray]
        ] = {}

    def accepts(self, line: OCRLine) -> bool:
        box = self._pixel_box(line)
        candidate, seed = self._surrounding_color(box)
        if candidate is None or seed is None or not self._is_bubble_color(candidate):
            return False

        metrics = self._component_metrics(candidate, seed)
        if metrics is None:
            return False

        # Cards and image previews can have the same gray as incoming bubbles,
        # but their background component is large and interrupted by rich media.
        is_large_block = metrics.width >= 0.22 and metrics.height >= 0.16
        if is_large_block and metrics.fill_ratio < 0.72:
            return False
        if metrics.width >= 0.72 or metrics.height >= 0.38:
            return False
        return True

    def accepts_system(self, line: OCRLine) -> bool:
        candidate, _seed = self._surrounding_color(self._pixel_box(line))
        if candidate is None:
            return False
        return bool(np.max(np.abs(candidate - self.background)) <= 2)

    def detect_voice_bubbles(self, lines: list[OCRLine]) -> list[DetectedVoiceBubble]:
        detections: list[DetectedVoiceBubble] = []
        for component in self._bubble_components():
            icon_score = self._voice_icon_score(component)
            duration_line, duration, position_score = self._duration_candidate(
                component, lines, icon_score
            )
            if duration_line is not None:
                if position_score < 0.8 and icon_score < 0.42:
                    continue
                confidence = min(
                    0.99,
                    max(0.84, duration_line.confidence + 0.08)
                    + (0.04 if icon_score >= 0.55 else 0.0),
                )
            elif icon_score >= 0.78:
                confidence = 0.70
            else:
                continue

            suppressed = tuple(
                line
                for line in lines
                if self._line_inside_component(line, component) or line is duration_line
            )
            crop = Image.fromarray(
                self.pixels[
                    component.top : component.bottom,
                    component.left : component.right,
                ].astype(np.uint8),
                mode="RGB",
            )
            detections.append(
                DetectedVoiceBubble(
                    source=(
                        duration_line.source
                        if duration_line is not None
                        else (lines[0].source if lines else self.image_path.name)
                    ),
                    x=component.left / self.image_width,
                    y=component.top / self.image_height,
                    width=component.width / self.image_width,
                    height=component.height / self.image_height,
                    duration_seconds=duration,
                    confidence=confidence,
                    visual_hash=image_object_dhash(crop),
                    suppressed_lines=suppressed,
                )
            )
        return detections

    def _bubble_components(self) -> list[BubbleComponent]:
        red = self.pixels[:, :, 0]
        green = self.pixels[:, :, 1]
        blue = self.pixels[:, :, 2]
        outgoing_mask = (
            (red >= 110)
            & (red <= 220)
            & (green >= 160)
            & (green <= 255)
            & (blue >= 80)
            & (blue <= 220)
            & (green - red >= 18)
            & (green - blue >= 18)
        )
        brightness = (red + green + blue) / 3
        spread = np.maximum.reduce((red, green, blue)) - np.minimum.reduce(
            (red, green, blue)
        )
        incoming_mask = (
            (spread <= 12)
            & (brightness >= 205)
            & (brightness <= 255)
            & (np.abs(brightness - float(np.mean(self.background))) >= 5)
        )

        components: list[BubbleComponent] = []
        for mask, outgoing in ((outgoing_mask, True), (incoming_mask, False)):
            labels, count = ndimage.label(mask)
            objects = ndimage.find_objects(labels, max_label=count)
            areas = np.asarray(
                ndimage.sum(np.ones(labels.shape), labels, range(1, count + 1))
            )
            for label_id, component_slice in enumerate(objects, 1):
                if component_slice is None:
                    continue
                y_slice, x_slice = component_slice
                width = x_slice.stop - x_slice.start
                height = y_slice.stop - y_slice.start
                if not self._voice_component_geometry(
                    x_slice.start,
                    x_slice.stop,
                    width,
                    height,
                    float(areas[label_id - 1]) / max(1, width * height),
                ):
                    continue
                component_pixels = self.pixels[labels == label_id]
                color = tuple(
                    int(value) for value in np.median(component_pixels, axis=0)
                )
                components.append(
                    BubbleComponent(
                        x_slice.start,
                        y_slice.start,
                        x_slice.stop,
                        y_slice.stop,
                        color,
                        outgoing,
                    )
                )
        return components

    def _voice_component_geometry(
        self,
        left: int,
        right: int,
        width: int,
        height: int,
        fill_ratio: float,
    ) -> bool:
        center = (left + right) / 2 / self.image_width
        return bool(
            max(42, self.image_width * 0.055) <= width <= self.image_width * 0.55
            and max(18, self.image_height * 0.022)
            <= height
            <= self.image_height * 0.115
            and width / max(1, height) >= 1.55
            and fill_ratio >= 0.56
            and (center <= 0.46 or center >= 0.54)
        )

    def _duration_candidate(
        self,
        component: BubbleComponent,
        lines: list[OCRLine],
        icon_score: float,
    ) -> tuple[OCRLine | None, int | None, float]:
        candidates: list[tuple[float, OCRLine, int]] = []
        for line in lines:
            position_score = self._duration_position_score(line, component)
            if position_score <= 0:
                continue
            duration = parse_voice_duration(line.text, allow_bare=icon_score >= 0.74)
            if duration is None:
                continue
            candidates.append((position_score + line.confidence * 0.1, line, duration))
        if not candidates:
            return None, None, 0.0
        score, line, duration = max(candidates, key=lambda item: item[0])
        return line, duration, min(score, 1.0)

    def _duration_position_score(
        self, line: OCRLine, component: BubbleComponent
    ) -> float:
        left, top, right, bottom = self._pixel_box(line)
        line_center_y = (top + bottom) / 2
        component_center_y = (component.top + component.bottom) / 2
        if abs(line_center_y - component_center_y) > component.height * 0.8:
            return 0.0
        inside = component.left <= (left + right) / 2 <= component.right
        gap_limit = max(70, round(component.height * 2.2))
        if component.outgoing:
            correct_outside = (
                right <= component.left + 5 and right >= component.left - gap_limit
            )
        else:
            correct_outside = (
                left >= component.right - 5 and left <= component.right + gap_limit
            )
        if correct_outside:
            return 1.0
        return 0.64 if inside else 0.0

    def _line_inside_component(self, line: OCRLine, component: BubbleComponent) -> bool:
        left, top, right, bottom = self._pixel_box(line)
        center_x = (left + right) / 2
        center_y = (top + bottom) / 2
        return bool(
            component.left - 3 <= center_x <= component.right + 3
            and component.top - 3 <= center_y <= component.bottom + 3
        )

    def _voice_icon_score(self, component: BubbleComponent) -> float:
        margin_x = max(3, round(component.width * 0.07))
        margin_y = max(3, round(component.height * 0.14))
        crop = self.pixels[
            component.top + margin_y : component.bottom - margin_y,
            component.left + margin_x : component.right - margin_x,
        ]
        if crop.size == 0:
            return 0.0
        color = np.asarray(component.color)
        foreground = np.max(np.abs(crop - color), axis=2) >= 38
        positions = np.argwhere(foreground)
        if len(positions) < 6:
            return 0.0

        crop_height, crop_width = foreground.shape
        if component.outgoing:
            expected = foreground[:, round(crop_width * 0.55) :]
        else:
            expected = foreground[:, : round(crop_width * 0.45)]
        edge_share = float(np.count_nonzero(expected)) / len(positions)
        horizontal_span = (
            int(positions[:, 1].max()) - int(positions[:, 1].min()) + 1
        ) / crop_width
        vertical_span = (
            int(positions[:, 0].max()) - int(positions[:, 0].min()) + 1
        ) / crop_height
        foreground_ratio = len(positions) / max(1, crop_height * crop_width)
        labels, count = ndimage.label(foreground)
        areas = np.asarray(
            ndimage.sum(np.ones(labels.shape), labels, range(1, count + 1))
        )
        component_count = int(np.count_nonzero(areas >= 2))

        edge_score = min(1.0, edge_share / 0.72)
        concentration_score = max(0.0, min(1.0, (0.48 - horizontal_span) / 0.22))
        vertical_score = min(1.0, vertical_span / 0.52)
        density_score = 1.0 if 0.005 <= foreground_ratio <= 0.16 else 0.0
        pieces_score = 1.0 if 1 <= component_count <= 8 else 0.0
        return (
            edge_score * 0.34
            + concentration_score * 0.24
            + vertical_score * 0.16
            + density_score * 0.14
            + pieces_score * 0.12
        )

    def _estimate_background(self) -> np.ndarray:
        sample = self.pixels[::12, ::12]
        quantized = (sample // 4).reshape(-1, 3)
        colors, counts = np.unique(quantized, axis=0, return_counts=True)
        return colors[int(np.argmax(counts))] * 4 + 2

    def _pixel_box(self, line: OCRLine) -> tuple[int, int, int, int]:
        left = max(0, round(line.x * self.image_width))
        top = max(0, round(line.y * self.image_height))
        right = min(self.image_width, round((line.x + line.width) * self.image_width))
        bottom = min(
            self.image_height, round((line.y + line.height) * self.image_height)
        )
        return left, top, right, bottom

    def _surrounding_color(
        self, box: tuple[int, int, int, int]
    ) -> tuple[np.ndarray | None, tuple[int, int] | None]:
        left, top, right, bottom = box
        text_height = max(1, bottom - top)
        pad_x = max(5, round(text_height * 0.45))
        pad_y = max(4, round(text_height * 0.35))
        outer_left = max(0, left - pad_x)
        outer_top = max(0, top - pad_y)
        outer_right = min(self.image_width, right + pad_x)
        outer_bottom = min(self.image_height, bottom + pad_y)
        crop = self.pixels[outer_top:outer_bottom, outer_left:outer_right]
        if crop.size == 0:
            return None, None

        ring = np.ones(crop.shape[:2], dtype=bool)
        inner_left = left - outer_left
        inner_top = top - outer_top
        inner_right = right - outer_left
        inner_bottom = bottom - outer_top
        ring[inner_top:inner_bottom, inner_left:inner_right] = False
        ring_pixels = crop[ring]
        if len(ring_pixels) < 8:
            return None, None

        # Ignore dark glyph edges and quantize anti-aliased colors before finding
        # the dominant surface immediately around the recognized text.
        light_pixels = ring_pixels[np.mean(ring_pixels, axis=1) >= 120]
        if len(light_pixels) < 8:
            return None, None
        quantized = light_pixels // 4
        colors, counts = np.unique(quantized, axis=0, return_counts=True)
        candidate = colors[int(np.argmax(counts))] * 4 + 2

        distances = np.max(np.abs(crop - candidate), axis=2)
        valid_positions = np.argwhere(ring & (distances <= 7))
        if len(valid_positions) == 0:
            return None, None
        local_y, local_x = valid_positions[0]
        return candidate, (outer_top + int(local_y), outer_left + int(local_x))

    def _is_bubble_color(self, color: np.ndarray) -> bool:
        red, green, blue = (int(value) for value in color)
        is_green = (
            110 <= red <= 220
            and 160 <= green <= 255
            and 80 <= blue <= 220
            and green - red >= 18
            and green - blue >= 18
        )
        spread = max(red, green, blue) - min(red, green, blue)
        brightness = (red + green + blue) / 3
        background_brightness = float(np.mean(self.background))
        is_incoming_gray = (
            spread <= 10
            and 205 <= brightness <= 248
            and abs(brightness - background_brightness) >= 6
        )
        return is_green or is_incoming_gray

    def _component_metrics(
        self, color: np.ndarray, seed: tuple[int, int]
    ) -> ComponentMetrics | None:
        key = tuple(int(value // 4 * 4) for value in color)
        cached = self._component_cache.get(key)
        if cached is None:
            distance = np.max(np.abs(self.pixels - color), axis=2)
            labels, count = ndimage.label(distance <= 7)
            objects = ndimage.find_objects(labels, max_label=count)
            areas = np.asarray(
                ndimage.sum(np.ones(labels.shape), labels, range(1, count + 1))
            )
            cached = labels, objects, areas
            self._component_cache[key] = cached

        labels, objects, areas = cached
        label_id = int(labels[seed])
        if label_id <= 0 or label_id > len(objects):
            return None
        component_slice = objects[label_id - 1]
        if component_slice is None:
            return None
        y_slice, x_slice = component_slice
        width_pixels = x_slice.stop - x_slice.start
        height_pixels = y_slice.stop - y_slice.start
        box_area = max(1, width_pixels * height_pixels)
        return ComponentMetrics(
            width=width_pixels / self.image_width,
            height=height_pixels / self.image_height,
            fill_ratio=float(areas[label_id - 1]) / box_area,
        )
