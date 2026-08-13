"""Онлайн-запись: услуги, сотрудники, свободные слоты.

Как это работает в диалоге: когда запись включена, агент получает в контекст
список ближайших свободных слотов. Клиент выбирает — модель возвращает время
и услугу, мы проверяем, что слот всё ещё свободен, и создаём запись.

Проверка на стороне сервера обязательна: между тем, как модель предложила
время, и тем, как клиент согласился, слот мог занять кто-то другой.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from . import db

log = logging.getLogger("booking")

WEEKDAYS = ["Понедельник", "Вторник", "Среда", "Четверг",
            "Пятница", "Суббота", "Воскресенье"]


def enabled() -> bool:
    return db.setting("booking_enabled", "0") == "1" and bool(services())


# ── справочники ────────────────────────────────────────────────────────

def services(only_enabled: bool = True) -> list:
    sql = "SELECT * FROM services"
    if only_enabled:
        sql += " WHERE enabled = 1"
    return db.q(sql + " ORDER BY id")


def staff(only_enabled: bool = True) -> list:
    sql = "SELECT * FROM staff"
    if only_enabled:
        sql += " WHERE enabled = 1"
    return db.q(sql + " ORDER BY id")


def hours() -> dict:
    """Часы работы по дням недели: {0: ('10:00', '19:00'), ...}."""
    result = {}
    for row in db.q("SELECT * FROM work_hours"):
        if row["open_at"] and row["close_at"]:
            result[row["weekday"]] = (row["open_at"], row["close_at"])
    return result


# ── слоты ──────────────────────────────────────────────────────────────

def _minutes(value: str) -> int:
    hour, _, minute = value.partition(":")
    return int(hour) * 60 + int(minute or 0)


def free_slots(service_id: int | None = None, staff_id: int | None = None,
               days: int = 7, limit: int = 12) -> list[dict]:
    """Ближайшие свободные окна.

    Шаг сетки равен длительности услуги: для стрижки на 40 минут окна идут
    через 40 минут, а не через час — иначе половина дня простаивает.
    """
    service = db.q1("SELECT * FROM services WHERE id = ?", (service_id,)) if service_id else None
    if service is None:
        service = db.q1("SELECT * FROM services WHERE enabled = 1 ORDER BY id LIMIT 1")
    if service is None:
        return []

    duration = max(int(service["duration_min"] or 60), 15)
    schedule = hours()
    people = staff()
    if staff_id:
        people = [p for p in people if p["id"] == staff_id] or people

    taken = {
        (row["staff_id"], row["starts_at"])
        for row in db.q(
            "SELECT staff_id, starts_at FROM bookings WHERE status != 'cancelled'"
            " AND starts_at > ?", (db.now(),)
        )
    }

    slots: list[dict] = []
    now = datetime.now()
    for day in range(days):
        date = (now + timedelta(days=day)).date()
        window = schedule.get(date.weekday())
        if not window:
            continue

        start_min, end_min = _minutes(window[0]), _minutes(window[1])
        cursor = start_min
        while cursor + duration <= end_min:
            moment = datetime.combine(date, datetime.min.time()) + timedelta(minutes=cursor)
            cursor += duration
            # прошедшее и ближайший час не предлагаем: нужен запас на дорогу
            if moment <= now + timedelta(hours=1):
                continue

            stamp = int(moment.timestamp())
            for person in (people or [None]):
                key = (person["id"] if person else None, stamp)
                if key in taken:
                    continue
                slots.append({
                    "at": stamp,
                    "label": moment.strftime("%d.%m %H:%M"),
                    "weekday": WEEKDAYS[date.weekday()],
                    "staff_id": person["id"] if person else None,
                    "staff": person["name"] if person else "",
                    "service_id": service["id"],
                    "service": service["title"],
                })
                break
            if len(slots) >= limit:
                return slots
    return slots


def slots_for_prompt() -> str:
    """Блок для модели: что можно предложить клиенту."""
    if not enabled():
        return ""

    lines = ["\n\nЗАПИСЬ НА УСЛУГИ. Доступные услуги:"]
    for row in services():
        price = f", {row['price']}" if row["price"] else ""
        lines.append(f"- {row['title']} ({row['duration_min']} мин{price})")

    slots = free_slots(limit=10)
    if not slots:
        lines.append("Свободных окон в ближайшие дни нет — предложи связаться с менеджером.")
        return "\n".join(lines)

    lines.append("\nБлижайшие свободные окна:")
    for slot in slots:
        who = f", {slot['staff']}" if slot["staff"] else ""
        lines.append(f"- {slot['weekday']} {slot['label']}{who}")

    lines.append(
        "\nЕсли клиент выбрал время — верни его в поле booking в формате "
        "{\"at\": \"ДД.ММ ЧЧ:ММ\", \"service\": \"название услуги\"}. "
        "Предлагай только окна из списка выше, другие времена не выдумывай."
    )
    return "\n".join(lines)


# ── запись ─────────────────────────────────────────────────────────────

def book(contact_id: int, at_label: str, service_name: str = "") -> dict:
    """Создать запись по выбранному клиентом времени.

    Слот проверяем заново: пока шёл разговор, его могли занять.
    """
    matched = None
    for slot in free_slots(limit=60):
        if slot["label"] == at_label.strip():
            if not service_name or service_name.lower() in slot["service"].lower():
                matched = slot
                break
            matched = matched or slot

    if matched is None:
        return {"ok": False, "reason": "слот занят или не найден"}

    db.run(
        "INSERT INTO bookings (contact_id, service_id, staff_id, starts_at,"
        " status, created_at) VALUES (?, ?, ?, ?, 'new', ?)",
        (contact_id, matched["service_id"], matched["staff_id"],
         matched["at"], db.now()),
    )
    log.info("запись создана: контакт %s на %s", contact_id, matched["label"])
    return {"ok": True, "slot": matched}


def upcoming(limit: int = 100) -> list:
    return db.q(
        "SELECT b.*, c.name, c.username, c.phone, c.channel, c.id AS cid,"
        " s.title AS service, st.name AS staff_name"
        " FROM bookings b"
        " JOIN contacts c ON c.id = b.contact_id"
        " LEFT JOIN services s ON s.id = b.service_id"
        " LEFT JOIN staff st ON st.id = b.staff_id"
        " WHERE b.status != 'cancelled'"
        " ORDER BY b.starts_at LIMIT ?",
        (limit,),
    )
