"""Чат для сайта — виджет на страницах клиента.

Единственный канал, который целиком наш: ни токенов, ни чужих API, ни лимитов.
Поэтому и работает всегда, и для демонстрации подходит лучше остальных.

Как устроено: на сайт ставится одна строка со скриптом, скрипт рисует кнопку
и окно чата и общается с нами по трём адресам — начать, отправить, забрать
новое. Посетитель опознаётся подписанным токеном в localStorage, поэтому
переписка не теряется при перезагрузке страницы.

Доставка обратная: мы не можем «отправить» сообщение в браузер, поэтому
исходящие просто пишутся в базу, а виджет забирает их опросом.
"""
from __future__ import annotations

import logging
import uuid

from itsdangerous import BadSignature, URLSafeSerializer

from .. import config, db

log = logging.getLogger("web-chat")

_signer = URLSafeSerializer(config.SECRET_KEY, salt="widget")


def new_visitor() -> str:
    """Подписанный токен посетителя. Подпись мешает читать чужую переписку."""
    return _signer.dumps({"v": uuid.uuid4().hex})


def visitor_id(token: str) -> str | None:
    try:
        data = _signer.loads(token)
        return data.get("v")
    except BadSignature:
        return None


def contact_for(token: str, name: str | None = None,
                contact: str | None = None, language: str | None = None) -> dict | None:
    """Найти или завести контакт посетителя сайта.

    Язык берём из браузера: в отличие от Telegram, тут его больше негде взять,
    а мультиязычной рассылке он нужен.
    """
    visitor = visitor_id(token)
    if not visitor:
        return None
    row = db.upsert_contact("web", visitor, None, name, contact, language=language)
    return dict(row) if row else None


async def send(contact_id: int, text: str, media_path: str | None = None,
               button: tuple[str, str] | None = None) -> tuple[bool, str]:
    """Для сайта «отправка» — это запись в базу: виджет заберёт её опросом."""
    if not (text or "").strip() and not media_path:
        return False, "empty"
    return True, "sent"


def history_after(contact_id: int, after: int = 0) -> list[dict]:
    """Новые сообщения для виджета."""
    rows = db.q(
        "SELECT id, direction, author, text, media_type, media_path, created_at"
        " FROM messages WHERE contact_id = ? AND id > ? ORDER BY id LIMIT 100",
        (contact_id, after),
    )
    return [dict(r) for r in rows]


def enabled() -> bool:
    return db.setting("widget_enabled", "1") == "1"


def snippet() -> str:
    """Строка, которую владелец вставляет на свой сайт."""
    return f'<script src="{config.PUBLIC_URL}/widget.js" async></script>'
