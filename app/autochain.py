"""Автоцепочки: догоняющие сообщения для тех, кто написал и замолчал.

Клиенту отвечает ИИ, поэтому цепочка здесь не «капельная рассылка всем», а
страховка от тишины: шаг уходит только если после постановки в очередь клиент не
написал ни одного сообщения. Ответил — остаток отменяется, дальше разговор ведёт
агент. Передали менеджеру — тоже отменяется: дописывать поверх живого человека
нельзя.

Задания обрабатывает общий планировщик, отдельного таймера нет.
"""
from __future__ import annotations

import json
import logging

from . import broadcast, db
from .channels import base

log = logging.getLogger("autochain")

STALE_CLAIM_SECONDS = 10 * 60
MAX_ATTEMPTS = 3
BATCH = 20


def chains(only_enabled: bool = False) -> list:
    sql = "SELECT * FROM autochains"
    if only_enabled:
        sql += " WHERE enabled = 1"
    return db.q(sql + " ORDER BY id")


def steps(chain_id: int) -> list:
    return db.q("SELECT * FROM autochain_steps WHERE chain_id = ? ORDER BY position, id",
                (chain_id,))


def save_chain(name: str, items: list[dict], chain_id: int | None = None) -> int:
    """Создать или заменить цепочку целиком.

    Шаги пересоздаются, поэтому ожидающие задания снимаются: отправлять шаг,
    которого больше нет в цепочке, нельзя.
    """
    name = (name or "").strip()
    if not name:
        raise ValueError("назовите цепочку")
    prepared = []
    for position, item in enumerate(items):
        texts = {code: str(value or "").strip()
                 for code, value in (item.get("texts") or {}).items()}
        texts = {code: value for code, value in texts.items() if value}
        if not texts:
            raise ValueError(f"шаг {position + 1}: напишите текст хотя бы на русском")
        delay = int(item.get("delay_min") or 0)
        if delay < 0:
            raise ValueError(f"шаг {position + 1}: задержка не может быть отрицательной")
        buttons = [b for b in (item.get("buttons") or [])
                   if str(b.get("text") or "").strip() and str(b.get("url") or "").strip()]
        prepared.append({"position": position, "delay_min": delay, "texts": texts,
                         "buttons": buttons[:3],
                         "enabled": 1 if item.get("enabled", True) else 0})
    if not prepared:
        raise ValueError("в цепочке должен быть хотя бы один шаг")

    if chain_id:
        db.run("UPDATE autochains SET name = ? WHERE id = ?", (name, chain_id))
        db.run("UPDATE autochain_jobs SET status = 'cancelled'"
               " WHERE chain_id = ? AND status = 'pending'", (chain_id,))
        db.run("DELETE FROM autochain_steps WHERE chain_id = ?", (chain_id,))
    else:
        chain_id = db.run("INSERT INTO autochains (name, enabled, created_at) VALUES (?, 1, ?)",
                          (name, db.now()))

    for step in prepared:
        db.run(
            "INSERT INTO autochain_steps (chain_id, position, delay_min, texts, buttons, enabled)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (chain_id, step["position"], step["delay_min"],
             json.dumps(step["texts"], ensure_ascii=False),
             json.dumps(step["buttons"], ensure_ascii=False), step["enabled"]),
        )
    return chain_id


def set_enabled(chain_id: int, enabled: bool) -> None:
    db.run("UPDATE autochains SET enabled = ? WHERE id = ?", (1 if enabled else 0, chain_id))
    if not enabled:
        db.run("UPDATE autochain_jobs SET status = 'cancelled'"
               " WHERE chain_id = ? AND status = 'pending'", (chain_id,))


def delete_chain(chain_id: int) -> None:
    db.run("DELETE FROM autochain_jobs WHERE chain_id = ?", (chain_id,))
    db.run("DELETE FROM autochain_steps WHERE chain_id = ?", (chain_id,))
    db.run("DELETE FROM autochains WHERE id = ?", (chain_id,))


def enroll(contact_id: int) -> int:
    """Поставить клиента в очередь по всем включённым цепочкам.

    Зовётся на первом сообщении клиента. Повторно не ставит: уникальность по
    паре (шаг, контакт) не даст задвоить, даже если позовут дважды.
    """
    created = 0
    now = db.now()
    last_in = db.q1("SELECT COALESCE(MAX(id), 0) AS id FROM messages"
                    " WHERE contact_id = ? AND direction = 'in'", (contact_id,))["id"]
    for chain in chains(only_enabled=True):
        delay = 0
        for step in steps(chain["id"]):
            if not step["enabled"]:
                continue
            delay += int(step["delay_min"] or 0)
            existing = db.q1("SELECT id FROM autochain_jobs WHERE step_id = ? AND contact_id = ?",
                             (step["id"], contact_id))
            if existing:
                continue
            db.run(
                "INSERT INTO autochain_jobs (chain_id, step_id, contact_id, enrolled_at,"
                " enrolled_msg_id, due_at, status) VALUES (?, ?, ?, ?, ?, ?, 'pending')",
                (chain["id"], step["id"], contact_id, now, last_in, now + delay * 60),
            )
            created += 1
    return created


def cancel_for(contact_id: int, reason: str) -> int:
    """Снять ожидающие шаги — клиент ответил или диалог ушёл менеджеру."""
    pending = db.q1("SELECT COUNT(*) AS c FROM autochain_jobs"
                    " WHERE contact_id = ? AND status = 'pending'", (contact_id,))["c"]
    if pending:
        db.run("UPDATE autochain_jobs SET status = 'cancelled', error = ?"
               " WHERE contact_id = ? AND status = 'pending'", (reason, contact_id))
    return pending


def _client_spoke_since(contact_id: int, message_id: int) -> bool:
    """Было ли входящее после постановки в очередь. Сравнение по номеру сообщения:
    время в секундах слишком грубое — постановка и ответ попадают в одну секунду."""
    row = db.q1("SELECT 1 FROM messages WHERE contact_id = ? AND direction = 'in'"
                " AND id > ? LIMIT 1", (contact_id, message_id))
    return row is not None


def _recover_stale() -> None:
    """Задание, зависшее в processing из-за перезапуска, вернуть в очередь."""
    edge = db.now() - STALE_CLAIM_SECONDS
    db.run("UPDATE autochain_jobs SET status = 'pending' WHERE status = 'processing'"
           " AND COALESCE(claimed_at, 0) < ? AND attempts < ?", (edge, MAX_ATTEMPTS))
    db.run("UPDATE autochain_jobs SET status = 'failed', error = 'прервано перезапуском'"
           " WHERE status = 'processing' AND COALESCE(claimed_at, 0) < ? AND attempts >= ?",
           (edge, MAX_ATTEMPTS))


async def process_due() -> dict:
    """Отправить подошедшие шаги. Зовётся планировщиком."""
    _recover_stale()
    now = db.now()
    jobs = db.q("SELECT * FROM autochain_jobs WHERE status = 'pending' AND due_at <= ?"
                " ORDER BY due_at LIMIT ?", (now, BATCH))
    sent = skipped = failed = 0

    for job in jobs:
        db.run("UPDATE autochain_jobs SET status = 'processing', claimed_at = ?,"
               " attempts = attempts + 1 WHERE id = ?", (db.now(), job["id"]))

        step = db.q1("SELECT * FROM autochain_steps WHERE id = ?", (job["step_id"],))
        chain = db.q1("SELECT * FROM autochains WHERE id = ?", (job["chain_id"],))
        contact = db.contact_by_id(job["contact_id"])
        if step is None or chain is None or contact is None or not chain["enabled"]:
            _finish(job["id"], "cancelled", "цепочка или шаг недоступны")
            skipped += 1
            continue

        # главное правило: догоняем только молчунов
        if _client_spoke_since(job["contact_id"], job["enrolled_msg_id"]):
            _finish(job["id"], "cancelled", "клиент ответил сам")
            skipped += 1
            continue
        if contact["blocked"] or not contact["opted_in"]:
            _finish(job["id"], "cancelled", "клиент отписался")
            skipped += 1
            continue

        text = broadcast.personalize(_text_for(step, contact["language"]), contact)
        if not text:
            _finish(job["id"], "cancelled", "нет текста для языка клиента")
            skipped += 1
            continue

        ok, status = await base.send(contact["id"], text, step["image_path"],
                                    author="autochain", buttons=_buttons(step))
        if ok:
            _finish(job["id"], "sent", None)
            sent += 1
        else:
            _finish(job["id"], "failed", f"канал ответил: {status}")
            failed += 1

    if sent or failed:
        log.info("автоцепочки: отправлено %s, отменено %s, ошибок %s", sent, skipped, failed)
    return {"sent": sent, "skipped": skipped, "failed": failed}


def _finish(job_id: int, status: str, error: str | None) -> None:
    db.run("UPDATE autochain_jobs SET status = ?, error = ? WHERE id = ?",
           (status, error, job_id))


def _text_for(step, language: str | None) -> str:
    try:
        texts = json.loads(step["texts"] or "{}")
    except (ValueError, TypeError):
        texts = {}
    code = db.normalize_language(language) or ""
    return (texts.get(code) or texts.get("ru") or "").strip()


def _buttons(step) -> list[tuple[str, str]]:
    try:
        items = json.loads(step["buttons"] or "[]")
    except (ValueError, TypeError):
        items = []
    return [(str(i.get("text") or ""), str(i.get("url") or "")) for i in items][:3]


def stats() -> dict:
    row = db.q1("SELECT COUNT(*) AS total,"
                " SUM(status = 'pending') AS pending,"
                " SUM(status = 'sent') AS sent,"
                " SUM(status = 'cancelled') AS cancelled"
                " FROM autochain_jobs")
    return {key: row[key] or 0 for key in ("total", "pending", "sent", "cancelled")}
