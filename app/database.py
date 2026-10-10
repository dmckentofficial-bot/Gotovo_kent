import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path


DATABASE_PATH = Path(os.environ.get("DATABASE_PATH", "data/app.sqlite3"))
DEFAULT_CONFIG = {
    "sources": [],
    "destination": "",
    "destinations": [],
    "filter_enabled": False,
    "filter_terms": "",
    "filter_mode": "any",
    "signature_enabled": False,
    "signature_text": "",
    "enabled": False,
}
MAX_DESTINATIONS = 5


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
            CREATE TABLE IF NOT EXISTS user_configs (
                user_key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT NOT NULL DEFAULT '',
                first_name TEXT NOT NULL DEFAULT '',
                role TEXT NOT NULL DEFAULT 'user',
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT '',
                invite_token TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS invites (
                token TEXT PRIMARY KEY,
                created_at TEXT NOT NULL DEFAULT '',
                expires_at TEXT NOT NULL DEFAULT '',
                max_uses INTEGER NOT NULL DEFAULT 0,
                uses INTEGER NOT NULL DEFAULT 0,
                revoked INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS cursors (
                source TEXT PRIMARY KEY,
                last_id INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS user_cursors (
                user_key TEXT NOT NULL,
                source TEXT NOT NULL,
                last_id INTEGER NOT NULL,
                PRIMARY KEY (user_key, source)
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
            """,
        )
        # Миграция: колонка назначения в истории (для нескольких своих каналов).
        columns = [row[1] for row in connection.execute("PRAGMA table_info(history)").fetchall()]
        if "destination" not in columns:
            connection.execute("ALTER TABLE history ADD COLUMN destination TEXT NOT NULL DEFAULT ''")
        if "user_key" not in [row[1] for row in connection.execute("PRAGMA table_info(history)").fetchall()]:
            connection.execute("ALTER TABLE history ADD COLUMN user_key TEXT NOT NULL DEFAULT ''")
        user_columns = [row[1] for row in connection.execute("PRAGMA table_info(users)").fetchall()]
        if "max_sources" not in user_columns:
            connection.execute("ALTER TABLE users ADD COLUMN max_sources INTEGER NOT NULL DEFAULT 5")
        if "max_destinations" not in user_columns:
            connection.execute("ALTER TABLE users ADD COLUMN max_destinations INTEGER NOT NULL DEFAULT 5")
        connection.execute(
            "INSERT OR IGNORE INTO config (id, value) VALUES (1, ?)",
            (json.dumps(DEFAULT_CONFIG, ensure_ascii=False),),
        )
    os.chmod(DATABASE_PATH, 0o600)


def _normalize_config(config):
    if not isinstance(config.get("sources"), list):
        config["sources"] = []
    if not isinstance(config.get("destinations"), list):
        config["destinations"] = []
    # Миграция со старого одиночного назначения.
    if not config["destinations"] and config.get("destination"):
        config["destinations"] = [config["destination"]]
    if not config.get("destination") and config["destinations"]:
        config["destination"] = config["destinations"][0]
    return config


def user_key_for(user_id):
    return f"u{int(user_id)}"


def get_config(user_key=None):
    if user_key is None:
        with connect() as connection:
            row = connection.execute("SELECT value FROM config WHERE id = 1").fetchone()
        config = {**DEFAULT_CONFIG, **json.loads(row["value"])} if row else DEFAULT_CONFIG.copy()
        return _normalize_config(config)
    with connect() as connection:
        row = connection.execute("SELECT value FROM user_configs WHERE user_key = ?", (user_key,)).fetchone()
    config = {**DEFAULT_CONFIG, **json.loads(row["value"])} if row else DEFAULT_CONFIG.copy()
    return _normalize_config(config)


def set_config(config, user_key=None):
    if user_key is None:
        with connect() as connection:
            connection.execute(
                "INSERT INTO config (id, value) VALUES (1, ?) "
                "ON CONFLICT(id) DO UPDATE SET value = excluded.value",
                (json.dumps(config, ensure_ascii=False),),
            )
        return
    with connect() as connection:
        connection.execute(
            "INSERT INTO user_configs (user_key, value) VALUES (?, ?) "
            "ON CONFLICT(user_key) DO UPDATE SET value = excluded.value",
            (user_key, json.dumps(config, ensure_ascii=False)),
        )


def delete_user_config(user_key):
    with connect() as connection:
        connection.execute("DELETE FROM user_configs WHERE user_key = ?", (user_key,))


def get_cursor(source, user_key=None):
    if user_key is None:
        with connect() as connection:
            row = connection.execute("SELECT last_id FROM cursors WHERE source = ?", (source,)).fetchone()
        return int(row["last_id"]) if row else None
    with connect() as connection:
        row = connection.execute(
            "SELECT last_id FROM user_cursors WHERE user_key = ? AND source = ?", (user_key, source)
        ).fetchone()
    return int(row["last_id"]) if row else None


def set_cursor(source, message_id, user_key=None):
    if user_key is None:
        with connect() as connection:
            connection.execute(
                "INSERT INTO cursors (source, last_id) VALUES (?, ?) "
                "ON CONFLICT(source) DO UPDATE SET last_id = excluded.last_id",
                (source, int(message_id)),
            )
        return
    with connect() as connection:
        connection.execute(
            "INSERT INTO user_cursors (user_key, source, last_id) VALUES (?, ?, ?) "
            "ON CONFLICT(user_key, source) DO UPDATE SET last_id = excluded.last_id",
            (user_key, source, int(message_id)),
        )


def delete_user_cursors(user_key):
    with connect() as connection:
        connection.execute("DELETE FROM user_cursors WHERE user_key = ?", (user_key,))


def already_forwarded(source, message_id, destination="", user_key=None):
    if not destination and user_key is None:
        query = (
            "SELECT 1 FROM history WHERE source = ? AND message_id = ? AND status = 'forwarded' LIMIT 1"
        )
        params = (source, str(message_id))
    else:
        query = (
            "SELECT 1 FROM history WHERE source = ? AND message_id = ? AND status = 'forwarded' "
            "AND (destination = ? OR destination = '')"
        )
        params = [source, str(message_id), destination or ""]
        if user_key is not None:
            query += " AND (user_key = ? OR user_key = '')"
            params.append(user_key)
        query += " LIMIT 1"
        params = tuple(params)
    with connect() as connection:
        row = connection.execute(query, params).fetchone()
    return row is not None


def write_history(post, source, status, details="", destination="", user_key=""):
    media = post.get("media", [])
    media_type = ", ".join(dict.fromkeys(item.get("type", "media") for item in media)) or "text"
    with connect() as connection:
        connection.execute(
            "INSERT INTO history (created_at, source, message_id, status, media_type, message, details, destination, user_key) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                source,
                str(post.get("id", "")),
                status,
                media_type,
                post.get("text", "")[:500],
                details[:1000],
                str(destination or "")[:100],
                str(user_key or ""),
            ),
        )


def recent_history(limit=50, user_key=None):
    if user_key is None:
        query = (
            "SELECT created_at, source, message_id, status, media_type, message, details, destination, user_key "
            "FROM history ORDER BY id DESC LIMIT ?"
        )
        params = (limit,)
    else:
        query = (
            "SELECT created_at, source, message_id, status, media_type, message, details, destination, user_key "
            "FROM history WHERE user_key = ? OR user_key = '' ORDER BY id DESC LIMIT ?"
        )
        params = (user_key, limit)
    with connect() as connection:
        rows = connection.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def history_stats(user_key=None):
    if user_key is None:
        query = "SELECT status, COUNT(*) AS count FROM history GROUP BY status"
        params = ()
    else:
        query = (
            "SELECT status, COUNT(*) AS count FROM history "
            "WHERE user_key = ? OR user_key = '' GROUP BY status"
        )
        params = (user_key,)
    with connect() as connection:
        rows = connection.execute(query, params).fetchall()
    counts = {row["status"]: row["count"] for row in rows}
    return {
        "forwarded": counts.get("forwarded", 0),
        "filtered": counts.get("filtered", 0),
        "skipped": counts.get("skipped", 0),
        "errors": counts.get("error", 0),
        "total": sum(counts.values()),
    }


def get_limits(user_key):
    """Лимиты пользователя (источники, свои каналы). По умолчанию 5/5."""
    default = (5, 5)
    if not user_key or not user_key.startswith("u"):
        return default
    try:
        user_id = int(user_key[1:])
    except ValueError:
        return default
    user = get_user(user_id)
    if not user:
        return default
    try:
        max_sources = max(1, min(20, int(user.get("max_sources", 5) or 5)))
    except (TypeError, ValueError):
        max_sources = 5
    try:
        max_destinations = max(1, min(20, int(user.get("max_destinations", 5) or 5)))
    except (TypeError, ValueError):
        max_destinations = 5
    return max_sources, max_destinations


def set_limits(user_id, max_sources=None, max_destinations=None):
    updates = []
    params = []
    if max_sources is not None:
        updates.append("max_sources = ?")
        params.append(max(1, min(20, int(max_sources))))
    if max_destinations is not None:
        updates.append("max_destinations = ?")
        params.append(max(1, min(20, int(max_destinations))))
    if not updates:
        return get_user(user_id)
    params.append(int(user_id))
    with connect() as connection:
        connection.execute(
            f"UPDATE users SET {', '.join(updates)} WHERE user_id = ?", tuple(params)
        )
    return get_user(user_id)


def user_state_key(user_key, name):
    return f"{user_key}:{name}"


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------- пользователи и приглашения ----------

def upsert_user(user_id, username="", first_name="", role=None, invite_token=""):
    user_id = int(user_id)
    existing = get_user(user_id)
    now = _now_iso()
    with connect() as connection:
        if existing is None:
            connection.execute(
                "INSERT INTO users (user_id, username, first_name, role, enabled, created_at, invite_token) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (user_id, username or "", first_name or "", role or "user",
                 0 if (role or "user") != "owner" else 1, now, invite_token or ""),
            )
        else:
            connection.execute(
                "UPDATE users SET username = ?, first_name = ? WHERE user_id = ?",
                (username or existing["username"], first_name or existing["first_name"], user_id),
            )
            if role:
                connection.execute("UPDATE users SET role = ? WHERE user_id = ?", (role, user_id))
            if invite_token:
                connection.execute("UPDATE users SET invite_token = ? WHERE user_id = ?", (invite_token, user_id))
    return get_user(user_id)


def ensure_owner(user_id, username="", first_name=""):
    user = upsert_user(user_id, username, first_name, role="owner")
    with connect() as connection:
        connection.execute("UPDATE users SET enabled = 1, role = 'owner' WHERE user_id = ?", (int(user_id),))
    return get_user(user_id)


def get_user(user_id):
    with connect() as connection:
        row = connection.execute("SELECT * FROM users WHERE user_id = ?", (int(user_id),)).fetchone()
    return dict(row) if row else None


def list_users():
    with connect() as connection:
        rows = connection.execute("SELECT * FROM users ORDER BY user_id").fetchall()
    return [dict(row) for row in rows]


def set_user_enabled(user_id, enabled):
    with connect() as connection:
        connection.execute(
            "UPDATE users SET enabled = ? WHERE user_id = ?", (1 if enabled else 0, int(user_id))
        )


def delete_user_everything(user_id):
    key = user_key_for(user_id)
    with connect() as connection:
        connection.execute("DELETE FROM users WHERE user_id = ?", (int(user_id),))
        connection.execute("DELETE FROM user_configs WHERE user_key = ?", (key,))
        connection.execute("DELETE FROM user_cursors WHERE user_key = ?", (key,))


def migrate_legacy_to(owner_key):
    """Одноразовый перенос старых общих данных владельцу (идемпотентно)."""
    with connect() as connection:
        row = connection.execute("SELECT value FROM config WHERE id = 1").fetchone()
        if row:
            connection.execute(
                "INSERT INTO user_configs (user_key, value) VALUES (?, ?) "
                "ON CONFLICT(user_key) DO NOTHING",
                (owner_key, row["value"]),
            )
        connection.execute(
            "INSERT OR IGNORE INTO user_cursors (user_key, source, last_id) "
            "SELECT ?, source, last_id FROM cursors",
            (owner_key,),
        )
        connection.execute(
            "UPDATE history SET user_key = ? WHERE user_key = ''", (owner_key,)
        )


def create_invite(max_uses=0, expires_days=0):
    import secrets

    token = secrets.token_urlsafe(16)
    now = datetime.now(timezone.utc)
    if expires_days and int(expires_days) > 0:
        expires_at = (now + timedelta(days=int(expires_days))).isoformat(timespec="seconds")
    else:
        expires_at = ""
    with connect() as connection:
        connection.execute(
            "INSERT INTO invites (token, created_at, expires_at, max_uses, uses, revoked) "
            "VALUES (?, ?, ?, ?, 0, 0)",
            (token, now.isoformat(timespec="seconds"), expires_at, int(max_uses or 0)),
        )
    return get_invite(token)


def get_invite(token):
    with connect() as connection:
        row = connection.execute("SELECT * FROM invites WHERE token = ?", (token,)).fetchone()
    return dict(row) if row else None


def list_invites():
    with connect() as connection:
        rows = connection.execute("SELECT * FROM invites ORDER BY created_at DESC").fetchall()
    return [dict(row) for row in rows]


def revoke_invite(token):
    with connect() as connection:
        connection.execute("UPDATE invites SET revoked = 1 WHERE token = ?", (token,))


def delete_invite(token):
    with connect() as connection:
        connection.execute("DELETE FROM invites WHERE token = ?", (token,))


def redeem_invite(token, user_id):
    """Привязка пользователя к приглашению. Возвращает (ok, текст)."""
    invite = get_invite((token or "").strip())
    if not invite:
        return False, "Ссылка-приглашение не найдена."
    if invite["revoked"]:
        return False, "Приглашение отозвано владельцем."
    if invite["expires_at"]:
        try:
            expiry = datetime.fromisoformat(invite["expires_at"])
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) > expiry:
                return False, "Срок приглашения истёк."
        except ValueError:
            pass
    if invite["max_uses"] and invite["uses"] >= invite["max_uses"]:
        return False, "Приглашение уже использовано."
    with connect() as connection:
        connection.execute(
            "INSERT INTO users (user_id, username, first_name, role, enabled, created_at, invite_token) "
            "VALUES (?, '', '', 'user', 0, ?, '') "
            "ON CONFLICT(user_id) DO NOTHING",
            (int(user_id), datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )
        connection.execute(
            "UPDATE users SET enabled = 1, invite_token = ? WHERE user_id = ?",
            (invite["token"], int(user_id)),
        )
        connection.execute(
            "UPDATE invites SET uses = uses + 1 WHERE token = ?", (invite["token"],)
        )
    return True, "Доступ открыт."


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
