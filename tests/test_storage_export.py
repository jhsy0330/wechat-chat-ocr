import json
from pathlib import Path

from wechat_archive.exporter import export_archive
from wechat_archive.models import Message
from wechat_archive.storage import ArchiveStore


def message(text: str, sequence: int = 0) -> Message:
    return Message(
        "女朋友", text, 0.9, "page.png", 0.1, 0.1, 0.2, 0.03,
        visible_time="20:00", sequence=sequence
    )


def test_incremental_storage_and_export(tmp_path: Path) -> None:
    store = ArchiveStore(tmp_path / "archive.sqlite3")
    all_messages, added = store.append_session(
        "女朋友", [message("一"), message("二")], 1, tmp_path / "session-1"
    )
    assert added == 2
    assert len(all_messages) == 2

    all_messages, added = store.append_session(
        "女朋友",
        [message("一"), message("二"), message("三")],
        2,
        tmp_path / "session-2",
    )
    assert added == 1
    assert [item.text for item in all_messages] == ["一", "二", "三"]

    html, markdown, data = export_archive(
        all_messages, "女朋友", tmp_path / "exports" / "chat"
    )
    assert "女朋友" in html.read_text(encoding="utf-8")
    assert "微信聊天记录" in markdown.read_text(encoding="utf-8")
    assert len(json.loads(data.read_text(encoding="utf-8"))) == 3


def test_existing_image_ocr_can_be_hidden_without_deleting_database(
    tmp_path: Path,
) -> None:
    store = ArchiveStore(tmp_path / "archive.sqlite3")
    store.append_session(
        "女朋友", [message("正常文字"), message("图片文字")], 1, tmp_path / "session-1"
    )
    visible, _added = store.append_session(
        "女朋友",
        [],
        0,
        tmp_path / "session-2",
        existing_message_filter=lambda item, _path: item.text != "图片文字",
    )
    assert [item.text for item in visible] == ["正常文字"]
    assert len(store.load_messages("女朋友")) == 2
