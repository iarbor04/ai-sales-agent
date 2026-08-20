"""Модель через шлюз ASCN: платит клиент нам, ключи провайдеров у нас.

Это главное отличие подписки от лицензии в коде. Проверку подписки клиент
может закомментировать — исходники у него. А доступ к модели вырезать нельзя:
запрос уходит на наш шлюз с ключом подписки, и когда подписка кончилась, шлюз
просто не отвечает. Клиенту при этом не нужно регистрироваться у провайдера и
платить картой в валюте — за него это делаем мы.

Формат запроса совместим с OpenAI, поэтому шлюз можно перевести на любого
провайдера, не трогая установки у клиентов.
"""
from __future__ import annotations

import httpx

from .. import config, license
from .base import LLMError, error_text, human_error, network_error

NAME = "ascn"
TITLE = "Модель ASCN"
DEFAULT_MODEL = "ascn/base"
# Вводить нечего: доступ выдаётся ключом подписки, он уже введён.
FIELDS: list[tuple[str, str, str, bool]] = []
HELP = ("Модель включена в подписку ASCN: ключей и оплаты в валюте не нужно. "
        "Доступ выдаётся тем же ключом, которым активирована подписка.")


def _url(path: str) -> str:
    return f"{config.GATEWAY_URL}{path}"


def key() -> str:
    return license.key()


def configured() -> bool:
    """Шлюз готов отвечать, только если подписка активна."""
    return bool(key()) and license.active()


def source() -> str:
    return "включена в подписку ASCN" if key() else ""


def _headers() -> dict:
    return {"Authorization": f"Bearer {key()}",
            "X-Install-Id": license.install_id(),
            "Content-Type": "application/json"}


async def complete(system: str, user: str, model: str, max_tokens: int) -> tuple[str, str]:
    if not key():
        raise LLMError("подписка ASCN не активирована — модель недоступна")
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
    }
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(_url("/v1/chat/completions"),
                                     json=payload, headers=_headers())
    except httpx.HTTPError as exc:
        raise network_error(TITLE, exc) from exc

    # 402 у шлюза значит одно: подписка закончилась. Это не поломка, и текст
    # должен говорить владельцу, что делать, а не «ошибка 402».
    if resp.status_code == 402:
        raise LLMError("подписка ASCN закончилась — агент не отвечает клиентам. "
                       "Продлите её в личном кабинете ASCN")
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
    if not key():
        return {"ok": False, "detail": "подписка не активирована"}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(_url("/v1/status"), headers=_headers())
    except httpx.HTTPError as exc:
        return {"ok": False, "detail": str(network_error(TITLE, exc))}
    if resp.status_code == 402:
        return {"ok": False, "detail": "подписка закончилась"}
    if resp.status_code >= 400:
        return {"ok": False, "detail": human_error(TITLE, resp.status_code, error_text(resp), "")}
    return {"ok": True, "detail": "шлюз ASCN отвечает, подписка активна"}


async def models() -> list[dict]:
    """Список моделей шлюза. Недоступен — панель покажет текущую и не упадёт."""
    if not key():
        return []
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(_url("/v1/models"), headers=_headers())
            resp.raise_for_status()
            data = resp.json().get("data", [])
    except Exception:  # noqa: BLE001 — список моделей не критичен
        return []
    return [{"id": item.get("id", "")} for item in data if item.get("id")]
