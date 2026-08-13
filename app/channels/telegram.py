"""Telegram — собственный бот на aiogram 3.

Важно из ТЗ: Telegram подключается токеном от BotFather как канал КЛИЕНТА,
а не как интеграция ASCN. Иначе переписка ушла бы в воркспейс агента, и
менеджеру пришлось бы отвечать оттуда — ровно то, чего требуется избежать.
"""
from __future__ import annotations

import asyncio
import logging
import mimetypes
import uuid
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
)

from .. import config, db

log = logging.getLogger("telegram")

bot: Bot | None = None
dispatcher: Dispatcher | None = None
_polling: asyncio.Task | None = None

MEDIA_FIELDS = [
    ("photo", "photo"), ("voice", "voice"), ("audio", "audio"),
    ("video", "video"), ("video_note", "video_note"),
    ("document", "document"), ("sticker", "sticker"), ("animation", "animation"),
]

# Клиент попросил не писать — по ТЗ это выключает ИИ в диалоге.
STOP_WORDS = ("стоп", "не пишите", "отпишись", "отписаться", "unsubscribe", "stop")


def _markup(button: tuple[str, str] | None) -> InlineKeyboardMarkup | None:
    if not button or not button[0] or not button[1]:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=button[0], url=button[1])]]
    )


async def send(chat_id: str, text: str, image_path: str | None = None,
               button: tuple[str, str] | None = None) -> tuple[bool, str]:
    if bot is None:
        return False, "no_bot"

    markup = _markup(button)
    for _ in range(3):
        try:
            if image_path:
                path = Path(image_path)
                if not path.is_absolute():
                    path = config.MEDIA_DIR / path.name
                if path.exists():
                    await bot.send_photo(
                        int(chat_id), FSInputFile(str(path)),
                        caption=text[:1024] or None, reply_markup=markup,
                    )
                    if len(text) > 1024:
                        await bot.send_message(int(chat_id), text[1024:])
                    return True, "sent"
            await bot.send_message(int(chat_id), text, reply_markup=markup)
            return True, "sent"
        except TelegramForbiddenError:
            return False, "blocked"
        except TelegramRetryAfter as exc:
            await asyncio.sleep(getattr(exc, "retry_after", 5) + 1)
        except TelegramBadRequest as exc:
            log.warning("не отправилось (%s): %s", chat_id, exc)
            return False, "error"
        except Exception as exc:  # noqa: BLE001 — один получатель не должен ронять цикл
            log.exception("ошибка отправки %s: %s", chat_id, exc)
            await asyncio.sleep(1)
    return False, "error"


async def _download(file_id: str, hint: str) -> str | None:
    if bot is None:
        return None
    try:
        info = await bot.get_file(file_id)
        src = info.file_path or ""
        name = f"{uuid.uuid4().hex}{Path(src).suffix or hint or '.bin'}"
        await bot.download_file(src, destination=str(config.MEDIA_DIR / name))
        return name
    except Exception as exc:  # noqa: BLE001
        log.warning("вложение не скачалось: %s", exc)
        return None


def _extract_media(message: Message) -> tuple[str | None, str | None, str]:
    for attr, kind in MEDIA_FIELDS:
        value = getattr(message, attr, None)
        if not value:
            continue
        if attr == "photo":
            return kind, value[-1].file_id, ".jpg"
        if attr == "voice":
            return kind, value.file_id, ".ogg"
        if attr == "sticker":
            return kind, value.file_id, ".webp"
        if attr == "video_note":
            return kind, value.file_id, ".mp4"
        hint = Path(getattr(value, "file_name", "") or "").suffix
        if not hint:
            hint = mimetypes.guess_extension(getattr(value, "mime_type", "") or "") or ""
        return kind, value.file_id, hint
    return None, None, ""


def _router() -> Router:
    router = Router(name="client")

    @router.message(CommandStart())
    async def on_start(message: Message) -> None:
        user = message.from_user
        if user is None:
            return
        contact = db.upsert_contact(
            "tg", user.id, user.username,
            " ".join(filter(None, [user.first_name, user.last_name])) or None,
        )
        # /start — это и есть подписка на рассылки
        db.run("UPDATE contacts SET opted_in = 1, blocked = 0 WHERE id = ?", (contact["id"],))
        greeting = db.setting("greeting", "Здравствуйте!")
        await send(str(user.id), greeting)
        db.add_message(contact["id"], "out", "ai", greeting, is_read=True)

    @router.message(Command("stop"))
    async def on_stop(message: Message) -> None:
        user = message.from_user
        if user is None:
            return
        contact = db.upsert_contact("tg", user.id, user.username, user.first_name)
        db.run("UPDATE contacts SET opted_in = 0, ai_enabled = 0 WHERE id = ?", (contact["id"],))
        await message.answer("Больше не пишем. Чтобы вернуться — отправьте /start.")

    @router.message(F.text | F.photo | F.voice | F.document | F.video | F.audio)
    async def on_message(message: Message) -> None:
        user = message.from_user
        if user is None:
            return
        contact = db.upsert_contact(
            "tg", user.id, user.username,
            " ".join(filter(None, [user.first_name, user.last_name])) or None,
        )

        kind, file_id, hint = _extract_media(message)
        media_path = await _download(file_id, hint) if file_id else None
        text = message.text or message.caption or ""

        from ..sales import handle_incoming
        await handle_incoming(contact["id"], text, kind, media_path)

    return router


async def start() -> None:
    """Поднять бота. В режиме webhook polling не запускаем."""
    global bot, dispatcher, _polling
    if not config.telegram_enabled():
        log.info("токен Telegram не задан — канал выключен")
        return

    bot = Bot(config.TELEGRAM_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dispatcher = Dispatcher()
    dispatcher.include_router(_router())

    if config.MODE == "webhook":
        url = f"{config.PUBLIC_URL}/hook/telegram"
        try:
            await bot.set_webhook(url, drop_pending_updates=True,
                                  secret_token=config.WEBHOOK_SECRET)
            log.info("Telegram: вебхук на %s", url)
        except Exception as exc:  # noqa: BLE001
            log.error("вебхук не поставился: %s", exc)
        return

    # оставшийся от прошлой конфигурации вебхук не даст polling'у работать
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("delete_webhook: %s", exc)

    _polling = asyncio.create_task(dispatcher.start_polling(bot, handle_signals=False))
    log.info("Telegram: polling запущен")


async def feed(payload: dict) -> None:
    """Скормить апдейт из вебхука."""
    if bot is None or dispatcher is None:
        return
    await dispatcher.feed_update(bot, Update.model_validate(payload, context={"bot": bot}))


async def stop() -> None:
    global _polling
    if _polling:
        _polling.cancel()
        _polling = None
    if bot:
        try:
            await bot.session.close()
        except Exception:  # noqa: BLE001
            pass
