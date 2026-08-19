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


async def _push(text: str, markup=None) -> None:
    """Уведомление менеджерам. Шлём служебным ботом, если он заведён."""
    chat = _chat_id()
    if not chat:
        return
    from .channels import telegram
    manager = db.manager_bot()
    if manager is None:
        return
    await telegram.send(chat, text, bot_id=manager["id"], markup=markup)


def _who(contact) -> str:
    name = contact["name"] or contact["username"] or contact["phone"] or f"id{contact['id']}"
    channel = config.CHANNEL_TITLES.get(contact["channel"], contact["channel"])
    return f"{name} ({channel})"


async def handed_off(contact_id: int, reason: str, manager: str = "",
                     request_id: int | None = None) -> None:
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

    # кнопки «Взять в работу» / «Передать» прямо под уведомлением
    markup = None
    if request_id:
        from .channels.telegram import request_markup
        markup = request_markup(request_id)
    await _push("\n".join(lines), markup=markup)


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


async def rival_changed(title: str, url: str, summary: str) -> None:
    """Важное изменение у конкурента — сразу в чат менеджеров."""
    await _push(
        f"👀 <b>Конкурент: {title}</b>\n{summary}\n"
        f'<a href="{url}">Открыть страницу</a> · '
        f'<a href="{config.PUBLIC_URL}/rivals">Все изменения</a>'
    )


async def booked(contact_id: int, slot: dict) -> None:
    """Клиент записался — сообщаем менеджеру."""
    contact = db.contact_by_id(contact_id)
    if contact is None:
        return
    who = f"\nМастер: {slot['staff']}" if slot.get("staff") else ""
    await _push(
        f"📅 <b>Новая запись</b>\n"
        f"Клиент: {_who(contact)}\n"
        f"Услуга: {slot['service']}\n"
        f"Когда: {slot['weekday']} {slot['label']}{who}"
    )


async def health_alert(problems: list[str]) -> None:
    """Служба проверила себя и нашла поломку. Пишем один раз, а не каждый час."""
    lines = ["⚠️ <b>Проверка сервиса нашла проблемы</b>"]
    lines += [f"— {problem}" for problem in problems]
    lines.append(f'<a href="{config.PUBLIC_URL}/settings">Открыть настройки</a>')
    await _push("\n".join(lines))


async def health_recovered() -> None:
    await _push("✅ <b>Проверка сервиса: всё снова работает</b>")


async def kb_pages_gone(count: int) -> None:
    """Страницы базы знаний перестали открываться — это дыра в ответах."""
    await _push(
        f"⚠️ <b>База знаний</b>\nСтраниц перестало открываться: {count}. "
        f"Агент больше не отвечает по ним и будет чаще звать менеджера.\n"
        f'<a href="{config.PUBLIC_URL}/knowledge">Проверить источники</a>'
    )
