from __future__ import annotations

import json
from pathlib import Path
import sqlite3

from PIL import Image

from wechat_archive.fingerprints import file_sha256, image_dhash, message_fingerprint
from wechat_archive.models import (
    CaptureSettings,
    Message,
    NormalizedRegion,
    OCRLine,
    WindowInfo,
)
from wechat_archive.storage import ArchiveStore, SCHEMA_VERSION
from wechat_archive.worker import ArchiveWorker


def make_message(
    text: str,
    *,
    confidence: float = 0.95,
    source: str = "page.png",
    x: float = 0.65,
    width: float = 0.2,
    occurred_date: str | None = "2026-07-30",
) -> Message:
    return Message(
        speaker="我",
        text=text,
        original_text=text,
        confidence=confidence,
        source=source,
        x=x,
        y=0.2,
        width=width,
        height=0.08,
        occurred_date=occurred_date,
        date_source="recognized" if occurred_date else "unresolved",
    )


def test_page_and_message_fingerprints_are_stable(tmp_path: Path) -> None:
    image = tmp_path / "page.png"
    Image.new("RGB", (100, 80), "white").save(image)
    first = make_message(" 你好\n世界 ")
    second = make_message("你好世界")

    assert len(file_sha256(image)) == 64
    assert len(image_dhash(image)) == 16
    assert message_fingerprint(first) == message_fingerprint(second)


def test_capture_session_page_and_ocr_checkpoints(tmp_path: Path) -> None:
    store = ArchiveStore(tmp_path / "archive.sqlite3")
    session_dir = tmp_path / "captures" / "one"
    session_dir.mkdir(parents=True)
    settings = CaptureSettings(
        window=WindowInfo(1, 2, "WeChat", "chat", 0, 0, 800, 600),
        region=NormalizedRegion(0.1, 0.1, 0.8, 0.8),
        partner_name="联系人",
        session_dir=tmp_path / "captures",
        direction="down",
    )
    session_id = store.create_capture_session(settings, session_dir)
    page = session_dir / "page-001.png"
    Image.new("RGB", (200, 120), "white").save(page)

    checkpoint = store.record_capture_page(session_id, 1, page)
    store.save_page_ocr(session_id, 1, "[]")
    store.update_session_status(session_id, "interrupted")

    assert checkpoint.sha256 == file_sha256(page)
    loaded = store.load_capture_pages(session_id)[0]
    assert loaded.ocr_status == "completed"
    assert loaded.ocr_json == "[]"
    resumable = store.list_resumable_sessions()[0]
    assert resumable.session_id == session_id
    assert resumable.direction == "down"
    assert resumable.page_count == 1
    assert resumable.ocr_page_count == 1


def test_schema_upgrade_creates_backup_before_migrating_content(
    tmp_path: Path,
) -> None:
    database = tmp_path / "archive.sqlite3"
    store = ArchiveStore(database)
    store.append_session(
        "联系人",
        [make_message("升级前消息")],
        1,
        tmp_path / "captures" / "legacy",
    )
    with sqlite3.connect(database) as connection:
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION - 1}")

    migrated = ArchiveStore(database)

    backups = list(tmp_path.glob(f"archive-pre-v{SCHEMA_VERSION}-*.sqlite3"))
    assert migrated.last_backup_path in backups
    assert len(backups) == 1
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    with sqlite3.connect(backups[0]) as connection:
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1


def test_new_empty_database_does_not_create_migration_backup(tmp_path: Path) -> None:
    ArchiveStore(tmp_path / "archive.sqlite3")

    assert not list(tmp_path.glob("archive-pre-v*-*.sqlite3"))


def test_unique_middle_anchor_inserts_older_messages_before_existing(
    tmp_path: Path,
) -> None:
    store = ArchiveStore(tmp_path / "archive.sqlite3")
    store.append_session(
        "联系人",
        [make_message("锚点消息一"), make_message("锚点消息二")],
        1,
        tmp_path / "captures" / "old",
    )

    records, added, conflict_id = store.append_session_checked(
        "联系人",
        [
            make_message("更早的新消息"),
            make_message("锚点消息一"),
            make_message("锚点消息二"),
        ],
        1,
        tmp_path / "captures" / "new",
    )

    assert conflict_id is None
    assert added == 1
    assert [message.text for message in records] == [
        "更早的新消息",
        "锚点消息一",
        "锚点消息二",
    ]


def test_missing_anchor_is_staged_until_conflict_is_resolved(tmp_path: Path) -> None:
    store = ArchiveStore(tmp_path / "archive.sqlite3")
    store.append_session(
        "联系人",
        [make_message("原有消息一"), make_message("原有消息二")],
        1,
        tmp_path / "captures" / "old",
    )

    _records, added, conflict_id = store.append_session_checked(
        "联系人",
        [make_message("完全不同的新消息")],
        1,
        tmp_path / "captures" / "new",
    )

    assert added == 0
    assert conflict_id is not None
    assert store.count_chat_messages("联系人") == 2
    conflict = store.list_capture_conflicts()[0]
    assert conflict.kind == "missing_anchor"
    store.resolve_capture_conflict(conflict.conflict_id, "append")
    assert [record.message.text for record in store.load_chat_messages("联系人")] == [
        "原有消息一",
        "原有消息二",
        "完全不同的新消息",
    ]


def test_soft_deleted_message_is_not_reinserted_by_later_scan(
    tmp_path: Path,
) -> None:
    store = ArchiveStore(tmp_path / "archive.sqlite3")
    original = make_message("这是一条足够长且可以作为稳定锚点的消息")
    store.append_session("联系人", [original], 1, tmp_path / "captures" / "old")
    record = store.load_chat_messages("联系人")[0]
    store.set_message_deleted(record.message_id, True)

    _records, added, conflict_id = store.append_session_checked(
        "联系人",
        [make_message(original.text)],
        1,
        tmp_path / "captures" / "new",
    )

    assert conflict_id is None
    assert added == 0
    assert store.count_chat_messages("联系人") == 0
    assert len(store.load_chat_messages("联系人", include_deleted=True)) == 1


def test_equal_anchor_candidates_create_ambiguous_conflict(tmp_path: Path) -> None:
    store = ArchiveStore(tmp_path / "archive.sqlite3")
    store.append_session(
        "联系人",
        [
            make_message("重复锚点甲"),
            make_message("重复锚点乙"),
            make_message("重复锚点甲"),
            make_message("重复锚点乙"),
        ],
        1,
        tmp_path / "captures" / "old",
    )

    _records, added, conflict_id = store.append_session_checked(
        "联系人",
        [make_message("重复锚点甲"), make_message("重复锚点乙")],
        1,
        tmp_path / "captures" / "new",
    )

    assert added == 0
    assert conflict_id is not None
    conflict = store.list_capture_conflicts()[0]
    assert conflict.kind == "ambiguous_anchor"
    assert conflict.details["suggested_alignment"] is None


def test_empty_ocr_batch_completes_without_conflict(tmp_path: Path) -> None:
    store = ArchiveStore(tmp_path / "archive.sqlite3")
    store.append_session(
        "联系人",
        [make_message("原有消息")],
        1,
        tmp_path / "captures" / "old",
    )

    _records, added, conflict_id = store.append_session_checked(
        "联系人", [], 1, tmp_path / "captures" / "empty"
    )

    assert added == 0
    assert conflict_id is None
    assert not store.list_capture_conflicts()


def test_review_queue_reasons_and_status_are_persisted(tmp_path: Path) -> None:
    store = ArchiveStore(tmp_path / "archive.sqlite3")
    store.append_session(
        "联系人",
        [
            make_message(
                "需要复核",
                confidence=0.45,
                source="missing.png",
                x=0.45,
                width=0.1,
                occurred_date=None,
            )
        ],
        1,
        tmp_path / "captures" / "one",
    )

    review = store.list_review_records(confidence_threshold=0.75)[0]
    assert set(review.reasons) >= {
        "low_confidence",
        "missing_date",
        "uncertain_speaker",
        "missing_screenshot",
    }
    store.set_review_status([review.record.message_id], "confirmed")
    assert not store.list_review_records(confidence_threshold=0.75, status="pending")
    assert (
        store.list_review_records(confidence_threshold=0.75, status="confirmed")[
            0
        ].status
        == "confirmed"
    )


def test_restoring_deleted_message_resets_review_status(tmp_path: Path) -> None:
    store = ArchiveStore(tmp_path / "archive.sqlite3")
    store.append_session(
        "联系人",
        [make_message("需要重新确认", confidence=0.4)],
        1,
        tmp_path / "captures" / "one",
    )
    message_id = store.load_chat_messages("联系人")[0].message_id
    store.set_review_status([message_id], "confirmed")
    store.set_message_deleted(message_id, True)
    store.set_message_deleted(message_id, False)

    reviews = store.list_review_records(confidence_threshold=0.75, status="pending")
    assert [review.record.message_id for review in reviews] == [message_id]


def test_duplicate_review_detection_does_not_cross_contacts(tmp_path: Path) -> None:
    store = ArchiveStore(tmp_path / "archive.sqlite3")
    for partner in ("联系人甲", "联系人乙"):
        session = tmp_path / "captures" / partner
        session.mkdir(parents=True)
        Image.new("RGB", (100, 80), "white").save(session / "page.png")
        store.append_session(
            partner,
            [make_message("两个联系人都说过的相同消息")],
            1,
            session,
        )

    assert not store.list_review_records(confidence_threshold=0.75)


def test_restore_version_adds_revert_revision(tmp_path: Path) -> None:
    store = ArchiveStore(tmp_path / "archive.sqlite3")
    store.append_session(
        "联系人",
        [make_message("OCR 原文")],
        1,
        tmp_path / "captures" / "one",
    )
    record = store.load_chat_messages("联系人")[0]
    store.update_message(
        record.message_id,
        text="第一次修改",
        speaker="联系人",
        occurred_date="2026-07-29",
        occurred_time="20:15",
    )
    revision = store.load_message_revisions(record.message_id)[0]

    store.restore_message_version(record.message_id, revision["before"])

    restored = store.load_chat_messages("联系人")[0]
    assert restored.message.text == "OCR 原文"
    assert [
        item["action"] for item in store.load_message_revisions(record.message_id)
    ] == ["edit", "revert"]


def test_worker_resume_reuses_completed_page_ocr(tmp_path: Path, monkeypatch) -> None:
    settings = CaptureSettings(
        window=WindowInfo(1, 2, "WeChat", "chat", 0, 0, 800, 600),
        region=NormalizedRegion(0, 0, 1, 1),
        partner_name="联系人",
        session_dir=tmp_path / "captures",
    )
    store = ArchiveStore(tmp_path / "archive.sqlite3")
    session_dir = tmp_path / "captures" / "resume"
    session_dir.mkdir(parents=True)
    session_id = store.create_capture_session(settings, session_dir)
    page = session_dir / "page-001.png"
    Image.new("RGB", (800, 600), "white").save(page)
    store.record_capture_page(session_id, 1, page)
    line = OCRLine("已经识别", 0.96, 0.7, 0.2, 0.2, 0.08, page.name)
    store.save_page_ocr(session_id, 1, json.dumps([line.__dict__], ensure_ascii=False))
    store.update_session_status(session_id, "ocr")

    class FakeOCR:
        def ensure_helper(self) -> None:
            pass

        def recognize(self, _path: Path):
            raise AssertionError("已完成页面不应再次执行 OCR")

    class FakeFilter:
        def __init__(self, _path: Path) -> None:
            pass

        def accepts(self, _line: OCRLine) -> bool:
            return True

        def accepts_system(self, _line: OCRLine) -> bool:
            return True

        def detect_voice_bubbles(self, _lines: list[OCRLine]):
            return []

    monkeypatch.setattr("wechat_archive.worker.VisionOCR", FakeOCR)
    monkeypatch.setattr("wechat_archive.worker.TextMessageFilter", FakeFilter)
    monkeypatch.setattr(
        "wechat_archive.worker.export_archive",
        lambda *_args, **_kwargs: (tmp_path / "result.html", None, None),
    )
    worker = ArchiveWorker(settings, tmp_path, resume_session_id=session_id)
    monkeypatch.setattr(
        worker.engine,
        "capture",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("OCR 阶段恢复不应重新截图")
        ),
    )

    worker.run()

    assert store.load_capture_session(session_id).status == "completed"
    assert [record.message.text for record in store.load_chat_messages("联系人")] == [
        "已经识别"
    ]
