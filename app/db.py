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
    # Боты живут в базе, а не в .env: владелец подключает их из панели,
    # без доступа к серверу. Ботов может быть сколько угодно.
    #   role = 'sales'   — говорит с клиентами
    #   role = 'manager' — служебный: уведомления и кнопки для менеджеров
    """CREATE TABLE IF NOT EXISTS bots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        token TEXT NOT NULL UNIQUE,
        role TEXT NOT NULL DEFAULT 'sales',
        username TEXT,
        enabled INTEGER NOT NULL DEFAULT 1,
        greeting TEXT,
        script_enabled INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,
        created_at INTEGER NOT NULL
    )""",
    # Сценарий продаж: шаги, по которым агент ведёт разговор.
    # Это не жёсткий автомат — модель получает цель текущего шага и сама
    # решает, как её достичь и когда шаг закрыт.
    """CREATE TABLE IF NOT EXISTS script_steps (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bot_id INTEGER,
        position INTEGER NOT NULL DEFAULT 0,
        title TEXT NOT NULL,
        goal TEXT,
        ask_field TEXT,
        enabled INTEGER NOT NULL DEFAULT 1
    )""",
    # Лог обращений: всё, что требует человека. Отсюда кнопки «взять в работу»
    # и «передать менеджеру» — и в панели, и в Telegram.
    """CREATE TABLE IF NOT EXISTS requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        contact_id INTEGER NOT NULL,
        kind TEXT NOT NULL DEFAULT 'handoff',
        reason TEXT,
        status TEXT NOT NULL DEFAULT 'new',
        manager TEXT,
        created_at INTEGER NOT NULL,
        taken_at INTEGER,
        closed_at INTEGER
    )""",
    # Один контакт на тройку (бот, канал, внешний id) — защита от дублей.
    """CREATE TABLE IF NOT EXISTS contacts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bot_id INTEGER,
        channel TEXT NOT NULL,
        external_id TEXT NOT NULL,
        username TEXT,
        phone TEXT,
        name TEXT,
        ai_enabled INTEGER NOT NULL DEFAULT 1,
        opted_in INTEGER NOT NULL DEFAULT 1,
        blocked INTEGER NOT NULL DEFAULT 0,
        manager TEXT,
        step INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL,
        last_msg_at INTEGER,
        UNIQUE (bot_id, channel, external_id)
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
    ("contacts", "bot_id", "INTEGER"),
    ("contacts", "step", "INTEGER NOT NULL DEFAULT 0"),
    ("bots", "script_enabled", "INTEGER NOT NULL DEFAULT 0"),
    ("bots", "greeting", "TEXT"),
    ("bots", "last_error", "TEXT"),
    ("leads", "summary", "TEXT"),
    ("leads", "manager", "TEXT"),
    ("leads", "synced_at", "INTEGER"),
    ("kb_pages", "chars", "INTEGER NOT NULL DEFAULT 0"),
    ("kb_pages", "status", "TEXT NOT NULL DEFAULT 'found'"),
    ("broadcasts", "button_text", "TEXT"),
    ("broadcasts", "button_url", "TEXT"),
    ("broadcasts", "failed_count", "INTEGER NOT NULL DEFAULT 0"),
]

# Сценарий по умолчанию. Владелец правит его в панели: шаги, цели, порядок.
DEFAULT_SCRIPT = [
    ("Знакомство", "Поздороваться, понять, с чем пришёл клиент, узнать имя.", "name"),
    ("Потребность", "Выяснить, какой продукт или услуга нужны и зачем.", "product"),
    ("Детали", "Уточнить объём, комплектацию или условия.", "need"),
    ("Сроки", "Узнать, к какому сроку это нужно.", "deadline"),
    ("Контакт", "Взять телефон или почту для связи.", "contact"),
    ("Передача", "Подвести итог и передать разговор менеджеру.", ""),
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
    # ключ вставляется в панели; .env остаётся запасным вариантом
    "openrouter_key": "",
    # Google Таблицы: база знаний на чтение, лиды на запись
    "sheets_kb_url": "",
    "sheets_crm_id": "",
    "sheets_crm_tab": "Лиды",
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

    _migrate_contacts_key()

    for key, value in DEFAULT_SETTINGS.items():
        run("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value))

    if not q("SELECT 1 FROM script_steps LIMIT 1"):
        for position, (title, goal, field) in enumerate(DEFAULT_SCRIPT):
            run(
                "INSERT INTO script_steps (bot_id, position, title, goal, ask_field, enabled)"
                " VALUES (NULL, ?, ?, ?, ?, 1)",
                (position, title, goal, field),
            )


def _migrate_contacts_key() -> None:
    """Перевести старую уникальность (канал, id) на (бот, канал, id).

    Понадобилось, когда ботов стало больше одного: один и тот же человек,
    написавший двум разным ботам, — это два разных разговора.
    SQLite не умеет менять UNIQUE через ALTER, поэтому таблица пересобирается.
    """
    with _lock:
        conn = connect()
        indexes = conn.execute("PRAGMA index_list(contacts)").fetchall()
        for index in indexes:
            if not index["unique"]:
                continue
            columns = [
                r["name"] for r in conn.execute(f"PRAGMA index_info({index['name']})")
            ]
            if columns == ["channel", "external_id"]:
                break
        else:
            return  # уже новая схема

        conn.executescript(
            """
            PRAGMA foreign_keys=OFF;
            BEGIN;
            CREATE TABLE contacts_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_id INTEGER,
                channel TEXT NOT NULL,
                external_id TEXT NOT NULL,
                username TEXT,
                phone TEXT,
                name TEXT,
                ai_enabled INTEGER NOT NULL DEFAULT 1,
                opted_in INTEGER NOT NULL DEFAULT 1,
                blocked INTEGER NOT NULL DEFAULT 0,
                manager TEXT,
                step INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,
                last_msg_at INTEGER,
                UNIQUE (bot_id, channel, external_id)
            );
            INSERT INTO contacts_new (id, bot_id, channel, external_id, username, phone,
                name, ai_enabled, opted_in, blocked, manager, step, created_at, last_msg_at)
            SELECT id, NULL, channel, external_id, username, phone, name,
                ai_enabled, opted_in, blocked, manager, 0, created_at, last_msg_at
            FROM contacts;
            DROP TABLE contacts;
            ALTER TABLE contacts_new RENAME TO contacts;
            COMMIT;
            PRAGMA foreign_keys=ON;
            """
        )
        conn.commit()


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

def get_contact(channel: str, external_id: str,
                bot_id: int | None = None) -> sqlite3.Row | None:
    return q1(
        "SELECT * FROM contacts WHERE channel = ? AND external_id = ?"
        " AND bot_id IS ?",
        (channel, str(external_id), bot_id),
    )


def upsert_contact(channel: str, external_id: str, username: str | None = None,
                   name: str | None = None, phone: str | None = None,
                   bot_id: int | None = None) -> sqlite3.Row:
    """Найти контакт или завести новый. Дубли невозможны по UNIQUE."""
    existing = get_contact(channel, external_id, bot_id)
    if existing:
        # не затираем уже известное пустотой
        run(
            "UPDATE contacts SET username = COALESCE(?, username),"
            " name = COALESCE(?, name), phone = COALESCE(?, phone) WHERE id = ?",
            (username, name, phone, existing["id"]),
        )
        return get_contact(channel, external_id, bot_id)  # type: ignore[return-value]

    run(
        "INSERT INTO contacts (bot_id, channel, external_id, username, name, phone, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (bot_id, channel, str(external_id), username, name, phone, now()),
    )
    return get_contact(channel, external_id, bot_id)  # type: ignore[return-value]


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


# ── боты ───────────────────────────────────────────────────────────────

def bots(role: str | None = None, only_enabled: bool = True) -> list[sqlite3.Row]:
    sql = "SELECT * FROM bots WHERE 1=1"
    params: list = []
    if role:
        sql += " AND role = ?"
        params.append(role)
    if only_enabled:
        sql += " AND enabled = 1"
    sql += " ORDER BY id"
    return q(sql, params)


def bot(bot_id: int) -> sqlite3.Row | None:
    return q1("SELECT * FROM bots WHERE id = ?", (bot_id,))


def add_bot(title: str, token: str, role: str = "sales") -> int:
    return run(
        "INSERT INTO bots (title, token, role, created_at) VALUES (?, ?, ?, ?)",
        (title, token.strip(), role, now()),
    )


def manager_bot() -> sqlite3.Row | None:
    """Бот для служебных уведомлений. Нет отдельного — берём первый рабочий."""
    row = q1("SELECT * FROM bots WHERE role = 'manager' AND enabled = 1 ORDER BY id LIMIT 1")
    return row or q1("SELECT * FROM bots WHERE enabled = 1 ORDER BY id LIMIT 1")


# ── сценарий продаж ────────────────────────────────────────────────────

def script(bot_id: int | None = None) -> list[sqlite3.Row]:
    """Шаги сценария: свои у бота, иначе общие."""
    own = q(
        "SELECT * FROM script_steps WHERE bot_id = ? AND enabled = 1 ORDER BY position",
        (bot_id,),
    )
    if own:
        return own
    return q("SELECT * FROM script_steps WHERE bot_id IS NULL AND enabled = 1 ORDER BY position")


def current_step(contact_id: int) -> sqlite3.Row | None:
    """Шаг, на котором стоит разговор с этим контактом."""
    contact = contact_by_id(contact_id)
    if contact is None:
        return None
    steps = script(contact["bot_id"])
    if not steps:
        return None
    index = min(contact["step"], len(steps) - 1)
    return steps[index]


def advance_step(contact_id: int) -> None:
    contact = contact_by_id(contact_id)
    if contact is None:
        return
    steps = script(contact["bot_id"])
    if contact["step"] < len(steps) - 1:
        run("UPDATE contacts SET step = step + 1 WHERE id = ?", (contact_id,))


# ── лог обращений ──────────────────────────────────────────────────────

REQUEST_STATUSES = {
    "new": "Новое",
    "in_work": "В работе",
    "closed": "Закрыто",
}


def open_request(contact_id: int, reason: str, kind: str = "handoff") -> int:
    """Завести обращение. Если по контакту уже есть открытое — не плодим."""
    existing = q1(
        "SELECT id FROM requests WHERE contact_id = ? AND status != 'closed'"
        " ORDER BY id DESC LIMIT 1",
        (contact_id,),
    )
    if existing:
        run("UPDATE requests SET reason = ? WHERE id = ?", (reason, existing["id"]))
        return existing["id"]
    return run(
        "INSERT INTO requests (contact_id, kind, reason, status, created_at)"
        " VALUES (?, ?, ?, 'new', ?)",
        (contact_id, kind, reason, now()),
    )


def take_request(request_id: int, manager: str) -> sqlite3.Row | None:
    run(
        "UPDATE requests SET status = 'in_work', manager = ?, taken_at = ?"
        " WHERE id = ? AND status = 'new'",
        (manager, now(), request_id),
    )
    row = q1("SELECT * FROM requests WHERE id = ?", (request_id,))
    if row:
        run("UPDATE contacts SET manager = ? WHERE id = ?", (manager, row["contact_id"]))
        run("UPDATE leads SET manager = ? WHERE contact_id = ?", (manager, row["contact_id"]))
    return row


def close_request(request_id: int) -> None:
    run(
        "UPDATE requests SET status = 'closed', closed_at = ? WHERE id = ?",
        (now(), request_id),
    )


def open_requests_count() -> int:
    row = q1("SELECT COUNT(*) AS c FROM requests WHERE status != 'closed'")
    return row["c"] if row else 0
