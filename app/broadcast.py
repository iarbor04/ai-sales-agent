"""Рассылки подписчикам Telegram-бота.

Порядок из ТЗ жёсткий: создать → показать предпросмотр → получить
подтверждение → отправить сразу или в назначенное время → показать результат.
Пока рассылка в статусе draft, она не уйдёт никому.

Повторная попытка не создаёт дублей: каждая пара (рассылка, контакт) пишется
в broadcast_log с UNIQUE, и получатели выбираются как «те, кого там ещё нет».
"""
from __future__ import annotations

import asyncio
import json
import logging

from . import config, db, notify
from .channels import base

log = logging.getLogger("broadcast")


def _audience_sql(stage: str | None) -> tuple[str, list]:
    """Условие отбора получателей и его параметры.

    Рассылать можно только в Telegram: WhatsApp запрещает писать первым вне
    окна в 24 часа, а посетителю сайта писать некуда — он уже ушёл. Лиды на
    финальном этапе исключаются: догонять письмами того, кто уже купил, — худший
    способ испортить впечатление.
    """
    sql = " FROM contacts c WHERE c.channel = 'tg' AND c.opted_in = 1 AND c.blocked = 0"
    params: list = []
    won = db.won_stages()
    if won:
        placeholders = ",".join("?" for _ in won)
        sql += (f" AND NOT EXISTS (SELECT 1 FROM leads l WHERE l.contact_id = c.id"
                f" AND l.status IN ({placeholders}))")
        params += sorted(won)
    if stage:
        sql += " AND EXISTS (SELECT 1 FROM leads l WHERE l.contact_id = c.id AND l.status = ?)"
        params.append(stage)
    return sql, params


def recipients(broadcast_id: int) -> list:
    """Кому ещё не отправляли — с учётом фильтра по этапу воронки."""
    row = db.q1("SELECT stage_filter FROM broadcasts WHERE id = ?", (broadcast_id,))
    where, params = _audience_sql(row["stage_filter"] if row else None)
    return db.q(
        "SELECT c.*" + where
        + " AND NOT EXISTS (SELECT 1 FROM broadcast_log b"
          "                 WHERE b.broadcast_id = ? AND b.contact_id = c.id)",
        (*params, broadcast_id),
    )


def audience_size(stage: str | None = None) -> int:
    where, params = _audience_sql(stage)
    row = db.q1("SELECT COUNT(*) AS c" + where, tuple(params))
    return row["c"] if row else 0


def create(text: str, image_path: str | None, button_text: str, button_url: str,
           send_at: int | None, texts: dict[str, str] | None = None,
           buttons: list[dict] | None = None, stage_filter: str | None = None) -> int:
    """Создать черновик. Ничего не отправляет — ждёт подтверждения."""
    return db.run(
        "INSERT INTO broadcasts (text, texts, image_path, button_text, button_url,"
        " buttons, stage_filter, send_at, status, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?)",
        (text, json.dumps(texts or {}, ensure_ascii=False), image_path,
         button_text or None, button_url or None,
         json.dumps(buttons or [], ensure_ascii=False), stage_filter or None,
         send_at, db.now()),
    )


def text_for(row, language: str | None) -> str:
    """Текст на языке клиента. Нет перевода — уходит русский вариант."""
    try:
        texts = json.loads(row["texts"] or "{}")
    except (ValueError, TypeError):
        texts = {}
    code = db.normalize_language(language) or ""
    return (texts.get(code) or texts.get("ru") or row["text"] or "").strip()


def buttons_of(row) -> list[tuple[str, str]]:
    try:
        items = json.loads(row["buttons"] or "[]")
    except (ValueError, TypeError):
        items = []
    pairs = [(str(item.get("text") or "").strip(), str(item.get("url") or "").strip())
             for item in items if isinstance(item, dict)]
    pairs = [pair for pair in pairs if pair[0] and pair[1]]
    if pairs:
        return pairs[:3]
    legacy = (row["button_text"] or "", row["button_url"] or "")
    return [legacy] if legacy[0] and legacy[1] else []


def personalize(text: str, contact) -> str:
    """Подставить имя клиента. Текст уходит как HTML, поэтому имя экранируем."""
    name = (contact["name"] or contact["username"] or "").strip()
    first = name.split()[0] if name else ""
    safe = first.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return text.replace("{{first_name}}", safe)


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

    buttons = buttons_of(row)
    sent = failed = 0

    for contact in recipients(broadcast_id):
        text = personalize(text_for(row, contact["language"]), contact)
        if not text and not row["image_path"]:
            # для этого языка текста нет и картинки нет — отправлять нечего
            db.run(
                "INSERT OR IGNORE INTO broadcast_log (broadcast_id, contact_id, status, sent_at)"
                " VALUES (?, ?, 'no_text', ?)",
                (broadcast_id, contact["id"], db.now()),
            )
            failed += 1
            continue
        ok, status = await base.send(
            contact["id"], text, row["image_path"], author="broadcast", buttons=buttons,
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
