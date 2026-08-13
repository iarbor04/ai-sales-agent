"""SQLite: схема, идемпотентные миграции и хелперы.

Миграции живут прямо здесь: при старте читаем PRAGMA table_info и добавляем
недостающие колонки через ALTER TABLE. Отдельной команды «накатить схему» нет
и быть не должно — деплой сам доводит базу до нужного состояния.

Доступ синхронный под общим замком: на нагрузке этого класса запрос
отрабатывает за доли миллисекунды, и это дешевле, чем тащить async-драйвер.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from typing import Any, Iterable

from . import config

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None


def connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA busy_timeout=5000")
    return _conn


def q(sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
    with _lock:
        return connect().execute(sql, tuple(params)).fetchall()


def q1(sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
    rows = q(sql, params)
    return rows[0] if rows else None


def run(sql: str, params: Iterable[Any] = ()) -> int:
    with _lock:
        conn = connect()
        cur = conn.execute(sql, tuple(params))
        conn.commit()
        return cur.lastrowid


def now() -> int:
    return int(time.time())


# ── схема ──────────────────────────────────────────────────────────────

# Статусы лида из ТЗ. Ключи в базе, подписи в интерфейсе.
LEAD_STATUSES = {
    "new": "Новый лид",
    "qualifying": "Квалификация",
    "handed": "Передан менеджеру",
}

SCHEMA = [
    # Один контакт на пару (канал, внешний id) — это и есть защита от дублей.
    """CREATE TABLE IF NOT EXISTS contacts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        channel TEXT NOT NULL,
        external_id TEXT NOT NULL,
        username TEXT,
        phone TEXT,
        name TEXT,
        ai_enabled INTEGER NOT NULL DEFAULT 1,
        opted_in INTEGER NOT NULL DEFAULT 1,
        blocked INTEGER NOT NULL DEFAULT 0,
        manager TEXT,
        created_at INTEGER NOT NULL,
        last_msg_at INTEGER,
        UNIQUE (channel, external_id)
    )""",
    """CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        contact_id INTEGER NOT NULL,
        direction TEXT NOT NULL,
        author TEXT NOT NULL DEFAULT 'client',
        text TEXT,
        media_type TEXT,
        media_path TEXT,
        created_at INTEGER NOT NULL,
        is_read INTEGER NOT NULL DEFAULT 0
    )""",
    # Один лид на контакт: повторное сообщение обновляет карточку, не плодит новую.
    """CREATE TABLE IF NOT EXISTS leads (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        contact_id INTEGER NOT NULL UNIQUE,
        name TEXT,
        contact TEXT,
        product TEXT,
        need TEXT,
        deadline TEXT,
        comment TEXT,
        summary TEXT,
        status TEXT NOT NULL DEFAULT 'new',
        manager TEXT,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS kb_pages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        url TEXT NOT NULL UNIQUE,
        title TEXT,
        text TEXT,
        included INTEGER NOT NULL DEFAULT 1,
        status TEXT NOT NULL DEFAULT 'found',
        chars INTEGER NOT NULL DEFAULT 0,
        fetched_at INTEGER
    )""",
    """CREATE TABLE IF NOT EXISTS kb_chunks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        page_id INTEGER NOT NULL,
        text TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS broadcasts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        text TEXT,
        image_path TEXT,
        button_text TEXT,
        button_url TEXT,
        send_at INTEGER,
        status TEXT NOT NULL DEFAULT 'draft',
        sent_count INTEGER NOT NULL DEFAULT 0,
        failed_count INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL
    )""",
    # Уникальность (broadcast_id, contact_id) — повтор рассылки не создаёт дублей.
    """CREATE TABLE IF NOT EXISTS broadcast_log (
        broadcast_id INTEGER NOT NULL,
        contact_id INTEGER NOT NULL,
        status TEXT NOT NULL,
        sent_at INTEGER NOT NULL,
        UNIQUE (broadcast_id, contact_id)
    )""",
    # Очередь того, что не удалось записать сразу (например, CRM была недоступна).
    """CREATE TABLE IF NOT EXISTS retry_queue (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL,
        payload TEXT,
        attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,
        created_at INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_msg_contact ON messages(contact_id, id)",
    "CREATE INDEX IF NOT EXISTS idx_contact_last ON contacts(last_msg_at)",
    "CREATE INDEX IF NOT EXISTS idx_chunk_page ON kb_chunks(page_id)",
]

# Колонки, которые могли появиться позже схемы: (таблица, колонка, тип).
LATE_COLUMNS = [
    ("contacts", "manager", "TEXT"),
    ("contacts", "blocked", "INTEGER NOT NULL DEFAULT 0"),
    ("contacts", "opted_in", "INTEGER NOT NULL DEFAULT 1"),
    ("leads", "summary", "TEXT"),
    ("leads", "manager", "TEXT"),
    ("kb_pages", "chars", "INTEGER NOT NULL DEFAULT 0"),
    ("kb_pages", "status", "TEXT NOT NULL DEFAULT 'found'"),
    ("broadcasts", "button_text", "TEXT"),
    ("broadcasts", "button_url", "TEXT"),
    ("broadcasts", "failed_count", "INTEGER NOT NULL DEFAULT 0"),
]

DEFAULT_SETTINGS = {
    "business_name": "",
    "business_site": "",
    "greeting": "Здравствуйте! Чем могу помочь?",
    "tone": "дружелюбный, короткий, по делу",
    "model": config.OPENROUTER_MODEL,
    "operator_chat_id": "",
    "managers": "",
    "handoff_note": "Передаю вас менеджеру, он ответит здесь же.",
    "ai_enabled_global": "1",
    "kb_extra": "",
}


def _columns(table: str) -> set[str]:
    return {r["name"] for r in q(f"PRAGMA table_info({table})")}


def init() -> None:
    """Создать схему и донакатить недостающие колонки. Безопасно к повтору."""
    with _lock:
        conn = connect()
        for stmt in SCHEMA:
            conn.execute(stmt)
        conn.commit()

    existing = {r["name"] for r in q("SELECT name FROM sqlite_master WHERE type='table'")}
    for table, column, decl in LATE_COLUMNS:
        if table not in existing:
            continue
        if column not in _columns(table):
            try:
                run(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            except sqlite3.OperationalError:
                # колонка появилась параллельно — это нормально
                pass

    for key, value in DEFAULT_SETTINGS.items():
        run("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value))


# ── настройки ──────────────────────────────────────────────────────────

def setting(key: str, default: str = "") -> str:
    row = q1("SELECT value FROM settings WHERE key = ?", (key,))
    return row["value"] if row and row["value"] is not None else default


def set_setting(key: str, value: str) -> None:
    run(
        "INSERT INTO settings (key, value) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


# ── контакты ───────────────────────────────────────────────────────────

def get_contact(channel: str, external_id: str) -> sqlite3.Row | None:
    return q1(
        "SELECT * FROM contacts WHERE channel = ? AND external_id = ?",
        (channel, str(external_id)),
    )


def upsert_contact(channel: str, external_id: str, username: str | None = None,
                   name: str | None = None, phone: str | None = None) -> sqlite3.Row:
    """Найти контакт или завести новый. Дубли невозможны по UNIQUE."""
    existing = get_contact(channel, external_id)
    if existing:
        # не затираем уже известное пустотой
        run(
            "UPDATE contacts SET username = COALESCE(?, username),"
            " name = COALESCE(?, name), phone = COALESCE(?, phone) WHERE id = ?",
            (username, name, phone, existing["id"]),
        )
        return get_contact(channel, external_id)  # type: ignore[return-value]

    run(
        "INSERT INTO contacts (channel, external_id, username, name, phone, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (channel, str(external_id), username, name, phone, now()),
    )
    return get_contact(channel, external_id)  # type: ignore[return-value]


def contact_by_id(contact_id: int) -> sqlite3.Row | None:
    return q1("SELECT * FROM contacts WHERE id = ?", (contact_id,))


def set_ai(contact_id: int, enabled: bool) -> None:
    run("UPDATE contacts SET ai_enabled = ? WHERE id = ?", (1 if enabled else 0, contact_id))


# ── сообщения ──────────────────────────────────────────────────────────

def add_message(contact_id: int, direction: str, author: str, text: str | None,
                media_type: str | None = None, media_path: str | None = None,
                is_read: bool = False) -> int:
    msg_id = run(
        "INSERT INTO messages (contact_id, direction, author, text, media_type, media_path,"
        " created_at, is_read) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (contact_id, direction, author, text, media_type, media_path, now(),
         1 if is_read else 0),
    )
    if direction == "in":
        run("UPDATE contacts SET last_msg_at = ? WHERE id = ?", (now(), contact_id))
    return msg_id


def history(contact_id: int, limit: int = 30) -> list[sqlite3.Row]:
    rows = q(
        "SELECT * FROM messages WHERE contact_id = ? ORDER BY id DESC LIMIT ?",
        (contact_id, limit),
    )
    return list(reversed(rows))


def unread_count() -> int:
    row = q1("SELECT COUNT(*) AS c FROM messages WHERE direction = 'in' AND is_read = 0")
    return row["c"] if row else 0


# ── лиды ───────────────────────────────────────────────────────────────

def get_lead(contact_id: int) -> sqlite3.Row | None:
    return q1("SELECT * FROM leads WHERE contact_id = ?", (contact_id,))


LEAD_FIELDS = ("name", "contact", "product", "need", "deadline", "comment", "summary")


def upsert_lead(contact_id: int, fields: dict, status: str | None = None) -> sqlite3.Row:
    """Создать карточку или дополнить существующую.

    Пустые значения не затирают уже собранное — модель может не повторить
    в новом ответе то, что уже узнала раньше.
    """
    clean = {k: v for k, v in fields.items() if k in LEAD_FIELDS and str(v or "").strip()}
    existing = get_lead(contact_id)

    if existing is None:
        cols = ["contact_id", "status", "created_at", "updated_at"] + list(clean)
        vals = [contact_id, status or "new", now(), now()] + [clean[k] for k in clean]
        placeholders = ", ".join("?" for _ in cols)
        run(f"INSERT INTO leads ({', '.join(cols)}) VALUES ({placeholders})", vals)
        return get_lead(contact_id)  # type: ignore[return-value]

    sets, vals = [], []
    for key, value in clean.items():
        sets.append(f"{key} = ?")
        vals.append(value)
    if status:
        sets.append("status = ?")
        vals.append(status)
    sets.append("updated_at = ?")
    vals.append(now())
    vals.append(contact_id)
    run(f"UPDATE leads SET {', '.join(sets)} WHERE contact_id = ?", vals)
    return get_lead(contact_id)  # type: ignore[return-value]


def set_lead_status(contact_id: int, status: str, manager: str | None = None) -> None:
    run(
        "UPDATE leads SET status = ?, manager = COALESCE(?, manager), updated_at = ?"
        " WHERE contact_id = ?",
        (status, manager, now(), contact_id),
    )
