from pathlib import Path

from PIL import Image

from wechat_archive.models import NormalizedRegion, WindowInfo


def test_region_pixel_box() -> None:
    image = Image.new("RGB", (1000, 800))
    region = NormalizedRegion(0.2, 0.1, 0.7, 0.8)
    assert region.pixel_box(image) == (200, 80, 900, 720)
    assert region.pixel_size(image.width, image.height) == (700, 640)


def test_region_screen_point() -> None:
    window = WindowInfo(1, 2, "WeChat", "chat", 100, 50, 1000, 800)
    region = NormalizedRegion(0.2, 0.1, 0.6, 0.8)
    assert region.screen_point(window) == (600, 450)


def test_invalid_region() -> None:
    image = Image.new("RGB", (10, 10))
    region = NormalizedRegion(0.8, 0.2, 0.3, 0.5)
    try:
        region.pixel_box(image)
    except ValueError:
        pass
    else:
        raise AssertionError("invalid region was accepted")
