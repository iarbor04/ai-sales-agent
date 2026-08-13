"""ВКонтакте — сообщения сообщества.

Токен берётся в настройках сообщества: Управление → Работа с API → создать
ключ доступа с правом «Сообщения сообщества».

Приём сообщений двумя способами, как и у остальных каналов:
  • Long Poll — по умолчанию, публичный адрес не нужен. ВК сам говорит,
    куда стучаться, через groups.getLongPollServer.
  • Callback API — при MODE=webhook. ВК сначала присылает запрос типа
    confirmation и ждёт в ответ строку-подтверждение из настроек сообщества,
    поэтому её просим при подключении бота.

Отправка требует random_id: без него ВК считает повтор дублем и молча
проглатывает сообщение.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random

import httpx

from .. import db

log = logging.getLogger("vk")

API = "https://api.vk.com/method"
VERSION = "5.199"

_polling: dict[int, asyncio.Task] = {}


def settings(bot_row) -> dict:
    """Настройки платформы у бота: подтверждение, id сообщества."""
    try:
        return json.loads(bot_row["extra"] or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}


async def _call(token: str, method: str, **params) -> dict:
    params.update({"access_token": token, "v": VERSION})
    async with httpx.AsyncClient(timeout=40) as client:
        resp = await client.post(f"{API}/{method}", data=params)
        resp.raise_for_status()
        data = resp.json()
    if "error" in data:
        raise RuntimeError(data["error"].get("error_msg", "ошибка ВК"))
    return data.get("response", {})


async def send(peer_id: str, text: str, media_path: str | None = None,
               button: tuple[str, str] | None = None,
               token: str | None = None,
               kind: str | None = None) -> tuple[bool, str]:
    """Отправить сообщение пользователю сообщества."""
    if not token:
        return False, "no_bot"

    if button and button[0] and button[1]:
        # кнопки-ссылки в ВК живут только в клавиатуре, а она требует
        # отдельного согласования: проще и надёжнее дописать ссылку в текст
        text = f"{text}\n\n{button[0]}: {button[1]}"

    params: dict = {
        "peer_id": peer_id,
        "message": text,
        "random_id": random.getrandbits(31),
    }

    if media_path:
        attachment = await _upload_photo(token, peer_id, media_path, kind)
        if attachment:
            params["attachment"] = attachment

    try:
        await _call(token, "messages.send", **params)
        return True, "sent"
    except RuntimeError as exc:
        text_error = str(exc).lower()
        # 901 и «privacy» — пользователь запретил сообщения от сообщества
        if "901" in text_error or "privacy" in text_error or "blacklist" in text_error:
            return False, "blocked"
        log.warning("ВК отказал: %s", exc)
        return False, "error"
    except Exception as exc:  # noqa: BLE001
        log.warning("ВК недоступен: %s", exc)
        return False, "error"


async def _upload_photo(token: str, peer_id: str, media_path: str,
                        kind: str | None) -> str | None:
    """Картинку ВК принимает в три шага: адрес, загрузка, сохранение."""
    from .base import media_file, media_kind
    if (kind or media_kind(media_path)) != "photo":
        # документы и голосовые грузятся другим методом; пока не поддерживаем,
        # чтобы не молчать — сообщение уйдёт текстом без вложения
        return None

    path = media_file(media_path)
    if not path.exists():
        return None

    try:
        server = await _call(token, "photos.getMessagesUploadServer", peer_id=peer_id)
        async with httpx.AsyncClient(timeout=60) as client:
            with path.open("rb") as fh:
                up = await client.post(server["upload_url"],
                                       files={"photo": (path.name, fh)})
            up.raise_for_status()
            blob = up.json()

        saved = await _call(
            token, "photos.saveMessagesPhoto",
            photo=blob.get("photo"), server=blob.get("server"), hash=blob.get("hash"),
        )
        item = saved[0] if isinstance(saved, list) and saved else None
        if not item:
            return None
        return f"photo{item['owner_id']}_{item['id']}"
    except Exception as exc:  # noqa: BLE001
        log.warning("картинка в ВК не загрузилась: %s", exc)
        return None


async def _name_of(token: str, user_id: str) -> tuple[str | None, str | None]:
    """Имя и ник пользователя — чтобы в панели был человек, а не номер."""
    try:
        people = await _call(token, "users.get", user_ids=user_id, fields="domain")
        if people:
            person = people[0]
            name = " ".join(filter(None, [person.get("first_name"),
                                          person.get("last_name")])).strip()
            return name or None, person.get("domain")
    except Exception:  # noqa: BLE001 — без имени тоже проживём
        pass
    return None, None


async def feed(bot_row, event: dict) -> None:
    """Обработать событие: из Callback API или из Long Poll."""
    if event.get("type") != "message_new":
        return

    obj = event.get("object", {})
    message = obj.get("message", obj)
    peer_id = message.get("peer_id") or message.get("from_id")
    if not peer_id:
        return

    name, username = await _name_of(bot_row["token"], str(message.get("from_id", peer_id)))
    contact = db.upsert_contact("vk", str(peer_id), username, name,
                                bot_id=bot_row["id"])

    from ..sales import handle_incoming
    await handle_incoming(contact["id"], message.get("text") or "")


async def _poll(bot_row) -> None:
    """Long Poll: адрес и ключ выдаёт сам ВК, потом ждём события."""
    token, bot_id = bot_row["token"], bot_row["id"]
    group_id = settings(bot_row).get("group_id")
    server = key = ts = None

    while True:
        try:
            if not server:
                info = await _call(token, "groups.getLongPollServer", group_id=group_id)
                server, key, ts = info["server"], info["key"], info["ts"]

            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.get(server, params={
                    "act": "a_check", "key": key, "ts": ts, "wait": 25,
                })
                resp.raise_for_status()
                data = resp.json()

            if data.get("failed"):
                # ключ протух — попросим новый на следующем круге
                server = None
                continue

            ts = data.get("ts", ts)
            for event in data.get("updates", []):
                await feed(bot_row, event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — цикл не должен умирать
            log.warning("ВК polling: %s", exc)
            server = None
            await asyncio.sleep(5)


async def check_token(token: str) -> dict:
    """Проверить ключ и заодно узнать, какому сообществу он принадлежит."""
    try:
        groups = await _call(token.strip(), "groups.getById")
        items = groups.get("groups", groups) if isinstance(groups, dict) else groups
        group = items[0] if items else {}
        return {"ok": True,
                "username": group.get("screen_name") or str(group.get("id", "")),
                "name": group.get("name", ""),
                "group_id": group.get("id")}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}


async def start_bot(bot_row) -> None:
    """При MODE=webhook ВК стучится сам, поднимать нечего."""
    from .. import config
    if config.MODE == "webhook":
        log.info("ВК-бот %s: ждём Callback на %s/hook/vk/%s",
                 bot_row["title"], config.PUBLIC_URL, bot_row["id"])
        return
    _polling[bot_row["id"]] = asyncio.create_task(_poll(bot_row))
    log.info("ВК-бот %s: long poll запущен", bot_row["title"])


async def stop_bot(bot_id: int) -> None:
    task = _polling.pop(bot_id, None)
    if task:
        task.cancel()


def live() -> set[int]:
    from .. import config
    if config.MODE == "webhook":
        return {b["id"] for b in db.bots(only_enabled=True) if b["platform"] == "vk"}
    return set(_polling)
