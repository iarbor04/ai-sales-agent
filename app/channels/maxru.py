"""MAX (мессенджер VK) — Bot API.

Устроен похоже на Telegram, но отличия есть, и они важны:
  • токен идёт query-параметром access_token
  • апдейты забираются GET /updates с маркером (long polling)
    либо приходят на вебхук, подписка через POST /subscriptions
  • получателя указываем chat_id или user_id параметром запроса

Токен берётся у @MasterBot внутри MAX.

Про адрес и авторизацию: страница документации утверждает, что база —
platform-api2.max.ru, а токен надо слать заголовком Authorization. Это
неверно: такой хост не резолвится вообще, а официальный SDK max-botapi-python
ходит на botapi.max.ru и передаёт access_token параметром запроса. Проверено:
botapi.max.ru отвечает 401 на неверный токен, то есть параметр он читает.
Заголовок отправляем тоже — на случай, если его когда-нибудь введут.

Структура апдейта сверена с моделями SDK: message.sender.user_id,
message.recipient.chat_id, message.body.text. Разбор всё равно оставлен
терпимым к форме — если MAX поменяет схему, чинить надо в _extract.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path

import httpx

from .. import config, db

log = logging.getLogger("max")

# адрес из официального SDK; тот, что в документации, не существует
API = "https://botapi.max.ru"

_polling: dict[int, asyncio.Task] = {}
_markers: dict[int, int] = {}


def _auth(token: str) -> dict:
    """Токен параметром запроса — так делает официальный SDK."""
    return {"access_token": token}


def _headers(token: str) -> dict:
    return {"Authorization": token, "Content-Type": "application/json"}


async def send(chat_id: str, text: str, media_path: str | None = None,
               button: tuple[str, str] | None = None,
               token: str | None = None,
               kind: str | None = None) -> tuple[bool, str]:
    """Отправить сообщение в MAX.

    Вложения грузятся отдельным запросом, поэтому при неудаче с файлом
    отправляем хотя бы текст — клиент не должен остаться без ответа.
    """
    if not token:
        return False, "no_bot"

    payload: dict = {"text": text}

    if button and button[0] and button[1]:
        payload["attachments"] = [{
            "type": "inline_keyboard",
            "payload": {"buttons": [[{
                "type": "link", "text": button[0], "url": button[1],
            }]]},
        }]

    if media_path:
        token_url = await _upload(token, media_path, kind)
        if token_url:
            payload.setdefault("attachments", []).append(token_url)

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{API}/messages",
                headers=_headers(token),
                params={**_auth(token), "chat_id": chat_id},
                json=payload,
            )
            if resp.status_code >= 400:
                body = resp.text[:250]
                if "block" in body.lower() or resp.status_code == 403:
                    return False, "blocked"
                log.warning("MAX отказал (%s): %s", resp.status_code, body)
                return False, "error"
        return True, "sent"
    except Exception as exc:  # noqa: BLE001
        log.warning("MAX недоступен: %s", exc)
        return False, "error"


async def _upload(token: str, media_path: str, kind: str | None) -> dict | None:
    """Загрузить файл и получить вложение для сообщения."""
    from .base import media_file, media_kind
    path = media_file(media_path)
    if not path.exists():
        return None
    kind = kind or media_kind(media_path)
    upload_type = {"photo": "image", "voice": "audio", "audio": "audio",
                   "video": "video"}.get(kind, "file")

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            # шаг 1: узнать, куда грузить
            prep = await client.post(
                f"{API}/uploads",
                headers={"Authorization": token},
                params={**_auth(token), "type": upload_type},
            )
            prep.raise_for_status()
            url = prep.json().get("url")
            if not url:
                return None

            # шаг 2: положить файл
            with path.open("rb") as fh:
                put = await client.post(url, files={"data": (path.name, fh)})
            put.raise_for_status()
            body = put.json() if put.text.strip().startswith("{") else {}

        payload = body.get("photos") or body.get("token") or body
        return {"type": upload_type, "payload": payload}
    except Exception as exc:  # noqa: BLE001
        log.warning("вложение в MAX не загрузилось: %s", exc)
        return None


def _extract(update: dict) -> dict | None:
    """Достать из апдейта то, что нам нужно, не зная точной формы схемы."""
    # типы апдейтов из SDK: message_created, bot_started и прочие
    if update.get("update_type") not in (None, "message_created", "bot_started"):
        return None

    message = update.get("message") or update
    body = message.get("body") or {}
    sender = message.get("sender") or update.get("user") or {}
    recipient = message.get("recipient") or {}

    chat_id = (recipient.get("chat_id") or update.get("chat_id")
               or message.get("chat_id") or sender.get("user_id"))
    user_id = sender.get("user_id") or update.get("user_id") or chat_id
    if not chat_id:
        return None

    name = " ".join(filter(None, [sender.get("first_name"), sender.get("last_name")]))
    return {
        "chat_id": str(chat_id),
        "user_id": str(user_id),
        "username": sender.get("username"),
        "name": name.strip() or sender.get("name"),
        "text": body.get("text") or message.get("text") or "",
    }


async def feed(bot_row, payload: dict) -> None:
    """Обработать апдейт: из вебхука или из long polling."""
    data = _extract(payload)
    if data is None:
        return

    contact = db.upsert_contact(
        "max", data["chat_id"], data["username"], data["name"],
        bot_id=bot_row["id"],
    )
    from ..sales import handle_incoming
    await handle_incoming(contact["id"], data["text"])


async def _poll(bot_row) -> None:
    """Long polling. Для боевой нагрузки MAX рекомендует вебхук."""
    bot_id, token = bot_row["id"], bot_row["token"]
    while True:
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                params = {**_auth(token), "timeout": 30}
                if _markers.get(bot_id):
                    params["marker"] = _markers[bot_id]
                resp = await client.get(
                    f"{API}/updates", headers={"Authorization": token}, params=params
                )
                resp.raise_for_status()
                data = resp.json()

            if data.get("marker"):
                _markers[bot_id] = data["marker"]
            for update in data.get("updates", []):
                await feed(bot_row, update)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — цикл не должен умирать
            log.warning("MAX polling: %s", exc)
            await asyncio.sleep(5)


async def check_token(token: str) -> dict:
    """Проверить токен до сохранения."""
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(f"{API}/me", headers={"Authorization": token.strip()},
                                    params=_auth(token.strip()))
            if resp.status_code >= 400:
                return {"ok": False, "error": f"MAX отклонил токен ({resp.status_code})"}
            info = resp.json()
        return {"ok": True, "username": info.get("username") or info.get("name") or "bot",
                "name": info.get("name", "")}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}


async def start_bot(bot_row) -> None:
    """Поднять одного MAX-бота: вебхук или polling."""
    token = bot_row["token"]

    if config.MODE == "webhook":
        url = f"{config.PUBLIC_URL}/hook/max/{bot_row['id']}"
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.post(
                    f"{API}/subscriptions",
                    headers=_headers(token),
                    params=_auth(token),
                    json={"url": url},
                )
            if resp.status_code >= 400:
                raise RuntimeError(resp.text[:200])
            log.info("MAX-бот %s: вебхук на %s", bot_row["title"], url)
        except Exception as exc:  # noqa: BLE001
            db.set_bot_error(bot_row["id"], str(exc))
            log.error("вебхук MAX не поставился: %s", exc)
        return

    _polling[bot_row["id"]] = asyncio.create_task(_poll(bot_row))
    log.info("MAX-бот %s: polling запущен", bot_row["title"])


async def stop_bot(bot_id: int) -> None:
    task = _polling.pop(bot_id, None)
    if task:
        task.cancel()
    _markers.pop(bot_id, None)


def live() -> set[int]:
    return set(_polling)
