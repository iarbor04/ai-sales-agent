"""Ежечасная самопроверка боевой установки.

Владелец узнаёт о поломке от клиента, который не дождался ответа, — и это
худший способ. Раз в час служба проверяет то, что ломается в реальности:
ключ модели, живые каналы, наличие базы знаний, зависшие задания и рассылки.

Уведомление уходит только когда набор проблем изменился: ежечасное «у вас всё
ещё сломано» люди перестают читать на второй день.
"""
from __future__ import annotations

import json
import logging

from . import channels, config, db, llm, notify

log = logging.getLogger("healthcheck")

STATE_KEY = "health_state"
STUCK_JOB_MINUTES = 60


async def run() -> dict:
    """Собрать состояние. Ничего не чинит и никого не будит — только смотрит."""
    problems: list[str] = []

    live = set(channels.live_ids())
    configured = db.bots(only_enabled=True)
    dead = [row["title"] for row in configured if row["id"] not in live]
    if not configured:
        problems.append("не подключён ни один бот — клиентам некуда писать")
    elif dead:
        problems.append("боты не на связи: " + ", ".join(dead))

    if not llm.ai_ready():
        problems.append("не задан ключ модели — агент молчит и зовёт менеджера")
    else:
        key = await llm.check_key()
        if not key["ok"]:
            problems.append(f"модель недоступна: {key['detail']}")

    chunks = db.q1("SELECT COUNT(*) AS c FROM kb_chunks")["c"]
    if not chunks:
        problems.append("база знаний пуста — агент не сможет отвечать по делу")

    stuck = db.q1(
        "SELECT COUNT(*) AS c FROM autochain_jobs WHERE status = 'pending' AND due_at < ?",
        (db.now() - STUCK_JOB_MINUTES * 60,),
    )["c"]
    if stuck:
        problems.append(f"шаги автоцепочек не отправляются: {stuck} висит больше часа")

    hanging = db.q1("SELECT COUNT(*) AS c FROM broadcasts WHERE status = 'sending'")["c"]
    if hanging:
        problems.append(f"рассылка застряла в отправке: {hanging}")

    if not db.setting("operator_chat_id", "").strip():
        problems.append("не задан чат менеджера — уведомления никуда не уходят")

    state = {
        "checked_at": db.now(),
        "problems": problems,
        "channels": channels.active(),
        "mode": config.MODE,
    }
    db.set_setting(STATE_KEY, json.dumps(state, ensure_ascii=False))
    return state


def last() -> dict:
    """Последний результат — для показа в панели."""
    try:
        return json.loads(db.setting(STATE_KEY, "") or "{}")
    except ValueError:
        return {}


async def run_and_alert() -> dict:
    """Проверить и написать менеджеру, если набор проблем изменился."""
    before = set(last().get("problems") or [])
    state = await run()
    now = set(state["problems"])

    if now and now != before:
        await notify.health_alert(state["problems"])
    elif before and not now:
        await notify.health_recovered()
    if now:
        log.warning("самопроверка: %s", "; ".join(state["problems"]))
    return state
