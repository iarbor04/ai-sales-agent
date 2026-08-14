"""Самопроверка проекта. Запускать после любой правки, до деплоя.

    .venv/bin/python selftest.py

Проверяет то, что чаще всего ломается при правках: синтаксис, шаблоны,
маршруты и главные инварианты поведения — что контакт не двоится, лид не
двоится, рассылка не уходит дважды, «стоп» выключает ИИ, а на вопрос вне
базы знаний агент зовёт человека.

Сеть не нужна, боты не нужны, боевую базу не трогает — работает на временной.
Возвращает код 0, если всё в порядке, и 1, если что-то сломано.
"""
from __future__ import annotations

import ast
import asyncio
import os
import pathlib
import sys
import tempfile

# работаем на временной базе, чтобы не задеть боевую
_tmp = tempfile.mkdtemp(prefix="selftest-")
os.environ["DB_PATH"] = str(pathlib.Path(_tmp) / "test.db")
os.environ["MEDIA_DIR"] = str(pathlib.Path(_tmp) / "media")
os.environ["OPENROUTER_API_KEY"] = ""
os.environ["TELEGRAM_BOT_TOKEN"] = ""

ROOT = pathlib.Path(__file__).resolve().parent
passed: list[str] = []
failed: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    (passed if condition else failed).append(name if condition else f"{name} — {detail}")
    print(f"  {'✓' if condition else '✗'} {name}" + (f"  ({detail})" if detail and not condition else ""))


def section(title: str) -> None:
    print(f"\n{title}")


def main() -> int:
    section("Синтаксис")
    bad = []
    for path in sorted(ROOT.rglob("*.py")):
        if ".venv" in path.parts:
            continue
        try:
            ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            bad.append(f"{path.name}:{exc.lineno}")
    check("все файлы python разбираются", not bad, ", ".join(bad))

    section("Приложение поднимается")
    try:
        from app import broadcast, db, knowledge, onboarding, retrieval, sales
        from app.channels import base
        from app.web.main import app, templates
        check("модули импортируются", True)
    except Exception as exc:  # noqa: BLE001
        check("модули импортируются", False, str(exc))
        return report()

    templates_dir = ROOT / "app" / "web" / "templates"
    broken = []
    for tpl in sorted(templates_dir.glob("*.html")):
        try:
            templates.env.get_template(tpl.name)
        except Exception as exc:  # noqa: BLE001
            broken.append(f"{tpl.name}: {exc}")
    check(f"шаблоны компилируются ({len(list(templates_dir.glob('*.html')))} шт.)",
          not broken, "; ".join(broken))

    routes = {r.path for r in app.routes if hasattr(r, "path")}
    expected = ["/", "/login", "/health", "/dialogs", "/requests", "/leads",
                "/knowledge", "/broadcast", "/bots", "/script", "/settings",
                "/onboarding", "/hook/whatsapp"]
    missing = [r for r in expected if r not in routes]
    check(f"маршруты на месте ({len(expected)} шт.)", not missing, ", ".join(missing))

    section("База данных")
    db.init()
    tables = {r["name"] for r in db.q(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    need = {"bots", "contacts", "messages", "leads", "requests", "script_steps",
            "kb_pages", "kb_chunks", "broadcasts", "broadcast_log", "settings"}
    check("схема создаётся", need <= tables, f"нет: {sorted(need - tables)}")

    db.init()  # повторный запуск не должен ломаться
    check("миграции идемпотентны", True)
    check("сценарий по умолчанию засеян", len(db.script()) >= 3,
          f"шагов: {len(db.script())}")
    check("шагов не больше пяти", len(db.script()) <= 5,
          f"шагов {len(db.script())} — каждый лишний вопрос роняет доходимость")

    section("Контакты и лиды")
    bot_id = db.add_bot("Тест", "111:TEST", "sales")
    a = db.upsert_contact("tg", "777", "ivan", "Иван", bot_id=bot_id)
    b = db.upsert_contact("tg", "777", "ivan", "Иван Петров", bot_id=bot_id)
    check("контакт не двоится", a["id"] == b["id"])
    check("имя обновляется", db.contact_by_id(a["id"])["name"] == "Иван Петров")

    other = db.add_bot("Второй", "222:TEST", "sales")
    c = db.upsert_contact("tg", "777", "ivan", "Иван", bot_id=other)
    check("разные боты — разные разговоры", c["id"] != a["id"])

    db.upsert_lead(a["id"], {"name": "Иван", "product": "Диван"})
    db.upsert_lead(a["id"], {"deadline": "к пятнице"})
    db.upsert_lead(a["id"], {"name": ""})
    lead = db.get_lead(a["id"])
    check("лид не двоится", db.q1("SELECT COUNT(*) c FROM leads")["c"] == 1)
    check("поля накапливаются", lead["product"] == "Диван" and lead["deadline"] == "к пятнице")
    check("пустое не затирает собранное", lead["name"] == "Иван")

    section("База знаний")
    page = db.run(
        "INSERT INTO kb_pages (url,title,text,chars,included,status,fetched_at)"
        " VALUES ('t://p','Прайс',?,?,1,'loaded',?)",
        ("Доставка по городу 500 рублей, срок один-два дня.", 48, db.now()))
    knowledge._rechunk(page, "Доставка по городу 500 рублей, срок один-два дня.")
    retrieval.invalidate()
    check("находит по смыслу", bool(retrieval.search("сколько стоит доставка")))
    check("не выдумывает лишнего", not retrieval.search("ремонт холодильников"))

    section("Вложения")
    kinds = {"a.jpg": "photo", "v.ogg": "voice", "s.mp3": "audio",
             "c.mp4": "video", "d.pdf": "document"}
    wrong = [f for f, k in kinds.items() if base.media_kind(f) != k]
    check("тип вложения определяется", not wrong, ", ".join(wrong))

    section("Поведение агента")

    async def behaviour() -> None:
        person = db.upsert_contact("tg", "900", "stop", "Стоп", bot_id=bot_id)
        await sales.handle_incoming(person["id"], "стоп")
        row = db.contact_by_id(person["id"])
        check("«стоп» выключает ИИ и подписку",
              not row["ai_enabled"] and not row["opted_in"])

        human = db.upsert_contact("tg", "901", "hum", "Человек", bot_id=bot_id)
        await sales.handle_incoming(human["id"], "позовите менеджера")
        row = db.contact_by_id(human["id"])
        req = db.q1("SELECT * FROM requests WHERE contact_id = ?", (human["id"],))
        check("просьба о человеке передаёт диалог", not row["ai_enabled"])
        check("заводится обращение", req is not None and req["status"] == "new")

        await sales.hand_off(human["id"], "ещё раз", silent=True)
        count = db.q1("SELECT COUNT(*) c FROM requests WHERE contact_id = ?",
                      (human["id"],))["c"]
        check("повтор не плодит обращения", count == 1, f"обращений {count}")

        db.take_request(req["id"], "Анна")
        check("обращение берётся в работу",
              db.q1("SELECT status FROM requests WHERE id=?", (req["id"],))["status"] == "in_work")

        sales.return_to_ai(human["id"])
        check("«Вернуть ИИ» включает агента и закрывает обращение",
              db.contact_by_id(human["id"])["ai_enabled"] and db.open_requests_count() == 0)

        # без ключа модели диалог обязан уходить человеку, а не молчать
        quiet = db.upsert_contact("tg", "902", "q", "Тихий", bot_id=bot_id)
        await sales.handle_incoming(quiet["id"], "сколько стоит?")
        check("без ключа модели зовёт менеджера",
              not db.contact_by_id(quiet["id"])["ai_enabled"])

        section("Рассылки")
        db.run("UPDATE contacts SET opted_in=1, blocked=0")
        bid = broadcast.create("Текст", None, "Кнопка", "https://x.ru", None)
        check("создаётся черновиком",
              db.q1("SELECT status FROM broadcasts WHERE id=?", (bid,))["status"] == "draft")
        before = len(broadcast.recipients(bid))
        broadcast.confirm(bid)
        await broadcast.send_broadcast(bid)
        await broadcast.send_broadcast(bid)
        logged = db.q1("SELECT COUNT(*) c FROM broadcast_log WHERE broadcast_id=?",
                       (bid,))["c"]
        check("повтор не создаёт дублей", logged == before,
              f"получателей {before}, записей {logged}")

    asyncio.run(behaviour())

    section("Шаблоны и конструктор сценария")
    keys = list(db.SCRIPT_TEMPLATES)
    check("шаблоны сценариев есть", len(keys) >= 3, f"шаблонов {len(keys)}")
    long_ones = [k for k, t in db.SCRIPT_TEMPLATES.items() if len(t["steps"]) > 5]
    check("в шаблонах не больше пяти шагов", not long_ones, ", ".join(long_ones))
    applied = db.apply_template(keys[0])
    check("шаблон применяется", applied == len(db.SCRIPT_TEMPLATES[keys[0]]["steps"]))
    ids = [s["id"] for s in db.script()]
    db.reorder_script(list(reversed(ids)))
    check("порядок шагов меняется", [s["id"] for s in db.script()] == list(reversed(ids)))

    section("Каналы")
    from app import config
    from app.channels import mail, vk
    need = {"tg", "wa", "max", "vk", "mail", "web", "avito"}
    check("все каналы названы", need <= set(config.CHANNEL_TITLES),
          f"нет: {sorted(need - set(config.CHANNEL_TITLES))}")

    vk_bot = db.add_bot("Сообщество", "vk-token", "sales", "vk",
                        '{"group_id": 42, "confirm": "abc123"}')
    row = db.bot(vk_bot)
    check("настройки ВК читаются", vk.settings(row).get("confirm") == "abc123")

    mail_bot = db.add_bot("Ящик", "app-password", "sales", "mail",
                          '{"login": "sales@example.com", "imap_host": "imap.example.com"}')
    check("настройки почты читаются",
          mail.settings(db.bot(mail_bot)).get("login") == "sales@example.com")
    check("цитата из письма отрезается",
          mail._trim_quote("Мой вопрос\n\n> старое письмо\n> ещё строка") == "Мой вопрос")
    check("тема письма расшифровывается",
          mail._decode("=?utf-8?B?0J/RgNC40LLQtdGC?=") == "Привет")

    from app.channels import avito
    parsed = avito._extract({"payload": {"type": "message", "value": {
        "chat_id": "c1", "author_id": 55, "content": {"text": "Ещё продаёте?"}}}})
    check("вебхук Авито разбирается",
          parsed and parsed["chat_id"] == "c1" and parsed["text"] == "Ещё продаёте?")
    check("не-сообщения от Авито пропускаются",
          avito._extract({"payload": {"type": "read", "value": {}}}) is None)
    bare = avito._extract({"chat_id": "c2", "author_id": 7, "type": "text",
                           "content": {"text": "без конверта"}})
    check("голая схема Авито тоже разбирается",
          bare and bare["text"] == "без конверта")

    from app.channels import maxru
    check("MAX ходит на живой хост", "botapi.max.ru" in maxru.API, maxru.API)
    check("MAX шлёт токен параметром", maxru._auth("t") == {"access_token": "t"})
    got = maxru._extract({"update_type": "message_created", "message": {
        "sender": {"user_id": 5, "first_name": "Пётр"},
        "recipient": {"chat_id": 99}, "body": {"text": "привет"}}})
    check("апдейт MAX разбирается по схеме SDK",
          got and got["chat_id"] == "99" and got["text"] == "привет"
          and got["name"] == "Пётр")

    from app import channels as ch
    check("каждая платформа знает свой модуль",
          all(ch._module(p) is not None for p in ch.EXTRA_PLATFORMS))

    section("Витрина каналов")
    from app.web.main import CHANNEL_CARDS
    codes = {c["code"] for c in CHANNEL_CARDS}
    check("карточка есть у каждого канала", codes == set(config.CHANNEL_TITLES),
          f"расходятся: {codes ^ set(config.CHANNEL_TITLES)}")
    check("у каждой карточки есть описание и метки",
          all(c["about"] and c["tags"] and c["link"] for c in CHANNEL_CARDS))
    base_tpl = (ROOT / "app/web/templates/base.html").read_text()
    missing_brand = [c for c in codes if f"'{c}'" not in base_tpl]
    check("у каждого канала есть значок", not missing_brand, ", ".join(missing_brand))

    section("Чат для сайта")
    from app.channels import web as webchat
    token = webchat.new_visitor()
    check("токен посетителя подписан", webchat.visitor_id(token) is not None)
    check("подделанный токен отвергается", webchat.visitor_id("подделка") is None)
    guest = webchat.contact_for(token)
    same = webchat.contact_for(token)
    check("посетитель не двоится", guest and same and guest["id"] == same["id"])
    check("чужой токен не даёт контакта", webchat.contact_for("nope") is None)
    db.add_message(guest["id"], "out", "ai", "Здравствуйте!", is_read=True)
    check("виджет забирает ответы", len(webchat.history_after(guest["id"], 0)) >= 1)
    check("сниппет содержит адрес", "widget.js" in webchat.snippet())

    section("Онлайн-запись")
    from app import booking as bk
    db.run("INSERT INTO services (title, duration_min, price) VALUES ('Стрижка', 40, '1500')")
    db.run("INSERT INTO staff (name) VALUES ('Ольга')")
    db.set_setting("booking_enabled", "1")
    check("часы работы засеяны", len(bk.hours()) >= 5, f"дней: {len(bk.hours())}")
    slots = bk.free_slots(limit=6)
    check("свободные окна считаются", len(slots) > 0, f"окон: {len(slots)}")
    if slots:
        step = (slots[1]["at"] - slots[0]["at"]) // 60 if len(slots) > 1 else 40
        check("шаг сетки равен длительности услуги", step == 40, f"шаг {step} мин")
        contact = db.upsert_contact("tg", "950", "bk", "Клиент", bot_id=bot_id)
        first = bk.book(contact["id"], slots[0]["label"])
        check("запись создаётся", first["ok"], str(first))
        again = bk.book(contact["id"], slots[0]["label"])
        check("занятое время повторно не бронируется", not again["ok"],
              "слот отдали дважды")
        check("запись видна в журнале", len(bk.upcoming()) == 1)
    check("блок для модели содержит услуги", "Стрижка" in bk.slots_for_prompt())

    section("Конкуренты")
    from app import rivals
    rid = db.add_rival("Тестовый конкурент", "https://example.com/price")
    check("конкурент добавляется", len(db.rivals()) == 1)
    db.add_rival("Он же", "https://example.com/price")
    check("дубль по адресу не создаётся", len(db.rivals()) == 1)
    diff = rivals._diff("Цена 100 руб\nДоставка бесплатно",
                        "Цена 150 руб\nДоставка бесплатно")
    check("разница по строкам считается", "150" in diff and "100" in diff, diff[:60])
    check("неизменное в разницу не попадает", "Доставка" not in diff)
    check("обход по расписанию определяется", rivals.due() is True)

    section("Онбординг")
    progress = onboarding.progress()
    check("чек-лист считается", progress["total"] > 0 and "steps" in progress)

    return report()


def report() -> int:
    print()
    if failed:
        print(f"СЛОМАНО: {len(failed)}")
        for item in failed:
            print(f"  ✗ {item}")
        print("\nДеплоить нельзя. Почини и запусти проверку заново.")
        return 1
    print(f"ВСЁ В ПОРЯДКЕ: проверок пройдено {len(passed)}. Можно деплоить.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
