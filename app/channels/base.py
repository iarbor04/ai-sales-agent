"""Единый интерфейс отправки: остальному коду всё равно, TG это или WA.

Благодаря этому логика продаж, рассылки и панель менеджера написаны один раз
и работают с любым каналом. Третий канал добавляется одним модулем и одной
строкой в send().
"""
from __future__ import annotations

import logging
from pathlib import Path

from .. import config, db

log = logging.getLogger("channels")

# По расширению понимаем, чем это отправлять. Голосовое и обычное аудио
# различаем: Telegram рисует их по-разному, и клиенту это заметно.
KINDS = {
    "photo": {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"},
    "voice": {".ogg", ".oga", ".opus"},
    "audio": {".mp3", ".m4a", ".wav", ".aac", ".flac"},
    "video": {".mp4", ".mov", ".webm", ".mkv"},
}


def media_kind(path: str | None) -> str | None:
    """Тип вложения по расширению. Неизвестное шлём документом."""
    if not path:
        return None
    suffix = Path(path).suffix.lower()
    for kind, extensions in KINDS.items():
        if suffix in extensions:
            return kind
    return "document"


def media_file(path: str) -> Path:
    """Абсолютный путь к вложению в хранилище."""
    candidate = Path(path)
    return candidate if candidate.is_absolute() else config.MEDIA_DIR / candidate.name


async def send(contact_id: int, text: str, media_path: str | None = None,
               button: tuple[str, str] | None = None, author: str = "ai") -> tuple[bool, str]:
    """Отправить сообщение контакту и записать его в переписку.

    Возвращает (успех, статус). Статусы: sent | blocked | empty | error | no_channel.
    Пустой текст без вложения не отправляем никогда — клиент получил бы тишину.
    """
    contact = db.contact_by_id(contact_id)
    if contact is None:
        return False, "no_contact"

    text = (text or "").strip()
    if not text and not media_path:
        return False, "empty"

    kind = media_kind(media_path)

    channel = contact["channel"]
    if channel == "tg":
        from . import telegram
        # отвечаем тем же ботом, которому человек написал
        ok, status = await telegram.send(
            contact["external_id"], text, media_path, button,
            bot_id=contact["bot_id"], kind=kind,
        )
    elif channel == "web":
        from . import web as webchat
        # для сайта отправка = запись в базу, виджет заберёт её опросом
        ok, status = await webchat.send(contact_id, text, media_path, button)
    elif channel == "max":
        from . import maxru
        row = db.bot(contact["bot_id"]) if contact["bot_id"] else None
        ok, status = await maxru.send(
            contact["external_id"], text, media_path, button,
            token=row["token"] if row else None, kind=kind,
        )
    elif channel == "wa":
        from . import whatsapp
        ok, status = await whatsapp.send(
            contact["external_id"], text, media_path, button, kind=kind
        )
    else:
        return False, "no_channel"

    if status == "blocked":
        db.run("UPDATE contacts SET blocked = 1, opted_in = 0 WHERE id = ?", (contact_id,))

    if ok:
        db.add_message(contact_id, "out", author, text, kind, media_path, is_read=True)
    return ok, status
