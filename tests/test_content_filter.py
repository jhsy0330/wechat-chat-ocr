from pathlib import Path

from PIL import Image, ImageDraw

from wechat_archive.content_filter import TextMessageFilter
from wechat_archive.models import OCRLine
from wechat_archive.processing import parse_page


def line(text: str, box: tuple[int, int, int, int], size: int = 800) -> OCRLine:
    left, top, right, bottom = box
    return OCRLine(
        text=text,
        confidence=0.95,
        x=left / size,
        y=top / size,
        width=(right - left) / size,
        height=(bottom - top) / size,
        source="page.png",
    )


def test_keeps_text_bubbles_and_rejects_image_text(tmp_path: Path) -> None:
    image = Image.new("RGB", (800, 800), (250, 250, 250))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((500, 100, 740, 180), 12, fill=(179, 239, 165))
    draw.rounded_rectangle((50, 220, 350, 320), 12, fill=(238, 238, 240))

    draw.rectangle((50, 380, 350, 700), fill="white", outline=(210, 210, 210))
    draw.rectangle((70, 500, 330, 680), fill=(80, 150, 220))

    draw.rounded_rectangle((430, 380, 780, 720), 12, fill=(238, 238, 240))
    draw.rectangle((450, 470, 760, 680), fill=(250, 210, 30))
    draw.rectangle((480, 520, 730, 650), fill=(80, 170, 90))

    path = tmp_path / "page.png"
    image.save(path)
    content_filter = TextMessageFilter(path)

    outgoing = line("正常文字", (540, 122, 690, 155))
    incoming = line("正常回复", (85, 248, 280, 282))
    image_text = line("图片里的文字", (90, 410, 290, 445))
    card_text = line("分享卡片标题", (465, 410, 700, 445))

    assert content_filter.accepts(outgoing)
    assert content_filter.accepts(incoming)
    assert not content_filter.accepts(image_text)
    assert not content_filter.accepts(card_text)


def test_system_time_uses_separate_background_filter() -> None:
    timestamp = line("12:30", (365, 40, 435, 65))
    messages = parse_page(
        [timestamp],
        "女朋友",
        text_filter=lambda _line: False,
        system_filter=lambda _line: True,
    )
    assert len(messages) == 1
    assert messages[0].speaker == "系统"

    rejected = parse_page(
        [timestamp], "女朋友", system_filter=lambda _line: False
    )
    assert rejected == []
