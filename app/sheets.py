"""Google Таблицы: база знаний на чтение и CRM на запись.

Два направления работают независимо и по-разному, потому что и требования у
них разные:

  База знаний ← таблица.  Владелец жмёт «Опубликовать в интернете», отдаёт
    ссылку на CSV. Ключи не нужны вообще. Минус: опубликованную таблицу видит
    любой по ссылке — для прайса это нормально, для персональных данных нет.

  Лиды → таблица.  Тут нужна запись, а значит сервисный аккаунт Google:
    JSON-ключ в GOOGLE_SA_FILE и таблица, расшаренная на его почту.

Оба направления опциональны. Не настроено — модуль молчит, остальное работает.
"""
from __future__ import annotations

import csv
import io
import logging
import re
import urllib.error
import urllib.parse
import urllib.request

import httpx

from . import config, db

log = logging.getLogger("sheets")

SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"
SCOPE = "https://www.googleapis.com/auth/spreadsheets"

# Колонки, которые мы держим в CRM-таблице. Порядок — это и есть порядок столбцов.
CRM_COLUMNS = [
    ("id", "ID"),
    ("created", "Создан"),
    ("updated", "Обновлён"),
    ("channel", "Канал"),
    ("name", "Имя"),
    ("contact", "Контакт"),
    ("product", "Продукт"),
    ("need", "Потребность"),
    ("deadline", "Срок"),
    ("comment", "Комментарий"),
    ("summary", "Резюме"),
    ("status", "Статус"),
    ("manager", "Менеджер"),
    ("dialog", "Диалог"),
]


# ── база знаний из таблицы ─────────────────────────────────────────────

def csv_url(link: str) -> str:
    """Привести любую ссылку на таблицу к виду, отдающему CSV.

    Понимает три формы: уже готовую ссылку /pub?output=csv, обычную ссылку
    /edit и голый идентификатор таблицы.
    """
    link = (link or "").strip()
    if not link:
        return ""
    if "output=csv" in link:
        return link
    if "/pub" in link:
        joiner = "&" if "?" in link else "?"
        return f"{link}{joiner}output=csv"

    match = re.search(r"/spreadsheets/d/(?:e/)?([A-Za-z0-9_-]+)", link)
    key = match.group(1) if match else link
    gid = ""
    gid_match = re.search(r"[#&?]gid=(\d+)", link)
    if gid_match:
        gid = f"&gid={gid_match.group(1)}"
    return f"https://docs.google.com/spreadsheets/d/{key}/export?format=csv{gid}"


def _fetch_csv(url: str) -> list[dict]:
    request = urllib.request.Request(url, headers={"User-Agent": "AiSalesBot/1.0"})
    with urllib.request.urlopen(request, timeout=config.CRAWL_TIMEOUT) as resp:
        raw = resp.read(5_000_000).decode("utf-8", errors="replace")
    return list(csv.DictReader(io.StringIO(raw)))


def sync_knowledge() -> dict:
    """Забрать таблицу и положить её в базу знаний одной страницей.

    Каждая строка превращается в блок «Заголовок: значение». Это одинаково
    хорошо работает и для таблицы «вопрос / ответ», и для прайса с десятком
    столбцов — разбирать структуру заранее не нужно.
    """
    link = db.setting("sheets_kb_url", "").strip()
    if not link:
        return {"rows": 0, "skipped": "не настроено"}

    try:
        rows = _fetch_csv(csv_url(link))
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as exc:
        log.warning("таблица базы знаний недоступна: %s", exc)
        return {"rows": 0, "error": str(exc)}

    blocks = []
    for row in rows:
        parts = [
            f"{(header or '').strip()}: {str(value).strip()}"
            for header, value in row.items()
            if header and str(value or "").strip()
        ]
        if parts:
            blocks.append("\n".join(parts))

    text = "\n\n".join(blocks)
    from . import knowledge, retrieval

    existing = db.q1("SELECT id FROM kb_pages WHERE url = 'sheet://knowledge'")
    if existing:
        page_id = existing["id"]
        db.run(
            "UPDATE kb_pages SET text = ?, chars = ?, status = 'loaded', fetched_at = ?"
            " WHERE id = ?",
            (text, len(text), db.now(), page_id),
        )
    else:
        page_id = db.run(
            "INSERT INTO kb_pages (url, title, text, chars, included, status, fetched_at)"
            " VALUES ('sheet://knowledge', 'Google Таблица', ?, ?, 1, 'loaded', ?)",
            (text, len(text), db.now()),
        )

    knowledge._rechunk(page_id, text)
    retrieval.invalidate()
    log.info("база знаний из таблицы: %s строк, %s символов", len(blocks), len(text))
    return {"rows": len(blocks), "chars": len(text)}


# ── лиды в таблицу ─────────────────────────────────────────────────────

def crm_ready() -> bool:
    return bool(config.GOOGLE_SA_FILE and db.setting("sheets_crm_id", "").strip())


def _token() -> str | None:
    """Токен доступа по сервисному аккаунту."""
    try:
        from google.auth.transport.requests import Request
        from google.oauth2 import service_account
    except ImportError:
        log.warning("нет пакета google-auth — запись в таблицу выключена")
        return None

    try:
        creds = service_account.Credentials.from_service_account_file(
            config.GOOGLE_SA_FILE, scopes=[SCOPE]
        )
        creds.refresh(Request())
        return creds.token
    except Exception as exc:  # noqa: BLE001
        log.warning("сервисный аккаунт не пускает: %s", exc)
        return None


def _lead_row(lead) -> list[str]:
    contact = db.contact_by_id(lead["contact_id"])
    who = ""
    if contact:
        who = contact["username"] and f"@{contact['username']}" or contact["phone"] or ""
    return [
        str(lead["id"]),
        db.q1("SELECT datetime(?, 'unixepoch') AS d", (lead["created_at"],))["d"],
        db.q1("SELECT datetime(?, 'unixepoch') AS d", (lead["updated_at"],))["d"],
        config.CHANNEL_TITLES.get(contact["channel"], "") if contact else "",
        lead["name"] or "",
        lead["contact"] or who,
        lead["product"] or "",
        lead["need"] or "",
        lead["deadline"] or "",
        lead["comment"] or "",
        lead["summary"] or "",
        db.LEAD_STATUSES.get(lead["status"], lead["status"]),
        lead["manager"] or "",
        f"{config.PUBLIC_URL}/dialogs?c={lead['contact_id']}",
    ]


async def _api(method: str, path: str, token: str, **kwargs) -> dict | None:
    url = f"{SHEETS_API}/{db.setting('sheets_crm_id').strip()}{path}"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.request(
                method, url,
                headers={"Authorization": f"Bearer {token}"},
                **kwargs,
            )
            if resp.status_code >= 400:
                log.warning("таблица отказала (%s): %s", resp.status_code, resp.text[:200])
                return None
            return resp.json()
    except Exception as exc:  # noqa: BLE001
        log.warning("таблица недоступна: %s", exc)
        return None


async def sync_leads() -> dict:
    """Выгрузить в таблицу лиды, изменившиеся с прошлой синхронизации.

    Строка ищется по ID в первом столбце: найденная обновляется на месте,
    новая дописывается в конец. Поэтому дублей в таблице не появляется,
    даже если синхронизация запустится дважды.
    """
    if not crm_ready():
        return {"synced": 0, "skipped": "не настроено"}

    pending = db.q(
        "SELECT * FROM leads WHERE synced_at IS NULL OR synced_at < updated_at"
        " ORDER BY updated_at LIMIT 50"
    )
    if not pending:
        return {"synced": 0}

    token = _token()
    if token is None:
        return {"synced": 0, "error": "нет доступа"}

    tab = db.setting("sheets_crm_tab", "Лиды").strip() or "Лиды"
    quoted = urllib.parse.quote(f"{tab}!A:A")

    # что уже в таблице: ID → номер строки
    existing = await _api("GET", f"/values/{quoted}", token)
    if existing is None:
        return {"synced": 0, "error": "не читается"}

    rows = existing.get("values", [])
    if not rows:
        # первая запись — сначала шапка
        await _api(
            "PUT", f"/values/{urllib.parse.quote(f'{tab}!A1')}?valueInputOption=RAW",
            token, json={"values": [[title for _, title in CRM_COLUMNS]]},
        )
        rows = [[title for _, title in CRM_COLUMNS]]

    index = {
        str(row[0]).strip(): number
        for number, row in enumerate(rows, start=1)
        if row and str(row[0]).strip()
    }

    synced = 0
    for lead in pending:
        values = [_lead_row(lead)]
        line = index.get(str(lead["id"]))

        if line:
            target = f"{tab}!A{line}"
            result = await _api(
                "PUT", f"/values/{urllib.parse.quote(target)}?valueInputOption=RAW",
                token, json={"values": values},
            )
        else:
            target = f"{tab}!A1"
            result = await _api(
                "POST",
                f"/values/{urllib.parse.quote(target)}:append"
                "?valueInputOption=RAW&insertDataOption=INSERT_ROWS",
                token, json={"values": values},
            )

        if result is None:
            # не записалось — synced_at не трогаем, попробуем в следующий тик
            continue

        db.run("UPDATE leads SET synced_at = ? WHERE id = ?", (db.now(), lead["id"]))
        synced += 1

    if synced:
        log.info("в таблицу выгружено лидов: %s", synced)
    return {"synced": synced}


async def check() -> dict:
    """Проверка настроек для страницы интеграций."""
    result = {"kb": "не настроено", "crm": "не настроено"}

    link = db.setting("sheets_kb_url", "").strip()
    if link:
        try:
            rows = _fetch_csv(csv_url(link))
            result["kb"] = f"ок, строк: {len(rows)}"
        except Exception as exc:  # noqa: BLE001
            result["kb"] = f"ошибка: {exc}"

    if db.setting("sheets_crm_id", "").strip():
        if not config.GOOGLE_SA_FILE:
            result["crm"] = "нет файла сервисного аккаунта в GOOGLE_SA_FILE"
        elif _token() is None:
            result["crm"] = "сервисный аккаунт не пускает — расшарьте таблицу на его почту"
        else:
            tab = db.setting("sheets_crm_tab", "Лиды").strip() or "Лиды"
            token = _token()
            probe = await _api("GET", f"/values/{urllib.parse.quote(tab + '!A1:A1')}", token)
            result["crm"] = "ок" if probe is not None else f"лист «{tab}» не найден"

    return result
