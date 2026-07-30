from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Callable

from .models import ArchivedMessage, ChatSummary, Message
from .processing import overlap_length
from .time_parser import parse_wechat_timestamp


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
    session_dir TEXT NOT NULL
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
"""


class ArchiveStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(SCHEMA)
            self._migrate(connection)

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
        self._migrate_session_paths(connection)

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
            edited_at = datetime.now().astimezone().isoformat(timespec="seconds")
            connection.execute(
                """
                UPDATE messages
                SET text = ?, speaker = ?, occurred_date = ?, occurred_at = ?,
                    date_source = 'manual', edited_at = ?
                WHERE id = ?
                """,
                (text, speaker, occurred_date or None, occurred_at, edited_at, message_id),
            )
            after = self._message_snapshot(connection, message_id)
            self._add_revision(connection, message_id, "edit", before, after)

    def set_message_deleted(self, message_id: int, deleted: bool) -> None:
        with self._connect() as connection:
            before = self._message_snapshot(connection, message_id)
            connection.execute(
                "UPDATE messages SET is_deleted = ? WHERE id = ?",
                (1 if deleted else 0, message_id),
            )
            after = self._message_snapshot(connection, message_id)
            self._add_revision(
                connection, message_id, "delete" if deleted else "restore", before, after
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
                   is_deleted, edited_at
            FROM messages WHERE id = ?
            """,
            (message_id,),
        ).fetchone()
        if row is None:
            raise ValueError("聊天记录不存在")
        return {key: row[key] for key in row.keys()}

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
        with self._connect() as connection:
            chat_id = self._chat_id(connection, partner_name)
            existing_rows = connection.execute(
                """
                SELECT m.*, s.session_dir, s.started_at FROM messages m
                JOIN sessions s ON s.id = m.session_id
                WHERE m.chat_id = ? ORDER BY m.sequence
                """,
                (chat_id,),
            ).fetchall()
            all_existing = [self._row_to_message(row) for row in existing_rows]
            overlap_existing: list[Message] = []
            visible_existing: list[Message] = []
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
                    if not bool(row["is_deleted"]):
                        visible_existing.append(message)
            overlap = overlap_length(overlap_existing, messages)
            additions = list(messages[overlap:])

            cursor = connection.execute(
                """
                INSERT INTO sessions(chat_id, started_at, page_count, session_dir)
                VALUES (?, ?, ?, ?)
                """,
                (
                    chat_id,
                    datetime.now().isoformat(timespec="seconds"),
                    page_count,
                    self.portable_session_dir(session_dir),
                ),
            )
            session_id = int(cursor.lastrowid)
            next_sequence = (
                max((message.sequence for message in all_existing), default=0) + 1
            )
            for offset, message in enumerate(additions):
                message.sequence = next_sequence + offset
                connection.execute(
                    """
                    INSERT INTO messages(
                        chat_id, session_id, sequence, speaker, text, confidence,
                        source, x, y, width, height, visible_time, occurred_at, kind,
                        is_visible, original_text, occurred_date, date_source,
                        is_deleted, edited_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, 0, NULL)
                    """,
                    (
                        chat_id,
                        session_id,
                        message.sequence,
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
                    ),
                )
        return visible_existing + additions, len(additions)

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
        )

    def _row_to_archived_message(self, row: sqlite3.Row) -> ArchivedMessage:
        message = self._row_to_message(row)
        source_path = (self.resolve_session_dir(str(row["session_dir"])) / message.source).resolve()
        return ArchivedMessage(
            message_id=int(row["id"]),
            partner_name=str(row["partner_name"]),
            message=message,
            source_path=source_path,
        )

    @staticmethod
    def _chat_message_filters(
        partner_name: str, *, query: str, speaker: str | None,
        include_deleted: bool = False
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
            return str(record.source_path.resolve().relative_to(self.path.parent.resolve()))
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
