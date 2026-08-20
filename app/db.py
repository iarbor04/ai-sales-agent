"""SQLite: схема, идемпотентные миграции и хелперы.

Миграции живут прямо здесь: при старте читаем PRAGMA table_info и добавляем
недостающие колонки через ALTER TABLE. Отдельной команды «накатить схему» нет
и быть не должно — деплой сам доводит базу до нужного состояния.

Доступ синхронный под общим замком: на нагрузке этого класса запрос
отрабатывает за доли миллисекунды, и это дешевле, чем тащить async-драйвер.
"""
from __future__ import annotations

import re
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

# Этапы воронки настраивает владелец, но два из них защищены:
#   is_system — куда падает лид при передаче менеджеру, без него сломается hand_off
#   is_won    — финальный: такие лиды исключаются из рассылок
# Этот набор засеивается при первом запуске и совпадает с прежними зашитыми
# статусами, поэтому лиды, созданные до появления настройки, остаются на месте.
DEFAULT_STAGES = [
    ("new", "Новый лид", "blue", 0, 0, 0),
    ("qualifying", "Квалификация", "violet", 1, 0, 0),
    ("handed", "Передан менеджеру", "amber", 2, 0, 1),
    ("won", "Сделка", "green", 3, 1, 0),
]
STAGE_COLORS = ("blue", "violet", "amber", "green", "red", "cyan", "pink", "gray")

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
        platform TEXT NOT NULL DEFAULT 'tg',
        extra TEXT,
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
    # Автоцепочки: последовательность сообщений для тех, кто написал и замолчал.
    """CREATE TABLE IF NOT EXISTS autochains (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1,
        created_at INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS autochain_steps (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chain_id INTEGER NOT NULL,
        position INTEGER NOT NULL DEFAULT 0,
        delay_min INTEGER NOT NULL DEFAULT 60,
        texts TEXT,
        buttons TEXT,
        image_path TEXT,
        enabled INTEGER NOT NULL DEFAULT 1
    )""",
    # Задание на отправку конкретного шага конкретному клиенту.
    # enrolled_msg_id — номер последнего входящего на момент постановки в
    # очередь. Сравниваем по номерам, а не по времени: постановка и ответ
    # укладываются в одну секунду, и по секундам «клиент ответил» не поймать.
    """CREATE TABLE IF NOT EXISTS autochain_jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chain_id INTEGER NOT NULL,
        step_id INTEGER NOT NULL,
        contact_id INTEGER NOT NULL,
        enrolled_at INTEGER NOT NULL,
        enrolled_msg_id INTEGER NOT NULL DEFAULT 0,
        due_at INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0,
        claimed_at INTEGER,
        error TEXT,
        UNIQUE (step_id, contact_id)
    )""",
    # Этапы воронки. Порядок задаёт position, а не порядок вставки: владелец
    # переставляет этапы в панели.
    """CREATE TABLE IF NOT EXISTS pipeline_stages (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        color TEXT NOT NULL DEFAULT 'gray',
        position INTEGER NOT NULL DEFAULT 0,
        is_won INTEGER NOT NULL DEFAULT 0,
        is_system INTEGER NOT NULL DEFAULT 0
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
    # Онлайн-запись: услуги, сотрудники, часы работы и журнал записей.
    """CREATE TABLE IF NOT EXISTS services (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        duration_min INTEGER NOT NULL DEFAULT 60,
        price TEXT,
        enabled INTEGER NOT NULL DEFAULT 1
    )""",
    """CREATE TABLE IF NOT EXISTS staff (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1
    )""",
    """CREATE TABLE IF NOT EXISTS work_hours (
        weekday INTEGER PRIMARY KEY,
        open_at TEXT,
        close_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS bookings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        contact_id INTEGER NOT NULL,
        service_id INTEGER,
        staff_id INTEGER,
        starts_at INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'new',
        reminded INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_booking_time ON bookings(starts_at)",
    # Конкуренты: следим за их публичными страницами и замечаем изменения.
    """CREATE TABLE IF NOT EXISTS rivals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        url TEXT NOT NULL UNIQUE,
        enabled INTEGER NOT NULL DEFAULT 1,
        last_hash TEXT,
        last_text TEXT,
        checked_at INTEGER,
        last_error TEXT,
        created_at INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS rival_changes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        rival_id INTEGER NOT NULL,
        summary TEXT,
        details TEXT,
        important INTEGER NOT NULL DEFAULT 0,
        found_at INTEGER NOT NULL
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
    ("bots", "platform", "TEXT NOT NULL DEFAULT 'tg'"),
    ("bots", "extra", "TEXT"),
    ("bots", "script_enabled", "INTEGER NOT NULL DEFAULT 0"),
    ("bots", "greeting", "TEXT"),
    ("bots", "last_error", "TEXT"),
    # Язык клиента: без него мультиязычная рассылка не знает, какой текст брать.
    ("contacts", "language", "TEXT"),
    # Рассылка: варианты текста по языкам, до трёх кнопок, отбор по этапу воронки.
    ("broadcasts", "texts", "TEXT"),
    ("broadcasts", "buttons", "TEXT"),
    ("broadcasts", "stage_filter", "TEXT"),
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
# Четыре шага, а не шесть: исследования по чат-ботам продаж показывают, что
# каждый вопрос сверх пятого роняет доходимость разговора примерно на 10%.
# Лучше собрать главное и отдать живому человеку, чем устроить допрос.
DEFAULT_SCRIPT = [
    ("Знакомство", "Понять, с чем пришёл клиент, и по ходу узнать его имя.", "name"),
    ("Потребность", "Выяснить, что именно нужно и зачем — продукт, объём, условия.", "product"),
    ("Сроки", "Узнать, к какому сроку это нужно.", "deadline"),
    ("Контакт", "Взять телефон или почту и передать разговор менеджеру.", "contact"),
]

# Готовые сценарии под нишу. Владелец выбирает свой в конструкторе и правит
# под себя — это быстрее, чем придумывать шаги с нуля.
SCRIPT_TEMPLATES = {
    "shop": {
        "title": "Интернет-магазин",
        "hint": "товар, наличие, доставка",
        "steps": [
            ("Знакомство", "Понять, какой товар интересует, и узнать имя.", "name"),
            ("Что нужно", "Выяснить модель, размер, цвет или комплектацию.", "product"),
            ("Доставка", "Узнать город и когда нужно получить.", "deadline"),
            ("Контакт", "Взять телефон и передать заказ менеджеру.", "contact"),
        ],
    },
    "services": {
        "title": "Услуги",
        "hint": "задача, объём, сроки",
        "steps": [
            ("Знакомство", "Понять, с какой задачей пришёл клиент, узнать имя.", "name"),
            ("Задача", "Выяснить, что именно нужно сделать и в каком объёме.", "need"),
            ("Сроки", "Узнать, к какому сроку нужен результат.", "deadline"),
            ("Контакт", "Взять телефон или почту и передать менеджеру.", "contact"),
        ],
    },
    "realty": {
        "title": "Недвижимость",
        "hint": "объект, бюджет, просмотр",
        "steps": [
            ("Знакомство", "Понять, что ищет клиент: покупка или аренда, узнать имя.", "name"),
            ("Объект", "Выяснить район, количество комнат и другие пожелания.", "product"),
            ("Условия", "Узнать про ипотеку, сроки заселения, что важно.", "need"),
            ("Просмотр", "Взять телефон и передать менеджеру для показа.", "contact"),
        ],
    },
    "education": {
        "title": "Обучение",
        "hint": "цель, уровень, старт",
        "steps": [
            ("Знакомство", "Узнать имя и чему человек хочет научиться.", "name"),
            ("Цель", "Выяснить, зачем это нужно и какой сейчас уровень.", "need"),
            ("Старт", "Узнать, когда готов начать.", "deadline"),
            ("Контакт", "Взять контакт и передать куратору.", "contact"),
        ],
    },
    "clinic": {
        "title": "Услуги с записью",
        "hint": "услуга, врач, время",
        "steps": [
            ("Знакомство", "Понять, с чем обращается клиент, узнать имя.", "name"),
            ("Услуга", "Выяснить, какая процедура или специалист нужны.", "product"),
            ("Время", "Узнать удобный день и время.", "deadline"),
            ("Запись", "Взять телефон и передать администратору.", "contact"),
        ],
    },
}

DEFAULT_SETTINGS = {
    "business_name": "",
    "business_site": "",
    "greeting": "Здравствуйте! Чем могу помочь?",
    "tone": "дружелюбный, короткий, по делу",
    "model": config.OPENROUTER_MODEL,
    "operator_chat_id": "",
    "managers": "",
    "handoff_note": "Передаю вас менеджеру, он ответит здесь же.",
    # Свои правила владельца, которые дописываются в системный промпт.
    "prompt_extra": "",
    # Промпт целиком. Пусто — собирается из простых настроек ниже.
    "prompt_template": "",
    # Простые настройки поведения: кто он, как отвечает, когда зовёт человека.
    "agent_role": "продавец-консультант",
    "reply_length": "short",
    "handoff_reasons": "human,buy,angry,special,unknown",
    "ai_enabled_global": "1",
    "kb_extra": "",
    # ключ вставляется в панели; .env остаётся запасным вариантом
    "openrouter_key": "",
    # какой провайдер отвечает клиентам: openrouter или yandex
    "model_provider": "openrouter",
    "yandex_api_key": "",
    "yandex_folder_id": "",
    # Google Таблицы: база знаний на чтение, лиды на запись
    "sheets_crm_id": "",
    "sheets_crm_tab": "Лиды",
    # как часто обходить сайты конкурентов, в часах
    "rivals_every_hours": "12",
    "rivals_notify": "1",
    # онлайн-запись
    # чат для сайта
    "mail_subject": "Ваш вопрос",
    # как часто перечитывать страницы сайта, в часах
    "kb_refresh_hours": "24",
    "widget_enabled": "1",
    "widget_title": "Чат с консультантом",
    "widget_color": "#0a7c47",
    "widget_greeting": "Здравствуйте! Чем помочь?",
    "booking_enabled": "0",
    "booking_remind_hours": "3",
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
    _migrate_sheet_knowledge()
    _drop_gigachat()

    for key, value in DEFAULT_SETTINGS.items():
        run("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value))

    if not q("SELECT 1 FROM pipeline_stages LIMIT 1"):
        for stage in DEFAULT_STAGES:
            run("INSERT INTO pipeline_stages (id, title, color, position, is_won, is_system)"
                " VALUES (?, ?, ?, ?, ?, ?)", stage)

    if not q("SELECT 1 FROM work_hours LIMIT 1"):
        # будни с 10 до 19, выходные закрыты — правится в панели
        for weekday in range(7):
            run("INSERT OR IGNORE INTO work_hours (weekday, open_at, close_at)"
                " VALUES (?, ?, ?)",
                (weekday, "10:00" if weekday < 5 else None,
                 "19:00" if weekday < 5 else None))

    if not q("SELECT 1 FROM script_steps LIMIT 1"):
        for position, (title, goal, field) in enumerate(DEFAULT_SCRIPT):
            run(
                "INSERT INTO script_steps (bot_id, position, title, goal, ask_field, enabled)"
                " VALUES (NULL, ?, ?, ?, ?, 1)",
                (position, title, goal, field),
            )


def _drop_gigachat() -> None:
    """GigaChat убран из продукта: его настройки — мёртвые данные.

    Если он был выбран провайдером, возвращаем OpenRouter, иначе панель показывала
    бы выбор, которого больше нет.
    """
    if q1("SELECT 1 FROM settings WHERE key = 'model_provider' AND value = 'gigachat'"):
        run("UPDATE settings SET value = 'openrouter' WHERE key = 'model_provider'")
        run("UPDATE settings SET value = 'deepseek/deepseek-v4-flash' WHERE key = 'model'")
    run("DELETE FROM settings WHERE key LIKE 'gigachat_%'")


def _migrate_sheet_knowledge() -> None:
    """Прайс, прочитанный когда-то из Google Таблицы, оставить в базе знаний.

    Источник «таблица по ссылке» убран: публикация открывала прайс всему
    интернету, а закрытая таблица молча отдавала страницу входа вместо данных.
    Уже загруженный текст не выбрасываем — он становится обычным источником
    в списке файлов, и владелец заменит его загрузкой xlsx или удалит сам.
    """
    row = q1("SELECT id FROM kb_pages WHERE url = 'sheet://knowledge'")
    if row:
        run(
            "UPDATE kb_pages SET url = 'file://прайс-из-google-таблицы',"
            " title = 'Прайс из Google Таблицы (загрузите файл, чтобы обновить)'"
            " WHERE id = ?",
            (row["id"],),
        )
    run("DELETE FROM settings WHERE key = 'sheets_kb_url'")


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


def normalize_language(value: str | None) -> str | None:
    """«ru-RU» → «ru». Рассылка хранит тексты по короткому коду."""
    code = (value or "").strip().lower().replace("_", "-").split("-")[0]
    return code if code.isalpha() and len(code) == 2 else None


def upsert_contact(channel: str, external_id: str, username: str | None = None,
                   name: str | None = None, phone: str | None = None,
                   bot_id: int | None = None, language: str | None = None) -> sqlite3.Row:
    """Найти контакт или завести новый. Дубли невозможны по UNIQUE."""
    language = normalize_language(language)
    existing = get_contact(channel, external_id, bot_id)
    if existing:
        # не затираем уже известное пустотой
        run(
            "UPDATE contacts SET username = COALESCE(?, username),"
            " name = COALESCE(?, name), phone = COALESCE(?, phone),"
            " language = COALESCE(?, language) WHERE id = ?",
            (username, name, phone, language, existing["id"]),
        )
        return get_contact(channel, external_id, bot_id)  # type: ignore[return-value]

    run(
        "INSERT INTO contacts (bot_id, channel, external_id, username, name, phone,"
        " language, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (bot_id, channel, str(external_id), username, name, phone, language, now()),
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


def pipeline_stages() -> list[sqlite3.Row]:
    return q("SELECT * FROM pipeline_stages ORDER BY position, id")


def stage_titles() -> dict[str, str]:
    """id → подпись. Заменяет прежний зашитый LEAD_STATUSES."""
    return {row["id"]: row["title"] for row in pipeline_stages()}


def system_stage() -> str:
    """Этап «передан менеджеру». Без него передача диалога некуда бы вела."""
    row = q1("SELECT id FROM pipeline_stages WHERE is_system = 1 ORDER BY position LIMIT 1")
    if row:
        return row["id"]
    row = q1("SELECT id FROM pipeline_stages ORDER BY position DESC LIMIT 1")
    return row["id"] if row else "handed"


def won_stages() -> set[str]:
    """Финальные этапы: в рассылки такие лиды не попадают."""
    return {row["id"] for row in q("SELECT id FROM pipeline_stages WHERE is_won = 1")}


def first_stage() -> str:
    row = q1("SELECT id FROM pipeline_stages ORDER BY position LIMIT 1")
    return row["id"] if row else "new"


def save_pipeline_stages(items: list[dict]) -> int:
    """Заменить набор этапов. Возвращает число перенесённых лидов.

    Лиды с исчезнувшего этапа переносятся на первый, иначе они выпали бы из
    воронки и перестали показываться вообще.
    """
    stages = []
    seen: set[str] = set()
    for position, item in enumerate(items):
        stage_id = str(item.get("id") or "").strip().lower()
        title = str(item.get("title") or "").strip()
        color = str(item.get("color") or "gray")
        if not re.fullmatch(r"[a-z0-9-]{1,32}", stage_id) or stage_id in seen:
            raise ValueError("у каждого этапа должен быть свой короткий английский id")
        if not title or len(title) > 40:
            raise ValueError("назовите каждый этап, не длиннее 40 символов")
        seen.add(stage_id)
        stages.append({
            "id": stage_id, "title": title,
            "color": color if color in STAGE_COLORS else "gray",
            "position": position,
            "is_won": 1 if item.get("is_won") else 0,
            "is_system": 1 if item.get("is_system") else 0,
        })

    if not 1 <= len(stages) <= 8:
        raise ValueError("этапов должно быть от одного до восьми")
    if sum(s["is_system"] for s in stages) != 1:
        raise ValueError("нужен ровно один этап для передачи менеджеру")
    if sum(s["is_won"] for s in stages) > 1:
        raise ValueError("финальным можно сделать только один этап")

    run("DELETE FROM pipeline_stages")
    for stage in stages:
        run("INSERT INTO pipeline_stages (id, title, color, position, is_won, is_system)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (stage["id"], stage["title"], stage["color"], stage["position"],
             stage["is_won"], stage["is_system"]))

    valid = ",".join("?" for _ in stages)
    moved = q1(f"SELECT COUNT(*) AS c FROM leads WHERE status NOT IN ({valid})",
               tuple(s["id"] for s in stages))["c"]
    if moved:
        run(f"UPDATE leads SET status = ?, updated_at = ? WHERE status NOT IN ({valid})",
            (stages[0]["id"], now(), *[s["id"] for s in stages]))
    return moved


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


def add_bot(title: str, token: str, role: str = "sales",
            platform: str = "tg", extra: str | None = None) -> int:
    """extra — настройки конкретной платформы в JSON (id сообщества и прочее)."""
    return run(
        "INSERT INTO bots (title, token, role, platform, extra, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (title, token.strip(), role, platform, extra, now()),
    )


def set_bot_error(bot_id: int, error: str | None) -> None:
    """Записать ошибку подключения бота человеческими словами.

    Раньше каждый канал клал в базу текст своей библиотеки, и владелец читал
    в панели «Telegram server says - Unauthorized» вместо понятного действия.
    """
    if not error:
        run("UPDATE bots SET last_error = NULL WHERE id = ?", (bot_id,))
        return
    from .channels import explain_token_error
    run("UPDATE bots SET last_error = ? WHERE id = ?", (explain_token_error(error)[:200], bot_id))


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


def template_prompt(key: str) -> str:
    """Шаблон сценария словами — чтобы вставить его прямо в промпт.

    Один и тот же набор шагов и применяется в «Сценарии», и вставляется в
    промпт: держать два разных описания одного сценария — верный способ развести
    их со временем.
    """
    template = SCRIPT_TEMPLATES.get(key)
    if not template:
        return ""
    lines = [f"Веди разговор по шагам ({template['title'].lower()}):"]
    lines += [f"{number}. {title} — {goal}"
              for number, (title, goal, _field) in enumerate(template["steps"], start=1)]
    lines.append("Не перескакивай через шаг и не возвращайся назад без нужды.")
    return "\n".join(lines)


def apply_template(key: str, bot_id: int | None = None) -> int:
    """Заменить сценарий готовым шаблоном. Возвращает число шагов."""
    template = SCRIPT_TEMPLATES.get(key)
    if not template:
        return 0
    run("DELETE FROM script_steps WHERE bot_id IS ?", (bot_id,))
    for position, (title, goal, field) in enumerate(template["steps"]):
        run(
            "INSERT INTO script_steps (bot_id, position, title, goal, ask_field, enabled)"
            " VALUES (?, ?, ?, ?, ?, 1)",
            (bot_id, position, title, goal, field),
        )
    return len(template["steps"])


def reorder_script(order: list[int]) -> None:
    """Сохранить новый порядок шагов после перетаскивания."""
    for position, step_id in enumerate(order):
        run("UPDATE script_steps SET position = ? WHERE id = ?", (position, step_id))


# ── конкуренты ─────────────────────────────────────────────────────────

def rivals(only_enabled: bool = False) -> list[sqlite3.Row]:
    sql = "SELECT * FROM rivals"
    if only_enabled:
        sql += " WHERE enabled = 1"
    return q(sql + " ORDER BY id")


def add_rival(title: str, url: str) -> int:
    return run(
        "INSERT OR IGNORE INTO rivals (title, url, created_at) VALUES (?, ?, ?)",
        (title.strip(), url.strip(), now()),
    )


def rival_changes(limit: int = 100) -> list[sqlite3.Row]:
    return q(
        "SELECT c.*, r.title, r.url FROM rival_changes c"
        " JOIN rivals r ON r.id = c.rival_id"
        " ORDER BY c.id DESC LIMIT ?",
        (limit,),
    )
