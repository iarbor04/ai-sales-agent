"""Telegram: сколько угодно ботов, все подключаются из панели.

Боты лежат в базе, а не в .env — владелец добавляет их сам, без доступа к
серверу. У бота есть роль:

  sales   — говорит с клиентами: отвечает, ведёт по сценарию, собирает лида
  manager — служебный: присылает менеджерам обращения с кнопками
            «Взять в работу» и «Передать менеджеру»

Подключение и отключение работают на живую: панель зовёт reload(), и бот
поднимается или гасится без перезапуска службы.
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
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
)

from .. import config, db

log = logging.getLogger("telegram")

# bot_id → живой объект. Ключ — идентификатор из таблицы bots.
BOTS: dict[int, Bot] = {}
DISPATCHERS: dict[int, Dispatcher] = {}
_polling: dict[int, asyncio.Task] = {}

MEDIA_FIELDS = [
    ("photo", "photo"), ("voice", "voice"), ("audio", "audio"),
    ("video", "video"), ("video_note", "video_note"),
    ("document", "document"), ("sticker", "sticker"), ("animation", "animation"),
]


# ── отправка ───────────────────────────────────────────────────────────

def _markup(button: tuple[str, str] | None) -> InlineKeyboardMarkup | None:
    if not button or not button[0] or not button[1]:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=button[0], url=button[1])]]
    )


async def send(chat_id: str, text: str, media_path: str | None = None,
               button: tuple[str, str] | None = None,
               bot_id: int | None = None,
               markup: InlineKeyboardMarkup | None = None,
               kind: str | None = None) -> tuple[bool, str]:
    """Отправить сообщение конкретным ботом.

    Без bot_id берём первого живого — так работают служебные уведомления,
    когда отдельный бот менеджера не заведён.
    """
    bot = BOTS.get(bot_id) if bot_id else None
    if bot is None:
        if not BOTS:
            return False, "no_bot"
        bot = next(iter(BOTS.values()))

    keyboard = markup or _markup(button)
    for _ in range(3):
        try:
            if media_path:
                from .base import media_file, media_kind
                path = media_file(media_path)
                if path.exists():
                    kind = kind or media_kind(media_path)
                    file = FSInputFile(str(path))
                    caption = text[:1024] or None
                    # у каждого типа свой метод: иначе голосовое приедет файлом
                    senders = {
                        "photo": bot.send_photo,
                        "voice": bot.send_voice,
                        "audio": bot.send_audio,
                        "video": bot.send_video,
                        "document": bot.send_document,
                    }
                    await senders.get(kind, bot.send_document)(
                        chat_id, file, caption=caption, reply_markup=keyboard
                    )
                    if len(text) > 1024:
                        await bot.send_message(chat_id, text[1024:])
                    return True, "sent"
            await bot.send_message(chat_id, text, reply_markup=keyboard)
            return True, "sent"
        except TelegramForbiddenError:
            return False, "blocked"
        except TelegramRetryAfter as exc:
            await asyncio.sleep(getattr(exc, "retry_after", 5) + 1)
        except TelegramBadRequest as exc:
            log.warning("не отправилось (%s): %s", chat_id, exc)
            return False, "error"
        except Exception as exc:  # noqa: BLE001 — один получатель не роняет цикл
            log.exception("ошибка отправки %s: %s", chat_id, exc)
            await asyncio.sleep(1)
    return False, "error"


async def _download(bot_id: int, file_id: str, hint: str) -> str | None:
    bot = BOTS.get(bot_id)
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


# ── кнопки менеджера ───────────────────────────────────────────────────

def request_markup(request_id: int, status: str = "new") -> InlineKeyboardMarkup:
    """Кнопки под уведомлением об обращении."""
    if status == "new":
        rows = [[
            InlineKeyboardButton(text="✅ Взять в работу", callback_data=f"take:{request_id}"),
            InlineKeyboardButton(text="👤 Передать", callback_data=f"pass:{request_id}"),
        ]]
    else:
        rows = [[
            InlineKeyboardButton(text="🔒 Закрыть", callback_data=f"close:{request_id}"),
            InlineKeyboardButton(text="🤖 Вернуть ИИ", callback_data=f"ai:{request_id}"),
        ]]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _manager_router() -> Router:
    """Служебный бот: только кнопки, клиентов он не обслуживает."""
    router = Router(name="manager")

    @router.callback_query(F.data.regexp(r"^(take|pass|close|ai):\d+$"))
    async def on_button(call: CallbackQuery) -> None:
        action, raw = (call.data or "").split(":", 1)
        request_id = int(raw)
        row = db.q1("SELECT * FROM requests WHERE id = ?", (request_id,))
        if row is None:
            await call.answer("Обращение не найдено")
            return

        who = call.from_user.full_name or call.from_user.username or "менеджер"
        contact = db.contact_by_id(row["contact_id"])
        name = (contact["name"] or contact["username"] or f"id{row['contact_id']}") if contact else "?"

        if action == "take":
            db.take_request(request_id, who)
            db.set_ai(row["contact_id"], False)
            text = f"✅ {name} — в работе у {who}"
            await call.answer("Взяли в работу")
        elif action == "pass":
            db.run("UPDATE requests SET status = 'new', manager = NULL WHERE id = ?", (request_id,))
            text = f"👤 {name} — свободно, нужен менеджер"
            await call.answer("Вернули в очередь")
        elif action == "close":
            db.close_request(request_id)
            text = f"🔒 {name} — обращение закрыто ({who})"
            await call.answer("Закрыто")
        else:
            from ..sales import return_to_ai
            return_to_ai(row["contact_id"])
            db.close_request(request_id)
            text = f"🤖 {name} — диалог вернули ИИ ({who})"
            await call.answer("Вернули ИИ")

        link = f"{config.PUBLIC_URL}/dialogs?c={row['contact_id']}"
        fresh = db.q1("SELECT status FROM requests WHERE id = ?", (request_id,))
        status = fresh["status"] if fresh else "closed"
        try:
            await call.message.edit_text(
                f"{text}\n<a href=\"{link}\">Открыть диалог</a>",
                reply_markup=request_markup(request_id, status) if status != "closed" else None,
            )
        except TelegramBadRequest:
            pass

    @router.message(CommandStart())
    async def on_start(message: Message) -> None:
        await message.answer(
            "Это служебный бот для менеджеров.\n"
            f"Chat ID этого чата: <code>{message.chat.id}</code>\n"
            "Впишите его в Настройки → Chat ID для уведомлений."
        )

    return router


# ── бот-продавец ───────────────────────────────────────────────────────

def _sales_router(bot_id: int) -> Router:
    router = Router(name=f"sales_{bot_id}")

    @router.message(CommandStart())
    async def on_start(message: Message) -> None:
        user = message.from_user
        if user is None:
            return
        contact = db.upsert_contact(
            "tg", user.id, user.username,
            " ".join(filter(None, [user.first_name, user.last_name])) or None,
            bot_id=bot_id,
        )
        # /start — это и подписка на рассылки, и сброс сценария в начало
        db.run(
            "UPDATE contacts SET opted_in = 1, blocked = 0, step = 0 WHERE id = ?",
            (contact["id"],),
        )
        row = db.bot(bot_id)
        greeting = (row["greeting"] if row and row["greeting"] else
                    db.setting("greeting", "Здравствуйте!"))
        ok, _ = await send(str(user.id), greeting, bot_id=bot_id)
        if ok:
            db.add_message(contact["id"], "out", "ai", greeting, is_read=True)

    @router.message(Command("stop"))
    async def on_stop(message: Message) -> None:
        user = message.from_user
        if user is None:
            return
        contact = db.upsert_contact("tg", user.id, user.username,
                                    user.first_name, bot_id=bot_id)
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
            bot_id=bot_id,
        )
        kind, file_id, hint = _extract_media(message)
        media_path = await _download(bot_id, file_id, hint) if file_id else None

        from ..sales import handle_incoming
        await handle_incoming(
            contact["id"], message.text or message.caption or "", kind, media_path
        )

    return router


# ── жизненный цикл ─────────────────────────────────────────────────────

async def _spin_up(row) -> None:
    """Поднять одного бота: проверить токен и включить приём сообщений."""
    bot_id = row["id"]
    bot = Bot(row["token"], default=DefaultBotProperties(parse_mode=ParseMode.HTML))

    try:
        me = await bot.get_me()
    except Exception as exc:  # noqa: BLE001
        db.run("UPDATE bots SET last_error = ? WHERE id = ?", (str(exc)[:200], bot_id))
        log.error("бот %s не отвечает: %s", row["title"], exc)
        await bot.session.close()
        return

    db.run(
        "UPDATE bots SET username = ?, last_error = NULL WHERE id = ?",
        (me.username, bot_id),
    )

    dispatcher = Dispatcher()
    dispatcher.include_router(
        _manager_router() if row["role"] == "manager" else _sales_router(bot_id)
    )
    BOTS[bot_id] = bot
    DISPATCHERS[bot_id] = dispatcher

    if config.MODE == "webhook":
        url = f"{config.PUBLIC_URL}/hook/telegram/{bot_id}"
        try:
            await bot.set_webhook(url, drop_pending_updates=True,
                                  secret_token=config.WEBHOOK_SECRET)
            log.info("бот @%s: вебхук на %s", me.username, url)
        except Exception as exc:  # noqa: BLE001
            db.run("UPDATE bots SET last_error = ? WHERE id = ?", (str(exc)[:200], bot_id))
            log.error("вебхук не поставился: %s", exc)
        return

    # оставшийся от прошлой конфигурации вебхук не даст polling'у работать
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("delete_webhook: %s", exc)

    _polling[bot_id] = asyncio.create_task(
        dispatcher.start_polling(bot, handle_signals=False)
    )
    log.info("бот @%s (%s): polling запущен", me.username, row["role"])


async def _spin_down(bot_id: int) -> None:
    task = _polling.pop(bot_id, None)
    if task:
        task.cancel()
    bot = BOTS.pop(bot_id, None)
    DISPATCHERS.pop(bot_id, None)
    if bot:
        try:
            await bot.session.close()
        except Exception:  # noqa: BLE001
            pass


def _mine() -> list:
    """Только Telegram-боты: MAX поднимает свой модуль."""
    return [r for r in db.bots(only_enabled=True) if r["platform"] == "tg"]


async def start() -> None:
    """Поднять всех включённых Telegram-ботов из базы."""
    rows = _mine()
    if not rows:
        log.info("Telegram-ботов нет — добавьте их в панели, раздел «Боты»")
    for row in rows:
        await _spin_up(row)


async def reload() -> None:
    """Привести живых ботов в соответствие с базой. Зовётся из панели."""
    wanted = {row["id"]: row for row in _mine()}

    for bot_id in list(BOTS):
        if bot_id not in wanted:
            await _spin_down(bot_id)

    for bot_id, row in wanted.items():
        if bot_id not in BOTS:
            await _spin_up(row)


async def check_token(token: str) -> dict:
    """Проверить токен до сохранения: жив ли и чей он."""
    probe = Bot(token.strip())
    try:
        me = await probe.get_me()
        return {"ok": True, "username": me.username, "name": me.full_name}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}
    finally:
        await probe.session.close()


async def feed(bot_id: int, payload: dict) -> None:
    """Скормить апдейт из вебхука нужному боту."""
    bot = BOTS.get(bot_id)
    dispatcher = DISPATCHERS.get(bot_id)
    if not bot or not dispatcher:
        return
    await dispatcher.feed_update(bot, Update.model_validate(payload, context={"bot": bot}))


async def stop() -> None:
    for bot_id in list(BOTS):
        await _spin_down(bot_id)
