"""Конфигурация целиком из .env — в коде не зашито ничего."""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _int(key: str, default: int) -> int:
    try:
        return int(_env(key) or default)
    except ValueError:
        return default


def _bool(key: str, default: bool = False) -> bool:
    value = _env(key).lower()
    if not value:
        return default
    return value in ("1", "true", "yes", "on")


def _load_dotenv() -> None:
    """Минимальный парсер .env — без зависимости от python-dotenv."""
    path = BASE_DIR / ".env"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        # переменные окружения имеют приоритет над файлом
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()

# ── каналы клиента ─────────────────────────────────────────────────────
# Telegram подключается своим ботом от BotFather, а НЕ каналом ASCN:
# канал ASCN дублировал бы воркспейс и уводил переписку из панели.
TELEGRAM_TOKEN = _env("TELEGRAM_BOT_TOKEN")

# WhatsApp Business через Meta Cloud API. Пусто — канал просто выключен.
WA_TOKEN = _env("WHATSAPP_TOKEN")
WA_PHONE_ID = _env("WHATSAPP_PHONE_ID")
WA_VERIFY_TOKEN = _env("WHATSAPP_VERIFY_TOKEN", "verify-me")

CHANNEL_TITLES = {"tg": "Telegram", "wa": "WhatsApp", "max": "MAX",
                  "vk": "ВКонтакте", "mail": "Почта", "avito": "Авито",
                  "web": "Чат на сайте"}


def telegram_enabled() -> bool:
    return bool(TELEGRAM_TOKEN)


def whatsapp_enabled() -> bool:
    return bool(WA_TOKEN and WA_PHONE_ID)


def active_channels() -> list[str]:
    channels = []
    if telegram_enabled():
        channels.append("tg")
    if whatsapp_enabled():
        channels.append("wa")
    return channels


# ── модель ─────────────────────────────────────────────────────────────
OPENROUTER_API_KEY = _env("OPENROUTER_API_KEY")
# Модель по умолчанию; владелец меняет её в Настройках из списка доступных.
OPENROUTER_MODEL = _env("OPENROUTER_MODEL", "openai/gpt-4o-mini")
AI_ENABLED = bool(OPENROUTER_API_KEY)

# ── панель ─────────────────────────────────────────────────────────────
ADMIN_LOGIN = _env("ADMIN_LOGIN", "admin")
ADMIN_PASSWORD = _env("ADMIN_PASSWORD", "admin")
SECRET_KEY = _env("SECRET_KEY", "change-me-in-env")
PUBLIC_URL = _env("PUBLIC_URL", "http://localhost:8000").rstrip("/")

HOST = _env("HOST", "0.0.0.0")
PORT = _int("PORT", 8000)

# ── хранилище ──────────────────────────────────────────────────────────
DB_PATH = Path(_env("DB_PATH", str(BASE_DIR / "data.db")))
MEDIA_DIR = Path(_env("MEDIA_DIR", str(BASE_DIR / "media")))
MEDIA_DIR.mkdir(parents=True, exist_ok=True)

# ── режим приёма сообщений ─────────────────────────────────────────────
# polling — long polling Telegram, публичный адрес не нужен
# webhook — Telegram и WhatsApp стучатся на PUBLIC_URL
# WhatsApp работает ТОЛЬКО в режиме webhook: Cloud API не умеет polling.
MODE = (_env("MODE", "polling") or "polling").lower()
WEBHOOK_SECRET = _env("WEBHOOK_SECRET", SECRET_KEY[:32] or "webhook-secret")

# ── Google Таблицы ─────────────────────────────────────────────────────
# Только выгрузка лидов: нужен сервисный аккаунт, путь к его JSON-ключу.
# Прайс загружается файлом в разделе «База знаний» — ключей не требует.
GOOGLE_SA_FILE = _env("GOOGLE_SA_FILE")

# ── база знаний ────────────────────────────────────────────────────────
CRAWL_MAX_PAGES = _int("CRAWL_MAX_PAGES", 80)
CRAWL_TIMEOUT = _int("CRAWL_TIMEOUT", 15)
CHUNK_CHARS = _int("CHUNK_CHARS", 1200)

# ── поведение ──────────────────────────────────────────────────────────
TICK_SECONDS = _int("TICK_SECONDS", 30)
SEND_PAUSE_MS = _int("SEND_PAUSE_MS", 60)
# после скольких собранных полей заводим карточку лида
LEAD_AFTER_FIELDS = _int("LEAD_AFTER_FIELDS", 2)
