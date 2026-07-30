from __future__ import annotations

import shutil
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageChops, ImageStat

from . import macos
from .models import CaptureSettings


StatusCallback = Callable[[str], None]
PageCallback = Callable[[int, Path], None]


class CaptureEngine:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._pause = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def pause(self) -> None:
        self._pause.set()

    def resume(self) -> None:
        self._pause.clear()

    def capture(
        self,
        settings: CaptureSettings,
        on_status: StatusCallback = lambda _message: None,
        on_page: PageCallback = lambda _number, _path: None,
    ) -> list[Path]:
        if settings.direction not in {"up", "down"}:
            raise ValueError("采集方向必须是 up 或 down")
        self._stop.clear()
        session_name = datetime.now().strftime("%Y%m%d-%H%M%S")
        session_dir = settings.session_dir / session_name
        working_dir = session_dir / ".working"
        working_dir.mkdir(parents=True, exist_ok=True)
        pages: list[Path] = []

        if not macos.activate_window(settings.window):
            raise RuntimeError("无法激活微信窗口，请确认微信仍在运行")
        time.sleep(0.5)

        on_status("正在截取当前页")
        first = self._capture_region(settings, working_dir / "initial.png")
        page_path = session_dir / "page-001.png"
        shutil.copy2(first, page_path)
        pages.append(page_path)
        on_page(1, page_path)
        previous_image = page_path
        unchanged = 0

        for page_number in range(2, settings.max_pages + 1):
            if self._should_stop():
                on_status("任务已停止")
                break
            self._wait_if_paused(on_status)
            if self._should_stop():
                break
            if not macos.is_frontmost_wechat(settings.window):
                raise RuntimeError("微信失去焦点，已停止以避免滚动其他窗口")

            direction_text = "向上" if settings.direction == "up" else "向下"
            on_status(f"{direction_text}滚动，准备第 {page_number} 页")
            scroll_delta = (
                settings.scroll_pixels
                if settings.direction == "up"
                else -settings.scroll_pixels
            )
            macos.post_scroll(
                settings.region.screen_point(settings.window), scroll_delta
            )
            time.sleep(0.25)
            stable_image = self._wait_for_stability(settings, working_dir, page_number)
            if self._same_content(previous_image, stable_image):
                unchanged += 1
                on_status(f"页面没有明显变化（{unchanged}/{settings.unchanged_limit}）")
                if unchanged >= settings.unchanged_limit:
                    edge = "顶部" if settings.direction == "up" else "底部"
                    on_status(f"已到达可读取记录的{edge}")
                    break
                continue

            unchanged = 0
            page_path = session_dir / f"page-{page_number:03d}.png"
            shutil.copy2(stable_image, page_path)
            pages.append(page_path)
            previous_image = page_path
            on_page(len(pages), page_path)

        shutil.rmtree(working_dir, ignore_errors=True)
        return pages

    def _capture_region(self, settings: CaptureSettings, destination: Path) -> Path:
        full_path = destination.with_name(destination.stem + "-full.png")
        macos.capture_window(settings.window.window_id, full_path)
        with Image.open(full_path) as image:
            image.crop(settings.region.pixel_box(image)).convert("RGB").save(destination)
        full_path.unlink(missing_ok=True)
        return destination

    def _wait_for_stability(
        self, settings: CaptureSettings, working_dir: Path, page_number: int
    ) -> Path:
        deadline = time.monotonic() + settings.stability_timeout
        previous_probe: Path | None = None
        stable_count = 0
        latest = working_dir / f"stable-{page_number}.png"
        attempt = 0
        while time.monotonic() < deadline:
            if self._should_stop():
                break
            attempt += 1
            candidate = working_dir / f"probe-{page_number}-{attempt}.png"
            self._capture_region(settings, candidate)
            if previous_probe is not None and self._same_content(
                previous_probe, candidate, threshold=0.15
            ):
                stable_count += 1
            else:
                stable_count = 0
            if previous_probe is not None:
                previous_probe.unlink(missing_ok=True)
            previous_probe = candidate
            shutil.copy2(candidate, latest)
            if stable_count >= 2:
                return latest
            time.sleep(settings.stability_interval)
        if not latest.exists():
            self._capture_region(settings, latest)
        return latest

    @staticmethod
    def _same_content(left: Path, right: Path, threshold: float = 0.35) -> bool:
        with Image.open(left) as left_image, Image.open(right) as right_image:
            size = (384, 384)
            first = left_image.convert("L").resize(size)
            second = right_image.convert("L").resize(size)
            difference = ImageChops.difference(first, second)
            mean_difference = ImageStat.Stat(difference).mean[0]
            return mean_difference <= threshold

    def _should_stop(self) -> bool:
        return self._stop.is_set() or macos.escape_pressed()

    def _wait_if_paused(self, on_status: StatusCallback) -> None:
        announced = False
        while self._pause.is_set() and not self._should_stop():
            if not announced:
                on_status("已暂停")
                announced = True
            time.sleep(0.1)
