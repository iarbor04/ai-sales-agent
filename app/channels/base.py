"""Единый интерфейс отправки: остальному коду всё равно, TG это или WA.

Благодаря этому логика продаж, рассылки и панель менеджера написаны один раз
и работают с любым каналом. Третий канал добавляется одним модулем и одной
строкой в send().
"""
from __future__ import annotations

import logging

from .. import db

log = logging.getLogger("channels")


async def send(contact_id: int, text: str, image_path: str | None = None,
               button: tuple[str, str] | None = None, author: str = "ai") -> tuple[bool, str]:
    """Отправить сообщение контакту и записать его в переписку.

    Возвращает (успех, статус). Статусы: sent | blocked | empty | error | no_channel.
    Пустой текст без картинки не отправляем никогда — клиент получил бы тишину.
    """
    contact = db.contact_by_id(contact_id)
    if contact is None:
        return False, "no_contact"

    text = (text or "").strip()
    if not text and not image_path:
        return False, "empty"

    channel = contact["channel"]
    if channel == "tg":
        from . import telegram
        # отвечаем тем же ботом, которому человек написал
        ok, status = await telegram.send(
            contact["external_id"], text, image_path, button, bot_id=contact["bot_id"]
        )
    elif channel == "wa":
        from . import whatsapp
        ok, status = await whatsapp.send(contact["external_id"], text, image_path, button)
    else:
        return False, "no_channel"

    if status == "blocked":
        db.run("UPDATE contacts SET blocked = 1, opted_in = 0 WHERE id = ?", (contact_id,))

    if ok:
        db.add_message(
            contact_id, "out", author, text,
            "photo" if image_path else None, image_path, is_read=True,
        )
    return ok, status
