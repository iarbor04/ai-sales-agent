"""Рассылки подписчикам Telegram-бота.

Порядок из ТЗ жёсткий: создать → показать предпросмотр → получить
подтверждение → отправить сразу или в назначенное время → показать результат.
Пока рассылка в статусе draft, она не уйдёт никому.

Повторная попытка не создаёт дублей: каждая пара (рассылка, контакт) пишется
в broadcast_log с UNIQUE, и получатели выбираются как «те, кого там ещё нет».
"""
from __future__ import annotations

import asyncio
import logging

from . import config, db, notify
from .channels import base

log = logging.getLogger("broadcast")


def recipients(broadcast_id: int) -> list:
    """Кому ещё не отправляли.

    Только те, кто сам запустил бота (opted_in) и не заблокировал его —
    рассылать в WhatsApp нельзя: Cloud API запрещает писать первым вне окна
    в 24 часа.
    """
    return db.q(
        "SELECT c.* FROM contacts c"
        " WHERE c.channel = 'tg' AND c.opted_in = 1 AND c.blocked = 0"
        "   AND NOT EXISTS (SELECT 1 FROM broadcast_log b"
        "                   WHERE b.broadcast_id = ? AND b.contact_id = c.id)",
        (broadcast_id,),
    )


def audience_size() -> int:
    row = db.q1(
        "SELECT COUNT(*) AS c FROM contacts"
        " WHERE channel = 'tg' AND opted_in = 1 AND blocked = 0"
    )
    return row["c"] if row else 0


def create(text: str, image_path: str | None, button_text: str, button_url: str,
           send_at: int | None) -> int:
    """Создать черновик. Ничего не отправляет — ждёт подтверждения."""
    return db.run(
        "INSERT INTO broadcasts (text, image_path, button_text, button_url, send_at,"
        " status, created_at) VALUES (?, ?, ?, ?, ?, 'draft', ?)",
        (text, image_path, button_text or None, button_url or None, send_at, db.now()),
    )


def confirm(broadcast_id: int) -> None:
    """Подтвердить рассылку. Дальше её заберёт планировщик или send_now."""
    db.run(
        "UPDATE broadcasts SET status = 'confirmed' WHERE id = ? AND status = 'draft'",
        (broadcast_id,),
    )


def cancel(broadcast_id: int) -> None:
    db.run(
        "UPDATE broadcasts SET status = 'cancelled' WHERE id = ? AND status IN ('draft','confirmed')",
        (broadcast_id,),
    )


async def send_broadcast(broadcast_id: int) -> dict:
    """Разослать подтверждённую рассылку. Безопасно вызывать повторно."""
    row = db.q1("SELECT * FROM broadcasts WHERE id = ?", (broadcast_id,))
    if row is None:
        return {"sent": 0, "failed": 0}
    if row["status"] not in ("confirmed", "sending"):
        log.info("рассылка %s в статусе %s — пропускаем", broadcast_id, row["status"])
        return {"sent": row["sent_count"], "failed": row["failed_count"]}

    db.run("UPDATE broadcasts SET status = 'sending' WHERE id = ?", (broadcast_id,))

    button = (row["button_text"] or "", row["button_url"] or "")
    sent = failed = 0

    for contact in recipients(broadcast_id):
        ok, status = await base.send(
            contact["id"], row["text"] or "", row["image_path"],
            button if button[0] and button[1] else None,
            author="broadcast",
        )
        # запись в лог идёт всегда — она и есть защита от повторной отправки
        db.run(
            "INSERT OR IGNORE INTO broadcast_log (broadcast_id, contact_id, status, sent_at)"
            " VALUES (?, ?, ?, ?)",
            (broadcast_id, contact["id"], status, db.now()),
        )
        if ok:
            sent += 1
        else:
            failed += 1
        await asyncio.sleep(config.SEND_PAUSE_MS / 1000)

    db.run(
        "UPDATE broadcasts SET status = 'done', sent_count = sent_count + ?,"
        " failed_count = failed_count + ? WHERE id = ?",
        (sent, failed, broadcast_id),
    )

    total = db.q1("SELECT sent_count, failed_count FROM broadcasts WHERE id = ?", (broadcast_id,))
    asyncio.create_task(
        notify.broadcast_done(broadcast_id, total["sent_count"], total["failed_count"])
    )
    log.info("рассылка %s: доставлено %s, ошибок %s", broadcast_id, sent, failed)
    return {"sent": sent, "failed": failed}


async def due() -> None:
    """Отправить подтверждённые рассылки, у которых подошло время."""
    rows = db.q(
        "SELECT id FROM broadcasts WHERE status = 'confirmed'"
        " AND (send_at IS NULL OR send_at <= ?)",
        (db.now(),),
    )
    for row in rows:
        await send_broadcast(row["id"])
