"""Онбординг: что осталось сделать, чтобы продажник заработал.

Шаги проверяются по факту, а не по галочке «я прочитал»: пока бота нет в
реестре, шаг «подключить бота» не закроется, сколько ни нажимай. Это честнее
и снимает главный вопрос новичка — «я всё настроил или нет?».
"""
from __future__ import annotations

from . import config, db, knowledge, llm


def steps() -> list[dict]:
    kb = knowledge.stats()
    sales_bots = db.bots(role="sales", only_enabled=True)
    manager_bot = db.q1("SELECT 1 FROM bots WHERE role = 'manager' AND enabled = 1")
    contacts = db.q1("SELECT COUNT(*) AS c FROM contacts")["c"]
    replied = db.q1("SELECT COUNT(*) AS c FROM messages WHERE author = 'ai'")["c"]

    return [
        {
            "key": "bot",
            "title": "Подключить бота-продажника",
            "why": "Через него клиенты будут писать вам в Telegram.",
            "how": "Создайте бота у @BotFather командой /newbot, скопируйте токен "
                   "и вставьте его в разделе «Боты».",
            "link": "/bots",
            "action": "Открыть «Боты»",
            "done": bool(sales_bots),
            "state": f"подключено: {len(sales_bots)}" if sales_bots else "бота нет",
        },
        {
            "key": "model",
            "title": "Вставить ключ модели",
            "why": "Без ключа агент не может отвечать и будет звать менеджера "
                   "на каждое сообщение.",
            "how": "Зарегистрируйтесь на openrouter.ai, создайте ключ в разделе Keys "
                   "и вставьте его на вкладке «Модель». Там же выбирается модель.",
            "link": "/agent#model",
            "action": "Открыть «Модель»",
            "done": llm.ai_ready(),
            "state": f"модель: {llm.current_model()}" if llm.ai_ready() else "ключа нет",
        },
        {
            "key": "kb",
            "title": "Заполнить базу знаний",
            "why": "Агент отвечает только по ней. Пустая база — все диалоги уходят "
                   "менеджеру.",
            "how": "Загрузите прайс файлом (xlsx или csv), условия документом "
                   "(DOCX) или впишите главное руками. Можно добавить сайт компании — "
                   "страницы загрузятся сами.",
            "link": "/agent#kb",
            "action": "Открыть «База знаний»",
            "done": kb["loaded"] > 0,
            "state": (f"источников: {kb['loaded']}, фрагментов: {kb['chunks']}"
                      if kb["loaded"] else "пусто"),
        },
        {
            "key": "notify",
            "title": "Настроить уведомления менеджеру",
            "why": "Иначе о клиенте, которому нужен человек, никто не узнает.",
            "how": "Добавьте второго бота с ролью «менеджерский», напишите ему /start — "
                   "он пришлёт Chat ID. Вставьте этот ID на вкладке «Передача».",
            "link": "/agent#handoff",
            "action": "Открыть «Передача»",
            "done": bool(db.setting("operator_chat_id", "").strip()),
            "state": ("чат указан" if db.setting("operator_chat_id", "").strip()
                      else "чат не указан") + ("" if manager_bot else ", служебного бота нет"),
        },
        {
            "key": "script",
            "title": "Проверить сценарий продаж",
            "why": "По нему агент ведёт разговор и решает, что спрашивать дальше.",
            "how": "Четыре шага уже заведены — больше пяти вопросов задавать вредно, "
                   "клиенты уходят. Поправьте цели под свой бизнес и включите "
                   "сценарий у бота галочкой.",
            "link": "/agent#script",
            "action": "Открыть «Сценарий»",
            "done": any(b["script_enabled"] for b in sales_bots),
            "state": ("сценарий включён" if any(b["script_enabled"] for b in sales_bots)
                      else "бот отвечает свободно"),
            "optional": True,
        },
        {
            "key": "test",
            "title": "Написать боту и проверить",
            "why": "Последняя проверка: агент отвечает, диалог виден, лид заводится.",
            "how": "Откройте своего бота в Telegram, отправьте /start и задайте "
                   "пару вопросов как клиент. Диалог появится в разделе «Диалоги».",
            "link": "/dialogs",
            "action": "Открыть «Диалоги»",
            "done": contacts > 0 and replied > 0,
            "state": (f"диалогов: {contacts}, ответов агента: {replied}"
                      if contacts else "ещё никто не писал"),
        },
    ]


def progress() -> dict:
    items = steps()
    required = [s for s in items if not s.get("optional")]
    done = [s for s in required if s["done"]]
    return {
        "steps": items,
        "total": len(required),
        "done": len(done),
        "ready": len(done) == len(required),
        "next": next((s for s in items if not s["done"]), None),
    }
