from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

import config

_db: aiosqlite.Connection | None = None


@dataclass
class Session:
    id: str
    chat_id: int
    project_path: str
    model: str
    created_at: str
    last_used_at: str
    total_input_tokens: int
    total_output_tokens: int
    total_cost_usd: float
    is_active: bool
    summary: str | None
    message_count: int = 0


@dataclass
class Message:
    id: int
    session_id: str
    chat_id: int
    role: str
    content: str
    tokens_used: int
    cost_usd: float
    created_at: str


async def init_db() -> None:
    global _db
    Path(config.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    _db = await aiosqlite.connect(config.DB_PATH)
    _db.row_factory = aiosqlite.Row
    await _db.executescript(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            chat_id INTEGER NOT NULL,
            project_path TEXT,
            model TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_used_at TIMESTAMP,
            total_input_tokens INTEGER DEFAULT 0,
            total_output_tokens INTEGER DEFAULT 0,
            total_cost_usd REAL DEFAULT 0,
            is_active INTEGER DEFAULT 1,
            summary TEXT
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT REFERENCES sessions(id),
            chat_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            tokens_used INTEGER DEFAULT 0,
            cost_usd REAL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
        CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id);
        CREATE INDEX IF NOT EXISTS idx_sessions_chat_active ON sessions(chat_id, is_active);
        """
    )
    await _db.commit()


async def close_db() -> None:
    global _db
    if _db:
        await _db.close()
        _db = None


def _conn() -> aiosqlite.Connection:
    assert _db is not None, "Database not initialized. Call init_db() first."
    return _db


async def get_active_session(chat_id: int) -> Session | None:
    cursor = await _conn().execute(
        """SELECT s.*, COUNT(m.id) as message_count
           FROM sessions s
           LEFT JOIN messages m ON m.session_id = s.id
           WHERE s.chat_id = ? AND s.is_active = 1
           GROUP BY s.id
           ORDER BY s.created_at DESC LIMIT 1""",
        (chat_id,),
    )
    row = await cursor.fetchone()
    if not row:
        return None
    return Session(
        id=row["id"],
        chat_id=row["chat_id"],
        project_path=row["project_path"] or config.PROJECTS_DIR,
        model=row["model"] or config.DEFAULT_MODEL,
        created_at=row["created_at"],
        last_used_at=row["last_used_at"] or row["created_at"],
        total_input_tokens=row["total_input_tokens"],
        total_output_tokens=row["total_output_tokens"],
        total_cost_usd=row["total_cost_usd"],
        is_active=bool(row["is_active"]),
        summary=row["summary"],
        message_count=row["message_count"],
    )


async def create_session(
    chat_id: int,
    project_path: str | None = None,
    model: str | None = None,
) -> Session:
    session_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    await _conn().execute(
        """INSERT INTO sessions (id, chat_id, project_path, model, created_at, last_used_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (session_id, chat_id, project_path or config.PROJECTS_DIR, model or config.DEFAULT_MODEL, now, now),
    )
    await _conn().commit()
    return Session(
        id=session_id,
        chat_id=chat_id,
        project_path=project_path or config.PROJECTS_DIR,
        model=model or config.DEFAULT_MODEL,
        created_at=now,
        last_used_at=now,
        total_input_tokens=0,
        total_output_tokens=0,
        total_cost_usd=0,
        is_active=True,
        summary=None,
        message_count=0,
    )


async def save_message(
    session_id: str,
    chat_id: int,
    role: str,
    content: str,
    tokens_used: int = 0,
    cost_usd: float = 0,
) -> None:
    await _conn().execute(
        """INSERT INTO messages (session_id, chat_id, role, content, tokens_used, cost_usd)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (session_id, chat_id, role, content, tokens_used, cost_usd),
    )
    await _conn().commit()


async def update_session_tokens(
    session_id: str,
    input_tokens: int,
    output_tokens: int,
    cost_usd: float,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    await _conn().execute(
        """UPDATE sessions
           SET total_input_tokens = ?,
               total_output_tokens = ?,
               total_cost_usd = total_cost_usd + ?,
               last_used_at = ?
           WHERE id = ?""",
        (input_tokens, output_tokens, cost_usd, now, session_id),
    )
    await _conn().commit()


async def deactivate_session(session_id: str, summary: str | None = None) -> None:
    await _conn().execute(
        "UPDATE sessions SET is_active = 0, summary = ? WHERE id = ?",
        (summary, session_id),
    )
    await _conn().commit()


async def get_recent_messages(
    chat_id: int, limit: int = 20, session_id: str | None = None
) -> list[Message]:
    if session_id:
        cursor = await _conn().execute(
            """SELECT * FROM messages WHERE session_id = ?
               ORDER BY created_at DESC LIMIT ?""",
            (session_id, limit),
        )
    else:
        cursor = await _conn().execute(
            """SELECT * FROM messages WHERE chat_id = ?
               ORDER BY created_at DESC LIMIT ?""",
            (chat_id, limit),
        )
    rows = await cursor.fetchall()
    return [
        Message(
            id=r["id"],
            session_id=r["session_id"],
            chat_id=r["chat_id"],
            role=r["role"],
            content=r["content"],
            tokens_used=r["tokens_used"],
            cost_usd=r["cost_usd"],
            created_at=r["created_at"],
        )
        for r in reversed(rows)
    ]


async def get_total_cost(chat_id: int | None = None) -> float:
    if chat_id:
        cursor = await _conn().execute(
            "SELECT COALESCE(SUM(total_cost_usd), 0) FROM sessions WHERE chat_id = ?",
            (chat_id,),
        )
    else:
        cursor = await _conn().execute(
            "SELECT COALESCE(SUM(total_cost_usd), 0) FROM sessions"
        )
    row = await cursor.fetchone()
    return row[0] if row else 0.0


async def update_session_project(session_id: str, project_path: str) -> None:
    await _conn().execute(
        "UPDATE sessions SET project_path = ? WHERE id = ?",
        (project_path, session_id),
    )
    await _conn().commit()


async def update_session_model(session_id: str, model: str) -> None:
    await _conn().execute(
        "UPDATE sessions SET model = ? WHERE id = ?",
        (model, session_id),
    )
    await _conn().commit()
