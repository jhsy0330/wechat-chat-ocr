from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from contextlib import closing
from datetime import datetime
from pathlib import Path

from .models import Message
from .processing import overlap_length


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
    kind TEXT NOT NULL,
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
                SELECT m.* FROM messages m
                JOIN chats c ON c.id = m.chat_id
                WHERE c.partner_name = ? ORDER BY m.sequence
                """,
                (partner_name,),
            ).fetchall()
        return [self._row_to_message(row) for row in rows]

    def append_session(
        self,
        partner_name: str,
        messages: Sequence[Message],
        page_count: int,
        session_dir: Path,
    ) -> tuple[list[Message], int]:
        with self._connect() as connection:
            chat_id = self._chat_id(connection, partner_name)
            existing_rows = connection.execute(
                "SELECT * FROM messages WHERE chat_id = ? ORDER BY sequence",
                (chat_id,),
            ).fetchall()
            existing = [self._row_to_message(row) for row in existing_rows]
            overlap = overlap_length(existing, messages)
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
            next_sequence = len(existing) + 1
            for offset, message in enumerate(additions):
                message.sequence = next_sequence + offset
                connection.execute(
                    """
                    INSERT INTO messages(
                        chat_id, session_id, sequence, speaker, text, confidence,
                        source, x, y, width, height, visible_time, kind
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        message.kind,
                    ),
                )
        return existing + additions, len(additions)

    @staticmethod
    def _row_to_message(row: sqlite3.Row) -> Message:
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
            kind=str(row["kind"]),
            sequence=int(row["sequence"]),
        )
