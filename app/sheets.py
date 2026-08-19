"""Google Таблица для выгрузки лидов.

Направление одно — лиды пишутся в таблицу. Для записи нужен сервисный аккаунт
Google: JSON-ключ в GOOGLE_SA_FILE и таблица, расшаренная на его почту.

Прайс и другие таблицы знаний загружаются файлом (xlsx или csv) в разделе
«База знаний» — см. pricefile.py. Читать их по опубликованной ссылке мы
перестали: публикация открывает таблицу всему интернету, а закрытая таблица
молча отдавала страницу входа Google вместо данных.

Выгрузка опциональна. Не настроено — модуль молчит, остальное работает.
"""
from __future__ import annotations

import logging
import urllib.parse

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
        db.stage_titles().get(lead["status"], lead["status"]),
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
    result = {"crm": "не настроено"}

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
