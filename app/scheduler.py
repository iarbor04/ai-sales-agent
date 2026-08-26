"""Фоновый цикл: отложенные рассылки и повторные попытки записи.

Один цикл на всё приложение, тик по умолчанию 30 секунд. Отдельного Celery
или брокера не нужно: состояние лежит в SQLite и переживает рестарт.
"""
from __future__ import annotations

import asyncio
import logging
import os

from . import (autochain, booking, broadcast, channels, config, db, healthcheck,
                knowledge, license, retrieval, rivals, sheets)

log = logging.getLogger("scheduler")

_task: asyncio.Task | None = None


_last_health = 0
HEALTH_EVERY = 3600


async def _health() -> None:
    """Самопроверка раз в час: о поломке владелец должен узнать не от клиента."""
    global _last_health
    if db.now() - _last_health < HEALTH_EVERY:
        return
    _last_health = db.now()
    await healthcheck.run_and_alert()


async def _subscription() -> None:
    """Раз в полсуток спрашиваем ASCN, оплачено ли, и гасим или поднимаем ботов.

    Подписку продлили — агент оживает сам, без перезапуска службы; кончилась —
    боты уходят, чтобы клиенты не писали в панель, которую владелец не откроет.
    """
    if not config.LICENSE_REQUIRED:
        return
    was = license.active()
    await license.refresh()
    now_active = license.active()
    if now_active == was:
        return
    if now_active:
        log.info("подписка активна — поднимаю ботов")
        await channels.start_all()
    else:
        log.warning("подписка закончилась — гашу ботов")
        await channels.stop_all()


async def _tick() -> None:
    await _subscription()
    # Без подписки фоновая работа не нужна: рассылки и автоцепочки уходят
    # клиентам от лица владельца, а он за это уже не платит.
    if not license.active():
        return
    await broadcast.due()
    await autochain.process_due()
    await _health()
    await _sync_sheets()
    await _watch_rivals()
    await _refresh_knowledge()
    await _remind_bookings()


async def _refresh_knowledge() -> None:
    """Перечитать сайт клиента: цены и условия там меняются без нас."""
    if not knowledge.refresh_due():
        return
    result = await asyncio.to_thread(knowledge.refresh)
    if result["changed"] or result["gone"]:
        retrieval.invalidate()
    if result["gone"]:
        from . import notify
        await notify.kb_pages_gone(result["gone"])


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
    """Выгрузка лидов в Google Таблицу — каждый тик: лидов мало, а нужны сразу."""
    if sheets.crm_ready():
        await sheets.sync_leads()


async def _loop() -> None:
    # Пометка процесса: живость ботов видна только внутри службы, и проверка
    # должна отличать себя от запуска из командной строки.
    db.set_setting("service_pid", str(os.getpid()))
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
    if _task is None or _task.done():
        _task = asyncio.create_task(_loop(), name="scheduler")


async def stop() -> None:
    global _task
    if _task:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
        _task = None
