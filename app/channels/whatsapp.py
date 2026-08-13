"""WhatsApp Business через Meta Cloud API.

Cloud API умеет только вебхуки — polling'а у него нет. Поэтому WhatsApp
работает исключительно при MODE=webhook и публичном HTTPS-адресе.

Подключение готового бизнес-аккаунта: в кабинете Meta указывается адрес
PUBLIC_URL/hook/whatsapp и тот же verify token, что в .env.
"""
from __future__ import annotations

import logging
import uuid
from pathlib import Path

import httpx

from .. import config, db

log = logging.getLogger("whatsapp")

API = "https://graph.facebook.com/v21.0"


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {config.WA_TOKEN}",
        "Content-Type": "application/json",
    }


async def send(phone: str, text: str, image_path: str | None = None,
               button: tuple[str, str] | None = None) -> tuple[bool, str]:
    """Отправить сообщение в WhatsApp.

    Кнопку-ссылку Cloud API в обычном сообщении не поддерживает (только в
    шаблонах), поэтому ссылку дописываем в текст — клиент всё равно её видит.
    """
    if not config.whatsapp_enabled():
        return False, "no_channel"

    if button and button[0] and button[1]:
        text = f"{text}\n\n{button[0]}: {button[1]}"

    url = f"{API}/{config.WA_PHONE_ID}/messages"
    payload: dict = {"messaging_product": "whatsapp", "to": phone}

    if image_path:
        path = Path(image_path)
        if not path.is_absolute():
            path = config.MEDIA_DIR / path.name
        media_id = await _upload(path) if path.exists() else None
        if media_id:
            payload["type"] = "image"
            payload["image"] = {"id": media_id, "caption": text[:1024]}
        else:
            payload["type"] = "text"
            payload["text"] = {"body": text}
    else:
        payload["type"] = "text"
        payload["text"] = {"body": text}

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, headers=_headers(), json=payload)
            if resp.status_code >= 400:
                body = resp.text[:300]
                # 131047 — окно в 24 часа закрыто, писать первым нельзя
                if "131047" in body or "re-engagement" in body.lower():
                    return False, "blocked"
                log.warning("WhatsApp отказал (%s): %s", resp.status_code, body)
                return False, "error"
        return True, "sent"
    except Exception as exc:  # noqa: BLE001
        log.warning("WhatsApp недоступен: %s", exc)
        return False, "error"


async def _upload(path: Path) -> str | None:
    """Загрузить картинку и получить media_id."""
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            with path.open("rb") as fh:
                resp = await client.post(
                    f"{API}/{config.WA_PHONE_ID}/media",
                    headers={"Authorization": f"Bearer {config.WA_TOKEN}"},
                    data={"messaging_product": "whatsapp"},
                    files={"file": (path.name, fh, "image/jpeg")},
                )
            resp.raise_for_status()
            return resp.json().get("id")
    except Exception as exc:  # noqa: BLE001
        log.warning("картинка не загрузилась: %s", exc)
        return None


async def _download(media_id: str) -> str | None:
    """Скачать входящее вложение: сначала ссылка, потом файл."""
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            meta = await client.get(f"{API}/{media_id}", headers=_headers())
            meta.raise_for_status()
            info = meta.json()
            link = info.get("url")
            if not link:
                return None
            blob = await client.get(
                link, headers={"Authorization": f"Bearer {config.WA_TOKEN}"}
            )
            blob.raise_for_status()

        suffix = {
            "image/jpeg": ".jpg", "image/png": ".png",
            "audio/ogg": ".ogg", "video/mp4": ".mp4",
        }.get(info.get("mime_type", ""), ".bin")
        name = f"{uuid.uuid4().hex}{suffix}"
        (config.MEDIA_DIR / name).write_bytes(blob.content)
        return name
    except Exception as exc:  # noqa: BLE001
        log.warning("вложение не скачалось: %s", exc)
        return None


async def feed(payload: dict) -> None:
    """Разобрать вебхук Meta и передать сообщение в логику продаж."""
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})

            # имя человека приходит отдельно от сообщения
            names = {
                c.get("wa_id"): (c.get("profile") or {}).get("name")
                for c in value.get("contacts", [])
            }

            for message in value.get("messages", []):
                phone = message.get("from")
                if not phone:
                    continue

                contact = db.upsert_contact(
                    "wa", phone, None, names.get(phone), phone
                )

                kind = message.get("type", "text")
                text, media_path, media_type = "", None, None

                if kind == "text":
                    text = (message.get("text") or {}).get("body", "")
                elif kind in ("image", "audio", "video", "document", "voice"):
                    blob = message.get(kind) or {}
                    text = blob.get("caption", "")
                    media_type = kind
                    if blob.get("id"):
                        media_path = await _download(blob["id"])
                elif kind == "button":
                    text = (message.get("button") or {}).get("text", "")
                elif kind == "interactive":
                    interactive = message.get("interactive") or {}
                    reply = interactive.get("button_reply") or interactive.get("list_reply") or {}
                    text = reply.get("title", "")

                from ..sales import handle_incoming
                await handle_incoming(contact["id"], text, media_type, media_path)


def verify(params: dict) -> str | None:
    """Ответ на GET-проверку вебхука от Meta."""
    if params.get("hub.mode") == "subscribe" and \
            params.get("hub.verify_token") == config.WA_VERIFY_TOKEN:
        return params.get("hub.challenge")
    return None
