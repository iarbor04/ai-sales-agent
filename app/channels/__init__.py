"""Каналы связи с клиентом."""
from __future__ import annotations

from .. import config, db


def telegram_enabled() -> bool:
    """Хоть один включённый Telegram-бот заведён в панели."""
    return any(b["platform"] == "tg" for b in db.bots(only_enabled=True))


def max_enabled() -> bool:
    return any(b["platform"] == "max" for b in db.bots(only_enabled=True))


def active() -> list[str]:
    channels = []
    if telegram_enabled():
        channels.append("tg")
    if max_enabled():
        channels.append("max")
    if config.whatsapp_enabled():
        channels.append("wa")
    from . import web as webchat
    if webchat.enabled():
        channels.append("web")
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


# ── общий подъём каналов ───────────────────────────────────────────────

async def start_all() -> None:
    """Поднять ботов всех платформ."""
    from . import maxru, telegram
    await telegram.start()
    for row in db.bots(only_enabled=True):
        if row["platform"] == "max":
            await maxru.start_bot(row)


async def reload_all() -> None:
    """Привести живых ботов в соответствие с базой — после правок в панели."""
    from . import maxru, telegram
    await telegram.reload()

    wanted = {r["id"]: r for r in db.bots(only_enabled=True) if r["platform"] == "max"}
    for bot_id in list(maxru.live()):
        if bot_id not in wanted:
            await maxru.stop_bot(bot_id)
    for bot_id, row in wanted.items():
        if bot_id not in maxru.live():
            await maxru.start_bot(row)


async def stop_all() -> None:
    from . import maxru, telegram
    await telegram.stop()
    for bot_id in list(maxru.live()):
        await maxru.stop_bot(bot_id)


async def check_token(platform: str, token: str) -> dict:
    """Проверить токен до сохранения — у каждой платформы свой способ."""
    from . import maxru, telegram
    if platform == "max":
        return await maxru.check_token(token)
    return await telegram.check_token(token)


def live_ids() -> set[int]:
    """Кто сейчас на связи — для отметки в панели."""
    from . import maxru, telegram
    return set(telegram.BOTS) | maxru.live()
