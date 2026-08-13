"""Веб-панель: рабочее окно менеджера, лиды, база знаний, рассылки.

Владелец и менеджеры живут только здесь. Открывать workflow ASCN или лезть
в технические сервисы, чтобы ответить клиенту, не нужно — это требование ТЗ.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeSerializer

from .. import (
    broadcast, config, db, knowledge, llm, onboarding, retrieval, rivals,
    sales, scheduler, sheets,
)
from .. import channels
from ..channels import base, telegram, whatsapp

log = logging.getLogger("web")

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
signer = URLSafeSerializer(config.SECRET_KEY, salt="session")

templates.env.filters["dt"] = lambda ts: (
    datetime.fromtimestamp(ts).strftime("%d.%m.%Y, %H:%M") if ts else ""
)
templates.env.filters["short"] = lambda text, n=60: (
    (text or "")[:n] + ("…" if text and len(text) > n else "")
)
templates.env.filters["time"] = lambda ts: (
    datetime.fromtimestamp(ts).strftime("%H:%M") if ts else ""
)


def _day(ts: int) -> str:
    """Подпись-разделитель между днями переписки."""
    if not ts:
        return ""
    when = datetime.fromtimestamp(ts).date()
    today = datetime.now().date()
    delta = (today - when).days
    if delta == 0:
        return "Сегодня"
    if delta == 1:
        return "Вчера"
    return when.strftime("%d.%m.%Y")


def _initials(name: str) -> str:
    parts = [p for p in (name or "").replace("@", " ").split() if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[1][0]).upper()


templates.env.filters["day"] = _day
templates.env.filters["initials"] = _initials
# цвет аватарки — от идентификатора, чтобы у человека он не менялся
templates.env.filters["hue"] = lambda value: (int(value) * 47) % 360


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    channels.adopt_env_token()
    knowledge.reindex_extra()
    await telegram.start()
    await scheduler.start()
    log.info("панель на %s", config.PUBLIC_URL)
    yield
    await scheduler.stop()
    await telegram.stop()


app = FastAPI(title="ИИ-продажник", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


# ── авторизация ────────────────────────────────────────────────────────

def _session(request: Request) -> dict:
    raw = request.cookies.get("session")
    if not raw:
        return {}
    try:
        return signer.loads(raw)
    except BadSignature:
        return {}


def authed(request: Request) -> bool:
    return bool(_session(request).get("auth"))


def page(request: Request, name: str, **ctx) -> HTMLResponse:
    ctx.setdefault("unread", db.unread_count())
    ctx.setdefault("open_requests", db.open_requests_count())
    ctx.setdefault("statuses", db.LEAD_STATUSES)
    ctx.setdefault("req_statuses", db.REQUEST_STATUSES)
    ctx.setdefault("channels", config.CHANNEL_TITLES)
    ctx.setdefault("ai_on", db.setting("ai_enabled_global", "1") == "1")
    ctx.setdefault("setup", onboarding.progress())
    ctx.setdefault("path", request.url.path)
    return templates.TemplateResponse(request, name, ctx)


@app.middleware("http")
async def guard(request: Request, call_next):
    """Всё, кроме входа, вебхуков и здоровья, закрыто сессией."""
    open_paths = ("/login", "/static", "/hook", "/health")
    if request.url.path.startswith(open_paths) or authed(request):
        return await call_next(request)
    return RedirectResponse("/login", status_code=303)


@app.get("/health")
async def health():
    return {
        "ok": True,
        "channels": channels.active(),
        "ai": llm.ai_ready(),
        "model": llm.current_model(),
        "contacts": db.q1("SELECT COUNT(*) AS c FROM contacts")["c"],
        "leads": db.q1("SELECT COUNT(*) AS c FROM leads")["c"],
    }


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": ""})


@app.post("/login")
async def login(request: Request, login: str = Form(""), password: str = Form("")):
    if login == config.ADMIN_LOGIN and password == config.ADMIN_PASSWORD:
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            "session", signer.dumps({"auth": True}),
            httponly=True, max_age=30 * 24 * 3600, samesite="lax",
        )
        return response
    return templates.TemplateResponse(
        request, "login.html", {"error": "Неверный логин или пароль"}, status_code=401
    )


@app.get("/logout")
async def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie("session")
    return response


@app.get("/media/{name}")
async def media(name: str):
    path = config.MEDIA_DIR / Path(name).name
    if not path.exists():
        return PlainTextResponse("нет файла", status_code=404)
    return FileResponse(path)


@app.get("/onboarding", response_class=HTMLResponse)
async def onboarding_page(request: Request):
    return page(request, "onboarding.html")


# ── все модели для выпадающего списка ──────────────────────────────────

@app.get("/api/models")
async def api_models():
    """Полный список моделей OpenRouter — панель фильтрует его на месте."""
    return JSONResponse(await llm.available_models())


# ── дашборд ────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    stats = {
        "contacts": db.q1("SELECT COUNT(*) AS c FROM contacts")["c"],
        "leads": db.q1("SELECT COUNT(*) AS c FROM leads")["c"],
        "handed": db.q1("SELECT COUNT(*) AS c FROM leads WHERE status='handed'")["c"],
        "audience": broadcast.audience_size(),
    }
    by_status = {
        key: db.q1("SELECT COUNT(*) AS c FROM leads WHERE status = ?", (key,))["c"]
        for key in db.LEAD_STATUSES
    }
    recent = db.q(
        "SELECT c.*, l.status AS lead_status FROM contacts c"
        " LEFT JOIN leads l ON l.contact_id = c.id"
        " WHERE c.last_msg_at IS NOT NULL ORDER BY c.last_msg_at DESC LIMIT 10"
    )
    return page(request, "dashboard.html", stats=stats, by_status=by_status,
                recent=recent, kb=knowledge.stats())


async def _save_upload(upload) -> str | None:
    """Сохранить присланный файл в хранилище. Возвращает имя файла."""
    if not upload or not getattr(upload, "filename", ""):
        return None
    suffix = Path(upload.filename).suffix or ".bin"
    name = f"{uuid.uuid4().hex}{suffix}"
    (config.MEDIA_DIR / name).write_bytes(await upload.read())
    return name


# ── диалоги ────────────────────────────────────────────────────────────

@app.get("/dialogs", response_class=HTMLResponse)
async def dialogs(request: Request, c: int | None = None):
    people = db.q(
        "SELECT c.*, l.status AS lead_status,"
        " (SELECT COUNT(*) FROM messages m WHERE m.contact_id = c.id"
        "  AND m.direction='in' AND m.is_read=0) AS unread,"
        " (SELECT text FROM messages m WHERE m.contact_id = c.id ORDER BY id DESC LIMIT 1) AS last"
        " FROM contacts c LEFT JOIN leads l ON l.contact_id = c.id"
        " WHERE EXISTS (SELECT 1 FROM messages m WHERE m.contact_id = c.id)"
        " ORDER BY c.last_msg_at DESC NULLS LAST LIMIT 200"
    )
    active = lead = None
    thread: list = []
    if c:
        active = db.contact_by_id(c)
        if active:
            db.run("UPDATE messages SET is_read = 1 WHERE contact_id = ? AND direction = 'in'", (c,))
            thread = db.history(c, limit=200)
            lead = db.get_lead(c)
    return page(request, "dialogs.html", people=people, active=active,
                thread=thread, lead=lead)


@app.get("/api/dialogs/{contact_id}/messages")
async def dialog_messages(contact_id: int, after: int = 0):
    rows = db.q(
        "SELECT id, direction, author, text, media_type, media_path, created_at"
        " FROM messages WHERE contact_id = ? AND id > ? ORDER BY id",
        (contact_id, after),
    )
    return JSONResponse([dict(r) for r in rows])


@app.post("/dialogs/{contact_id}/reply")
async def reply(request: Request, contact_id: int):
    """Ответ менеджера уходит клиенту в его исходный мессенджер.

    Можно отправить текст, файл или то и другое сразу: фото, голосовое,
    видео и документы уходят нужным типом, а не одинаковым вложением.
    """
    form = await request.form()
    text = str(form.get("text") or "").strip()
    media = await _save_upload(form.get("file"))  # type: ignore[arg-type]

    if text or media:
        await base.send(contact_id, text, media, author="manager")
        # менеджер вступил в разговор — ИИ замолкает
        db.set_ai(contact_id, False)
    return RedirectResponse(f"/dialogs?c={contact_id}", status_code=303)


@app.post("/dialogs/{contact_id}/take")
async def take(contact_id: int, manager: str = Form("")):
    """Забрать диалог себе."""
    await sales.hand_off(contact_id, "менеджер забрал диалог", silent=True)
    if manager.strip():
        db.run("UPDATE contacts SET manager = ? WHERE id = ?", (manager.strip(), contact_id))
    return RedirectResponse(f"/dialogs?c={contact_id}", status_code=303)


@app.post("/dialogs/{contact_id}/return-ai")
async def return_ai(contact_id: int):
    """«Вернуть ИИ» — агент продолжает с сохранённой историей."""
    sales.return_to_ai(contact_id)
    return RedirectResponse(f"/dialogs?c={contact_id}", status_code=303)


# ── лиды ───────────────────────────────────────────────────────────────

@app.get("/leads", response_class=HTMLResponse)
async def leads(request: Request, status: str = ""):
    sql = (
        "SELECT l.*, c.channel, c.username, c.phone, c.name AS contact_name, c.id AS cid"
        " FROM leads l JOIN contacts c ON c.id = l.contact_id"
    )
    params: tuple = ()
    if status in db.LEAD_STATUSES:
        sql += " WHERE l.status = ?"
        params = (status,)
    sql += " ORDER BY l.updated_at DESC LIMIT 300"
    return page(request, "leads.html", rows=db.q(sql, params), current=status)


@app.post("/leads/{contact_id}/status")
async def lead_status(contact_id: int, status: str = Form(...)):
    if status in db.LEAD_STATUSES:
        db.set_lead_status(contact_id, status)
    return RedirectResponse(f"/dialogs?c={contact_id}", status_code=303)


@app.post("/leads/{contact_id}/save")
async def lead_save(request: Request):
    form = await request.form()
    contact_id = int(request.path_params["contact_id"])
    fields = {key: str(form.get(key) or "") for key in db.LEAD_FIELDS}
    db.upsert_lead(contact_id, fields)
    if form.get("manager"):
        db.run("UPDATE leads SET manager = ? WHERE contact_id = ?",
               (str(form.get("manager")), contact_id))
    return RedirectResponse(f"/dialogs?c={contact_id}", status_code=303)


# ── база знаний ────────────────────────────────────────────────────────

@app.get("/knowledge", response_class=HTMLResponse)
async def kb_page(request: Request):
    pages = db.q(
        "SELECT * FROM kb_pages WHERE url NOT LIKE 'manual://%' AND url NOT LIKE 'sheet://%'"
        " ORDER BY included DESC, chars DESC, url LIMIT 500"
    )
    sheet = db.q1("SELECT * FROM kb_pages WHERE url = 'sheet://knowledge'")
    return page(request, "knowledge.html", pages=pages, stats=knowledge.stats(),
                site=db.setting("business_site", ""), extra=db.setting("kb_extra", ""),
                sheet_url=db.setting("sheets_kb_url", ""), sheet=sheet)


@app.post("/knowledge/sheet")
async def kb_sheet(url: str = Form("")):
    """Таблица — такой же источник знаний, как сайт. Живёт здесь же."""
    db.set_setting("sheets_kb_url", url.strip())
    if url.strip():
        await asyncio.to_thread(sheets.sync_knowledge)
    else:
        row = db.q1("SELECT id FROM kb_pages WHERE url = 'sheet://knowledge'")
        if row:
            db.run("DELETE FROM kb_chunks WHERE page_id = ?", (row["id"],))
            db.run("DELETE FROM kb_pages WHERE id = ?", (row["id"],))
    retrieval.invalidate()
    return RedirectResponse("/knowledge", status_code=303)


@app.post("/knowledge/discover")
async def kb_discover(site: str = Form(...)):
    """Найти внутренние страницы сайта. Текст пока не грузим."""
    db.set_setting("business_site", site.strip())
    await asyncio.to_thread(knowledge.discover, site.strip())
    return RedirectResponse("/knowledge", status_code=303)


@app.post("/knowledge/select")
async def kb_select(request: Request):
    """Сохранить, какие страницы включены, и загрузить включённые."""
    form = await request.form()
    keep = {int(key[5:]) for key in form if key.startswith("page_")}
    for row in db.q("SELECT id FROM kb_pages WHERE url != 'manual://extra'"):
        db.run("UPDATE kb_pages SET included = ? WHERE id = ?",
               (1 if row["id"] in keep else 0, row["id"]))
    await asyncio.to_thread(knowledge.fetch_pending)
    retrieval.invalidate()
    return RedirectResponse("/knowledge", status_code=303)


@app.post("/knowledge/extra")
async def kb_extra(text: str = Form("")):
    """Текст, вписанный руками: прайс, условия, частые вопросы."""
    db.set_setting("kb_extra", text)
    knowledge.reindex_extra()
    retrieval.invalidate()
    return RedirectResponse("/knowledge", status_code=303)


@app.get("/knowledge/test")
async def kb_test(q: str = ""):
    """Проверить, что агент найдёт по вопросу."""
    return JSONResponse(retrieval.search(q, top_k=4))


# ── рассылки ───────────────────────────────────────────────────────────

@app.get("/broadcast", response_class=HTMLResponse)
async def broadcast_page(request: Request, preview: int | None = None):
    draft = db.q1("SELECT * FROM broadcasts WHERE id = ?", (preview,)) if preview else None
    history = db.q("SELECT * FROM broadcasts ORDER BY id DESC LIMIT 30")
    return page(request, "broadcast.html", draft=draft, history=history,
                audience=broadcast.audience_size())


@app.post("/broadcast")
async def broadcast_create(request: Request):
    """Создать черновик и показать предпросмотр. Ничего не отправляет."""
    form = await request.form()
    image = await _save_upload(form.get("image"))  # type: ignore[arg-type]

    send_at = None
    when = str(form.get("send_at") or "").strip()
    if when:
        try:
            send_at = int(datetime.fromisoformat(when).timestamp())
        except ValueError:
            send_at = None

    broadcast_id = broadcast.create(
        str(form.get("text") or "").strip(), image,
        str(form.get("button_text") or "").strip(),
        str(form.get("button_url") or "").strip(),
        send_at,
    )
    return RedirectResponse(f"/broadcast?preview={broadcast_id}", status_code=303)


@app.post("/broadcast/{broadcast_id}/confirm")
async def broadcast_confirm(broadcast_id: int):
    """Подтверждение владельца. Только после него рассылка уходит."""
    broadcast.confirm(broadcast_id)
    row = db.q1("SELECT send_at FROM broadcasts WHERE id = ?", (broadcast_id,))
    if row and not row["send_at"]:
        asyncio.create_task(broadcast.send_broadcast(broadcast_id))
    return RedirectResponse("/broadcast", status_code=303)


@app.post("/broadcast/{broadcast_id}/cancel")
async def broadcast_cancel(broadcast_id: int):
    broadcast.cancel(broadcast_id)
    return RedirectResponse("/broadcast", status_code=303)


@app.post("/broadcast/{broadcast_id}/retry")
async def broadcast_retry(broadcast_id: int):
    """Повтор для тех, кому не дошло. Дублей не создаёт."""
    db.run("UPDATE broadcasts SET status = 'confirmed' WHERE id = ?", (broadcast_id,))
    asyncio.create_task(broadcast.send_broadcast(broadcast_id))
    return RedirectResponse("/broadcast", status_code=303)


# ── настройки ──────────────────────────────────────────────────────────

@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    keys = ["business_name", "business_site", "greeting", "tone", "model",
            "operator_chat_id", "managers", "handoff_note", "ai_enabled_global",
            "sheets_kb_url", "sheets_crm_id", "sheets_crm_tab"]
    values = {key: db.setting(key, "") for key in keys}
    return page(request, "settings.html", values=values,
                models=await llm.available_models(),
                telegram_on=channels.telegram_enabled(),
                whatsapp_on=config.whatsapp_enabled(),
                ai_ready=llm.ai_ready(),
                key_source=_key_source(),
                sa_file=bool(config.GOOGLE_SA_FILE),
                mode=config.MODE)


def _key_source() -> str:
    """Откуда взялся ключ — чтобы владелец видел, что он вообще есть."""
    if db.setting("openrouter_key", "").strip():
        return "вставлен в панели"
    if config.OPENROUTER_API_KEY:
        return "взят из .env"
    return ""


@app.post("/settings")
async def settings_save(request: Request):
    form = await request.form()
    for key in ("business_name", "business_site", "greeting", "tone", "model",
                "operator_chat_id", "managers", "handoff_note",
                "sheets_kb_url", "sheets_crm_id", "sheets_crm_tab"):
        if key in form:
            db.set_setting(key, str(form.get(key) or ""))

    # Ключ пишем только если поле заполнили: пустое поле означает
    # «оставить как было», иначе ключ стирался бы при каждом сохранении.
    new_key = str(form.get("openrouter_key") or "").strip()
    if new_key:
        db.set_setting("openrouter_key", new_key)
    if form.get("drop_key"):
        db.set_setting("openrouter_key", "")

    db.set_setting("ai_enabled_global", "1" if form.get("ai_enabled_global") else "0")
    return RedirectResponse("/settings", status_code=303)


@app.post("/settings/sheets/sync")
async def sheets_sync():
    """Синхронизировать таблицы прямо сейчас, не дожидаясь планировщика."""
    await asyncio.to_thread(sheets.sync_knowledge)
    await sheets.sync_leads()
    return RedirectResponse("/settings", status_code=303)


@app.get("/settings/sheets/check")
async def sheets_check():
    return JSONResponse(await sheets.check())


# ── вебхуки ────────────────────────────────────────────────────────────

@app.post("/hook/telegram/{bot_id}")
async def hook_telegram(bot_id: int, request: Request):
    """У каждого бота свой адрес — так апдейты не путаются между ними."""
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token", "") != config.WEBHOOK_SECRET:
        return JSONResponse({"ok": False}, status_code=403)
    payload = await request.json()
    asyncio.create_task(telegram.feed(bot_id, payload))
    return JSONResponse({"ok": True})


@app.get("/hook/whatsapp")
async def hook_whatsapp_verify(request: Request):
    """Meta проверяет адрес GET-запросом и ждёт обратно hub.challenge."""
    challenge = whatsapp.verify(dict(request.query_params))
    if challenge is None:
        return PlainTextResponse("forbidden", status_code=403)
    return PlainTextResponse(challenge)


@app.post("/hook/whatsapp")
async def hook_whatsapp(request: Request):
    payload = await request.json()
    asyncio.create_task(whatsapp.feed(payload))
    return JSONResponse({"ok": True})


# ── боты ───────────────────────────────────────────────────────────────

@app.get("/bots", response_class=HTMLResponse)
async def bots_page(request: Request):
    rows = db.bots(only_enabled=False)
    live = set(telegram.BOTS)
    return page(request, "bots.html", rows=rows, live=live, mode=config.MODE,
                public_url=config.PUBLIC_URL)


@app.post("/bots/add")
async def bots_add(title: str = Form(""), token: str = Form(...), role: str = Form("sales")):
    """Добавить бота по токену от BotFather. Токен проверяем до сохранения."""
    token = token.strip()
    probe = await telegram.check_token(token)
    if not probe["ok"]:
        return RedirectResponse(f"/bots?error={probe['error'][:120]}", status_code=303)

    if db.q1("SELECT 1 FROM bots WHERE token = ?", (token,)):
        return RedirectResponse("/bots?error=такой+бот+уже+добавлен", status_code=303)

    db.add_bot(title.strip() or f"@{probe['username']}", token,
               role if role in ("sales", "manager") else "sales")
    await telegram.reload()
    return RedirectResponse("/bots", status_code=303)


@app.post("/bots/{bot_id}/save")
async def bots_save(bot_id: int, title: str = Form(""), role: str = Form("sales"),
                    greeting: str = Form(""), enabled: str = Form(""),
                    script_enabled: str = Form("")):
    db.run(
        "UPDATE bots SET title = ?, role = ?, greeting = ?, enabled = ?, script_enabled = ?"
        " WHERE id = ?",
        (title.strip(), role, greeting.strip() or None,
         1 if enabled else 0, 1 if script_enabled else 0, bot_id),
    )
    await telegram.reload()
    return RedirectResponse("/bots", status_code=303)


@app.post("/bots/{bot_id}/delete")
async def bots_delete(bot_id: int):
    db.run("DELETE FROM bots WHERE id = ?", (bot_id,))
    await telegram.reload()
    return RedirectResponse("/bots", status_code=303)


@app.post("/bots/reload")
async def bots_reload():
    """Перечитать реестр и переставить вебхуки — без перезапуска службы."""
    await telegram.reload()
    return RedirectResponse("/bots", status_code=303)


# ── лог обращений ──────────────────────────────────────────────────────

@app.get("/requests", response_class=HTMLResponse)
async def requests_page(request: Request, status: str = ""):
    sql = (
        "SELECT r.*, c.name, c.username, c.phone, c.channel, c.ai_enabled,"
        " l.summary, l.status AS lead_status"
        " FROM requests r JOIN contacts c ON c.id = r.contact_id"
        " LEFT JOIN leads l ON l.contact_id = r.contact_id"
    )
    params: tuple = ()
    if status in db.REQUEST_STATUSES:
        sql += " WHERE r.status = ?"
        params = (status,)
    sql += " ORDER BY CASE r.status WHEN 'new' THEN 0 WHEN 'in_work' THEN 1 ELSE 2 END," \
           " r.id DESC LIMIT 300"
    managers = [m.strip() for m in db.setting("managers", "").split(",") if m.strip()]
    return page(request, "requests.html", rows=db.q(sql, params),
                current=status, managers=managers)


@app.post("/requests/{request_id}/take")
async def request_take(request_id: int, manager: str = Form("")):
    """«Взять в работу»: ИИ замолкает, ответственный записан."""
    row = db.take_request(request_id, manager.strip() or "менеджер")
    if row:
        db.set_ai(row["contact_id"], False)
    return RedirectResponse("/requests", status_code=303)


@app.post("/requests/{request_id}/pass")
async def request_pass(request_id: int, manager: str = Form("")):
    """«Передать менеджеру»: сменить ответственного или вернуть в очередь."""
    if manager.strip():
        db.take_request(request_id, manager.strip())
        db.run("UPDATE requests SET manager = ? WHERE id = ?", (manager.strip(), request_id))
    else:
        db.run(
            "UPDATE requests SET status = 'new', manager = NULL, taken_at = NULL"
            " WHERE id = ?", (request_id,),
        )
    return RedirectResponse("/requests", status_code=303)


@app.post("/requests/{request_id}/close")
async def request_close(request_id: int):
    db.close_request(request_id)
    return RedirectResponse("/requests", status_code=303)


@app.post("/requests/{request_id}/return-ai")
async def request_return_ai(request_id: int):
    row = db.q1("SELECT contact_id FROM requests WHERE id = ?", (request_id,))
    if row:
        sales.return_to_ai(row["contact_id"])
    return RedirectResponse("/requests", status_code=303)


# ── сценарий продаж ────────────────────────────────────────────────────

@app.get("/script", response_class=HTMLResponse)
async def script_page(request: Request, bot: int | None = None):
    steps = db.q(
        "SELECT * FROM script_steps WHERE bot_id IS ? ORDER BY position", (bot,)
    )
    return page(request, "script.html", steps=steps, bot_id=bot,
                sales_bots=db.bots(role="sales", only_enabled=False),
                templates=db.SCRIPT_TEMPLATES,
                fields=db.LEAD_FIELDS)


@app.post("/script/save")
async def script_save(request: Request):
    """Сохранить шаги целиком: порядок, цели и что спрашивать."""
    form = await request.form()
    bot_id = form.get("bot_id")
    bot_id = int(bot_id) if bot_id else None

    for key in form:
        if not key.startswith("title_"):
            continue
        step_id = int(key[6:])
        title = str(form.get(f"title_{step_id}") or "").strip()
        if not title:
            db.run("DELETE FROM script_steps WHERE id = ?", (step_id,))
            continue
        db.run(
            "UPDATE script_steps SET title = ?, goal = ?, ask_field = ?,"
            " position = ?, enabled = ? WHERE id = ?",
            (title,
             str(form.get(f"goal_{step_id}") or "").strip(),
             str(form.get(f"field_{step_id}") or "").strip(),
             int(form.get(f"pos_{step_id}") or 0),
             1 if form.get(f"on_{step_id}") else 0,
             step_id),
        )

    new_title = str(form.get("new_title") or "").strip()
    if new_title:
        row = db.q1(
            "SELECT COALESCE(MAX(position), -1) + 1 AS p FROM script_steps WHERE bot_id IS ?",
            (bot_id,),
        )
        db.run(
            "INSERT INTO script_steps (bot_id, position, title, goal, ask_field, enabled)"
            " VALUES (?, ?, ?, ?, ?, 1)",
            (bot_id, row["p"], new_title,
             str(form.get("new_goal") or "").strip(),
             str(form.get("new_field") or "").strip()),
        )

    target = f"/script?bot={bot_id}" if bot_id else "/script"
    return RedirectResponse(target, status_code=303)


@app.post("/script/copy")
async def script_copy(bot_id: int = Form(...)):
    """Сделать боту свой сценарий — копией общего, дальше правится отдельно."""
    if db.q1("SELECT 1 FROM script_steps WHERE bot_id = ?", (bot_id,)):
        return RedirectResponse(f"/script?bot={bot_id}", status_code=303)
    for step in db.q("SELECT * FROM script_steps WHERE bot_id IS NULL ORDER BY position"):
        db.run(
            "INSERT INTO script_steps (bot_id, position, title, goal, ask_field, enabled)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (bot_id, step["position"], step["title"], step["goal"],
             step["ask_field"], step["enabled"]),
        )
    return RedirectResponse(f"/script?bot={bot_id}", status_code=303)


# ── мастер запуска ─────────────────────────────────────────────────────
# Как у конкурентов: бот собирается кликами за 10-30 минут, а не хождением
# по разделам панели. Каждый шаг делается прямо здесь и сразу сохраняется.

SETUP_STEPS = [
    ("bot", "Подключить бота"),
    ("business", "О бизнесе"),
    ("knowledge", "База знаний"),
    ("script", "Сценарий"),
    ("launch", "Проверка и запуск"),
]


def _setup_ready(step: str) -> bool:
    """Пройден ли шаг по факту."""
    if step == "bot":
        return bool(db.bots(role="sales", only_enabled=True))
    if step == "business":
        return bool(db.setting("business_name", "").strip())
    if step == "knowledge":
        return knowledge.stats()["loaded"] > 0
    if step == "script":
        return bool(db.script())
    if step == "launch":
        return llm.ai_ready() and bool(db.bots(role="sales", only_enabled=True))
    return False


@app.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request, step: str = "bot"):
    if step not in dict(SETUP_STEPS):
        step = "bot"
    index = [k for k, _ in SETUP_STEPS].index(step)

    return page(
        request, "setup.html",
        steps=SETUP_STEPS, step=step, index=index,
        done={key: _setup_ready(key) for key, _ in SETUP_STEPS},
        bots=db.bots(only_enabled=False),
        live=set(telegram.BOTS),
        values={k: db.setting(k, "") for k in
                ("business_name", "business_site", "greeting", "tone",
                 "operator_chat_id", "managers")},
        kb=knowledge.stats(),
        sheet_url=db.setting("sheets_kb_url", ""),
        extra=db.setting("kb_extra", ""),
        script=db.script(),
        templates=db.SCRIPT_TEMPLATES,
        ai_ready=llm.ai_ready(),
        key_source=_key_source(),
        model=llm.current_model(),
    )


@app.post("/setup/bot")
async def setup_bot(title: str = Form(""), token: str = Form(...), role: str = Form("sales")):
    probe = await telegram.check_token(token.strip())
    if not probe["ok"]:
        return RedirectResponse(f"/setup?step=bot&error={probe['error'][:100]}", status_code=303)
    if not db.q1("SELECT 1 FROM bots WHERE token = ?", (token.strip(),)):
        db.add_bot(title.strip() or f"@{probe['username']}", token.strip(),
                   role if role in ("sales", "manager") else "sales")
        await telegram.reload()
    nxt = "business" if role == "sales" else "bot"
    return RedirectResponse(f"/setup?step={nxt}", status_code=303)


@app.post("/setup/business")
async def setup_business(request: Request):
    form = await request.form()
    for key in ("business_name", "business_site", "greeting", "tone"):
        if key in form:
            db.set_setting(key, str(form.get(key) or ""))
    return RedirectResponse("/setup?step=knowledge", status_code=303)


@app.post("/setup/knowledge")
async def setup_knowledge(request: Request):
    """Все три источника знаний прямо в мастере, без перехода на другую страницу."""
    form = await request.form()

    sheet = str(form.get("sheet_url") or "").strip()
    if sheet != db.setting("sheets_kb_url", ""):
        db.set_setting("sheets_kb_url", sheet)
        if sheet:
            await asyncio.to_thread(sheets.sync_knowledge)

    text = str(form.get("kb_extra") or "").strip()
    if text != db.setting("kb_extra", ""):
        db.set_setting("kb_extra", text)
        knowledge.reindex_extra()

    site = str(form.get("site") or "").strip()
    if site:
        db.set_setting("business_site", site)
        await asyncio.to_thread(knowledge.discover, site)
        await asyncio.to_thread(knowledge.fetch_pending)

    retrieval.invalidate()
    target = "knowledge" if form.get("stay") else "script"
    return RedirectResponse(f"/setup?step={target}", status_code=303)


@app.post("/setup/script")
async def setup_script(template: str = Form(""), enable: str = Form("")):
    if template:
        db.apply_template(template)
    for bot in db.bots(role="sales", only_enabled=False):
        db.run("UPDATE bots SET script_enabled = ? WHERE id = ?",
               (1 if enable else 0, bot["id"]))
    return RedirectResponse("/setup?step=launch", status_code=303)


@app.post("/setup/launch")
async def setup_launch(request: Request):
    form = await request.form()
    key = str(form.get("openrouter_key") or "").strip()
    if key:
        db.set_setting("openrouter_key", key)
    if form.get("model"):
        db.set_setting("model", str(form.get("model")))
    if "operator_chat_id" in form:
        db.set_setting("operator_chat_id", str(form.get("operator_chat_id") or ""))
    if "managers" in form:
        db.set_setting("managers", str(form.get("managers") or ""))
    return RedirectResponse("/setup?step=launch&saved=1", status_code=303)


# ── конструктор сценария ───────────────────────────────────────────────

@app.post("/script/template")
async def script_template(request: Request, template: str = Form(...),
                          bot_id: str = Form("")):
    target = int(bot_id) if bot_id else None
    db.apply_template(template, target)
    return RedirectResponse(f"/script?bot={bot_id}" if bot_id else "/script",
                            status_code=303)


@app.post("/script/reorder")
async def script_reorder(request: Request):
    """Новый порядок после перетаскивания карточек."""
    data = await request.json()
    order = [int(x) for x in data.get("order", [])]
    db.reorder_script(order)
    return JSONResponse({"ok": True})


@app.post("/script/step/{step_id}/delete")
async def script_step_delete(step_id: int, bot_id: str = Form("")):
    db.run("DELETE FROM script_steps WHERE id = ?", (step_id,))
    return RedirectResponse(f"/script?bot={bot_id}" if bot_id else "/script",
                            status_code=303)


# ── конкуренты ─────────────────────────────────────────────────────────

@app.get("/rivals", response_class=HTMLResponse)
async def rivals_page(request: Request):
    last = db.setting("rivals_last_run", "")
    return page(request, "rivals.html",
                rows=db.rivals(), changes=db.rival_changes(60),
                every=db.setting("rivals_every_hours", "12"),
                notify_on=db.setting("rivals_notify", "1") == "1",
                last_run=int(last) if last.isdigit() else 0,
                ai_ready=llm.ai_ready())


@app.post("/rivals/add")
async def rivals_add(title: str = Form(""), url: str = Form(...)):
    url = url.strip()
    if not url.startswith("http"):
        url = "https://" + url
    db.add_rival(title.strip() or url, url)
    return RedirectResponse("/rivals", status_code=303)


@app.post("/rivals/{rival_id}/toggle")
async def rivals_toggle(rival_id: int):
    db.run("UPDATE rivals SET enabled = 1 - enabled WHERE id = ?", (rival_id,))
    return RedirectResponse("/rivals", status_code=303)


@app.post("/rivals/{rival_id}/delete")
async def rivals_delete(rival_id: int):
    db.run("DELETE FROM rival_changes WHERE rival_id = ?", (rival_id,))
    db.run("DELETE FROM rivals WHERE id = ?", (rival_id,))
    return RedirectResponse("/rivals", status_code=303)


@app.post("/rivals/check")
async def rivals_check():
    """Проверить всех прямо сейчас, не дожидаясь расписания."""
    await rivals.check_all()
    return RedirectResponse("/rivals", status_code=303)


@app.post("/rivals/settings")
async def rivals_settings(every: str = Form("12"), notify_on: str = Form("")):
    db.set_setting("rivals_every_hours", every.strip() or "12")
    db.set_setting("rivals_notify", "1" if notify_on else "0")
    return RedirectResponse("/rivals", status_code=303)
