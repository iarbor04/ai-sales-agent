"""YandexGPT через Foundation Models.

Для российских клиентов: оплата в рублях, данные не покидают Яндекс.Облако.
Алиса — голосовой ассистент и к текстовому API отношения не имеет, поэтому
подключаем именно Foundation Models.

Нужны две вещи: API-ключ сервисного аккаунта и идентификатор каталога — модель
задаётся строкой gpt://<каталог>/<модель>/latest, без каталога запрос не собрать.
"""
from __future__ import annotations

import httpx

from .. import db
from .base import LLMError, error_text, human_error, network_error, tidy

NAME = "yandex"
TITLE = "YandexGPT"
DEFAULT_MODEL = "yandexgpt-lite/latest"
FIELDS = [
    ("yandex_api_key", "API-ключ сервисного аккаунта", "AQVN…", True),
    ("yandex_folder_id", "Идентификатор каталога", "b1g…", False),
]
HELP = ("Консоль Яндекс.Облака: сервисный аккаунт с ролью ai.languageModels.user, "
        "затем API-ключ. Каталог — это folder id из адреса консоли.")

API_URL = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"
# Список моделей публичным эндпоинтом не отдаётся — держим известные.
KNOWN_MODELS = ["yandexgpt-lite/latest", "yandexgpt/latest", "yandexgpt-32k/latest",
                "llama-lite/latest", "llama/latest"]


def api_key() -> str:
    return db.setting("yandex_api_key", "").strip()


def folder() -> str:
    return db.setting("yandex_folder_id", "").strip()


def configured() -> bool:
    return bool(api_key() and folder())


def source() -> str:
    return "вставлен в панели" if api_key() else ""


def model_uri(model: str) -> str:
    """gpt://<каталог>/<модель>. Уже готовый uri пропускаем как есть."""
    if model.startswith("gpt://") or model.startswith("ds://"):
        return model
    return f"gpt://{folder()}/{model}"


async def complete(system: str, user: str, model: str, max_tokens: int) -> tuple[str, str]:
    if not api_key():
        raise LLMError("не задан API-ключ YandexGPT")
    if not folder():
        raise LLMError("не задан идентификатор каталога YandexGPT — без него не собрать запрос")

    payload = {
        "modelUri": model_uri(model),
        "completionOptions": {"stream": False, "temperature": 0.3,
                              "maxTokens": str(max_tokens)},
        "messages": [{"role": "system", "text": system},
                     {"role": "user", "text": user}],
    }
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(API_URL, json=payload, headers={
                "Authorization": f"Api-Key {api_key()}", "Content-Type": "application/json"})
    except httpx.HTTPError as exc:
        raise network_error(TITLE, exc) from exc

    if resp.status_code >= 400:
        raise LLMError(human_error(TITLE, resp.status_code, error_text(resp), model))
    try:
        body = resp.json()
    except ValueError as exc:
        raise LLMError(f"{TITLE} вернул не-JSON") from exc

    alternatives = ((body.get("result") or {}).get("alternatives") or [])
    if not alternatives:
        raise LLMError(f"модель «{model}» вернула пустой ответ")
    first = alternatives[0]
    text = ((first.get("message") or {}).get("text") or "").strip()
    # Яндекс сообщает об обрезке статусом, а не полем finish_reason.
    status = str(first.get("status") or "")
    finish = "length" if "TRUNCATED" in status else "stop"
    return text, finish


async def check_credentials() -> dict:
    """Проверяем самым дешёвым настоящим запросом: у Яндекса нет эндпоинта ключа."""
    if not api_key():
        return {"ok": False, "detail": "API-ключ не задан"}
    if not folder():
        return {"ok": False, "detail": "не задан идентификатор каталога"}
    try:
        text, _ = await complete("Ответь одним словом: ок.", "Проверка связи.",
                                 db.setting("model", DEFAULT_MODEL) or DEFAULT_MODEL, 60)
    except LLMError as exc:
        return {"ok": False, "detail": str(exc)}
    return {"ok": True, "detail": f"ключ принят YandexGPT, модель ответила: {tidy(text)[:40]}"}


async def models() -> list[dict]:
    return [{"id": name, "name": name} for name in KNOWN_MODELS]
