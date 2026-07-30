from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image
from scipy import ndimage

from .models import OCRLine


@dataclass(frozen=True)
class ComponentMetrics:
    width: float
    height: float
    fill_ratio: float


class TextMessageFilter:
    """Accept OCR only when it is backed by a WeChat text-bubble surface."""

    def __init__(self, image_path: Path) -> None:
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
