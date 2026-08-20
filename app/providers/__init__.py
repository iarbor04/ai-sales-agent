"""Реестр провайдеров модели.

Провайдер выбирается в настройках. Всё остальное в проекте зовёт llm.py и не
знает, чей API за ним — так новый провайдер добавляется одним модулем.
"""
from __future__ import annotations

from .. import config, db
from . import ascn, openrouter, yandex
from .base import LLMError, LLMTruncated, human_error, tidy  # noqa: F401 — общий вход

# ascn первым: в установке из маркетплейса модель включена в подписку, и
# владельцу не нужно ни ключа, ни карты в валюте.
ALL = (ascn, openrouter, yandex)
DEFAULT = ascn.NAME if config.LICENSE_REQUIRED else openrouter.NAME


def get(name: str):
    for provider in ALL:
        if provider.NAME == name:
            return provider
    return get(DEFAULT) if DEFAULT != name else openrouter


def current():
    """Выбранный провайдер. Неизвестное имя — как будто OpenRouter."""
    return get(db.setting("model_provider", DEFAULT) or DEFAULT)


def options() -> list[dict]:
    """Список для панели: что выбрать и что заполнить."""
    return [{"name": provider.NAME, "title": provider.TITLE, "help": provider.HELP,
             "fields": [{"key": key, "label": label, "hint": hint, "secret": secret}
                        for key, label, hint, secret in provider.FIELDS],
             "configured": provider.configured(),
             "default_model": provider.DEFAULT_MODEL}
            for provider in ALL]
