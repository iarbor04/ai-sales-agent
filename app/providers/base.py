"""Общее для всех провайдеров модели.

Провайдер отвечает только за транспорт: отправить запрос и вернуть текст.
Промпт, разбор JSON, повторные попытки и логика продаж живут в llm.py и не
зависят от того, чей это API.
"""
from __future__ import annotations

import httpx


class LLMError(RuntimeError):
    """Понятная причина, почему модель не ответила.

    Текст уходит менеджеру в уведомление и в журнал обращений, поэтому он
    написан для человека, а не для разработчика.
    """


class LLMTruncated(LLMError):
    """Ответ обрезан лимитом токенов — значит, JSON пришёл неполным."""


def tidy(detail: str) -> str:
    """Без хвостовой точки: текст подставляется в середину предложения."""
    return (detail or "").strip().rstrip(".").strip()[:200]


def error_text(resp: httpx.Response) -> str:
    """Что именно ответил провайдер.

    Одного кода состояния мало: 401 при отозванном ключе и 401 при ключе от
    другого аккаунта выглядят одинаково, а сообщение провайдера их различает.
    """
    try:
        body = resp.json()
    except ValueError:
        return tidy(resp.text)
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and error.get("message"):
            return tidy(str(error["message"]))
        if isinstance(error, str):
            return tidy(error)
        for key in ("message", "detail", "description"):
            if body.get(key):
                return tidy(str(body[key]))
    return tidy(resp.text)


def human_error(provider: str, status: int, detail: str, model: str) -> str:
    """Код ответа и текст провайдера — человеческими словами."""
    detail = tidy(detail)
    tail = f": {detail}" if detail else ""
    if status == 401:
        return f"{provider} отклонил доступ (401){tail or ': ключ недействителен или отозван'}"
    if status == 402:
        return f"на счёте {provider} нет средств (402){tail}"
    if status == 403:
        return f"{provider} запретил запрос (403){tail or ': проверьте права ключа'}"
    if status == 404:
        return f"модель «{model}» недоступна по этому ключу (404){tail}"
    if status == 429:
        return f"{provider} ограничил частоту запросов (429){tail}"
    if status >= 500:
        return f"{provider} временно недоступен ({status}){tail}"
    return f"{provider} вернул ошибку {status}{tail}"


def network_error(provider: str, exc: Exception) -> LLMError:
    """Сетевые беды объясняем действием, а не текстом библиотеки."""
    text = str(exc).lower()
    if isinstance(exc, httpx.TimeoutException):
        return LLMError(f"{provider} не ответил вовремя")
    if "certificate" in text or "ssl" in text:
        return LLMError(
            f"{provider}: сервер не доверяет сертификату. Установите на сервер "
            "корневые сертификаты НУЦ Минцифры или отключите проверку в настройках")
    if "name or service not known" in text or "nodename nor servname" in text:
        return LLMError(f"{provider}: сервер не смог найти адрес — проверьте DNS")
    return LLMError(f"не удалось связаться с {provider}: {tidy(str(exc))}")
