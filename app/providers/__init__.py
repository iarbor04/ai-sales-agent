"""Реестр провайдеров модели.

Провайдер выбирается в настройках. Всё остальное в проекте зовёт llm.py и не
знает, чей API за ним — так новый провайдер добавляется одним модулем.
"""
from __future__ import annotations

from .. import db
from . import openrouter, yandex
from .base import LLMError, LLMTruncated, human_error, tidy  # noqa: F401 — общий вход

ALL = (openrouter, yandex)
DEFAULT = openrouter.NAME


def get(name: str):
    for provider in ALL:
        if provider.NAME == name:
            return provider
    return openrouter


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
