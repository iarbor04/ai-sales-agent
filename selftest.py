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
import time

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
                "/settings/ai/check", "/knowledge/file", "/onboarding",
                "/hook/whatsapp"]
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

    section("Этапы воронки")
    stage_ids = [row["id"] for row in db.pipeline_stages()]
    check("этапы засеяны при первом запуске", stage_ids == ["new", "qualifying", "handed", "won"],
          ", ".join(stage_ids))
    check("этап передачи менеджеру найден", db.system_stage() == "handed")
    check("финальный этап исключается из рассылок", db.won_stages() == {"won"})

    moved = db.save_pipeline_stages([
        {"id": "new", "title": "Входящие", "color": "blue"},
        {"id": "demo", "title": "Демо", "color": "cyan"},
        {"id": "handed", "title": "У менеджера", "color": "amber", "is_system": True},
        {"id": "paid", "title": "Оплачено", "color": "green", "is_won": True},
    ])
    check("свой набор этапов сохраняется",
          [row["title"] for row in db.pipeline_stages()] == ["Входящие", "Демо", "У менеджера", "Оплачено"])
    check("подписи этапов уходят в промпт модели",
          "Демо" in ", ".join(db.stage_titles().values()))

    for bad, why in (
        ([{"id": "one", "title": "Без передачи"}], "нет этапа передачи"),
        ([{"id": "a", "title": "A", "is_system": True}, {"id": "a", "title": "B"}], "повтор id"),
        ([{"id": "ok", "title": "", "is_system": True}], "пустое название"),
        ([{"id": "Плохой", "title": "Кириллица", "is_system": True}], "id не латиницей"),
    ):
        try:
            db.save_pipeline_stages(bad)
            outcome = f"принят кривой набор ({why})"
        except ValueError:
            outcome = ""
        check(f"кривой набор не сохраняется — {why}", not outcome, outcome)
    check("после отказа этапы остались прежними",
          [row["id"] for row in db.pipeline_stages()] == ["new", "demo", "handed", "paid"])

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

    orphan = db.upsert_contact("tg", "960", "orphan", "Сирота")
    db.upsert_lead(orphan["id"], {"product": "Шляпа"}, status="demo")
    moved = db.save_pipeline_stages([
        {"id": "new", "title": "Входящие", "color": "blue"},
        {"id": "handed", "title": "У менеджера", "color": "amber", "is_system": True},
    ])
    check("удаление этапа переносит его лидов на первый",
          moved >= 1 and db.get_lead(orphan["id"])["status"] == "new", f"перенесено {moved}")
    db.save_pipeline_stages([{"id": stage, "title": title, "color": "gray",
                              "is_system": stage == "handed", "is_won": stage == "won"}
                             for stage, title in (("new", "Новый лид"), ("qualifying", "Квалификация"),
                                                  ("handed", "Передан менеджеру"), ("won", "Сделка"))])

    section("База знаний")
    page = db.run(
        "INSERT INTO kb_pages (url,title,text,chars,included,status,fetched_at)"
        " VALUES ('https://example.com/price','Прайс',?,?,1,'loaded',?)",
        ("Доставка по городу 500 рублей, срок один-два дня.", 48, db.now()))
    knowledge._rechunk(page, "Доставка по городу 500 рублей, срок один-два дня.")
    retrieval.invalidate()
    check("находит по смыслу", bool(retrieval.search("сколько стоит доставка")))
    check("не выдумывает лишнего", not retrieval.search("ремонт холодильников"))

    section("Обновление базы знаний")
    # источник, загруженный один раз и забытый, начинает врать: клиент
    # поменяет цену на сайте, а агент будет отвечать по старой копии
    check("есть перечитывание источников", hasattr(knowledge, "refresh"))
    check("перечитывание идёт по расписанию", hasattr(knowledge, "refresh_due"))
    db.set_setting("kb_refresh_hours", "24")
    db.set_setting("kb_last_refresh", str(db.now()))
    check("только что проверенное не перечитывается", not knowledge.refresh_due())
    db.set_setting("kb_last_refresh", str(db.now() - 25 * 3600))
    check("просроченное перечитывается", knowledge.refresh_due())
    db.set_setting("kb_refresh_hours", "0")
    check("ноль часов выключает перечитывание", not knowledge.refresh_due())

    section("Самопроверка службы")

    async def health() -> None:
        from app import healthcheck
        from app.web import main as web

        state = await healthcheck.run()
        problems = " | ".join(state["problems"])
        check("проверка называет незаполненное",
              any("ключ модели" in p for p in state["problems"])
              and any("чат менеджера" in p for p in state["problems"]), problems)

        db.run("UPDATE bots SET enabled = 0")
        without = await healthcheck.run()
        check("без ботов проверка говорит, что клиентам некуда писать",
              any("ни один бот" in p for p in without["problems"]),
              " | ".join(without["problems"]))
        db.run("UPDATE bots SET enabled = 1")

        fresh = await healthcheck.run()
        check("результат сохраняется для панели",
              healthcheck.last().get("problems") == fresh["problems"],
              f"в базе {healthcheck.last().get('problems')}")

        # запуск не из службы не должен объявлять живых ботов мёртвыми
        db.add_bot("Живой", "111:AA", role="sales")
        db.set_setting("service_pid", "-1")
        outside = await healthcheck.run()
        check("вне службы проверка не хоронит ботов",
              not any("не на связи" in p for p in outside["problems"])
              and outside["full"] is False, " | ".join(outside["problems"]))
        db.run("DELETE FROM bots WHERE title = 'Живой'")

        # уведомление уходит только при изменении набора проблем
        alerts = []

        async def fake_alert(items):
            alerts.append(list(items))

        original = healthcheck.notify.health_alert
        healthcheck.notify.health_alert = fake_alert
        db.set_setting(healthcheck.STATE_KEY, "")  # свежая установка: прошлого состояния нет
        try:
            await healthcheck.run_and_alert()
            first = len(alerts)
            await healthcheck.run_and_alert()
            check("о поломке пишут один раз, а не каждый час",
                  first == 1 and len(alerts) == 1, f"уведомлений {len(alerts)}")
        finally:
            healthcheck.notify.health_alert = original

        check("в настройках перечислены все каналы, а не два",
              "{% for item in connections %}" in
              (ROOT / "app/web/templates/settings.html").read_text(encoding="utf-8"))
        check("страница настроек знает про самопроверку",
              "/settings/health" in {r.path for r in web.app.routes if hasattr(r, "path")})

    asyncio.run(health())

    section("Сайт из настроек")

    async def site_reading() -> None:
        from app import knowledge as kb
        from app.web import main as web

        calls = {}
        original_discover, original_fetch = kb.discover, kb.fetch_pending
        def fake_discover(site, max_pages=None):
            calls["site"] = site
            return {"found": 3}

        kb.discover = fake_discover
        kb.fetch_pending = lambda: {"loaded": 3}
        try:
            result = await web.read_site("example.com")
        finally:
            kb.discover, kb.fetch_pending = original_discover, original_fetch
        check("сохранение сайта запускает обход и загрузку",
              calls.get("site") == "example.com" and result["loaded"] == 3, str(result))

        # недоступный сайт не должен ронять фоновую задачу
        def boom(site, max_pages=None):
            raise OSError("сайт недоступен")

        kb.discover = boom
        try:
            result = await web.read_site("example.com")
        finally:
            kb.discover = original_discover
        check("недоступный сайт не роняет службу", result["loaded"] == 0 and "error" in result)

    asyncio.run(site_reading())
    check("под полем сайта написано, что будет дальше",
          "обойдёт страницы" in (ROOT / "app/web/templates/settings.html").read_text(encoding="utf-8"))

    section("Нет мёртвого кода")
    tables_now = {r["name"] for r in db.q(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    check("очереди-пустышки retry_queue больше нет", "retry_queue" not in tables_now)
    sched = (ROOT / "app/scheduler.py").read_text()
    check("планировщик её не зовёт", "retry_queue" not in sched)
    vk_src = (ROOT / "app/channels/vk.py").read_text()
    check("ВК грузит не только картинки",
          "docs.getMessagesUploadServer" in vk_src and "audio_message" in vk_src)

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

        # пустое поле = тишина для клиента; оператор должен видеть это в диалоге
        db.set_setting("handoff_note", "")
        mute = db.upsert_contact("tg", "907", "mute", "Тишина", bot_id=bot_id)
        await sales.hand_off(mute["id"], "проверка тишины")
        note_row = db.q1("SELECT text FROM messages WHERE contact_id = ? AND author = 'system'"
                         " ORDER BY id DESC", (mute["id"],))
        check("при пустой фразе передачи в диалоге видно, что клиент молчит",
              note_row is not None and "клиент ничего не получил" in note_row["text"],
              note_row["text"] if note_row else "нет системной записи")
        db.set_setting("handoff_note", "Передаю вас менеджеру, он ответит здесь же.")
        talk = db.upsert_contact("tg", "908", "talk", "Ответ", bot_id=bot_id)
        await sales.hand_off(talk["id"], "проверка фразы")
        note_row = db.q1("SELECT text FROM messages WHERE contact_id = ? AND author = 'system'"
                         " ORDER BY id DESC", (talk["id"],))
        check("с заполненной фразой пометки о тишине нет",
              note_row is not None and "клиент ничего не получил" not in note_row["text"])
        # обращения этих двух проверок закрываем: следующая проверка считает открытые
        for row in db.q("SELECT id FROM requests WHERE contact_id IN (?, ?)",
                        (mute["id"], talk["id"])):
            db.close_request(row["id"])

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
        # мультиязычность: у контакта появился язык, тексты хранятся по языкам
        db.set_setting("handoff_note", "Передаю вас менеджеру, он ответит здесь же.")
        check("язык приводится к короткому коду",
              db.normalize_language("ru-RU") == "ru" and db.normalize_language("") is None
              and db.normalize_language("кириллица") is None)
        english = db.upsert_contact("tg", "970", "eng", "John Smith",
                                    bot_id=bot_id, language="en-GB")
        check("язык контакта сохраняется", db.contact_by_id(english["id"])["language"] == "en")

        multi = broadcast.create("Привет", None, "", "", None,
                                 texts={"ru": "Привет, {{first_name}}", "en": "Hi, {{first_name}}"},
                                 buttons=[{"text": "Открыть", "url": "https://ascn.ai"},
                                          {"text": "Прайс", "url": "https://ascn.ai/price"}])
        row = db.q1("SELECT * FROM broadcasts WHERE id = ?", (multi,))
        check("англичанину уходит английский текст",
              broadcast.text_for(row, "en-GB") == "Hi, {{first_name}}")
        check("без перевода уходит русский",
              broadcast.text_for(row, "de") == "Привет, {{first_name}}"
              and broadcast.text_for(row, None) == "Привет, {{first_name}}")
        check("кнопок сохраняется несколько", len(broadcast.buttons_of(row)) == 2)
        check("имя подставляется и экранируется",
              broadcast.personalize("Привет, {{first_name}}",
                                    db.upsert_contact("tg", "971", None, "<b>Оля", bot_id=bot_id))
              == "Привет, &lt;b&gt;Оля")

        # фильтр по этапу и защита финального этапа
        picked = db.upsert_contact("tg", "972", "stage", "Этапный", bot_id=bot_id)
        db.run("UPDATE contacts SET opted_in = 1, blocked = 0")
        db.upsert_lead(picked["id"], {"product": "Шляпа"}, status="qualifying")
        winner = db.upsert_contact("tg", "973", "won", "Купил", bot_id=bot_id)
        db.upsert_lead(winner["id"], {"product": "Кепка"}, status="won")
        staged = broadcast.create("Только квалификация", None, "", "", None,
                                  texts={"ru": "Только квалификация"}, stage_filter="qualifying")
        ids = {row["id"] for row in broadcast.recipients(staged)}
        qualifying = {row["contact_id"] for row in
                      db.q("SELECT contact_id FROM leads WHERE status = 'qualifying'")}
        check("фильтр по этапу оставляет только своих",
              picked["id"] in ids and ids <= qualifying,
              f"получателей {len(ids)}, из них не на этапе: {len(ids - qualifying)}")
        everyone = broadcast.create("Всем", None, "", "", None, texts={"ru": "Всем"})
        all_ids = [row["id"] for row in broadcast.recipients(everyone)]
        check("клиент с финального этапа в рассылку не попадает",
              winner["id"] not in all_ids and picked["id"] in all_ids)

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

    section("Провайдеры модели")

    def stub_server(handler):
        """Локальная заглушка провайдера: отдаёт то, что вернёт handler(path, body)."""
        import http.server
        import json as _json
        import threading

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self):
                length = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(length).decode() if length else ""
                code, body = handler(self.path, raw)
                payload = _json.dumps(body).encode()
                self.send_response(code)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_POST = _reply
            do_GET = _reply

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    async def model_providers() -> None:
        from app import llm, providers
        from app.providers import gigachat, yandex

        check("провайдеров трое", {p.NAME for p in providers.ALL}
              == {"openrouter", "yandex", "gigachat"})
        check("неизвестное имя не роняет панель", providers.get("что-то").NAME == "openrouter")

        # ── YandexGPT: свой формат запроса и ответа ──
        seen = {}

        def yandex_handler(path, raw):
            import json as _json
            seen["body"] = _json.loads(raw or "{}")
            return 200, {"result": {"alternatives": [
                {"message": {"role": "assistant", "text": "Ок"},
                 "status": "ALTERNATIVE_STATUS_FINAL"}]}}

        server = stub_server(yandex_handler)
        original = yandex.API_URL
        yandex.API_URL = f"http://127.0.0.1:{server.server_port}/completion"
        db.set_setting("model_provider", "yandex")
        db.set_setting("yandex_api_key", "AQVN-test")
        db.set_setting("yandex_folder_id", "b1gtest")
        db.set_setting("model", "yandexgpt-lite/latest")
        try:
            check("YandexGPT настроен и выбран",
                  llm.ai_ready() and llm.provider().NAME == "yandex")
            text = await llm._call("система", "вопрос", max_tokens=100)
            check("YandexGPT отвечает через общий вызов", text == "Ок", text)
            check("каталог подставляется в modelUri",
                  seen["body"]["modelUri"] == "gpt://b1gtest/yandexgpt-lite/latest",
                  seen["body"].get("modelUri"))
            check("сообщения уходят в формате Яндекса",
                  seen["body"]["messages"][0].get("text") == "система", str(seen["body"])[:80])

            # обрезку Яндекс сообщает статусом, а не finish_reason
            server.shutdown()
            server = stub_server(lambda path, raw: (200, {"result": {"alternatives": [
                {"message": {"text": "начало"}, "status": "ALTERNATIVE_STATUS_TRUNCATED_FINAL"}]}}))
            yandex.API_URL = f"http://127.0.0.1:{server.server_port}/completion"
            try:
                await llm._call("система", "вопрос", max_tokens=10)
                outcome = "обрезку не заметили"
            except llm.LLMTruncated:
                outcome = ""
            check("обрезанный ответ Яндекса распознан", not outcome, outcome)

            # без каталога запрос собрать нельзя — говорим об этом прямо
            db.set_setting("yandex_folder_id", "")
            try:
                await llm._call("система", "вопрос")
                outcome = "запрос ушёл без каталога"
            except llm.LLMError as exc:
                outcome = "" if "каталог" in str(exc) else str(exc)
            check("без каталога Яндекс объясняет, чего не хватает", not outcome, outcome)
        finally:
            server.shutdown()
            yandex.API_URL = original
            db.set_setting("yandex_folder_id", "b1gtest")

        # ── GigaChat: токен на полчаса, обновление при 401 ──
        calls = {"oauth": 0, "chat": 0}

        def giga_handler(path, raw):
            if "oauth" in path:
                calls["oauth"] += 1
                return 200, {"access_token": f"token-{calls['oauth']}",
                             "expires_at": int((time.time() + 1800) * 1000)}
            calls["chat"] += 1
            if calls["chat"] == 2:  # имитируем истёкший раньше срока токен
                return 401, {"message": "Unauthorized"}
            return 200, {"choices": [{"finish_reason": "stop",
                                      "message": {"content": "Готово"}}]}

        server = stub_server(giga_handler)
        oauth_url, api_url = gigachat.OAUTH_URL, gigachat.API_URL
        gigachat.OAUTH_URL = f"http://127.0.0.1:{server.server_port}/api/v2/oauth"
        gigachat.API_URL = f"http://127.0.0.1:{server.server_port}/chat/completions"
        db.set_setting("model_provider", "gigachat")
        db.set_setting("gigachat_client_id", "id-test")
        db.set_setting("gigachat_client_secret", "secret-test")
        db.set_setting("model", "GigaChat")
        gigachat.forget_token()
        try:
            check("GigaChat настроен и выбран",
                  llm.ai_ready() and llm.provider().NAME == "gigachat")
            first = await llm._call("система", "вопрос", max_tokens=100)
            check("GigaChat отвечает через общий вызов", first == "Готово", first)
            check("токен берётся один раз", calls["oauth"] == 1, f"запросов токена {calls['oauth']}")
            second = await llm._call("система", "вопрос", max_tokens=100)
            check("на 401 токен обновляется и запрос повторяется",
                  second == "Готово" and calls["oauth"] == 2,
                  f"токенов {calls['oauth']}, чатов {calls['chat']}")
            checked = await gigachat.check_credentials()
            check("проверка пары доступа проходит", checked["ok"], str(checked))
        finally:
            server.shutdown()
            gigachat.OAUTH_URL, gigachat.API_URL = oauth_url, api_url
            gigachat.forget_token()

        # смена провайдера меняет модель на его собственную
        from app.web import main as web
        db.set_setting("model_provider", "openrouter")
        db.set_setting("model", "GigaChat")
        db.set_setting("model_provider", "yandex")
        db.set_setting("model", providers.get("yandex").DEFAULT_MODEL)
        check("у каждого провайдера своя модель по умолчанию",
              providers.get("yandex").DEFAULT_MODEL != providers.get("openrouter").DEFAULT_MODEL
              != providers.get("gigachat").DEFAULT_MODEL)
        check("панель знает про поля всех провайдеров",
              all(item["fields"] for item in providers.options())
              and any(f["key"] == "yandex_folder_id" for f in providers.options()[1]["fields"]))

        db.set_setting("model_provider", "openrouter")
        db.set_setting("model", "openai/gpt-4o-mini")
        db.set_setting("yandex_api_key", "")
        db.set_setting("gigachat_client_id", "")
        db.set_setting("gigachat_client_secret", "")

    asyncio.run(model_providers())

    section("Отказ модели объясняется")
    import httpx

    from app import llm

    from app.providers import base as provider_base

    def failure(status, detail="", model="gpt"):
        return provider_base.human_error("OpenRouter", status, detail, model)

    check("401 говорит про ключ",
          "401" in failure(401) and "ключ" in failure(401).lower())
    check("ответ провайдера попадает в текст",
          "User not found" in failure(401, "User not found"))
    check("402 говорит про счёт", "средств" in failure(402))
    check("404 называет модель", "«gpt»" in failure(404))
    check("429 говорит про частоту", "429" in failure(429))
    check("сообщение об ошибке читается из тела ответа",
          provider_base.error_text(
              httpx.Response(401, json={"error": {"message": "User not found"}}))
          == "User not found")

    from app.web import main as web

    check("пробелы и переносы в ключе убираются",
          web._clean_key(" sk-or-v1-abc\n def ") == "sk-or-v1-abcdef")
    check("пароль от панели не принимается за ключ", not web._key_looks_real("admin123"))
    check("настоящий ключ проходит проверку",
          web._key_looks_real("sk-or-v1-0123456789abcdef"))
    check("сообщения из адреса выводятся на странице",
          "query_params.get('error')" in (ROOT / "app/web/templates/base.html").read_text(encoding="utf-8"))

    # Рассуждающая модель (gpt-5) тратила на размышления тот же лимит, что и на
    # ответ: JSON обрывался, и владелец видел «ответила не по формату».
    def truncating_server(script: list[dict]):
        """Локальная заглушка OpenRouter. Отдаёт ответы по списку, по одному на запрос."""
        import http.server
        import json as _json
        import threading

        state = {"n": 0}

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers.get("content-length") or 0))
                body = script[min(state["n"], len(script) - 1)]
                state["n"] += 1
                raw = _json.dumps(body).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server, state

    def reply_body(content: str, finish: str = "stop") -> dict:
        return {"choices": [{"finish_reason": finish, "message": {"content": content}}]}

    async def truncation() -> None:
        db.set_setting("openrouter_key", "sk-or-v1-selftest-0000000000")
        db.set_setting("model", "openai/gpt-5")
        cut = '{"reply": "У нас есть панама «Havana», цена 6720'
        whole = '{"reply": "У нас есть панама «Havana», 6720 ₽.", "handoff": false, "fields": {}}'
        server, state = truncating_server([reply_body(cut, "length"), reply_body(whole)])
        from app.providers import openrouter
        original_url = openrouter.API_URL
        openrouter.API_URL = f"http://127.0.0.1:{server.server_port}/chat"
        try:
            person = db.upsert_contact("tg", "906", "cut", "Обрыв", bot_id=bot_id)
            result = await llm.answer(person["id"], "что у вас есть")
            check("обрезанный лимитом ответ повторяется с большим запасом",
                  state["n"] == 2 and not result["handoff"], f"запросов {state['n']}, {result}")
            check("со второй попытки клиент получает ответ",
                  "панама" in result["reply"].lower(), result["reply"])

            # обрыв дважды — причина должна называть лимит, а не формат
            server.shutdown()
            server, state = truncating_server([reply_body(cut, "length")])
            openrouter.API_URL = f"http://127.0.0.1:{server.server_port}/chat"
            result = await llm.answer(person["id"], "что у вас есть")
            check("если обрыв повторился, причина называет лимит токенов",
                  result["handoff"] and "лимит" in result["handoff_reason"],
                  result["handoff_reason"])

            # резюме — обычный текст, обрезанное полезнее пустого
            db.add_message(person["id"], "in", "client", "что у вас есть")
            summary = await llm.summarize(person["id"])
            check("обрезанное резюме не выбрасывается", summary.startswith("{\"reply\""), repr(summary))
        finally:
            openrouter.API_URL = original_url
            server.shutdown()
            db.set_setting("openrouter_key", "")
            db.set_setting("model", "openai/gpt-4o-mini")

    asyncio.run(truncation())

    async def model_failures() -> None:
        # ключ есть, но OpenRouter его не принимает — самый частый случай
        db.set_setting("openrouter_key", "sk-or-v1-selftest-0000000000")
        original = llm._call

        async def refuse(system: str, user: str, max_tokens: int = 0,
                         strict: bool = True) -> str:
            raise llm.LLMError("OpenRouter отклонил ключ (401): User not found")

        person = db.upsert_contact("tg", "903", "err", "Отказ", bot_id=bot_id)
        llm._call = refuse
        try:
            await sales.handle_incoming(person["id"], "сколько стоит доставка?")
        finally:
            llm._call = original
        row = db.q1("SELECT reason FROM requests WHERE contact_id = ? ORDER BY id DESC",
                    (person["id"],))
        check("причина отказа доходит до менеджера",
              row is not None and "401" in (row["reason"] or ""),
              f"в обращении: {row['reason'] if row else 'обращения нет'}")

        async def junk(system: str, user: str, max_tokens: int = 0,
                       strict: bool = True) -> str:
            return "Здравствуйте! Отвечаю обычным текстом вместо JSON."

        other = db.upsert_contact("tg", "904", "junk", "Мусор", bot_id=bot_id)
        llm._call = junk
        try:
            await sales.handle_incoming(other["id"], "а рассрочка есть?")
        finally:
            llm._call = original
        row = db.q1("SELECT reason FROM requests WHERE contact_id = ? ORDER BY id DESC",
                    (other["id"],))
        check("ответ не по формату отличается от отказа сети",
              row is not None and "формат" in (row["reason"] or ""),
              f"в обращении: {row['reason'] if row else 'обращения нет'}")

        # слабая модель, которая с первого раза забыла про JSON, получает
        # второй шанс — иначе диалог уходит человеку без причины
        calls = []

        async def second_try(system: str, user: str, max_tokens: int = 0,
                             strict: bool = True) -> str:
            calls.append(system)
            if len(calls) == 1:
                return "Конечно, расскажу!"
            return '{"reply": "Панамы есть в наличии.", "handoff": false, "fields": {}}'

        retry = db.upsert_contact("tg", "905", "retry", "Повтор", bot_id=bot_id)
        llm._call = second_try
        try:
            result = await llm.answer(retry["id"], "панамы есть?")
        finally:
            llm._call = original
        check("после ответа не по формату модель просят повторить",
              len(calls) == 2 and llm.JSON_REMINDER in calls[1],
              f"вызовов {len(calls)}")
        check("со второй попытки ответ доходит до клиента",
              result["reply"] == "Панамы есть в наличии." and not result["handoff"],
              str(result))

        # резюме не должно ронять передачу диалога
        llm._call = refuse
        try:
            summary = await llm.summarize(person["id"])
        finally:
            llm._call = original
        check("резюме при отказе модели не падает", summary == "")
        db.set_setting("openrouter_key", "")

    asyncio.run(model_failures())

    section("Прайс файлом")
    from app import pricefile as pricefile_mod

    check("закупочная цена считается внутренним столбцом",
          pricefile_mod.internal_column("Закупка, ₽"))
    check("наценка считается внутренним столбцом",
          pricefile_mod.internal_column("Наценка, %"))
    check("розничная цена остаётся видимой",
          not pricefile_mod.internal_column("Розница, ₽"))
    check("название модели остаётся видимым",
          not pricefile_mod.internal_column("Модель"))
    check("оптовая цена скрывается", pricefile_mod.internal_column("Оптовая цена"))
    check("похожее слово не прячет столбец зря",
          not pricefile_mod.internal_column("Оптимальный размер"))

    from app import pricefile

    def make_xlsx(rows: list[list[str]]) -> bytes:
        """Собрать xlsx так же, как это делает Excel: строки в sharedStrings."""
        import io as _io
        import zipfile as _zip
        from xml.sax.saxutils import escape

        strings, order = {}, []
        for row in rows:
            for value in row:
                if value and not value.replace(".", "", 1).isdigit() and value not in strings:
                    strings[value] = len(order)
                    order.append(value)

        body = []
        for number, row in enumerate(rows, start=1):
            cells = []
            for position, value in enumerate(row):
                if value == "":
                    continue
                ref = f"{chr(65 + position)}{number}"
                if value in strings:
                    cells.append(f'<c r="{ref}" t="s"><v>{strings[value]}</v></c>')
                else:
                    cells.append(f'<c r="{ref}"><v>{value}</v></c>')
            body.append(f'<row r="{number}">{"".join(cells)}</row>')

        ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
        rel_ns = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
        buffer = _io.BytesIO()
        with _zip.ZipFile(buffer, "w") as archive:
            archive.writestr("xl/workbook.xml",
                             f'<workbook {ns} {rel_ns}><sheets>'
                             f'<sheet name="Прайс" sheetId="1" r:id="rId1"/></sheets></workbook>')
            archive.writestr("xl/_rels/workbook.xml.rels",
                             '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                             '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>')
            archive.writestr("xl/sharedStrings.xml",
                             f'<sst {ns}>' + "".join(f"<si><t>{escape(v)}</t></si>" for v in order) + "</sst>")
            archive.writestr("xl/worksheets/sheet1.xml",
                             f'<worksheet {ns}><sheetData>{"".join(body)}</sheetData></worksheet>')
        return buffer.getvalue()

    price = make_xlsx([
        ["Модель", "Материал", "Сезон", "Розница, ₽", "Закупка, ₽"],
        ["Панама «Havana»", "Соломка", "Лето", "6720", "3200"],
        ["Бини «Warm»", "", "Зима", "1050", "350"],
    ])
    rows = pricefile.read_rows("прайс.xlsx", price)
    check("xlsx читается без сторонних библиотек", len(rows) == 2, f"строк {len(rows)}")
    check("цена не превращается в 6720.0",
          rows[0].get("Розница, ₽") == "6720", str(rows[0]))
    check("пустая ячейка не сдвигает столбцы",
          rows[1].get("Розница, ₽") == "1050", str(rows[1]))

    csv_rows = pricefile.read_rows("прайс.csv", "Модель;Розница\nКепка;1100\n".encode("cp1251"))
    check("csv с точкой с запятой и кириллицей читается",
          csv_rows and csv_rows[0].get("Розница") == "1100", str(csv_rows))

    try:
        pricefile.read_rows("прайс.xls", b"\xd0\xcf\x11\xe0")
        old_format = "старый .xls принят"
    except pricefile.PriceFileError as exc:
        old_format = "" if "xlsx" in str(exc) else str(exc)
    check("старый .xls объясняет, что делать", not old_format, old_format)

    text, hidden = pricefile.rows_to_text(rows)
    check("закупочная цена до модели не доходит", "3200" not in text and hidden == ["Закупка, ₽"])
    check("розничная цена доходит до модели", "6720" in text)

    saved = pricefile.save("прайс.xlsx", price)
    stored = db.q1("SELECT text FROM kb_pages WHERE id = ?", (saved["page_id"],))["text"]
    check("файл попадает в базу знаний", saved["rows"] == 2 and "6720" in stored)
    check("файл виден в списке источников",
          any(f["id"] == saved["page_id"] for f in pricefile.files()))
    retrieval.invalidate()
    check("агент находит товар из файла",
          "Панама" in retrieval.context_for("есть панама?"))
    # клиент спрашивает «на зиму», в прайсе стоит «Зима» — без учёта окончаний
    # строка не находилась и агент звал менеджера на вопрос, ответ на который есть
    check("«зиму» и «зима» ищутся как одно слово",
          retrieval.stem("зиму") == retrieval.stem("зима") == retrieval.stem("зимы"))
    check("короткие слова не превращаются в кашу",
          retrieval.stem("мех") == "мех" and retrieval.stem("фетр") == "фетр")
    check("агент находит зимнюю модель по вопросу «шапка на зиму»",
          "Бини" in retrieval.context_for("нужна тёплая шапка на зиму"))

    # Общие вопросы — самые частые, а слов из прайса в них нет вообще. Пока
    # каталог влезает в запрос, он уходит в модель целиком, без поиска.
    for question in ("что у вас есть", "покажите ассортимент", "прайс"):
        context = retrieval.context_for(question)
        check(f"на «{question}» агент видит каталог",
              "Панама" in context and "Бини" in context, context[:60])

    # Панель показывала «ничего не нашлось» там, где агент видел весь каталог:
    # проверка и агент обязаны спрашивать одно и то же.
    check("проверка в панели совпадает с тем, что получит модель",
          all(all(hit["text"] in retrieval.context_for(q) for hit in retrieval.hits_for(q))
              and retrieval.hits_for(q)
              for q in ("что у вас есть", "сколько стоит панама", "здравствуйте")))
    check("на пустой запрос панель не врёт про находки",
          len(retrieval.hits_for("")) == len(retrieval.hits_for("что угодно")))

    try:
        pricefile.save("прайс.xlsx", "это не таблица".encode("utf-8"))
    except pricefile.PriceFileError:
        pass
    kept = db.q1("SELECT text FROM kb_pages WHERE id = ?", (saved["page_id"],))
    check("битый файл не стирает прежний прайс", kept is not None and "6720" in kept["text"])

    web_pages = db.q(f"SELECT url FROM kb_pages WHERE {knowledge.WEB_PAGES}")
    check("обходчик сайта не трогает загруженный файл",
          all(not row["url"].startswith("file://") for row in web_pages),
          ", ".join(row["url"] for row in web_pages))

    pricefile.remove(saved["page_id"])
    check("файл убирается из базы знаний",
          db.q1("SELECT id FROM kb_pages WHERE id = ?", (saved["page_id"],)) is None)

    section("Автоцепочки")
    from app import autochain, scheduler

    async def chains() -> None:
        # тик планировщика прогоняем целиком: пропущенный импорт внутри модуля
        # иначе всплывёт только в продакшене
        await scheduler._tick()
        check("тик планировщика проходит целиком", True)

        chain_id = autochain.save_chain("Прогрев", [
            {"delay_min": 0, "texts": {"ru": "Здравствуйте, {{first_name}}! Ещё актуально?"}},
            {"delay_min": 60, "texts": {"ru": "Напоминаю о себе", "en": "Just a reminder"},
             "buttons": [{"text": "Прайс", "url": "https://ascn.ai"}]},
        ])
        check("цепочка сохраняется с шагами", len(autochain.steps(chain_id)) == 2)

        for bad, why in (
            (("", [{"delay_min": 0, "texts": {"ru": "текст"}}]), "без названия"),
            (("Пустая", []), "без шагов"),
            (("Без текста", [{"delay_min": 0, "texts": {}}]), "шаг без текста"),
            (("Минус", [{"delay_min": -5, "texts": {"ru": "текст"}}]), "отрицательная задержка"),
        ):
            try:
                autochain.save_chain(*bad)
                outcome = f"приняли кривую цепочку ({why})"
            except ValueError:
                outcome = ""
            check(f"кривая цепочка не сохраняется — {why}", not outcome, outcome)

        # молчун получает первый шаг
        quiet = db.upsert_contact("tg", "980", "quiet", "Молчун", bot_id=bot_id)
        db.add_message(quiet["id"], "in", "client", "здравствуйте")
        created = autochain.enroll(quiet["id"])
        check("клиент ставится в очередь по включённой цепочке", created == 2, f"заданий {created}")
        check("повторная постановка не задваивает", autochain.enroll(quiet["id"]) == 0)

        sent = []

        async def fake_send(contact_id, text, media_path=None, author="ai", buttons=None, **kw):
            sent.append((contact_id, text, tuple(buttons or ())))
            db.add_message(contact_id, "out", author, text)
            return True, "sent"

        original = autochain.base.send
        autochain.base.send = fake_send
        try:
            result = await autochain.process_due()
            check("подошедший шаг уходит клиенту",
                  result["sent"] == 1 and "Молчун" in sent[0][1], f"{result}, {sent}")

            # ответил — остаток снимается
            talker = db.upsert_contact("tg", "981", "talk", "Говорун", bot_id=bot_id)
            db.add_message(talker["id"], "in", "client", "привет")
            autochain.enroll(talker["id"])
            db.add_message(talker["id"], "in", "client", "и ещё вопрос")
            before = len(sent)
            result = await autochain.process_due()
            check("ответившему цепочка не пишет",
                  len(sent) == before and result["skipped"] >= 1, f"{result}")
            reasons = {row["error"] for row in db.q(
                "SELECT error FROM autochain_jobs WHERE contact_id = ? AND status = 'cancelled'",
                (talker["id"],))}
            check("в задании написана причина отмены", "клиент ответил сам" in reasons, str(reasons))

            # передача менеджеру снимает остаток
            handed = db.upsert_contact("tg", "982", "handed", "Передан", bot_id=bot_id)
            db.add_message(handed["id"], "in", "client", "нужен человек")
            autochain.enroll(handed["id"])
            await sales.hand_off(handed["id"], "проверка", silent=True)
            left = db.q1("SELECT COUNT(*) AS c FROM autochain_jobs"
                         " WHERE contact_id = ? AND status = 'pending'", (handed["id"],))["c"]
            check("после передачи менеджеру шаги сняты", left == 0, f"осталось {left}")

            # зависшее задание возвращается в очередь
            stuck = db.upsert_contact("tg", "983", "stuck", "Зависший", bot_id=bot_id)
            db.add_message(stuck["id"], "in", "client", "привет")
            autochain.enroll(stuck["id"])
            job = db.q1("SELECT id FROM autochain_jobs WHERE contact_id = ? ORDER BY id",
                        (stuck["id"],))
            db.run("UPDATE autochain_jobs SET status = 'processing', claimed_at = ?"
                   " WHERE id = ?", (db.now() - 3600, job["id"]))
            autochain._recover_stale()
            check("зависшее задание возвращается в очередь",
                  db.q1("SELECT status FROM autochain_jobs WHERE id = ?",
                        (job["id"],))["status"] == "pending")

            # выключение цепочки снимает ожидающие шаги
            autochain.set_enabled(chain_id, False)
            pending = db.q1("SELECT COUNT(*) AS c FROM autochain_jobs"
                            " WHERE chain_id = ? AND status = 'pending'", (chain_id,))["c"]
            check("выключенная цепочка ничего не досылает", pending == 0, f"осталось {pending}")
        finally:
            autochain.base.send = original
            autochain.delete_chain(chain_id)

    asyncio.run(chains())

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

    section("Подключение каналов")
    from app import channels as channels_mod
    from app.web import main as web_main

    check("ошибка «Unauthorized» переводится на человеческий",
          "токен" in channels_mod.explain_token_error("Telegram server says - Unauthorized"))
    check("сетевой сбой не выдают за неверный токен",
          "интернет" in channels_mod.explain_token_error("fetch failed"))
    check("незнакомую ошибку показываем как есть",
          channels_mod.explain_token_error("странное") == "странное")
    probe_bot = db.add_bot("Проверка ошибок", "111:AA", role="sales")
    db.set_bot_error(probe_bot, "Telegram server says - Unauthorized")
    stored = db.bot(probe_bot)["last_error"]
    check("ошибка бота в карточке тоже человеческая", "токен" in (stored or ""), stored)
    db.set_bot_error(probe_bot, None)
    check("успешное подключение стирает ошибку", db.bot(probe_bot)["last_error"] is None)
    db.run("DELETE FROM bots WHERE id = ?", (probe_bot,))
    check("у каждого мессенджера своя форма подключения",
          len(web_main.BOT_PLATFORMS) >= 5
          and {p["code"] for p in web_main.BOT_PLATFORMS} >= {"tg", "max", "vk", "avito", "mail"})
    check("поля почты не показываются в форме Telegram",
          not next(p for p in web_main.BOT_PLATFORMS if p["code"] == "tg")["fields"]
          and any(f["name"] == "imap_host"
                  for f in next(p for p in web_main.BOT_PLATFORMS if p["code"] == "mail")["fields"]))

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

    # у мессенджеров — настоящие логотипы файлами, а не наши рисунки
    logos = ROOT / "app/web/static/logos"
    need_logos = ["telegram.svg", "whatsapp.svg", "vk.svg", "max.svg", "avito.svg"]
    absent = [n for n in need_logos if not (logos / n).exists()]
    check("настоящие логотипы на месте", not absent, ", ".join(absent))
    # xmlns="http://www.w3.org/2000/svg" — это объявление пространства имён,
    # а не загрузка извне. Ищем именно ссылки на чужие адреса.
    import re as _re
    external = []
    for name in need_logos:
        path = logos / name
        if not path.exists():
            continue
        body = path.read_text(errors="ignore")
        if _re.search(r'(?:href|src|url\()\s*=?\s*["\']?https?://', body):
            external.append(name)
    check("логотипы не тянут ничего извне", not external, ", ".join(external))

    # размер логотипа задан прямо на теге: устаревший css в браузере
    # не должен разносить вёрстку
    check("у логотипов размер прописан на теге",
          'width="{{ px }}" height="{{ px }}"' in base_tpl)
    # адрес стилей с версией — правка css видна сразу, без чистки кеша
    check("стили подключаются с версией", "style.css?v=" in base_tpl)

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
    slots = bk.free_slots(limit=12)
    check("свободные окна считаются", len(slots) > 0, f"окон: {len(slots)}")
    # Шаг меряем внутри одного дня: разрыв между днями — это ночь, а не сетка.
    # Раньше проверка сравнивала последнее окно дня с первым окном следующего и
    # падала во второй половине дня, когда на сегодня оставалось одно окно.
    from datetime import datetime as _dt
    steps = [(second["at"] - first["at"]) // 60
             for first, second in zip(slots, slots[1:])
             if _dt.fromtimestamp(first["at"]).date() == _dt.fromtimestamp(second["at"]).date()]
    check("шаг сетки равен длительности услуги",
          bool(steps) and all(step == 40 for step in steps), f"шаги {steps}")

    if slots:
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
