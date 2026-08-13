"""Почта — тот же агент отвечает на письма.

Работает на стандартной библиотеке: IMAP на приём, SMTP на отправку.
Никаких сторонних сервисов и ключей — только логин и пароль от ящика.
У Яндекса, Mail.ru и Gmail для этого нужен пароль приложения, обычный пароль
от аккаунта они по IMAP не пускают.

Отличие от мессенджеров: письмо — не реплика в чате, а тред. Поэтому
храним Message-ID первого письма и отвечаем в тот же тред заголовками
In-Reply-To и References, иначе у клиента в почте будет каша из отдельных писем.
"""
from __future__ import annotations

import asyncio
import email
import email.utils
import imaplib
import json
import logging
import smtplib
from email.header import decode_header, make_header
from email.message import EmailMessage

from .. import db

log = logging.getLogger("mail")

_polling: dict[int, asyncio.Task] = {}


def settings(bot_row) -> dict:
    """Настройки ящика лежат в extra: сервера, порты, логин."""
    try:
        return json.loads(bot_row["extra"] or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}


def _decode(raw: str | None) -> str:
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw)))
    except Exception:  # noqa: BLE001 — кривые заголовки не должны ронять приём
        return raw


def _body_of(message) -> str:
    """Текст письма без вложений и без html, если есть простая часть."""
    if not message.is_multipart():
        payload = message.get_payload(decode=True) or b""
        return payload.decode(message.get_content_charset() or "utf-8", "replace")

    plain = html = ""
    for part in message.walk():
        if part.get_content_disposition() == "attachment":
            continue
        charset = part.get_content_charset() or "utf-8"
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        text = payload.decode(charset, "replace")
        if part.get_content_type() == "text/plain" and not plain:
            plain = text
        elif part.get_content_type() == "text/html" and not html:
            html = text

    if plain:
        return plain
    if html:
        import re
        return re.sub(r"<[^>]+>", " ", html)
    return ""


def _trim_quote(text: str) -> str:
    """Отрезать цитату предыдущей переписки — модели она только мешает."""
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(">"):
            break
        if stripped.startswith(("-----Original", "________", "On ", "От:", "Отправлено:")):
            break
        lines.append(line)
    return "\n".join(lines).strip()


async def send(address: str, text: str, media_path: str | None = None,
               button: tuple[str, str] | None = None,
               bot_row=None, kind: str | None = None) -> tuple[bool, str]:
    """Ответить письмом в тот же тред."""
    if bot_row is None:
        return False, "no_bot"
    conf = settings(bot_row)
    login = conf.get("login")
    if not login:
        return False, "no_bot"

    if button and button[0] and button[1]:
        text = f"{text}\n\n{button[0]}: {button[1]}"

    contact = db.q1(
        "SELECT * FROM contacts WHERE channel = 'mail' AND external_id = ?", (address,)
    )
    thread = {}
    if contact:
        try:
            thread = json.loads(contact["phone"] or "{}")
        except (json.JSONDecodeError, TypeError):
            thread = {}

    message = EmailMessage()
    message["From"] = login
    message["To"] = address
    message["Subject"] = thread.get("subject") or db.setting("mail_subject", "Ваш вопрос")
    if thread.get("msgid"):
        message["In-Reply-To"] = thread["msgid"]
        message["References"] = thread["msgid"]
    message.set_content(text)

    if media_path:
        from .base import media_file
        path = media_file(media_path)
        if path.exists():
            message.add_attachment(
                path.read_bytes(), maintype="application",
                subtype="octet-stream", filename=path.name,
            )

    def _deliver() -> None:
        with smtplib.SMTP_SSL(conf.get("smtp_host", ""),
                              int(conf.get("smtp_port", 465)), timeout=30) as server:
            server.login(login, bot_row["token"])
            server.send_message(message)

    try:
        await asyncio.to_thread(_deliver)
        return True, "sent"
    except Exception as exc:  # noqa: BLE001
        log.warning("письмо не ушло: %s", exc)
        return False, "error"


def _fetch(bot_row) -> list[dict]:
    """Забрать непрочитанные письма и пометить их прочитанными."""
    conf = settings(bot_row)
    letters: list[dict] = []
    try:
        box = imaplib.IMAP4_SSL(conf.get("imap_host", ""),
                                int(conf.get("imap_port", 993)))
        box.login(conf.get("login", ""), bot_row["token"])
        box.select("INBOX")
        status, data = box.search(None, "UNSEEN")
        if status == "OK":
            for num in data[0].split()[:20]:
                ok, raw = box.fetch(num, "(RFC822)")
                if ok != "OK" or not raw or not raw[0]:
                    continue
                message = email.message_from_bytes(raw[0][1])
                sender = email.utils.parseaddr(message.get("From", ""))[1]
                if not sender or sender == conf.get("login"):
                    continue
                letters.append({
                    "from": sender,
                    "name": _decode(email.utils.parseaddr(message.get("From", ""))[0]),
                    "subject": _decode(message.get("Subject")),
                    "msgid": message.get("Message-ID", ""),
                    "text": _trim_quote(_body_of(message)),
                })
                box.store(num, "+FLAGS", "\\Seen")
        box.logout()
    except Exception as exc:  # noqa: BLE001
        log.warning("почта недоступна: %s", exc)
    return letters


async def _poll(bot_row) -> None:
    """Проверяем ящик раз в минуту — письмо не чат, секунды тут не нужны."""
    while True:
        try:
            for letter in await asyncio.to_thread(_fetch, bot_row):
                contact = db.upsert_contact(
                    "mail", letter["from"], None, letter["name"] or letter["from"],
                    bot_id=bot_row["id"],
                )
                # тему и Message-ID держим при контакте, чтобы отвечать в тред
                db.run(
                    "UPDATE contacts SET phone = ? WHERE id = ?",
                    (json.dumps({"subject": f"Re: {letter['subject']}".replace("Re: Re:", "Re:"),
                                 "msgid": letter["msgid"]}, ensure_ascii=False),
                     contact["id"]),
                )
                from ..sales import handle_incoming
                await handle_incoming(contact["id"], letter["text"])
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("цикл почты: %s", exc)
        await asyncio.sleep(60)


async def check_token(token: str, conf: dict) -> dict:
    """Проверить, что ящик пускает по IMAP."""
    def _try() -> None:
        box = imaplib.IMAP4_SSL(conf.get("imap_host", ""), int(conf.get("imap_port", 993)))
        box.login(conf.get("login", ""), token)
        box.select("INBOX")
        box.logout()

    try:
        await asyncio.to_thread(_try)
        return {"ok": True, "username": conf.get("login", "почта"), "name": "Почта"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"ящик не пустил: {str(exc)[:150]}"}


async def start_bot(bot_row) -> None:
    _polling[bot_row["id"]] = asyncio.create_task(_poll(bot_row))
    log.info("почта %s: проверка раз в минуту", settings(bot_row).get("login", ""))


async def stop_bot(bot_id: int) -> None:
    task = _polling.pop(bot_id, None)
    if task:
        task.cancel()


def live() -> set[int]:
    return set(_polling)
