import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


DATABASE_PATH = Path(os.environ.get("DATABASE_PATH", "data/app.sqlite3"))
DEFAULT_CONFIG = {
    "sources": [],
    "destination": "",
    "filter_enabled": False,
    "filter_terms": "",
    "filter_mode": "any",
    "enabled": False,
}


def connect():
    DATABASE_PATH.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(DATABASE_PATH.parent, 0o700)
    connection = sqlite3.connect(DATABASE_PATH, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    return connection


def initialize():
    os.umask(0o077)
    with connect() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS config (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS cursors (
                source TEXT PRIMARY KEY,
                last_id INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                source TEXT NOT NULL,
                message_id TEXT NOT NULL,
                status TEXT NOT NULL,
                media_type TEXT NOT NULL,
                message TEXT NOT NULL,
                details TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS service_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT OR IGNORE INTO config (id, value) VALUES (1, ?)",
            (json.dumps(DEFAULT_CONFIG, ensure_ascii=False),),
        )
    os.chmod(DATABASE_PATH, 0o600)


def get_config():
    with connect() as connection:
        row = connection.execute("SELECT value FROM config WHERE id = 1").fetchone()
    return {**DEFAULT_CONFIG, **json.loads(row["value"])} if row else DEFAULT_CONFIG.copy()


def set_config(config):
    with connect() as connection:
        connection.execute(
            "INSERT INTO config (id, value) VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET value = excluded.value",
            (json.dumps(config, ensure_ascii=False),),
        )


def get_cursor(source):
    with connect() as connection:
        row = connection.execute("SELECT last_id FROM cursors WHERE source = ?", (source,)).fetchone()
    return int(row["last_id"]) if row else None


def set_cursor(source, message_id):
    with connect() as connection:
        connection.execute(
            "INSERT INTO cursors (source, last_id) VALUES (?, ?) "
            "ON CONFLICT(source) DO UPDATE SET last_id = excluded.last_id",
            (source, int(message_id)),
        )


def write_history(post, source, status, details=""):
    media = post.get("media", [])
    media_type = ", ".join(dict.fromkeys(item.get("type", "media") for item in media)) or "text"
    with connect() as connection:
        connection.execute(
            "INSERT INTO history (created_at, source, message_id, status, media_type, message, details) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                source,
                str(post.get("id", "")),
                status,
                media_type,
                post.get("text", "")[:500],
                details[:1000],
            ),
        )


def recent_history(limit=50):
    with connect() as connection:
        rows = connection.execute(
            "SELECT created_at, source, message_id, status, media_type, message, details "
            "FROM history ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def history_stats():
    with connect() as connection:
        rows = connection.execute(
            "SELECT status, COUNT(*) AS count FROM history GROUP BY status"
        ).fetchall()
    counts = {row["status"]: row["count"] for row in rows}
    return {
        "forwarded": counts.get("forwarded", 0),
        "filtered": counts.get("filtered", 0),
        "skipped": counts.get("skipped", 0),
        "errors": counts.get("error", 0),
        "total": sum(counts.values()),
    }


def set_state(key, value):
    with connect() as connection:
        connection.execute(
            "INSERT INTO service_state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )


def get_state(key, default=""):
    with connect() as connection:
        row = connection.execute("SELECT value FROM service_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default
