"""Логика продавца: что делать с входящим сообщением.

Здесь собраны все правила из ТЗ, и намеренно в одном месте — чтобы человек
(или агент), которому передадут проект, читал поведение подряд, а не собирал
его по кускам из трёх модулей.
"""
from __future__ import annotations

import asyncio
import logging

from . import booking, config, db, llm, notify
from .channels import base

log = logging.getLogger("sales")

# Клиент попросил не писать. Гасим ИИ навсегда, до ручного включения.
STOP_WORDS = ("стоп", "не пишите", "не пиши", "отстаньте", "отписаться", "stop", "unsubscribe")

# Слова, при которых зовём человека, даже если модель этого не поняла.
HUMAN_WORDS = ("менеджер", "человек", "оператор", "живой", "жалоб", "верните деньги", "директор")


def _wants_stop(text: str) -> bool:
    low = (text or "").strip().lower()
    return any(low == word or low.startswith(word) for word in STOP_WORDS)


def _wants_human(text: str) -> bool:
    low = (text or "").lower()
    return any(word in low for word in HUMAN_WORDS)


def _pick_manager() -> str:
    """Ответственный по кругу — чтобы лиды не падали всегда на одного."""
    managers = [m.strip() for m in db.setting("managers", "").split(",") if m.strip()]
    if not managers:
        return ""
    row = db.q1("SELECT COUNT(*) AS c FROM leads WHERE status = ?", (db.system_stage(),))
    return managers[(row["c"] if row else 0) % len(managers)]


async def handle_incoming(contact_id: int, text: str, media_type: str | None = None,
                          media_path: str | None = None) -> None:
    """Единая точка входа для любого канала."""
    contact = db.contact_by_id(contact_id)
    if contact is None:
        return

    db.add_message(contact_id, "in", "client", text or None, media_type, media_path)

    # 1. Явный отказ от общения выключает ИИ и рассылки
    if _wants_stop(text):
        db.run("UPDATE contacts SET ai_enabled = 0, opted_in = 0 WHERE id = ?", (contact_id,))
        db.add_message(contact_id, "out", "system", "Клиент попросил не писать. ИИ выключен.",
                       is_read=True)
        return

    # 2. Диалог у менеджера — ИИ молчит, но менеджер получает уведомление
    if not contact["ai_enabled"] or db.setting("ai_enabled_global", "1") != "1":
        asyncio.create_task(notify.new_message(contact_id, text or f"[{media_type}]"))
        return

    # 3. Прямая просьба о человеке — не тратим вызов модели
    if _wants_human(text):
        await hand_off(contact_id, "клиент попросил человека")
        return

    # 4. Обычный путь: спрашиваем модель
    result = await llm.answer(contact_id, text or f"[{media_type or 'вложение'}]")

    if result["fields"]:
        _save_fields(contact_id, result["fields"], result.get("summary", ""))

    if result["handoff"]:
        await hand_off(contact_id, result["handoff_reason"] or "нет ответа в базе знаний",
                       summary=result.get("summary", ""))
        return

    ok, status = await base.send(contact_id, result["reply"], author="ai")
    if not ok:
        # Второй ответ при ошибке отправки не создаём — зовём человека.
        log.warning("ответ не доставлен (%s), передаём менеджеру", status)
        await hand_off(contact_id, f"сообщение не доставлено ({status})", silent=True)
        return

    # клиент выбрал время — фиксируем запись
    if result.get("booking"):
        await _make_booking(contact_id, result["booking"])

    # шаг сценария закрыт — следующее сообщение пойдёт по следующему шагу
    if result.get("step_done"):
        db.advance_step(contact_id)

    _bump_status(contact_id)


def _save_fields(contact_id: int, fields: dict, summary: str = "") -> None:
    """Дополнить карточку лида. Карточка заводится, когда собрано достаточно."""
    payload = {key: str(value).strip() for key, value in fields.items()
               if key in db.LEAD_FIELDS and str(value or "").strip()}
    if summary:
        payload["summary"] = summary
    if not payload:
        return

    lead = db.get_lead(contact_id)
    # По ТЗ карточка появляется после 2-4 вопросов, а не с первого «привет».
    if lead is None and len([k for k in payload if k != "summary"]) < config.LEAD_AFTER_FIELDS:
        return

    db.upsert_lead(contact_id, payload)

    # имя и контакт из карточки полезны и в самом контакте
    contact = db.contact_by_id(contact_id)
    if payload.get("name") and contact and not contact["name"]:
        db.run("UPDATE contacts SET name = ? WHERE id = ?", (payload["name"], contact_id))


async def _make_booking(contact_id: int, choice: dict) -> None:
    """Записать клиента на выбранное время.

    Слот перепроверяем: пока шёл разговор, его мог занять другой человек.
    """
    at = str(choice.get("at") or "").strip()
    if not at:
        return

    result = booking.book(contact_id, at, str(choice.get("service") or ""))
    if not result["ok"]:
        await base.send(
            contact_id,
            "Это время только что заняли. Давайте подберём другое —"
            " подскажите, когда вам удобно?",
            author="ai",
        )
        return

    slot = result["slot"]
    who = f", мастер {slot['staff']}" if slot["staff"] else ""
    db.add_message(contact_id, "out", "system",
                   f"Запись создана: {slot['service']} — {slot['label']}{who}",
                   is_read=True)
    asyncio.create_task(notify.booked(contact_id, slot))


def _bump_status(contact_id: int) -> None:
    """Новый лид → Квалификация, когда пошёл предметный разговор."""
    lead = db.get_lead(contact_id)
    if lead is None or lead["status"] != db.first_stage():
        return
    filled = sum(1 for key in ("product", "need", "deadline") if lead[key])
    if filled:
        db.set_lead_status(contact_id, "qualifying")


async def hand_off(contact_id: int, reason: str, summary: str = "",
                   silent: bool = False) -> None:
    """Передать диалог человеку: ИИ замолкает, менеджер получает уведомление."""
    manager = _pick_manager()

    if not summary:
        summary = await llm.summarize(contact_id)

    lead = db.get_lead(contact_id)
    if lead is None:
        db.upsert_lead(contact_id, {"summary": summary, "comment": reason},
                       status=db.system_stage())
    else:
        if summary:
            db.upsert_lead(contact_id, {"summary": summary})
        db.set_lead_status(contact_id, db.system_stage(), manager or None)

    db.set_ai(contact_id, False)
    db.run("UPDATE contacts SET manager = ? WHERE id = ?", (manager or None, contact_id))

    # Пустое поле «что агент пишет клиенту при передаче» означает тишину для
    # клиента: разговор для него просто обрывается. Это законный выбор
    # владельца, но оператор обязан видеть, что человек остался без ответа.
    note = db.setting("handoff_note", "").strip()
    silent_for_client = silent or not note
    tail = " — клиент ничего не получил" if silent_for_client else ""
    db.add_message(contact_id, "out", "system",
                   f"Передано менеджеру: {reason}{tail}", is_read=True)

    # обращение в лог: отсюда кнопки «взять в работу» и «передать»
    request_id = db.open_request(contact_id, reason)

    # silent — когда отправка клиенту уже не удалась, второй раз не пробуем
    if not silent and note:
        await base.send(contact_id, note, author="ai")

    asyncio.create_task(notify.handed_off(contact_id, reason, manager, request_id))


def return_to_ai(contact_id: int) -> None:
    """«Вернуть ИИ»: агент продолжает с сохранённой историей.

    Историю чистить не надо — она вся в messages, и модель получает её
    следующим же вызовом. Достаточно снова включить ИИ.
    """
    db.set_ai(contact_id, True)
    db.add_message(contact_id, "out", "system", "Диалог возвращён ИИ.", is_read=True)
    lead = db.get_lead(contact_id)
    if lead and lead["status"] == db.system_stage():
        db.set_lead_status(contact_id, "qualifying")

    # открытое обращение закрываем: человек больше не нужен
    row = db.q1(
        "SELECT id FROM requests WHERE contact_id = ? AND status != 'closed'"
        " ORDER BY id DESC LIMIT 1",
        (contact_id,),
    )
    if row:
        db.close_request(row["id"])
