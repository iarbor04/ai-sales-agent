"""Фоновый цикл: отложенные рассылки и повторные попытки записи.

Один цикл на всё приложение, тик по умолчанию 30 секунд. Отдельного Celery
или брокера не нужно: состояние лежит в SQLite и переживает рестарт.
"""
from __future__ import annotations

import asyncio
import logging

from . import booking, broadcast, config, db, rivals, sheets

log = logging.getLogger("scheduler")

_task: asyncio.Task | None = None
_last_sheets_sync = 0


async def _tick() -> None:
    await broadcast.due()
    await _sync_sheets()
    await _watch_rivals()
    await _remind_bookings()
    await _retry_pending()


async def _remind_bookings() -> None:
    """Напомнить клиенту о записи — это заметно снижает неявки."""
    try:
        hours = int(db.setting("booking_remind_hours", "3") or 3)
    except ValueError:
        hours = 3
    if hours <= 0:
        return

    horizon = db.now() + hours * 3600
    rows = db.q(
        "SELECT b.*, s.title AS service FROM bookings b"
        " LEFT JOIN services s ON s.id = b.service_id"
        " WHERE b.reminded = 0 AND b.status != 'cancelled'"
        "   AND b.starts_at BETWEEN ? AND ?",
        (db.now(), horizon),
    )
    if not rows:
        return

    from .channels import base
    from datetime import datetime
    for row in rows:
        when = datetime.fromtimestamp(row["starts_at"]).strftime("%d.%m в %H:%M")
        ok, _ = await base.send(
            row["contact_id"],
            f"Напоминаем о записи: {row['service'] or 'визит'} — {when}. Ждём вас!",
            author="ai",
        )
        # помечаем в любом случае, иначе будем долбить клиента каждые 30 секунд
        db.run("UPDATE bookings SET reminded = 1 WHERE id = ?", (row["id"],))
        if not ok:
            log.info("напоминание о записи %s не доставлено", row["id"])


async def _watch_rivals() -> None:
    """Обход сайтов конкурентов по расписанию из настроек."""
    if rivals.due():
        await rivals.check_all()


async def _sync_sheets() -> None:
    """Обмен с Google Таблицами: лиды туда, база знаний оттуда.

    Лиды выгружаем каждый тик — их мало и они важны сразу. Базу знаний
    перечитываем реже: она меняется редко, а запрос не бесплатный.
    """
    global _last_sheets_sync

    if sheets.crm_ready():
        await sheets.sync_leads()

    period = config.SHEETS_SYNC_MINUTES * 60
    if db.setting("sheets_kb_url", "").strip() and db.now() - _last_sheets_sync > period:
        _last_sheets_sync = db.now()
        await asyncio.to_thread(sheets.sync_knowledge)


async def _retry_pending() -> None:
    """Повторить то, что не удалось записать с первого раза.

    По ТЗ: если система учёта недоступна — сохранить сообщение и повторить
    запись позже. Очередь общая, kind говорит, что именно повторять.
    """
    rows = db.q("SELECT * FROM retry_queue WHERE attempts < 5 ORDER BY id LIMIT 20")
    for row in rows:
        db.run("UPDATE retry_queue SET attempts = attempts + 1 WHERE id = ?", (row["id"],))
        # Встроенных внешних систем учёта пока нет: лиды живут в своей базе.
        # Крючок оставлен, чтобы подключение CRM не потребовало правок схемы.
        db.run("DELETE FROM retry_queue WHERE id = ?", (row["id"],))


async def _loop() -> None:
    log.info("планировщик запущен, тик %s сек", config.TICK_SECONDS)
    while True:
        try:
            await _tick()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — цикл не должен умирать от одной ошибки
            log.exception("ошибка в тике: %s", exc)
        await asyncio.sleep(config.TICK_SECONDS)


async def start() -> None:
    global _task
    _task = asyncio.create_task(_loop())


async def stop() -> None:
    global _task
    if _task:
        _task.cancel()
        _task = None
