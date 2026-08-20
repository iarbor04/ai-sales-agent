"""Подписка: активна ли она и что делать, когда закончилась.

Продукт ставится на сервер клиента, а платит он нам. Значит, установка должна
сама узнавать своё состояние у ASCN и уметь остановиться. Устройство такое:

* раз в сутки установка стучится в ASCN своим ключом и получает **подписанный**
  ответ: до какого числа оплачено. Подпись нужна, чтобы клиент не подсунул
  вместо нашего сервера свой и не выписал себе подписку навсегда;
* ответ живёт в базе. Пока он не просрочен, сеть не нужна — иначе наш упавший
  сервер выключил бы агентов у платящих клиентов;
* когда оплаченный срок кончился, панель закрывается, боты гасятся, но данные
  остаются: страница подписки отдаёт всю переписку и лидов одним архивом.

Обход правкой кода возможен — исходники у клиента. Настоящий замок не здесь, а
в том, что модель отвечает через шлюз ASCN по этому же ключу: перестал платить —
шлюз молчит, и патчить нечего. Этот модуль отвечает за честный путь и за то,
чтобы отключение выглядело понятно, а не «всё сломалось».
"""
from __future__ import annotations

import base64
import json
import logging
import time
import uuid

import httpx

from . import config, db

log = logging.getLogger("license")

# Публичный ключ ASCN. Им проверяется подпись ответа о подписке; приватный
# лежит только на нашем сервере лицензий.
PUBLIC_KEY_PEM = config.LICENSE_PUBLIC_KEY or ""

CHECK_TIMEOUT = 15
# Как часто спрашивать состояние. Чаще не нужно: срок в ответе — сутки и больше.
CHECK_EVERY = 12 * 3600


def install_id() -> str:
    """Идентификатор установки: одна подписка — один сервер.

    Заводится при первом обращении и живёт в базе, поэтому переустановка кода
    его не меняет, а перенос базы на другой сервер — сохраняет.
    """
    value = db.setting("install_id", "").strip()
    if not value:
        value = uuid.uuid4().hex
        db.set_setting("install_id", value)
    return value


def key() -> str:
    """Ключ подписки: из панели, а если там пусто — из .env."""
    return (db.setting("license_key", "").strip() or config.LICENSE_KEY).strip()


def _verify(payload: bytes, signature: bytes) -> bool:
    """Проверить подпись ASCN. Без ключа или без библиотеки — не верим."""
    if not PUBLIC_KEY_PEM:
        return False
    try:
        import rsa
    except ImportError:  # pragma: no cover — в requirements есть
        log.warning("нет библиотеки rsa — подпись подписки не проверить")
        return False
    try:
        pem = PUBLIC_KEY_PEM.encode()
        # PEM бывает в двух видах: «PUBLIC KEY» (OpenSSL) и «RSA PUBLIC KEY»
        # (PKCS#1). Принимаем оба, чтобы сторона ASCN не подбирала формат.
        try:
            pub = rsa.PublicKey.load_pkcs1_openssl_pem(pem)
        except Exception:  # noqa: BLE001
            pub = rsa.PublicKey.load_pkcs1(pem)
        rsa.verify(payload, signature, pub)
        return True
    except Exception as exc:  # noqa: BLE001 — любая ошибка здесь значит «не верим»
        log.warning("подпись подписки не сошлась: %s", exc)
        return False


def _decode(token: str) -> dict | None:
    """Разобрать подписанный ответ ASCN: полезная часть и подпись через точку."""
    if not token or "." not in token:
        return None
    body, _, sig = token.rpartition(".")
    try:
        payload = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        signature = base64.urlsafe_b64decode(sig + "=" * (-len(sig) % 4))
    except Exception:  # noqa: BLE001
        return None
    if not _verify(payload, signature):
        return None
    try:
        data = json.loads(payload)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def state() -> dict:
    """Состояние подписки для панели и для решения «отвечать или молчать».

    Установка без включённой подписки (свой сервер, свой ключ модели) считается
    активной: этот механизм для тех, кто пришёл из маркетплейса.
    """
    if not config.LICENSE_REQUIRED:
        return {"active": True, "mode": "self", "until": 0,
                "note": "подписка не требуется: установка своя"}

    if not key():
        return {"active": False, "mode": "new", "until": 0,
                "note": "ключ подписки не введён"}

    data = _decode(db.setting("license_token", ""))
    if not data:
        return {"active": False, "mode": "unknown", "until": 0,
                "note": "подписка ещё не подтверждена — нажмите «Проверить подписку»"}

    # Ответ выписан другой установке: базу перенесли или ключом поделились.
    if data.get("install") and data["install"] != install_id():
        return {"active": False, "mode": "foreign", "until": 0,
                "note": "ключ уже используется на другом сервере"}

    until = int(data.get("until") or 0)
    plan = str(data.get("plan") or "")
    if until > int(time.time()):
        return {"active": True, "mode": "paid", "until": until, "plan": plan,
                "note": f"оплачено до {time.strftime('%d.%m.%Y', time.localtime(until))}"}
    ended = (f" {time.strftime('%d.%m.%Y', time.localtime(until))}" if until else "")
    return {"active": False, "mode": "expired", "until": until, "plan": plan,
            "note": f"подписка закончилась{ended}"}


def active() -> bool:
    return bool(state()["active"])


async def refresh(force: bool = False) -> dict:
    """Спросить у ASCN, оплачено ли, и запомнить подписанный ответ.

    Сеть недоступна — оставляем прежний ответ: он действует до своего срока.
    Именно так наш упавший сервер не выключает агентов у платящих клиентов.
    """
    if not config.LICENSE_REQUIRED:
        return {"ok": True, "detail": "подписка не требуется"}
    token = key()
    if not token:
        return {"ok": False, "detail": "ключ подписки не введён"}

    last = int(db.setting("license_checked_at", "0") or 0)
    if not force and time.time() - last < CHECK_EVERY:
        return {"ok": True, "detail": "проверено недавно"}

    payload = {
        "key": token,
        "install": install_id(),
        "url": config.PUBLIC_URL,
        "contacts": db.q1("SELECT COUNT(*) AS c FROM contacts")["c"],
        "leads": db.q1("SELECT COUNT(*) AS c FROM leads")["c"],
    }
    try:
        async with httpx.AsyncClient(timeout=CHECK_TIMEOUT) as client:
            resp = await client.post(f"{config.LICENSE_URL}/api/license/check", json=payload)
    except httpx.HTTPError as exc:
        log.warning("сервер лицензий недоступен: %s", exc)
        return {"ok": False, "detail": "сервер подписок не ответил — работаем по прежнему ответу"}

    db.set_setting("license_checked_at", str(int(time.time())))
    if resp.status_code == 404:
        return {"ok": False, "detail": "такого ключа подписки нет"}
    if resp.status_code >= 400:
        return {"ok": False, "detail": f"сервер подписок ответил {resp.status_code}"}

    try:
        body = resp.json()
    except ValueError:
        return {"ok": False, "detail": "сервер подписок ответил не-JSON"}

    received = str((body or {}).get("token") or "")
    if not _decode(received):
        # Подпись не сошлась — ответ подделан или ключ ASCN сменился.
        return {"ok": False, "detail": "ответ сервера подписок не заверен подписью ASCN"}
    db.set_setting("license_token", received)
    return {"ok": True, "detail": state()["note"]}


async def activate(new_key: str) -> dict:
    """Записать ключ из панели и сразу проверить его."""
    new_key = "".join(new_key.split())
    if not new_key:
        return {"ok": False, "detail": "ключ пустой"}
    db.set_setting("license_key", new_key)
    db.set_setting("license_token", "")
    return await refresh(force=True)
