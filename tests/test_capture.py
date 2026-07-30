from pathlib import Path

from PIL import Image, ImageDraw

from wechat_archive.capture import CaptureEngine
from wechat_archive.models import CaptureSettings, NormalizedRegion, WindowInfo


def test_capture_stops_after_unchanged_pages(tmp_path: Path, monkeypatch) -> None:
    state = {"page": 0}
    scrolls: list[int] = []

    def fake_capture(_window_id: int, destination: Path) -> None:
        image = Image.new("RGB", (800, 600), "white")
        draw = ImageDraw.Draw(image)
        draw.rectangle((80, 80 + state["page"] * 20, 300, 180), fill="#95ec69")
        draw.text((100, 110), f"page {state['page']}", fill="black")
        image.save(destination)

    def fake_scroll(_point, pixels) -> None:
        scrolls.append(pixels)
        state["page"] = min(1, state["page"] + 1)

    monkeypatch.setattr("wechat_archive.capture.macos.capture_window", fake_capture)
    monkeypatch.setattr("wechat_archive.capture.macos.activate_window", lambda _window: True)
    monkeypatch.setattr("wechat_archive.capture.macos.is_frontmost_wechat", lambda _window: True)
    monkeypatch.setattr("wechat_archive.capture.macos.post_scroll", fake_scroll)
    monkeypatch.setattr("wechat_archive.capture.macos.escape_pressed", lambda: False)
    monkeypatch.setattr("wechat_archive.capture.time.sleep", lambda _seconds: None)

    window = WindowInfo(1, 2, "WeChat", "chat", 0, 0, 800, 600)
    settings = CaptureSettings(
        window=window,
        region=NormalizedRegion(0, 0, 1, 1),
        partner_name="女朋友",
        max_pages=6,
        stability_interval=0,
        stability_timeout=0.2,
        unchanged_limit=2,
        session_dir=tmp_path,
    )
    pages = CaptureEngine().capture(settings)
    assert len(pages) == 2
    assert all(path.exists() for path in pages)
    assert scrolls and all(value > 0 for value in scrolls)


def test_capture_scrolls_down_with_negative_delta(tmp_path: Path, monkeypatch) -> None:
    scrolls: list[int] = []

    def fake_capture(_window_id, destination: Path) -> None:
        Image.new("RGB", (800, 600), "white").save(destination)

    monkeypatch.setattr("wechat_archive.capture.macos.capture_window", fake_capture)
    monkeypatch.setattr("wechat_archive.capture.macos.activate_window", lambda _window: True)
    monkeypatch.setattr("wechat_archive.capture.macos.is_frontmost_wechat", lambda _window: True)
    monkeypatch.setattr(
        "wechat_archive.capture.macos.post_scroll",
        lambda _point, pixels: scrolls.append(pixels),
    )
    monkeypatch.setattr("wechat_archive.capture.macos.escape_pressed", lambda: False)
    monkeypatch.setattr("wechat_archive.capture.time.sleep", lambda _seconds: None)
    settings = CaptureSettings(
        window=WindowInfo(1, 2, "WeChat", "chat", 0, 0, 800, 600),
        region=NormalizedRegion(0, 0, 1, 1),
        partner_name="女朋友",
        max_pages=2,
        stability_interval=0,
        stability_timeout=0.01,
        unchanged_limit=1,
        session_dir=tmp_path,
        direction="down",
    )
    CaptureEngine().capture(settings)
    assert scrolls == [-settings.scroll_pixels]
