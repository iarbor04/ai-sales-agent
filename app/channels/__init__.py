"""Каналы связи с клиентом."""
from __future__ import annotations

from .. import config, db


def telegram_enabled() -> bool:
    """Хоть один включённый Telegram-бот заведён в панели."""
    return any(b["platform"] == "tg" for b in db.bots(only_enabled=True))


def max_enabled() -> bool:
    return any(b["platform"] == "max" for b in db.bots(only_enabled=True))


def vk_enabled() -> bool:
    return any(b["platform"] == "vk" for b in db.bots(only_enabled=True))


def mail_enabled() -> bool:
    return any(b["platform"] == "mail" for b in db.bots(only_enabled=True))


def avito_enabled() -> bool:
    return any(b["platform"] == "avito" for b in db.bots(only_enabled=True))


def active() -> list[str]:
    channels = []
    if telegram_enabled():
        channels.append("tg")
    if max_enabled():
        channels.append("max")
    if vk_enabled():
        channels.append("vk")
    if mail_enabled():
        channels.append("mail")
    if avito_enabled():
        channels.append("avito")
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

# платформы, у которых свой модуль со start_bot/stop_bot/live
EXTRA_PLATFORMS = ("max", "vk", "mail", "avito")


def _module(platform: str):
    from . import avito, mail, maxru, vk
    return {"max": maxru, "vk": vk, "mail": mail, "avito": avito}[platform]


async def start_all() -> None:
    """Поднять ботов всех платформ."""
    from . import telegram
    await telegram.start()
    for row in db.bots(only_enabled=True):
        if row["platform"] in EXTRA_PLATFORMS:
            await _module(row["platform"]).start_bot(row)


async def reload_all() -> None:
    """Привести живых ботов в соответствие с базой — после правок в панели."""
    from . import telegram
    await telegram.reload()

    for platform in EXTRA_PLATFORMS:
        module = _module(platform)
        wanted = {r["id"]: r for r in db.bots(only_enabled=True)
                  if r["platform"] == platform}
        for bot_id in list(module.live()):
            if bot_id not in wanted:
                await module.stop_bot(bot_id)
        for bot_id, row in wanted.items():
            if bot_id not in module.live():
                await module.start_bot(row)


async def stop_all() -> None:
    from . import telegram
    await telegram.stop()
    for platform in EXTRA_PLATFORMS:
        module = _module(platform)
        for bot_id in list(module.live()):
            await module.stop_bot(bot_id)


async def check_token(platform: str, token: str, conf: dict | None = None) -> dict:
    """Проверить доступ до сохранения — у каждой платформы свой способ."""
    from . import telegram
    if platform in ("mail", "avito"):
        return await _module(platform).check_token(token, conf or {})
    if platform in EXTRA_PLATFORMS:
        return await _module(platform).check_token(token)
    return await telegram.check_token(token)


def live_ids() -> set[int]:
    """Кто сейчас на связи — для отметки в панели."""
    from . import telegram
    ids = set(telegram.BOTS)
    for platform in EXTRA_PLATFORMS:
        ids |= _module(platform).live()
    return ids
