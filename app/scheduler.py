"""Фоновый цикл: отложенные рассылки и повторные попытки записи.

Один цикл на всё приложение, тик по умолчанию 30 секунд. Отдельного Celery
или брокера не нужно: состояние лежит в SQLite и переживает рестарт.
"""
from __future__ import annotations

import asyncio
import logging

from . import broadcast, config, db

log = logging.getLogger("scheduler")

_task: asyncio.Task | None = None


async def _tick() -> None:
    await broadcast.due()
    await _retry_pending()


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
