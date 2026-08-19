"""Авито — переписка с покупателями в объявлениях.

Доступ выдаёт сам продавец: личный кабинет → Настройки → Avito API →
регистрация приложения. Оттуда client_id и client_secret, они и хранятся
у бота: id в токене, secret в настройках.

Отличия от мессенджеров:
  • авторизация по OAuth client_credentials, токен живёт ограниченное время,
    поэтому обновляем его сами и держим в памяти;
  • свой user_id узнаём у API — он входит в каждый адрес;
  • входящие только вебхуком: у Авито нет long polling, поэтому канал
    требует MODE=webhook и публичного HTTPS.

Сверено с официальным OpenAPI-спеком каталога developers.avito.ru:
тело отправки — {"message": {"text": ...}, "type": "text"}, подписка на
вебхук — {"url": ..., "secret": ...}, входящее событие описано схемой
WebhookMessage с полями author_id, chat_id, content.text.

Секрет из подписки используем по назначению: Авито возвращает его в заголовке,
и чужой запрос на наш адрес мы отбрасываем. Адрес вебхука угадать несложно,
поэтому без проверки любой мог бы слать нам поддельные сообщения.

На живом кабинете продавца не гонялось — доступа нет.
"""
from __future__ import annotations

import json
import logging
import secrets
import time

import httpx

from .. import config, db

log = logging.getLogger("avito")

API = "https://api.avito.ru"

# bot_id → (токен, когда протухнет)
_tokens: dict[int, tuple[str, float]] = {}


def settings(bot_row) -> dict:
    try:
        return json.loads(bot_row["extra"] or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}


async def _token(bot_row) -> str | None:
    """Токен доступа. Держим в памяти и обновляем за минуту до конца."""
    bot_id = bot_row["id"]
    cached = _tokens.get(bot_id)
    if cached and cached[1] > time.time():
        return cached[0]

    conf = settings(bot_row)
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(f"{API}/token", data={
                "grant_type": "client_credentials",
                "client_id": bot_row["token"],
                "client_secret": conf.get("client_secret", ""),
            })
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:  # noqa: BLE001
        log.warning("Авито не выдал токен: %s", exc)
        return None

    token = data.get("access_token")
    if not token:
        return None
    _tokens[bot_id] = (token, time.time() + int(data.get("expires_in", 3600)) - 60)
    return token


async def _self_id(bot_row, token: str) -> str | None:
    """Свой идентификатор продавца — он входит в каждый адрес мессенджера."""
    conf = settings(bot_row)
    if conf.get("user_id"):
        return str(conf["user_id"])
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(f"{API}/core/v1/accounts/self",
                                    headers={"Authorization": f"Bearer {token}"})
            resp.raise_for_status()
            user_id = str(resp.json().get("id", ""))
    except Exception as exc:  # noqa: BLE001
        log.warning("Авито не отдал профиль: %s", exc)
        return None

    if user_id:
        conf["user_id"] = user_id
        db.run("UPDATE bots SET extra = ? WHERE id = ?",
               (json.dumps(conf, ensure_ascii=False), bot_row["id"]))
    return user_id or None


async def send(chat_id: str, text: str, media_path: str | None = None,
               button: tuple[str, str] | None = None,
               bot_row=None, kind: str | None = None) -> tuple[bool, str]:
    """Ответить в чат объявления."""
    if bot_row is None:
        return False, "no_bot"

    token = await _token(bot_row)
    if not token:
        return False, "error"
    user_id = await _self_id(bot_row, token)
    if not user_id:
        return False, "error"

    if button and button[0] and button[1]:
        text = f"{text}\n\n{button[0]}: {button[1]}"

    url = f"{API}/messenger/v1/accounts/{user_id}/chats/{chat_id}/messages"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                url,
                headers={"Authorization": f"Bearer {token}",
                         "Content-Type": "application/json"},
                json={"message": {"text": text}, "type": "text"},
            )
            if resp.status_code >= 400:
                body = resp.text[:250]
                if resp.status_code in (403, 429):
                    log.warning("Авито ограничил отправку: %s", body)
                    return False, "blocked"
                log.warning("Авито отказал (%s): %s", resp.status_code, body)
                return False, "error"
        return True, "sent"
    except Exception as exc:  # noqa: BLE001
        log.warning("Авито недоступен: %s", exc)
        return False, "error"


def _extract(payload: dict) -> dict | None:
    """Достать сообщение из вебхука.

    Событие приходит в конверте {"payload": {"type": "message", "value": {...}}},
    но принимаем и голую схему WebhookMessage — на случай, если конверт
    когда-нибудь уберут.
    """
    body = payload.get("payload") or payload
    value = body.get("value") or body

    # у конверта type = message, у самого сообщения type = text/image/link
    envelope_type = body.get("type")
    if envelope_type and envelope_type not in ("message", "text", "image", "link"):
        return None

    chat_id = value.get("chat_id") or value.get("chatId")
    author = str(value.get("author_id") or value.get("authorId") or "")
    if not chat_id:
        return None

    content = value.get("content") or {}
    text = content.get("text") or value.get("text") or ""
    return {"chat_id": str(chat_id), "author": author, "text": text}


def webhook_secret(bot_row) -> str:
    """Секрет подписки — им проверяем, что событие действительно от Авито."""
    return settings(bot_row).get("secret", "")


async def feed(bot_row, payload: dict) -> None:
    """Входящее письмо от Авито."""
    data = _extract(payload)
    if data is None or not data["text"]:
        return

    # собственные сообщения приходят тем же вебхуком — на них не отвечаем
    conf = settings(bot_row)
    if data["author"] and str(conf.get("user_id", "")) == data["author"]:
        return

    contact = db.upsert_contact("avito", data["chat_id"], None, None,
                                bot_id=bot_row["id"])
    from ..sales import handle_incoming
    await handle_incoming(contact["id"], data["text"])


async def check_token(client_id: str, conf: dict) -> dict:
    """Проверить доступ до сохранения."""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(f"{API}/token", data={
                "grant_type": "client_credentials",
                "client_id": client_id.strip(),
                "client_secret": conf.get("client_secret", ""),
            })
            if resp.status_code >= 400:
                return {"ok": False, "error": f"Авито отклонил доступ ({resp.status_code})"}
            token = resp.json().get("access_token")

            profile = await client.get(f"{API}/core/v1/accounts/self",
                                       headers={"Authorization": f"Bearer {token}"})
            profile.raise_for_status()
            info = profile.json()
        return {"ok": True, "username": str(info.get("id", "avito")),
                "name": info.get("name", "Авито"), "user_id": info.get("id")}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}


async def start_bot(bot_row) -> None:
    """Подписаться на уведомления. Long polling у Авито нет — только вебхук."""
    if config.MODE != "webhook":
        db.set_bot_error(bot_row["id"], "Авито работает только при MODE=webhook и HTTPS")
        log.warning("Авито требует вебхук — включите MODE=webhook")
        return

    token = await _token(bot_row)
    if not token:
        return

    # секрет генерируем один раз и храним рядом с доступами
    conf = settings(bot_row)
    secret = conf.get("secret")
    if not secret:
        secret = secrets.token_urlsafe(24)
        conf["secret"] = secret
        db.run("UPDATE bots SET extra = ? WHERE id = ?",
               (json.dumps(conf, ensure_ascii=False), bot_row["id"]))

    url = f"{config.PUBLIC_URL}/hook/avito/{bot_row['id']}"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{API}/messenger/v3/webhook",
                headers={"Authorization": f"Bearer {token}",
                         "Content-Type": "application/json"},
                json={"url": url, "secret": secret},
            )
            resp.raise_for_status()
        db.set_bot_error(bot_row["id"], None)
        log.info("Авито: вебхук на %s", url)
    except Exception as exc:  # noqa: BLE001
        db.set_bot_error(bot_row["id"], str(exc))
        log.error("вебхук Авито не поставился: %s", exc)


async def stop_bot(bot_id: int) -> None:
    _tokens.pop(bot_id, None)


def live() -> set[int]:
    if config.MODE != "webhook":
        return set()
    return {b["id"] for b in db.bots(only_enabled=True) if b["platform"] == "avito"}
