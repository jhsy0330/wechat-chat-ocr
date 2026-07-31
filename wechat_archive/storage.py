from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Callable

from .fingerprints import file_sha256, image_dhash, message_fingerprint
from .models import (
    ArchivedMessage,
    CaptureConflict,
    CapturePageInfo,
    CaptureSessionSummary,
    CaptureSettings,
    ChatSummary,
    Message,
    ReviewRecord,
)
from .processing import overlap_length
from .time_parser import parse_wechat_timestamp


SCHEMA_VERSION = 5


SCHEMA = """
PRAGMA journal_mode = WAL;
CREATE TABLE IF NOT EXISTS chats (
    id INTEGER PRIMARY KEY,
    partner_name TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY,
    chat_id INTEGER NOT NULL REFERENCES chats(id),
    started_at TEXT NOT NULL,
    page_count INTEGER NOT NULL,
    session_dir TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'completed',
    direction TEXT NOT NULL DEFAULT 'up',
    settings_json TEXT NOT NULL DEFAULT '{}',
    completed_at TEXT,
    last_page_number INTEGER NOT NULL DEFAULT 0,
    last_page_hash TEXT,
    last_page_phash TEXT,
    ocr_page_count INTEGER NOT NULL DEFAULT 0,
    error_message TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    chat_id INTEGER NOT NULL REFERENCES chats(id),
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    sequence INTEGER NOT NULL,
    speaker TEXT NOT NULL,
    text TEXT NOT NULL,
    confidence REAL NOT NULL,
    source TEXT NOT NULL,
    x REAL NOT NULL,
    y REAL NOT NULL,
    width REAL NOT NULL,
    height REAL NOT NULL,
    visible_time TEXT,
    occurred_at TEXT,
    kind TEXT NOT NULL,
    is_visible INTEGER DEFAULT 1,
    original_text TEXT,
    occurred_date TEXT,
    date_source TEXT NOT NULL DEFAULT 'unresolved',
    is_deleted INTEGER NOT NULL DEFAULT 0,
    edited_at TEXT,
    fingerprint TEXT,
    previous_fingerprint TEXT,
    next_fingerprint TEXT,
    voice_duration_seconds INTEGER,
    voice_visual_hash TEXT,
    UNIQUE(chat_id, sequence)
);
CREATE TABLE IF NOT EXISTS message_revisions (
    id INTEGER PRIMARY KEY,
    message_id INTEGER NOT NULL REFERENCES messages(id),
    action TEXT NOT NULL,
    before_json TEXT NOT NULL,
    after_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_chat_sequence
ON messages(chat_id, sequence);
CREATE TABLE IF NOT EXISTS capture_pages (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    page_number INTEGER NOT NULL,
    source TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    perceptual_hash TEXT NOT NULL,
    ocr_status TEXT NOT NULL DEFAULT 'pending',
    ocr_json TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(session_id, page_number)
);
CREATE TABLE IF NOT EXISTS message_reviews (
    message_id INTEGER PRIMARY KEY REFERENCES messages(id),
    status TEXT NOT NULL DEFAULT 'pending',
    reviewed_at TEXT
);
CREATE TABLE IF NOT EXISTS capture_conflicts (
    id INTEGER PRIMARY KEY,
    chat_id INTEGER NOT NULL REFERENCES chats(id),
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    kind TEXT NOT NULL,
    details_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    resolved_at TEXT
);
"""


class ArchiveStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.last_backup_path: Path | None = None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._backup_legacy_database()
        with self._connect() as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise RuntimeError(f"数据库版本 {version} 高于程序支持的版本 {SCHEMA_VERSION}")
            connection.executescript(SCHEMA)
            self._migrate(connection)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _backup_legacy_database(self) -> None:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return
        with sqlite3.connect(self.path) as source:
            version = int(source.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise RuntimeError(f"数据库版本 {version} 高于程序支持的版本 {SCHEMA_VERSION}")
            if version >= SCHEMA_VERSION:
                return
            table_names = {
                str(row[0])
                for row in source.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            core_tables = table_names.intersection({"chats", "sessions", "messages"})
            has_legacy_content = any(
                source.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is not None
                for table in core_tables
            )
            if not has_legacy_content:
                return
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            backup_path = self.path.with_name(
                f"{self.path.stem}-pre-v{SCHEMA_VERSION}-{timestamp}{self.path.suffix}"
            )
            with sqlite3.connect(backup_path) as target:
                source.backup(target)
            self.last_backup_path = backup_path

    def _migrate(self, connection: sqlite3.Connection) -> None:
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(messages)")
        }
        if "occurred_at" not in columns:
            connection.execute("ALTER TABLE messages ADD COLUMN occurred_at TEXT")
        if "is_visible" not in columns:
            # NULL distinguishes legacy rows that still need screenshot validation.
            connection.execute("ALTER TABLE messages ADD COLUMN is_visible INTEGER")
        additions = {
            "original_text": "TEXT",
            "occurred_date": "TEXT",
            "date_source": "TEXT NOT NULL DEFAULT 'unresolved'",
            "is_deleted": "INTEGER NOT NULL DEFAULT 0",
            "edited_at": "TEXT",
            "fingerprint": "TEXT",
            "previous_fingerprint": "TEXT",
            "next_fingerprint": "TEXT",
            "voice_duration_seconds": "INTEGER",
            "voice_visual_hash": "TEXT",
        }
        for name, declaration in additions.items():
            if name not in columns:
                connection.execute(
                    f"ALTER TABLE messages ADD COLUMN {name} {declaration}"
                )
        connection.execute(
            "UPDATE messages SET original_text = text WHERE original_text IS NULL"
        )
        connection.execute(
            """
            UPDATE messages
            SET occurred_date = substr(occurred_at, 1, 10)
            WHERE occurred_date IS NULL AND occurred_at IS NOT NULL
            """
        )
        connection.execute(
            """
            UPDATE messages
            SET date_source = CASE
                WHEN occurred_date IS NOT NULL AND occurred_date != '' THEN 'recognized'
                ELSE 'unresolved'
            END
            WHERE date_source IS NULL OR date_source = '' OR date_source = 'unresolved'
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS message_revisions (
                id INTEGER PRIMARY KEY,
                message_id INTEGER NOT NULL REFERENCES messages(id),
                action TEXT NOT NULL,
                before_json TEXT NOT NULL,
                after_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS revisions_message_created
            ON message_revisions(message_id, created_at)
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS messages_chat_visible_sequence
            ON messages(chat_id, is_visible, sequence)
            """
        )
        session_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(sessions)")
        }
        session_additions = {
            "status": "TEXT NOT NULL DEFAULT 'completed'",
            "direction": "TEXT NOT NULL DEFAULT 'up'",
            "settings_json": "TEXT NOT NULL DEFAULT '{}'",
            "completed_at": "TEXT",
            "last_page_number": "INTEGER NOT NULL DEFAULT 0",
            "last_page_hash": "TEXT",
            "last_page_phash": "TEXT",
            "ocr_page_count": "INTEGER NOT NULL DEFAULT 0",
            "error_message": "TEXT",
        }
        for name, declaration in session_additions.items():
            if name not in session_columns:
                connection.execute(
                    f"ALTER TABLE sessions ADD COLUMN {name} {declaration}"
                )
        connection.execute(
            "UPDATE sessions SET status = 'completed' WHERE status IS NULL OR status = ''"
        )
        connection.executescript(
            """
            CREATE INDEX IF NOT EXISTS sessions_status_started
            ON sessions(status, started_at);
            CREATE INDEX IF NOT EXISTS messages_chat_fingerprint
            ON messages(chat_id, fingerprint);
            CREATE INDEX IF NOT EXISTS reviews_status
            ON message_reviews(status);
            CREATE INDEX IF NOT EXISTS conflicts_status_created
            ON capture_conflicts(status, created_at);
            """
        )
        self._migrate_message_fingerprints(connection)
        self._migrate_session_paths(connection)

    def _migrate_message_fingerprints(self, connection: sqlite3.Connection) -> None:
        rows = connection.execute(
            """
            SELECT m.*, s.started_at
            FROM messages m JOIN sessions s ON s.id = m.session_id
            WHERE m.fingerprint IS NULL OR m.fingerprint = ''
            ORDER BY m.chat_id, m.sequence
            """
        ).fetchall()
        if not rows:
            return
        connection.executemany(
            "UPDATE messages SET fingerprint = ? WHERE id = ?",
            [
                (message_fingerprint(self._row_to_message(row)), int(row["id"]))
                for row in rows
            ],
        )
        for chat_id in {int(row["chat_id"]) for row in rows}:
            self._refresh_adjacency(connection, chat_id)

    def _migrate_session_paths(self, connection: sqlite3.Connection) -> None:
        archive_root = self.path.parent.resolve()
        rows = connection.execute("SELECT id, session_dir FROM sessions").fetchall()
        for row in rows:
            stored = Path(str(row["session_dir"])).expanduser()
            if not stored.is_absolute():
                continue
            try:
                relative = stored.resolve().relative_to(archive_root)
            except ValueError:
                continue
            connection.execute(
                "UPDATE sessions SET session_dir = ? WHERE id = ?",
                (str(relative), int(row["id"])),
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _chat_id(self, connection: sqlite3.Connection, partner_name: str) -> int:
        connection.execute(
            "INSERT OR IGNORE INTO chats(partner_name, created_at) VALUES (?, ?)",
            (partner_name, datetime.now().isoformat(timespec="seconds")),
        )
        row = connection.execute(
            "SELECT id FROM chats WHERE partner_name = ?", (partner_name,)
        ).fetchone()
        if row is None:
            raise RuntimeError("无法创建聊天档案")
        return int(row["id"])

    def load_messages(self, partner_name: str) -> list[Message]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT m.*, s.started_at FROM messages m
                JOIN sessions s ON s.id = m.session_id
                JOIN chats c ON c.id = m.chat_id
                WHERE c.partner_name = ? ORDER BY m.sequence
                """,
                (partner_name,),
            ).fetchall()
        return [self._row_to_message(row) for row in rows]

    def list_chats(self) -> list[ChatSummary]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT
                    c.id,
                    c.partner_name,
                    COUNT(m.id) AS message_count,
                    (
                        SELECT COALESCE(latest.occurred_at, latest.occurred_date)
                        FROM messages latest
                        WHERE latest.chat_id = c.id
                          AND latest.is_visible = 1
                          AND latest.is_deleted = 0
                          AND (latest.occurred_at IS NOT NULL OR latest.occurred_date IS NOT NULL)
                        ORDER BY latest.sequence DESC, latest.id DESC
                        LIMIT 1
                    ) AS latest_occurred_at
                FROM chats c
                LEFT JOIN messages m ON m.chat_id = c.id
                    AND m.is_visible = 1 AND m.is_deleted = 0
                GROUP BY c.id, c.partner_name
                """
            ).fetchall()
            summaries = [
                ChatSummary(
                    partner_name=str(row["partner_name"]),
                    message_count=int(row["message_count"]),
                    latest_occurred_at=(
                        str(row["latest_occurred_at"])
                        if row["latest_occurred_at"]
                        else self._latest_legacy_timestamp(connection, int(row["id"]))
                    ),
                )
                for row in rows
            ]
        return sorted(
            summaries,
            key=lambda summary: (
                -self._timestamp_value(summary.latest_occurred_at),
                summary.partner_name.casefold(),
            ),
        )

    def count_chat_messages(
        self,
        partner_name: str,
        *,
        query: str = "",
        speaker: str | None = None,
        include_deleted: bool = False,
    ) -> int:
        conditions, parameters = self._chat_message_filters(
            partner_name,
            query=query,
            speaker=speaker,
            include_deleted=include_deleted,
        )
        with closing(self._connect()) as connection:
            row = connection.execute(
                f"""
                SELECT COUNT(*) AS message_count
                FROM messages m
                JOIN chats c ON c.id = m.chat_id
                WHERE {' AND '.join(conditions)}
                """,
                parameters,
            ).fetchone()
        return int(row["message_count"]) if row is not None else 0

    def list_chat_speakers(self, partner_name: str) -> list[str]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT m.speaker
                FROM messages m
                JOIN chats c ON c.id = m.chat_id
                WHERE c.partner_name = ? AND m.is_visible = 1 AND m.is_deleted = 0
                ORDER BY m.speaker COLLATE NOCASE
                """,
                (partner_name,),
            ).fetchall()
        return [str(row["speaker"]) for row in rows]

    def load_chat_messages(
        self,
        partner_name: str,
        *,
        query: str = "",
        speaker: str | None = None,
        limit: int | None = None,
        offset: int = 0,
        newest_first: bool = False,
        include_deleted: bool = False,
    ) -> list[ArchivedMessage]:
        if limit is not None and limit <= 0:
            return []
        if offset < 0:
            raise ValueError("消息偏移量不能为负数")
        conditions, parameters = self._chat_message_filters(
            partner_name,
            query=query,
            speaker=speaker,
            include_deleted=include_deleted,
        )
        order = "DESC" if newest_first else "ASC"
        pagination = ""
        if limit is not None:
            pagination = "LIMIT ? OFFSET ?"
            parameters.extend((limit, offset))
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"""
                SELECT m.*, s.session_dir, s.started_at, c.partner_name
                FROM messages m
                JOIN sessions s ON s.id = m.session_id
                JOIN chats c ON c.id = m.chat_id
                WHERE {' AND '.join(conditions)}
                ORDER BY m.sequence {order}, m.id {order}
                {pagination}
                """,
                parameters,
            ).fetchall()
        return [self._row_to_archived_message(row) for row in rows]

    def unreviewed_message_count(self) -> int:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS message_count FROM messages WHERE is_visible IS NULL"
            ).fetchone()
        return int(row["message_count"]) if row is not None else 0

    def load_unreviewed_messages(self, limit: int = 500) -> list[ArchivedMessage]:
        if limit <= 0:
            return []
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT m.*, s.session_dir, s.started_at, c.partner_name
                FROM messages m
                JOIN sessions s ON s.id = m.session_id
                JOIN chats c ON c.id = m.chat_id
                WHERE m.is_visible IS NULL
                ORDER BY m.session_id, m.source, m.id
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._row_to_archived_message(row) for row in rows]

    def set_message_visibility(self, results: Sequence[tuple[int, bool]]) -> None:
        if not results:
            return
        with self._connect() as connection:
            connection.executemany(
                "UPDATE messages SET is_visible = ? WHERE id = ?",
                [(1 if visible else 0, message_id) for message_id, visible in results],
            )

    def create_capture_session(
        self, settings: CaptureSettings, session_dir: Path
    ) -> int:
        payload = {
            "partner_name": settings.partner_name,
            "region": {
                "x": settings.region.x,
                "y": settings.region.y,
                "width": settings.region.width,
                "height": settings.region.height,
            },
            "max_pages": settings.max_pages,
            "scroll_pixels": settings.scroll_pixels,
            "stability_interval": settings.stability_interval,
            "stability_timeout": settings.stability_timeout,
            "unchanged_limit": settings.unchanged_limit,
            "direction": settings.direction,
        }
        with self._connect() as connection:
            chat_id = self._chat_id(connection, settings.partner_name)
            cursor = connection.execute(
                """
                INSERT INTO sessions(
                    chat_id, started_at, page_count, session_dir, status,
                    direction, settings_json
                ) VALUES (?, ?, 0, ?, 'capturing', ?, ?)
                """,
                (
                    chat_id,
                    datetime.now().astimezone().isoformat(timespec="seconds"),
                    self.portable_session_dir(session_dir),
                    settings.direction,
                    json.dumps(payload, ensure_ascii=False),
                ),
            )
        return int(cursor.lastrowid)

    def update_session_status(
        self, session_id: int, status: str, error_message: str | None = None
    ) -> None:
        completed_at = (
            datetime.now().astimezone().isoformat(timespec="seconds")
            if status in {"completed", "conflict", "abandoned"}
            else None
        )
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE sessions
                SET status = ?, error_message = ?, completed_at = ?
                WHERE id = ?
                """,
                (status, error_message, completed_at, session_id),
            )

    def record_capture_page(
        self, session_id: int, page_number: int, source_path: Path
    ) -> CapturePageInfo:
        sha256 = file_sha256(source_path)
        perceptual_hash = image_dhash(source_path)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO capture_pages(
                    session_id, page_number, source, sha256, perceptual_hash,
                    ocr_status, created_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', ?)
                ON CONFLICT(session_id, page_number) DO UPDATE SET
                    source = excluded.source,
                    sha256 = excluded.sha256,
                    perceptual_hash = excluded.perceptual_hash
                """,
                (
                    session_id,
                    page_number,
                    source_path.name,
                    sha256,
                    perceptual_hash,
                    datetime.now().astimezone().isoformat(timespec="seconds"),
                ),
            )
            connection.execute(
                """
                UPDATE sessions
                SET page_count = MAX(page_count, ?), last_page_number = ?,
                    last_page_hash = ?, last_page_phash = ?
                WHERE id = ?
                """,
                (page_number, page_number, sha256, perceptual_hash, session_id),
            )
        return CapturePageInfo(
            page_number,
            source_path,
            sha256,
            perceptual_hash,
            "pending",
        )

    def save_page_ocr(self, session_id: int, page_number: int, ocr_json: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE capture_pages
                SET ocr_status = 'completed', ocr_json = ?
                WHERE session_id = ? AND page_number = ?
                """,
                (ocr_json, session_id, page_number),
            )
            connection.execute(
                """
                UPDATE sessions SET ocr_page_count = (
                    SELECT COUNT(*) FROM capture_pages
                    WHERE session_id = ? AND ocr_status = 'completed'
                ) WHERE id = ?
                """,
                (session_id, session_id),
            )

    def load_capture_pages(self, session_id: int) -> list[CapturePageInfo]:
        with closing(self._connect()) as connection:
            session = connection.execute(
                "SELECT session_dir FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if session is None:
                raise ValueError("采集会话不存在")
            session_dir = self.resolve_session_dir(str(session["session_dir"]))
            rows = connection.execute(
                """
                SELECT page_number, source, sha256, perceptual_hash,
                       ocr_status, ocr_json
                FROM capture_pages WHERE session_id = ? ORDER BY page_number
                """,
                (session_id,),
            ).fetchall()
        return [
            CapturePageInfo(
                page_number=int(row["page_number"]),
                source_path=(session_dir / str(row["source"])).resolve(),
                sha256=str(row["sha256"]),
                perceptual_hash=str(row["perceptual_hash"]),
                ocr_status=str(row["ocr_status"]),
                ocr_json=str(row["ocr_json"]) if row["ocr_json"] else None,
            )
            for row in rows
        ]

    def load_capture_session(self, session_id: int) -> CaptureSessionSummary:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT s.*, c.partner_name FROM sessions s
                JOIN chats c ON c.id = s.chat_id WHERE s.id = ?
                """,
                (session_id,),
            ).fetchone()
        if row is None:
            raise ValueError("采集会话不存在")
        return self._row_to_capture_session(row)

    def list_resumable_sessions(self) -> list[CaptureSessionSummary]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT s.*, c.partner_name FROM sessions s
                JOIN chats c ON c.id = s.chat_id
                WHERE s.status IN ('capturing', 'ocr', 'interrupted', 'failed')
                ORDER BY s.started_at DESC
                """
            ).fetchall()
        return [self._row_to_capture_session(row) for row in rows]

    def abandon_session(self, session_id: int) -> None:
        self.update_session_status(session_id, "abandoned")

    def _row_to_capture_session(self, row: sqlite3.Row) -> CaptureSessionSummary:
        try:
            settings = json.loads(str(row["settings_json"] or "{}"))
        except json.JSONDecodeError:
            settings = {}
        return CaptureSessionSummary(
            session_id=int(row["id"]),
            partner_name=str(row["partner_name"]),
            status=str(row["status"]),
            direction=str(row["direction"]),
            page_count=int(row["page_count"]),
            ocr_page_count=int(row["ocr_page_count"]),
            session_dir=self.resolve_session_dir(str(row["session_dir"])).resolve(),
            settings=settings,
            started_at=str(row["started_at"]),
            error_message=(str(row["error_message"]) if row["error_message"] else None),
        )

    def update_message(
        self,
        message_id: int,
        *,
        text: str,
        speaker: str,
        occurred_date: str | None,
        occurred_time: str | None,
    ) -> None:
        text = text.strip()
        speaker = speaker.strip()
        if not text or not speaker:
            raise ValueError("消息内容和发送人不能为空")
        occurred_at = None
        if occurred_time and not occurred_date:
            raise ValueError("填写时间时必须同时填写日期")
        if occurred_date:
            try:
                datetime.strptime(occurred_date, "%Y-%m-%d")
                if occurred_time:
                    datetime.strptime(occurred_time, "%H:%M")
                    occurred_at = f"{occurred_date}T{occurred_time}"
            except ValueError as error:
                raise ValueError("日期应为 YYYY-MM-DD，时间应为 HH:MM") from error
        with self._connect() as connection:
            before = self._message_snapshot(connection, message_id)
            if before["kind"] == "voice":
                text = "[语音消息]"
            edited_at = datetime.now().astimezone().isoformat(timespec="seconds")
            connection.execute(
                """
                UPDATE messages
                SET text = ?, speaker = ?, occurred_date = ?, occurred_at = ?,
                    date_source = 'manual', edited_at = ?
                WHERE id = ?
                """,
                (
                    text,
                    speaker,
                    occurred_date or None,
                    occurred_at,
                    edited_at,
                    message_id,
                ),
            )
            after = self._message_snapshot(connection, message_id)
            self._add_revision(connection, message_id, "edit", before, after)
            connection.execute(
                "DELETE FROM message_reviews WHERE message_id = ?", (message_id,)
            )

    def set_message_deleted(self, message_id: int, deleted: bool) -> None:
        self.set_messages_deleted([message_id], deleted)

    def set_messages_deleted(self, message_ids: Sequence[int], deleted: bool) -> None:
        unique_ids = list(dict.fromkeys(message_ids))
        if not unique_ids:
            return
        with self._connect() as connection:
            for message_id in unique_ids:
                before = self._message_snapshot(connection, message_id)
                if bool(before["is_deleted"]) == deleted:
                    continue
                connection.execute(
                    "UPDATE messages SET is_deleted = ? WHERE id = ?",
                    (1 if deleted else 0, message_id),
                )
                after = self._message_snapshot(connection, message_id)
                self._add_revision(
                    connection,
                    message_id,
                    "delete" if deleted else "restore",
                    before,
                    after,
                )
                if not deleted:
                    connection.execute(
                        "DELETE FROM message_reviews WHERE message_id = ?",
                        (message_id,),
                    )

    def load_message_revisions(self, message_id: int) -> list[dict[str, object]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT action, before_json, after_json, created_at
                FROM message_revisions WHERE message_id = ? ORDER BY id
                """,
                (message_id,),
            ).fetchall()
        return [
            {
                "action": str(row["action"]),
                "before": json.loads(str(row["before_json"])),
                "after": json.loads(str(row["after_json"])),
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]

    @staticmethod
    def _message_snapshot(
        connection: sqlite3.Connection, message_id: int
    ) -> dict[str, object]:
        row = connection.execute(
            """
            SELECT text, speaker, occurred_date, occurred_at, date_source,
                   is_deleted, edited_at, original_text, kind,
                   voice_duration_seconds
            FROM messages WHERE id = ?
            """,
            (message_id,),
        ).fetchone()
        if row is None:
            raise ValueError("聊天记录不存在")
        return {key: row[key] for key in row.keys()}

    def restore_message_version(
        self, message_id: int, snapshot: dict[str, object]
    ) -> None:
        with self._connect() as connection:
            before = self._message_snapshot(connection, message_id)
            edited_at = datetime.now().astimezone().isoformat(timespec="seconds")
            connection.execute(
                """
                UPDATE messages
                SET text = ?, speaker = ?, occurred_date = ?, occurred_at = ?,
                    date_source = ?, is_deleted = ?, edited_at = ?
                WHERE id = ?
                """,
                (
                    str(snapshot.get("text") or before["text"]),
                    str(snapshot.get("speaker") or before["speaker"]),
                    snapshot.get("occurred_date"),
                    snapshot.get("occurred_at"),
                    str(snapshot.get("date_source") or "manual"),
                    1 if bool(snapshot.get("is_deleted", False)) else 0,
                    edited_at,
                    message_id,
                ),
            )
            after = self._message_snapshot(connection, message_id)
            self._add_revision(connection, message_id, "revert", before, after)
            connection.execute(
                "DELETE FROM message_reviews WHERE message_id = ?", (message_id,)
            )

    @staticmethod
    def _add_revision(
        connection: sqlite3.Connection,
        message_id: int,
        action: str,
        before: dict[str, object],
        after: dict[str, object],
    ) -> None:
        connection.execute(
            """
            INSERT INTO message_revisions(
                message_id, action, before_json, after_json, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                message_id,
                action,
                json.dumps(before, ensure_ascii=False),
                json.dumps(after, ensure_ascii=False),
                datetime.now().astimezone().isoformat(timespec="seconds"),
            ),
        )

    def append_session(
        self,
        partner_name: str,
        messages: Sequence[Message],
        page_count: int,
        session_dir: Path,
        existing_message_filter: Callable[[Message, Path], bool] | None = None,
    ) -> tuple[list[Message], int]:
        all_messages, added, _conflict_id = self.append_session_checked(
            partner_name,
            messages,
            page_count,
            session_dir,
            existing_message_filter=existing_message_filter,
            detect_conflicts=False,
        )
        return all_messages, added

    def append_session_checked(
        self,
        partner_name: str,
        messages: Sequence[Message],
        page_count: int,
        session_dir: Path,
        *,
        session_id: int | None = None,
        existing_message_filter: Callable[[Message, Path], bool] | None = None,
        detect_conflicts: bool = True,
    ) -> tuple[list[Message], int, int | None]:
        additions_count = 0
        conflict_id: int | None = None
        with self._connect() as connection:
            chat_id = self._chat_id(connection, partner_name)
            if session_id is None:
                cursor = connection.execute(
                    """
                    INSERT INTO sessions(
                        chat_id, started_at, page_count, session_dir, status
                    ) VALUES (?, ?, ?, ?, 'completed')
                    """,
                    (
                        chat_id,
                        datetime.now().astimezone().isoformat(timespec="seconds"),
                        page_count,
                        self.portable_session_dir(session_dir),
                    ),
                )
                session_id = int(cursor.lastrowid)
            else:
                session_row = connection.execute(
                    "SELECT chat_id FROM sessions WHERE id = ?", (session_id,)
                ).fetchone()
                if session_row is None or int(session_row["chat_id"]) != chat_id:
                    raise ValueError("采集会话与联系人不匹配")
            existing_rows = connection.execute(
                """
                SELECT m.*, s.session_dir, s.started_at FROM messages m
                JOIN sessions s ON s.id = m.session_id
                WHERE m.chat_id = ? AND m.session_id != ? ORDER BY m.sequence
                """,
                (chat_id, session_id),
            ).fetchall()
            all_existing = [self._row_to_message(row) for row in existing_rows]
            overlap_existing: list[Message] = []
            for message, row in zip(all_existing, existing_rows, strict=True):
                stored_visibility = row["is_visible"]
                if stored_visibility is not None:
                    visible = bool(stored_visibility)
                elif existing_message_filter is not None:
                    visible = existing_message_filter(
                        message,
                        self.resolve_session_dir(str(row["session_dir"]))
                        / message.source,
                    )
                    connection.execute(
                        "UPDATE messages SET is_visible = ? WHERE id = ?",
                        (1 if visible else 0, int(row["id"])),
                    )
                else:
                    visible = True
                if visible:
                    overlap_existing.append(message)

            incoming = list(messages)
            if not incoming:
                connection.execute(
                    """
                    UPDATE sessions
                    SET status = 'completed', completed_at = ?, page_count = ?
                    WHERE id = ?
                    """,
                    (
                        datetime.now().astimezone().isoformat(timespec="seconds"),
                        page_count,
                        session_id,
                    ),
                )
            elif not all_existing:
                self._insert_messages(connection, chat_id, session_id, 1, incoming)
                additions_count = len(incoming)
            elif detect_conflicts:
                alignment, ambiguous = self._find_alignment(overlap_existing, incoming)
                if alignment is None or ambiguous:
                    conflict_id = self._create_capture_conflict(
                        connection,
                        chat_id,
                        session_id,
                        partner_name,
                        overlap_existing,
                        incoming,
                        "ambiguous_anchor" if ambiguous else "missing_anchor",
                        alignment,
                    )
                    connection.execute(
                        "UPDATE sessions SET status = 'conflict' WHERE id = ?",
                        (session_id,),
                    )
                else:
                    existing_start, incoming_start, length = alignment
                    before = incoming[:incoming_start]
                    after = incoming[incoming_start + length :]
                    target_sequence = overlap_existing[existing_start].sequence
                    if before:
                        self._shift_sequences(
                            connection, chat_id, target_sequence, len(before)
                        )
                        self._insert_messages(
                            connection,
                            chat_id,
                            session_id,
                            target_sequence,
                            before,
                        )
                    matched_end = overlap_existing[
                        existing_start + length - 1
                    ].sequence + len(before)
                    if after:
                        insert_at = matched_end + 1
                        self._shift_sequences(
                            connection, chat_id, insert_at, len(after)
                        )
                        self._insert_messages(
                            connection,
                            chat_id,
                            session_id,
                            insert_at,
                            after,
                        )
                    additions_count = len(before) + len(after)
                    connection.execute(
                        """
                        UPDATE sessions
                        SET status = 'completed', completed_at = ?, page_count = ?
                        WHERE id = ?
                        """,
                        (
                            datetime.now().astimezone().isoformat(timespec="seconds"),
                            page_count,
                            session_id,
                        ),
                    )
            else:
                overlap = overlap_length(overlap_existing, incoming)
                additions = incoming[overlap:]
                next_sequence = (
                    max((message.sequence for message in all_existing), default=0) + 1
                )
                self._insert_messages(
                    connection, chat_id, session_id, next_sequence, additions
                )
                additions_count = len(additions)
            self._refresh_adjacency(connection, chat_id)

        records = self.load_chat_messages(partner_name, newest_first=False)
        return [record.message for record in records], additions_count, conflict_id

    @staticmethod
    def _find_alignment(
        existing: Sequence[Message], incoming: Sequence[Message]
    ) -> tuple[tuple[int, int, int] | None, bool]:
        candidates: list[tuple[int, int, int, int]] = []
        for existing_start in range(len(existing)):
            for incoming_start in range(len(incoming)):
                if not ArchiveStore._messages_anchor_match(
                    existing[existing_start], incoming[incoming_start]
                ):
                    continue
                if (
                    existing_start > 0
                    and incoming_start > 0
                    and ArchiveStore._messages_anchor_match(
                        existing[existing_start - 1], incoming[incoming_start - 1]
                    )
                ):
                    continue
                length = 0
                text_size = 0
                while (
                    existing_start + length < len(existing)
                    and incoming_start + length < len(incoming)
                    and ArchiveStore._messages_anchor_match(
                        existing[existing_start + length],
                        incoming[incoming_start + length],
                    )
                ):
                    text_size += len(
                        incoming[incoming_start + length].original_text
                        or incoming[incoming_start + length].text
                    )
                    length += 1
                if length >= 2 or text_size >= 12:
                    candidates.append(
                        (length, text_size, existing_start, incoming_start)
                    )
        if not candidates:
            return None, False
        candidates.sort(reverse=True)
        best = candidates[0]
        ambiguous = any(
            candidate[:2] == best[:2] and candidate[2:] != best[2:]
            for candidate in candidates[1:]
        )
        return (best[2], best[3], best[0]), ambiguous

    @staticmethod
    def _messages_anchor_match(left: Message, right: Message) -> bool:
        left_fingerprint = left.fingerprint or message_fingerprint(left)
        right_fingerprint = right.fingerprint or message_fingerprint(right)
        if left_fingerprint == right_fingerprint:
            return True
        from .processing import message_similarity

        return message_similarity(left, right) >= 92

    @staticmethod
    def _shift_sequences(
        connection: sqlite3.Connection, chat_id: int, start: int, amount: int
    ) -> None:
        if amount <= 0:
            return
        connection.execute(
            """
            UPDATE messages SET sequence = -sequence
            WHERE chat_id = ? AND sequence >= ?
            """,
            (chat_id, start),
        )
        connection.execute(
            """
            UPDATE messages SET sequence = -sequence + ?
            WHERE chat_id = ? AND sequence < 0
            """,
            (amount, chat_id),
        )

    @staticmethod
    def _insert_messages(
        connection: sqlite3.Connection,
        chat_id: int,
        session_id: int,
        start_sequence: int,
        messages: Sequence[Message],
    ) -> None:
        for offset, message in enumerate(messages):
            sequence = start_sequence + offset
            message.sequence = sequence
            fingerprint = message.fingerprint or message_fingerprint(message)
            message.fingerprint = fingerprint
            connection.execute(
                """
                INSERT INTO messages(
                    chat_id, session_id, sequence, speaker, text, confidence,
                    source, x, y, width, height, visible_time, occurred_at, kind,
                    is_visible, original_text, occurred_date, date_source,
                    is_deleted, edited_at, fingerprint, voice_duration_seconds,
                    voice_visual_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, 0, NULL, ?, ?, ?)
                """,
                (
                    chat_id,
                    session_id,
                    sequence,
                    message.speaker,
                    message.text,
                    message.confidence,
                    message.source,
                    message.x,
                    message.y,
                    message.width,
                    message.height,
                    message.visible_time,
                    message.occurred_at,
                    message.kind,
                    message.original_text or message.text,
                    message.occurred_date,
                    message.date_source,
                    fingerprint,
                    message.voice_duration_seconds,
                    message.voice_visual_hash,
                ),
            )

    @staticmethod
    def _refresh_adjacency(connection: sqlite3.Connection, chat_id: int) -> None:
        rows = connection.execute(
            """
            SELECT id, fingerprint FROM messages
            WHERE chat_id = ? ORDER BY sequence, id
            """,
            (chat_id,),
        ).fetchall()
        updates = []
        for index, row in enumerate(rows):
            previous_fingerprint = (
                str(rows[index - 1]["fingerprint"]) if index > 0 else None
            )
            next_fingerprint = (
                str(rows[index + 1]["fingerprint"]) if index + 1 < len(rows) else None
            )
            updates.append((previous_fingerprint, next_fingerprint, int(row["id"])))
        connection.executemany(
            """
            UPDATE messages
            SET previous_fingerprint = ?, next_fingerprint = ? WHERE id = ?
            """,
            updates,
        )

    @staticmethod
    def _create_capture_conflict(
        connection: sqlite3.Connection,
        chat_id: int,
        session_id: int,
        partner_name: str,
        existing: Sequence[Message],
        messages: Sequence[Message],
        kind: str,
        alignment: tuple[int, int, int] | None,
    ) -> int:
        suggested_alignment = alignment if kind != "ambiguous_anchor" else None
        anchor: list[dict[str, object]] = []
        if suggested_alignment is not None:
            existing_start, _incoming_start, length = suggested_alignment
            for message in existing[existing_start : existing_start + length]:
                anchor.append(
                    {
                        "sequence": message.sequence,
                        "fingerprint": message.fingerprint
                        or message_fingerprint(message),
                    }
                )
        details = {
            "partner_name": partner_name,
            "messages": [message.to_dict() for message in messages],
            "suggested_alignment": suggested_alignment,
            "anchor": anchor,
        }
        cursor = connection.execute(
            """
            INSERT INTO capture_conflicts(
                chat_id, session_id, kind, details_json, status, created_at
            ) VALUES (?, ?, ?, ?, 'pending', ?)
            """,
            (
                chat_id,
                session_id,
                kind,
                json.dumps(details, ensure_ascii=False),
                datetime.now().astimezone().isoformat(timespec="seconds"),
            ),
        )
        return int(cursor.lastrowid)

    def list_review_records(
        self,
        *,
        confidence_threshold: float = 0.75,
        status: str = "pending",
        reason_filter: str | None = None,
        partner_name: str | None = None,
    ) -> list[ReviewRecord]:
        conditions = ["m.is_visible = 1", "m.is_deleted = 0"]
        parameters: list[object] = []
        if partner_name:
            conditions.append("c.partner_name = ?")
            parameters.append(partner_name)
        if status != "all":
            conditions.append("COALESCE(r.status, 'pending') = ?")
            parameters.append(status)
        with closing(self._connect()) as connection:
            duplicate_rows = connection.execute(
                """
                SELECT m.id FROM messages m
                JOIN (
                    SELECT chat_id, fingerprint, previous_fingerprint,
                           next_fingerprint
                    FROM messages
                    WHERE fingerprint IS NOT NULL
                      AND is_visible = 1 AND is_deleted = 0
                    GROUP BY chat_id, fingerprint, previous_fingerprint,
                             next_fingerprint
                    HAVING COUNT(*) > 1
                ) duplicates
                ON duplicates.chat_id = m.chat_id
                AND duplicates.fingerprint = m.fingerprint
                AND duplicates.previous_fingerprint IS m.previous_fingerprint
                AND duplicates.next_fingerprint IS m.next_fingerprint
                WHERE m.is_visible = 1 AND m.is_deleted = 0
                """
            ).fetchall()
            duplicate_ids = {int(row["id"]) for row in duplicate_rows}
            rows = connection.execute(
                f"""
                SELECT m.*, s.session_dir, s.started_at, c.partner_name,
                       COALESCE(r.status, 'pending') AS review_status,
                       r.reviewed_at
                FROM messages m
                JOIN sessions s ON s.id = m.session_id
                JOIN chats c ON c.id = m.chat_id
                LEFT JOIN message_reviews r ON r.message_id = m.id
                WHERE {' AND '.join(conditions)}
                ORDER BY m.confidence, m.sequence
                """,
                parameters,
            ).fetchall()
        records: list[ReviewRecord] = []
        for row in rows:
            archived = self._row_to_archived_message(row)
            message = archived.message
            reasons: list[str] = []
            if message.confidence < confidence_threshold:
                reasons.append("low_confidence")
            if message.kind != "system" and not message.occurred_date:
                reasons.append("missing_date")
            if message.kind == "voice" and message.confidence < 0.85:
                reasons.append("uncertain_voice")
            if message.kind == "voice" and message.voice_duration_seconds is None:
                reasons.append("missing_voice_duration")
            center = message.x + message.width / 2
            if message.kind != "system" and abs(center - 0.5) <= 0.055:
                reasons.append("uncertain_speaker")
            coordinates = (message.x, message.y, message.width, message.height)
            if (
                any(value < 0 or value > 1 for value in coordinates)
                or message.width <= 0
                or message.height <= 0
                or message.x + message.width > 1.001
                or message.y + message.height > 1.001
            ):
                reasons.append("invalid_coordinates")
            if not archived.source_path.is_file():
                reasons.append("missing_screenshot")
            if archived.message_id in duplicate_ids:
                reasons.append("suspected_duplicate")
            if not reasons or (reason_filter and reason_filter not in reasons):
                continue
            records.append(
                ReviewRecord(
                    record=archived,
                    reasons=tuple(reasons),
                    status=message.review_status,
                    reviewed_at=message.reviewed_at,
                )
            )
        return records

    def set_review_status(self, message_ids: Sequence[int], status: str) -> None:
        if status not in {"pending", "confirmed", "deferred"}:
            raise ValueError("不支持的复核状态")
        reviewed_at = (
            datetime.now().astimezone().isoformat(timespec="seconds")
            if status != "pending"
            else None
        )
        with self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO message_reviews(message_id, status, reviewed_at)
                VALUES (?, ?, ?)
                ON CONFLICT(message_id) DO UPDATE SET
                    status = excluded.status,
                    reviewed_at = excluded.reviewed_at
                """,
                [(message_id, status, reviewed_at) for message_id in message_ids],
            )

    def pending_review_count(self, confidence_threshold: float = 0.75) -> int:
        return len(
            self.list_review_records(
                confidence_threshold=confidence_threshold, status="pending"
            )
        ) + len(self.list_capture_conflicts(status="pending"))

    def list_capture_conflicts(
        self, *, status: str = "pending"
    ) -> list[CaptureConflict]:
        conditions = "WHERE cc.status = ?" if status != "all" else ""
        parameters = (status,) if status != "all" else ()
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"""
                SELECT cc.*, c.partner_name
                FROM capture_conflicts cc
                JOIN chats c ON c.id = cc.chat_id
                {conditions}
                ORDER BY cc.created_at DESC
                """,
                parameters,
            ).fetchall()
        conflicts: list[CaptureConflict] = []
        for row in rows:
            try:
                details = json.loads(str(row["details_json"]))
            except json.JSONDecodeError:
                details = {}
            conflicts.append(
                CaptureConflict(
                    conflict_id=int(row["id"]),
                    partner_name=str(row["partner_name"]),
                    session_id=int(row["session_id"]),
                    kind=str(row["kind"]),
                    details=details,
                    status=str(row["status"]),
                    created_at=str(row["created_at"]),
                )
            )
        return conflicts

    def resolve_capture_conflict(
        self,
        conflict_id: int,
        action: str,
        *,
        insert_sequence: int | None = None,
    ) -> int:
        if action not in {"ignore", "append", "insert", "merge"}:
            raise ValueError("不支持的冲突处理方式")
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT cc.*, c.partner_name
                FROM capture_conflicts cc
                JOIN chats c ON c.id = cc.chat_id
                WHERE cc.id = ? AND cc.status = 'pending'
                """,
                (conflict_id,),
            ).fetchone()
            if row is None:
                raise ValueError("冲突记录不存在或已经处理")
            chat_id = int(row["chat_id"])
            session_id = int(row["session_id"])
            details = json.loads(str(row["details_json"]))
            messages = [Message(**payload) for payload in details.get("messages", [])]
            added = 0
            if action != "ignore":
                existing_rows = connection.execute(
                    """
                    SELECT * FROM messages
                    WHERE chat_id = ? AND is_visible = 1 ORDER BY sequence
                    """,
                    (chat_id,),
                ).fetchall()
                existing = [self._row_to_message(item) for item in existing_rows]
                if action == "append":
                    target = max((item.sequence for item in existing), default=0) + 1
                    self._insert_messages(
                        connection, chat_id, session_id, target, messages
                    )
                    added = len(messages)
                elif action == "insert":
                    maximum = max((item.sequence for item in existing), default=0) + 1
                    target = min(max(insert_sequence or maximum, 1), maximum)
                    self._shift_sequences(connection, chat_id, target, len(messages))
                    self._insert_messages(
                        connection, chat_id, session_id, target, messages
                    )
                    added = len(messages)
                else:
                    alignment, ambiguous = self._find_alignment(existing, messages)
                    if alignment is None or ambiguous:
                        raise ValueError("当前冲突没有可使用的建议锚点")
                    existing_start, incoming_start, length = alignment
                    before = messages[:incoming_start]
                    after = messages[incoming_start + length :]
                    target = existing[existing_start].sequence
                    if before:
                        self._shift_sequences(connection, chat_id, target, len(before))
                        self._insert_messages(
                            connection, chat_id, session_id, target, before
                        )
                    matched_end = existing[existing_start + length - 1].sequence + len(
                        before
                    )
                    if after:
                        self._shift_sequences(
                            connection, chat_id, matched_end + 1, len(after)
                        )
                        self._insert_messages(
                            connection,
                            chat_id,
                            session_id,
                            matched_end + 1,
                            after,
                        )
                    added = len(before) + len(after)
                self._refresh_adjacency(connection, chat_id)
            resolved_at = datetime.now().astimezone().isoformat(timespec="seconds")
            connection.execute(
                """
                UPDATE capture_conflicts
                SET status = ?, resolved_at = ? WHERE id = ?
                """,
                (f"resolved_{action}", resolved_at, conflict_id),
            )
            connection.execute(
                """
                UPDATE sessions
                SET status = 'completed', completed_at = ? WHERE id = ?
                """,
                (resolved_at, session_id),
            )
        return added

    @staticmethod
    def _row_to_message(row: sqlite3.Row) -> Message:
        occurred_at = row["occurred_at"]
        if not occurred_at and row["visible_time"] and "started_at" in row.keys():
            try:
                reference = datetime.fromisoformat(str(row["started_at"]))
            except ValueError:
                reference = datetime.now().astimezone()
            parsed = parse_wechat_timestamp(str(row["visible_time"]), reference)
            occurred_at = parsed.isoformat(timespec="minutes") if parsed else None
        occurred_date = (
            str(row["occurred_date"])
            if row["occurred_date"]
            else (str(occurred_at)[:10] if occurred_at else None)
        )
        date_source = str(row["date_source"]) if row["date_source"] else "unresolved"
        if occurred_date and date_source == "unresolved":
            date_source = "recognized"
        return Message(
            speaker=str(row["speaker"]),
            text=str(row["text"]),
            confidence=float(row["confidence"]),
            source=str(row["source"]),
            x=float(row["x"]),
            y=float(row["y"]),
            width=float(row["width"]),
            height=float(row["height"]),
            visible_time=row["visible_time"],
            occurred_at=occurred_at,
            kind=str(row["kind"]),
            sequence=int(row["sequence"]),
            original_text=(
                str(row["original_text"])
                if row["original_text"] is not None
                else str(row["text"])
            ),
            occurred_date=occurred_date,
            date_source=date_source,
            is_deleted=bool(row["is_deleted"]),
            edited_at=(str(row["edited_at"]) if row["edited_at"] else None),
            fingerprint=(str(row["fingerprint"]) if row["fingerprint"] else None),
            previous_fingerprint=(
                str(row["previous_fingerprint"])
                if row["previous_fingerprint"]
                else None
            ),
            next_fingerprint=(
                str(row["next_fingerprint"]) if row["next_fingerprint"] else None
            ),
            voice_duration_seconds=(
                int(row["voice_duration_seconds"])
                if row["voice_duration_seconds"] is not None
                else None
            ),
            voice_visual_hash=(
                str(row["voice_visual_hash"]) if row["voice_visual_hash"] else None
            ),
            review_status=(
                str(row["review_status"])
                if "review_status" in row.keys() and row["review_status"]
                else "pending"
            ),
            reviewed_at=(
                str(row["reviewed_at"])
                if "reviewed_at" in row.keys() and row["reviewed_at"]
                else None
            ),
        )

    def _row_to_archived_message(self, row: sqlite3.Row) -> ArchivedMessage:
        message = self._row_to_message(row)
        source_path = (
            self.resolve_session_dir(str(row["session_dir"])) / message.source
        ).resolve()
        return ArchivedMessage(
            message_id=int(row["id"]),
            partner_name=str(row["partner_name"]),
            message=message,
            source_path=source_path,
        )

    @staticmethod
    def _chat_message_filters(
        partner_name: str,
        *,
        query: str,
        speaker: str | None,
        include_deleted: bool = False,
    ) -> tuple[list[str], list[object]]:
        conditions = ["c.partner_name = ?", "m.is_visible = 1"]
        if not include_deleted:
            conditions.append("m.is_deleted = 0")
        parameters: list[object] = [partner_name]
        if query:
            escaped = (
                query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            )
            conditions.append("m.text LIKE ? ESCAPE '\\' COLLATE NOCASE")
            parameters.append(f"%{escaped}%")
        if speaker is not None:
            conditions.append("m.speaker = ?")
            parameters.append(speaker)
        return conditions, parameters

    def portable_session_dir(self, session_dir: Path) -> str:
        expanded = session_dir.expanduser()
        try:
            return str(expanded.resolve().relative_to(self.path.parent.resolve()))
        except ValueError:
            return str(expanded)

    def resolve_session_dir(self, stored: str) -> Path:
        path = Path(stored).expanduser()
        return path if path.is_absolute() else self.path.parent / path

    def relative_source_path(self, record: ArchivedMessage) -> str:
        try:
            return str(
                record.source_path.resolve().relative_to(self.path.parent.resolve())
            )
        except ValueError:
            return str(record.source_path)

    def _latest_legacy_timestamp(
        self, connection: sqlite3.Connection, chat_id: int
    ) -> str | None:
        rows = connection.execute(
            """
            SELECT m.*, s.started_at
            FROM messages m
            JOIN sessions s ON s.id = m.session_id
            WHERE m.chat_id = ?
              AND m.is_visible = 1
              AND m.is_deleted = 0
              AND m.visible_time IS NOT NULL
            ORDER BY m.sequence, m.id
            """,
            (chat_id,),
        ).fetchall()
        occurred_at_values = [
            message.occurred_at
            for message in (self._row_to_message(row) for row in rows)
            if message.occurred_at
        ]
        if not occurred_at_values:
            return None
        return max(occurred_at_values, key=self._timestamp_value)

    @staticmethod
    def _timestamp_value(value: str | None) -> float:
        if not value:
            return float("-inf")
        try:
            return datetime.fromisoformat(value).timestamp()
        except (OverflowError, ValueError):
            return float("-inf")
