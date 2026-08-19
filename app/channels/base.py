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


def contact_channel(contact_id: int) -> str:
    row = db.contact_by_id(contact_id)
    return row["channel"] if row else ""


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
               button: tuple[str, str] | None = None, author: str = "ai",
               buttons: list[tuple[str, str]] | None = None) -> tuple[bool, str]:
    """Отправить сообщение контакту и записать его в переписку.

    Возвращает (успех, статус). Статусы: sent | blocked | empty | error | no_channel.
    Пустой текст без вложения не отправляем никогда — клиент получил бы тишину.
    """
    contact = db.contact_by_id(contact_id)
    if contact is None:
        return False, "no_contact"

    text = (text or "").strip()

    # Несколько кнопок умеет только Telegram. В остальных каналах вторая и
    # третья уезжают ссылками в текст — молча терять их нельзя.
    pairs = [pair for pair in (buttons or ([button] if button else [])) if pair and pair[0] and pair[1]]
    if contact_channel(contact_id) != "tg" and len(pairs) > 1:
        extra = "\n".join(f"{name}: {url}" for name, url in pairs[1:])
        text = f"{text}\n\n{extra}".strip()
        pairs = pairs[:1]
    button = pairs[0] if pairs else None

    if not text and not media_path:
        return False, "empty"

    kind = media_kind(media_path)

    channel = contact["channel"]
    if channel == "tg":
        from . import telegram
        # отвечаем тем же ботом, которому человек написал
        ok, status = await telegram.send(
            contact["external_id"], text, media_path, button,
            bot_id=contact["bot_id"], kind=kind, buttons=pairs,
        )
    elif channel == "web":
        from . import web as webchat
        # для сайта отправка = запись в базу, виджет заберёт её опросом
        ok, status = await webchat.send(contact_id, text, media_path, button)
    elif channel == "avito":
        from . import avito
        row = db.bot(contact["bot_id"]) if contact["bot_id"] else None
        ok, status = await avito.send(
            contact["external_id"], text, media_path, button,
            bot_row=row, kind=kind,
        )
    elif channel == "mail":
        from . import mail
        row = db.bot(contact["bot_id"]) if contact["bot_id"] else None
        ok, status = await mail.send(
            contact["external_id"], text, media_path, button,
            bot_row=row, kind=kind,
        )
    elif channel == "vk":
        from . import vk
        row = db.bot(contact["bot_id"]) if contact["bot_id"] else None
        ok, status = await vk.send(
            contact["external_id"], text, media_path, button,
            token=row["token"] if row else None, kind=kind,
        )
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
