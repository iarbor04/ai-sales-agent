"""OpenRouter: один ключ, сотни моделей. Провайдер по умолчанию."""
from __future__ import annotations

import httpx

from .. import config, db
from .base import LLMError, LLMTruncated, error_text, human_error, network_error

NAME = "openrouter"
TITLE = "OpenRouter"
DEFAULT_MODEL = "deepseek/deepseek-v4-flash"
# Поля для панели: ключ настройки, подпись, подсказка, скрывать ли ввод.
FIELDS = [
    ("openrouter_key", "Ключ OpenRouter", "sk-or-v1-…", True),
]
HELP = "Ключ берётся на openrouter.ai → Keys. Оплата картой, счёт в долларах."

API_URL = "https://openrouter.ai/api/v1/chat/completions"
MODELS_URL = "https://openrouter.ai/api/v1/models"
KEY_URL = "https://openrouter.ai/api/v1/key"

# Глубокие размышления в заполнении шаблона не нужны и только съедают бюджет.
REASONING = {"effort": "low"}


def key() -> str:
    """Ключ из панели, а если там пусто — из .env."""
    return (db.setting("openrouter_key", "").strip() or config.OPENROUTER_API_KEY).strip()


def configured() -> bool:
    return bool(key())


def source() -> str:
    if db.setting("openrouter_key", "").strip():
        return "вставлен в панели"
    return "взят из .env" if config.OPENROUTER_API_KEY else ""


async def complete(system: str, user: str, model: str, max_tokens: int) -> tuple[str, str]:
    token = key()
    if not token:
        raise LLMError("не задан ключ OpenRouter")
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "reasoning": REASONING,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
    }
    try:
        async with httpx.AsyncClient(timeout=60) as client:
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

    # OpenRouter умеет отвечать 200 с телом-ошибкой — это тоже отказ.
    error = body.get("error") if isinstance(body, dict) else None
    if error:
        detail = error.get("message") if isinstance(error, dict) else str(error)
        code = error.get("code") if isinstance(error, dict) else None
        raise LLMError(human_error(TITLE, int(code) if isinstance(code, int) else 400,
                                   str(detail or ""), model))

    choices = body.get("choices") or []
    if not choices:
        raise LLMError(f"модель «{model}» вернула пустой ответ")
    text = (choices[0].get("message", {}).get("content") or "").strip()
    return text, str(choices[0].get("finish_reason") or "")


async def check_credentials() -> dict:
    """Проверка на эндпоинте, который без ключа не отвечает.

    Список моделей OpenRouter отдаёт публично, поэтому «модели загрузились»
    не доказывает, что ключ рабочий.
    """
    token = key()
    if not token:
        return {"ok": False, "detail": "ключ не задан"}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(KEY_URL, headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as exc:
        return {"ok": False, "detail": str(network_error(TITLE, exc))}

    if resp.status_code >= 400:
        return {"ok": False, "detail": human_error(TITLE, resp.status_code, error_text(resp), "")}
    data = {}
    try:
        data = (resp.json() or {}).get("data") or {}
    except ValueError:
        pass
    detail = "ключ принят OpenRouter"
    limit, usage = data.get("limit"), data.get("usage")
    if limit is not None:
        detail += f", лимит {limit}, потрачено {usage or 0}"
    elif usage is not None:
        detail += f", потрачено {usage}"
    return {"ok": True, "detail": detail}


async def models() -> list[dict]:
    if not configured():
        return []
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(MODELS_URL, headers={"Authorization": f"Bearer {key()}"})
            resp.raise_for_status()
            data = resp.json().get("data", [])
    except Exception:  # noqa: BLE001 — список моделей не критичен
        return []
    rows = [{"id": item.get("id", ""), "name": item.get("name", item.get("id", ""))}
            for item in data if item.get("id")]
    rows.sort(key=lambda row: row["id"])
    return rows
