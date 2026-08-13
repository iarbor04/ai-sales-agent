"""Слежка за сайтами конкурентов.

Работает так: раз в несколько часов забираем публичную страницу конкурента,
превращаем её в текст тем же разбором, что и базу знаний, и сравниваем с тем,
что было в прошлый раз. Изменился текст — просим модель сказать человеческим
языком, что именно поменялось, и стоит ли это внимания.

Почему не «следить за всем сайтом»: страницы вроде блога меняются постоянно и
дают шум. Владелец сам указывает конкретные адреса — прайс, тарифы, страницу
услуги, — и следим только за ними.

Берём только то, что и так открыто всем в интернете: robots.txt уважаем,
ходим редко, ничего не обходим и не логинимся.
"""
from __future__ import annotations

import asyncio
import difflib
import hashlib
import logging
import urllib.robotparser
import urllib.parse

from . import db, knowledge, llm, notify

log = logging.getLogger("rivals")


def _allowed(url: str) -> bool:
    """Не ходим туда, где сайт просит не ходить."""
    try:
        parts = urllib.parse.urlsplit(url)
        parser = urllib.robotparser.RobotFileParser()
        parser.set_url(f"{parts.scheme}://{parts.netloc}/robots.txt")
        parser.read()
        return parser.can_fetch(knowledge.UA, url)
    except Exception:  # noqa: BLE001 — нет robots.txt значит можно
        return True


def _text_of(url: str) -> str | None:
    """Страница в чистый текст. None — если недоступна."""
    html = knowledge._get(url)
    if html is None:
        return None
    parser = knowledge._TextParser()
    try:
        parser.feed(html)
    except Exception:  # noqa: BLE001 — кривая разметка не должна ронять обход
        pass
    return parser.text()


def _diff(old: str, new: str, limit: int = 40) -> str:
    """Только изменившиеся строки — чтобы не гонять в модель всю страницу."""
    lines = []
    matcher = difflib.unified_diff(
        old.splitlines(), new.splitlines(), lineterm="", n=0
    )
    for line in matcher:
        if line.startswith(("---", "+++", "@@")):
            continue
        text = line[1:].strip()
        if not text:
            continue
        lines.append(("убрали: " if line[0] == "-" else "добавили: ") + text[:200])
        if len(lines) >= limit:
            break
    return "\n".join(lines)


async def _explain(title: str, changes: str) -> dict:
    """Попросить модель объяснить изменения по-человечески."""
    fallback = {"summary": "Страница изменилась", "important": False}
    if not llm.ai_ready() or not changes:
        return fallback

    system = (
        "Ты следишь за сайтами конкурентов для отдела продаж. "
        "Тебе дают список изменений на странице. Скажи коротко, что поменялось "
        "по сути, одним-двумя предложениями, деловым языком. "
        "Отдельно реши, важно ли это: важно — если поменялись цены, тарифы, "
        "условия, появился новый продукт или акция. Не важно — если это "
        "правки текста, даты, счётчики, служебные мелочи. "
        'Верни СТРОГО JSON: {"summary": "...", "important": true или false}'
    )
    raw = await llm._call(system, f"Страница: {title}\n\nИзменения:\n{changes}", max_tokens=300)
    data = llm._parse(raw)
    if not data or not data.get("summary"):
        return fallback
    return {
        "summary": str(data["summary"]).strip(),
        "important": bool(data.get("important")),
    }


async def check(rival) -> dict:
    """Проверить одного конкурента. Возвращает, что нашли."""
    url = rival["url"]

    if not _allowed(url):
        db.run("UPDATE rivals SET last_error = ?, checked_at = ? WHERE id = ?",
               ("сайт закрыл доступ в robots.txt", db.now(), rival["id"]))
        return {"changed": False, "skipped": "robots.txt"}

    text = await asyncio.to_thread(_text_of, url)
    if text is None:
        db.run("UPDATE rivals SET last_error = ?, checked_at = ? WHERE id = ?",
               ("страница недоступна", db.now(), rival["id"]))
        return {"changed": False, "error": "недоступна"}

    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()

    # первый заход: просто запоминаем, менять пока нечего
    if not rival["last_hash"]:
        db.run(
            "UPDATE rivals SET last_hash = ?, last_text = ?, checked_at = ?,"
            " last_error = NULL WHERE id = ?",
            (digest, text, db.now(), rival["id"]),
        )
        return {"changed": False, "first": True}

    if digest == rival["last_hash"]:
        db.run("UPDATE rivals SET checked_at = ?, last_error = NULL WHERE id = ?",
               (db.now(), rival["id"]))
        return {"changed": False}

    details = _diff(rival["last_text"] or "", text)
    verdict = await _explain(rival["title"], details)

    db.run(
        "INSERT INTO rival_changes (rival_id, summary, details, important, found_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (rival["id"], verdict["summary"], details,
         1 if verdict["important"] else 0, db.now()),
    )
    db.run(
        "UPDATE rivals SET last_hash = ?, last_text = ?, checked_at = ?,"
        " last_error = NULL WHERE id = ?",
        (digest, text, db.now(), rival["id"]),
    )

    if verdict["important"] and db.setting("rivals_notify", "1") == "1":
        asyncio.create_task(
            notify.rival_changed(rival["title"], url, verdict["summary"])
        )

    log.info("у конкурента «%s» изменения: %s", rival["title"], verdict["summary"])
    return {"changed": True, "summary": verdict["summary"],
            "important": verdict["important"]}


async def check_all() -> dict:
    """Обойти всех включённых конкурентов."""
    found = 0
    for rival in db.rivals(only_enabled=True):
        result = await check(rival)
        if result.get("changed"):
            found += 1
        await asyncio.sleep(1)  # вежливость к чужим серверам
    db.set_setting("rivals_last_run", str(db.now()))
    return {"checked": len(db.rivals(only_enabled=True)), "changed": found}


def due() -> bool:
    """Пора ли обходить — по расписанию из настроек."""
    if not db.rivals(only_enabled=True):
        return False
    try:
        hours = int(db.setting("rivals_every_hours", "12") or 12)
    except ValueError:
        hours = 12
    last = db.setting("rivals_last_run", "")
    if not last:
        return True
    try:
        return db.now() - int(last) >= hours * 3600
    except ValueError:
        return True
