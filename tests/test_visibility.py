import sqlite3
from pathlib import Path

from PIL import Image

from wechat_archive.models import Message
from wechat_archive.storage import ArchiveStore
from wechat_archive.visibility import ArchiveVisibilityScanner


def message(text: str, source: str = "page.png") -> Message:
    return Message(
        speaker="联系人",
        text=text,
        confidence=0.95,
        source=source,
        x=0.1,
        y=0.2,
        width=0.3,
        height=0.04,
    )


def mark_as_legacy(database: Path) -> None:
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE messages SET is_visible = NULL")


def test_scanner_persists_visibility_and_updates_summary(
    tmp_path: Path, monkeypatch
) -> None:
    database = tmp_path / "archive.sqlite3"
    session = tmp_path / "session"
    session.mkdir()
    Image.new("RGB", (100, 100), "white").save(session / "page.png")
    store = ArchiveStore(database)
    store.append_session("联系人", [message("正常文字"), message("图片文字")], 1, session)
    mark_as_legacy(database)

    class FakeFilter:
        def __init__(self, _path: Path) -> None:
            pass

        def accepts(self, line) -> bool:
            return line.text != "图片文字"

        def accepts_system(self, _line) -> bool:
            return True

    monkeypatch.setattr("wechat_archive.visibility.TextMessageFilter", FakeFilter)

    processed, total, complete = ArchiveVisibilityScanner(store).scan()

    assert (processed, total, complete) == (2, 2, True)
    assert store.unreviewed_message_count() == 0
    assert store.list_chats()[0].message_count == 1
    assert [record.message.text for record in store.load_chat_messages("联系人")] == [
        "正常文字"
    ]
    assert len(store.load_messages("联系人")) == 2


def test_scanner_retains_message_when_source_screenshot_is_missing(
    tmp_path: Path,
) -> None:
    database = tmp_path / "archive.sqlite3"
    store = ArchiveStore(database)
    store.append_session(
        "联系人", [message("保留待核对", "missing.png")], 1, tmp_path / "session"
    )
    mark_as_legacy(database)

    ArchiveVisibilityScanner(store).scan()

    assert store.list_chats()[0].message_count == 1
    assert store.load_chat_messages("联系人")[0].message.text == "保留待核对"
