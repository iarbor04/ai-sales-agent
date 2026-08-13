"""Каналы связи с клиентом."""
from __future__ import annotations

from .. import config, db


def telegram_enabled() -> bool:
    """Хоть один включённый бот заведён в панели."""
    return bool(db.bots(only_enabled=True))


def active() -> list[str]:
    channels = []
    if telegram_enabled():
        channels.append("tg")
    if config.whatsapp_enabled():
        channels.append("wa")
    return channels


def adopt_env_token() -> None:
    """Перенести токен из .env в панель при первом запуске.

    Нужно ради тех, кто уже поднял проект со старым .env: бот не должен
    молча пропасть после обновления. Повторно ничего не создаёт.
    """
    token = config.TELEGRAM_TOKEN
    if not token:
        return
    if db.q1("SELECT 1 FROM bots WHERE token = ?", (token,)):
        return
    db.add_bot("Продажник", token, role="sales")
