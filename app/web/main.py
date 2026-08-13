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

from .. import broadcast, config, db, knowledge, llm, retrieval, sales, scheduler
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


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
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
    ctx.setdefault("statuses", db.LEAD_STATUSES)
    ctx.setdefault("channels", config.CHANNEL_TITLES)
    ctx.setdefault("ai_on", db.setting("ai_enabled_global", "1") == "1")
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
        "channels": config.active_channels(),
        "ai": config.AI_ENABLED,
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
async def reply(contact_id: int, text: str = Form("")):
    """Ответ менеджера уходит клиенту в его исходный мессенджер."""
    text = text.strip()
    if text:
        await base.send(contact_id, text, author="manager")
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
    pages = db.q("SELECT * FROM kb_pages ORDER BY included DESC, chars DESC, url LIMIT 500")
    return page(request, "knowledge.html", pages=pages, stats=knowledge.stats(),
                site=db.setting("business_site", ""), extra=db.setting("kb_extra", ""))


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


async def _save_upload(upload) -> str | None:
    if not upload or not getattr(upload, "filename", ""):
        return None
    suffix = Path(upload.filename).suffix or ".jpg"
    name = f"{uuid.uuid4().hex}{suffix}"
    (config.MEDIA_DIR / name).write_bytes(await upload.read())
    return name


# ── настройки ──────────────────────────────────────────────────────────

@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    keys = ["business_name", "business_site", "greeting", "tone", "model",
            "operator_chat_id", "managers", "handoff_note", "ai_enabled_global"]
    values = {key: db.setting(key, "") for key in keys}
    return page(request, "settings.html", values=values,
                models=await llm.available_models(),
                telegram_on=config.telegram_enabled(),
                whatsapp_on=config.whatsapp_enabled(),
                ai_ready=config.AI_ENABLED, mode=config.MODE)


@app.post("/settings")
async def settings_save(request: Request):
    form = await request.form()
    for key in ("business_name", "business_site", "greeting", "tone", "model",
                "operator_chat_id", "managers", "handoff_note"):
        if key in form:
            db.set_setting(key, str(form.get(key) or ""))
    db.set_setting("ai_enabled_global", "1" if form.get("ai_enabled_global") else "0")
    return RedirectResponse("/settings", status_code=303)


# ── вебхуки ────────────────────────────────────────────────────────────

@app.post("/hook/telegram")
async def hook_telegram(request: Request):
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token", "") != config.WEBHOOK_SECRET:
        return JSONResponse({"ok": False}, status_code=403)
    payload = await request.json()
    asyncio.create_task(telegram.feed(payload))
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
