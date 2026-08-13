"""Уведомления менеджеру — тем же ботом, в указанный чат или группу.

Отдельный бот не нужен: клиент и менеджер не пересекаются, потому что чат
менеджера задаётся вручную в Настройках.
"""
from __future__ import annotations

import logging

from . import config, db

log = logging.getLogger("notify")


def _chat_id() -> str:
    return db.setting("operator_chat_id", "").strip()


async def _push(text: str) -> None:
    chat = _chat_id()
    if not chat or not config.telegram_enabled():
        return
    from .channels import telegram
    await telegram.send(chat, text)


def _who(contact) -> str:
    name = contact["name"] or contact["username"] or contact["phone"] or f"id{contact['id']}"
    channel = config.CHANNEL_TITLES.get(contact["channel"], contact["channel"])
    return f"{name} ({channel})"


async def handed_off(contact_id: int, reason: str, manager: str = "") -> None:
    contact = db.contact_by_id(contact_id)
    if contact is None:
        return
    lead = db.get_lead(contact_id)
    link = f"{config.PUBLIC_URL}/dialogs?c={contact_id}"

    lines = [
        "🔔 <b>Нужен менеджер</b>",
        f"Клиент: {_who(contact)}",
        f"Причина: {reason}",
    ]
    if manager:
        lines.append(f"Ответственный: {manager}")
    if lead and lead["summary"]:
        lines.append(f"Суть: {lead['summary']}")
    lines.append(f'<a href="{link}">Открыть диалог</a>')
    await _push("\n".join(lines))


async def new_message(contact_id: int, preview: str) -> None:
    """Клиент написал в диалог, который уже ведёт человек."""
    contact = db.contact_by_id(contact_id)
    if contact is None:
        return
    link = f"{config.PUBLIC_URL}/dialogs?c={contact_id}"
    await _push(
        f"💬 <b>{_who(contact)}</b>\n{(preview or '')[:300]}\n"
        f'<a href="{link}">Ответить</a>'
    )


async def broadcast_done(broadcast_id: int, sent: int, failed: int) -> None:
    await _push(
        f"📣 Рассылка #{broadcast_id} завершена.\n"
        f"Доставлено: {sent}. Ошибок: {failed}."
    )
