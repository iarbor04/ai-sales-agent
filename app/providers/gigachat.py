"""GigaChat от Сбера.

Двухшаговый доступ: пара client id и client secret меняется на токен, который
живёт полчаса. Токен держим в памяти и обновляем заранее — просить новый на
каждое сообщение клиента дорого и медленно.

Отдельная особенность российских сервисов: сертификат подписан НУЦ Минцифры.
Если на сервере нет его корневых сертификатов, запрос упадёт на проверке TLS —
в этом случае в настройках можно проверку отключить, но по умолчанию она включена.
"""
from __future__ import annotations

import base64
import time
import uuid

import httpx

from .. import db
from .base import LLMError, error_text, human_error, network_error

NAME = "gigachat"
TITLE = "GigaChat"
DEFAULT_MODEL = "GigaChat"
FIELDS = [
    ("gigachat_client_id", "Client ID", "из кабинета GigaChat API", False),
    ("gigachat_client_secret", "Client Secret", "секрет пары", True),
    ("gigachat_scope", "Область доступа", "GIGACHAT_API_PERS", False),
]
HELP = ("Кабинет developers.sber.ru → GigaChat API: создайте проект и возьмите пару "
        "client id и secret. Область: GIGACHAT_API_PERS для физлиц, "
        "GIGACHAT_API_B2B или GIGACHAT_API_CORP для организаций.")

OAUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
API_URL = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"
MODELS_URL = "https://gigachat.devices.sberbank.ru/api/v1/models"

# токен и момент, после которого его надо менять
_token: tuple[str, float] | None = None
TOKEN_MARGIN_SECONDS = 60


def client_id() -> str:
    return db.setting("gigachat_client_id", "").strip()


def client_secret() -> str:
    return db.setting("gigachat_client_secret", "").strip()


def scope() -> str:
    return db.setting("gigachat_scope", "").strip() or "GIGACHAT_API_PERS"


def verify_tls() -> bool:
    return db.setting("gigachat_verify_tls", "1") != "0"


def configured() -> bool:
    return bool(client_id() and client_secret())


def source() -> str:
    return "вставлен в панели" if configured() else ""


def forget_token() -> None:
    """Сбросить токен — например, когда владелец поменял пару доступа."""
    global _token
    _token = None


async def access_token(force: bool = False) -> str:
    """Токен на полчаса. Обновляем заранее, чтобы не упасть на границе."""
    global _token
    if not configured():
        raise LLMError("не заданы client id и secret GigaChat")
    if _token and not force and _token[1] - TOKEN_MARGIN_SECONDS > time.monotonic():
        return _token[0]

    pair = base64.b64encode(f"{client_id()}:{client_secret()}".encode()).decode()
    try:
        async with httpx.AsyncClient(timeout=30, verify=verify_tls()) as client:
            resp = await client.post(OAUTH_URL, data={"scope": scope()}, headers={
                "Authorization": f"Basic {pair}",
                "RqUID": str(uuid.uuid4()),
                "Content-Type": "application/x-www-form-urlencoded",
            })
    except httpx.HTTPError as exc:
        raise network_error(TITLE, exc) from exc

    if resp.status_code >= 400:
        raise LLMError(human_error(TITLE, resp.status_code, error_text(resp), ""))
    try:
        body = resp.json()
    except ValueError as exc:
        raise LLMError(f"{TITLE} вернул не-JSON на запрос токена") from exc

    token = str(body.get("access_token") or "")
    if not token:
        raise LLMError(f"{TITLE} не выдал токен по этой паре доступа")
    # expires_at приходит в миллисекундах эпохи; считаем срок от текущего момента
    expires_at = body.get("expires_at")
    lifetime = 1800.0
    if isinstance(expires_at, (int, float)) and expires_at > 0:
        lifetime = max(60.0, expires_at / 1000 - time.time())
    _token = (token, time.monotonic() + lifetime)
    return token


async def complete(system: str, user: str, model: str, max_tokens: int) -> tuple[str, str]:
    token = await access_token()
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
    }
    try:
        async with httpx.AsyncClient(timeout=60, verify=verify_tls()) as client:
            resp = await client.post(API_URL, json=payload, headers={
                "Authorization": f"Bearer {token}", "Content-Type": "application/json"})
            # токен мог истечь раньше срока — один раз пробуем со свежим
            if resp.status_code == 401:
                token = await access_token(force=True)
                resp = await client.post(API_URL, json=payload, headers={
                    "Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    except httpx.HTTPError as exc:
        raise network_error(TITLE, exc) from exc

    if resp.status_code >= 400:
        raise LLMError(human_error(TITLE, resp.status_code, error_text(resp), model))
    try:
        body = resp.json()
    except ValueError as exc:
        raise LLMError(f"{TITLE} вернул не-JSON") from exc

    choices = body.get("choices") or []
    if not choices:
        raise LLMError(f"модель «{model}» вернула пустой ответ")
    text = (choices[0].get("message", {}).get("content") or "").strip()
    return text, str(choices[0].get("finish_reason") or "")


async def check_credentials() -> dict:
    if not configured():
        return {"ok": False, "detail": "не заданы client id и secret"}
    try:
        await access_token(force=True)
    except LLMError as exc:
        return {"ok": False, "detail": str(exc)}
    return {"ok": True, "detail": f"пара доступа принята GigaChat, область {scope()}"}


async def models() -> list[dict]:
    if not configured():
        return []
    try:
        token = await access_token()
        async with httpx.AsyncClient(timeout=20, verify=verify_tls()) as client:
            resp = await client.get(MODELS_URL, headers={"Authorization": f"Bearer {token}"})
            resp.raise_for_status()
            data = resp.json().get("data", [])
    except Exception:  # noqa: BLE001 — список моделей не критичен
        return [{"id": DEFAULT_MODEL, "name": DEFAULT_MODEL}]
    rows = [{"id": item.get("id", ""), "name": item.get("id", "")}
            for item in data if item.get("id")]
    return rows or [{"id": DEFAULT_MODEL, "name": DEFAULT_MODEL}]
