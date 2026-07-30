from __future__ import annotations

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
    UNIQUE(chat_id, sequence)
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

    @staticmethod
    def _migrate(connection: sqlite3.Connection) -> None:
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(messages)")
        }
        if "occurred_at" not in columns:
            connection.execute("ALTER TABLE messages ADD COLUMN occurred_at TEXT")
        if "is_visible" not in columns:
            # NULL distinguishes legacy rows that still need screenshot validation.
            connection.execute("ALTER TABLE messages ADD COLUMN is_visible INTEGER")
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS messages_chat_visible_sequence
            ON messages(chat_id, is_visible, sequence)
            """
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
                        SELECT latest.occurred_at
                        FROM messages latest
                        WHERE latest.chat_id = c.id
                          AND latest.is_visible = 1
                          AND latest.occurred_at IS NOT NULL
                          AND latest.occurred_at != ''
                        ORDER BY latest.sequence DESC, latest.id DESC
                        LIMIT 1
                    ) AS latest_occurred_at
                FROM chats c
                LEFT JOIN messages m ON m.chat_id = c.id AND m.is_visible = 1
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
    ) -> int:
        conditions, parameters = self._chat_message_filters(
            partner_name, query=query, speaker=speaker
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
                WHERE c.partner_name = ? AND m.is_visible = 1
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
    ) -> list[ArchivedMessage]:
        if limit is not None and limit <= 0:
            return []
        if offset < 0:
            raise ValueError("消息偏移量不能为负数")
        conditions, parameters = self._chat_message_filters(
            partner_name, query=query, speaker=speaker
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
            visible_existing: list[Message] = []
            for message, row in zip(all_existing, existing_rows, strict=True):
                stored_visibility = row["is_visible"]
                if stored_visibility is not None:
                    visible = bool(stored_visibility)
                elif existing_message_filter is not None:
                    visible = existing_message_filter(
                        message, Path(str(row["session_dir"])) / message.source
                    )
                    connection.execute(
                        "UPDATE messages SET is_visible = ? WHERE id = ?",
                        (1 if visible else 0, int(row["id"])),
                    )
                else:
                    visible = True
                if visible:
                    visible_existing.append(message)
            overlap = overlap_length(visible_existing, messages)
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
                    str(session_dir),
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
                        is_visible
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
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
        )

    @classmethod
    def _row_to_archived_message(cls, row: sqlite3.Row) -> ArchivedMessage:
        message = cls._row_to_message(row)
        source_path = (
            Path(str(row["session_dir"])).expanduser() / message.source
        ).resolve()
        return ArchivedMessage(
            message_id=int(row["id"]),
            partner_name=str(row["partner_name"]),
            message=message,
            source_path=source_path,
        )

    @staticmethod
    def _chat_message_filters(
        partner_name: str, *, query: str, speaker: str | None
    ) -> tuple[list[str], list[object]]:
        conditions = ["c.partner_name = ?", "m.is_visible = 1"]
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
