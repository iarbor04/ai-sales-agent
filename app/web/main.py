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
from urllib.parse import quote

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
    autochain, booking, broadcast, config, db, knowledge, llm, onboarding,
    pricefile, retrieval, rivals, sales, scheduler, sheets,
)
from .. import channels
from ..channels import base, telegram, whatsapp
from ..channels import web as webchat

log = logging.getLogger("web")

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.filters["fromjson"] = lambda value: json.loads(value or "{}")
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
    await channels.start_all()
    await scheduler.start()
    log.info("панель на %s", config.PUBLIC_URL)
    yield
    await scheduler.stop()
    await channels.stop_all()


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


def assets_version() -> str:
    """Метка стилей для адреса. Правка css видна сразу, без чистки кеша."""
    try:
        return str(int((BASE_DIR / "static" / "style.css").stat().st_mtime))
    except OSError:
        return "1"


def page(request: Request, name: str, **ctx) -> HTMLResponse:
    ctx.setdefault("assets", assets_version())
    ctx.setdefault("unread", db.unread_count())
    ctx.setdefault("open_requests", db.open_requests_count())
    ctx.setdefault("statuses", db.stage_titles())
    ctx.setdefault("stages", db.pipeline_stages())
    ctx.setdefault("system_stage", db.system_stage())
    ctx.setdefault("colors", db.STAGE_COLORS)
    ctx.setdefault("req_statuses", db.REQUEST_STATUSES)
    ctx.setdefault("channels", config.CHANNEL_TITLES)
    ctx.setdefault("ai_on", db.setting("ai_enabled_global", "1") == "1")
    ctx.setdefault("setup", onboarding.progress())
    ctx.setdefault("path", request.url.path)
    return templates.TemplateResponse(request, name, ctx)


@app.middleware("http")
async def guard(request: Request, call_next):
    """Всё, кроме входа, вебхуков и здоровья, закрыто сессией."""
    open_paths = ("/login", "/static", "/hook", "/health",
                  "/widget.js", "/api/widget")
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
    return templates.TemplateResponse(
        request, "login.html", {"error": "", "assets": assets_version()})


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
        request, "login.html",
        {"error": "Неверный логин или пароль", "assets": assets_version()},
        status_code=401,
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
        "handed": db.q1("SELECT COUNT(*) AS c FROM leads WHERE status = ?",
                        (db.system_stage(),))["c"],
        "audience": broadcast.audience_size(),
    }
    by_status = {
        key: db.q1("SELECT COUNT(*) AS c FROM leads WHERE status = ?", (key,))["c"]
        for key in db.stage_titles()
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
async def leads(request: Request, status: str = "", view: str = "board"):
    sql = (
        "SELECT l.*, c.channel, c.username, c.phone, c.name AS contact_name, c.id AS cid"
        " FROM leads l JOIN contacts c ON c.id = l.contact_id"
    )
    params: tuple = ()
    if status in db.stage_titles():
        sql += " WHERE l.status = ?"
        params = (status,)
    sql += " ORDER BY l.updated_at DESC LIMIT 300"
    rows = db.q(sql, params)
    # Доска — по колонкам этапов; таблица остаётся для тех, кому нужен список.
    board = {stage["id"]: [] for stage in db.pipeline_stages()}
    for row in rows:
        board.setdefault(row["status"], []).append(row)
    return page(request, "leads.html", rows=rows, board=board, current=status,
                view="table" if view == "table" else "board")


@app.post("/leads/stages")
async def leads_stages(request: Request):
    """Сохранить набор этапов воронки целиком."""
    form = await request.form()
    order = [key.split(".", 1)[1] for key in form if key.startswith("id.")]
    items = [{
        "id": str(form.get(f"id.{index}") or ""),
        "title": str(form.get(f"title.{index}") or ""),
        "color": str(form.get(f"color.{index}") or "gray"),
        "is_won": form.get("won") == index,
        "is_system": form.get("system") == index,
    } for index in order]
    try:
        moved = await asyncio.to_thread(db.save_pipeline_stages, items)
    except ValueError as exc:
        return RedirectResponse("/leads?error=" + quote(str(exc)), status_code=303)
    note = "Воронка сохранена"
    if moved:
        note += f". Лидов перенесено на первый этап: {moved}"
    return RedirectResponse("/leads?ok=" + quote(note), status_code=303)


@app.post("/leads/{contact_id}/stage")
async def lead_stage(contact_id: int, stage: str = Form(...)):
    """Перетащили карточку в другую колонку."""
    if stage not in db.stage_titles():
        return JSONResponse({"ok": False, "error": "неизвестный этап"}, status_code=400)
    db.set_lead_status(contact_id, stage)
    return JSONResponse({"ok": True})


@app.post("/leads/{contact_id}/status")
async def lead_status(contact_id: int, status: str = Form(...)):
    if status in db.stage_titles():
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
        f"SELECT * FROM kb_pages WHERE {knowledge.WEB_PAGES}"
        " ORDER BY included DESC, chars DESC, url LIMIT 500"
    )
    last = db.setting("kb_last_refresh", "")
    return page(request, "knowledge.html", pages=pages, stats=knowledge.stats(),
                site=db.setting("business_site", ""), extra=db.setting("kb_extra", ""),
                files=pricefile.files(),
                refresh_hours=db.setting("kb_refresh_hours", "24"),
                last_refresh=int(last) if last.isdigit() else 0)


@app.post("/knowledge/refresh")
async def kb_refresh(request: Request):
    """Перечитать страницы сайта прямо сейчас."""
    result = await asyncio.to_thread(knowledge.refresh)
    retrieval.invalidate()
    return RedirectResponse(
        f"/knowledge?changed={result['changed']}&gone={result['gone']}",
        status_code=303,
    )


@app.post("/knowledge/schedule")
async def kb_schedule(hours: str = Form("24")):
    db.set_setting("kb_refresh_hours", hours.strip() or "24")
    return RedirectResponse("/knowledge", status_code=303)


@app.post("/knowledge/file")
async def kb_file(request: Request):
    """Прайс файлом: xlsx или csv. Ключи, публикация и доступы не нужны."""
    form = await request.form()
    upload = form.get("file")
    if upload is None or not getattr(upload, "filename", ""):
        return RedirectResponse("/knowledge?error=" + quote("Выберите файл xlsx или csv"),
                                status_code=303)
    data = await upload.read()
    try:
        result = await asyncio.to_thread(pricefile.save, upload.filename, data)
    except pricefile.PriceFileError as exc:
        # Неудача не трогает уже загруженное: прежний прайс остаётся в базе знаний.
        return RedirectResponse("/knowledge?error=" + quote(f"Файл не прочитан: {exc}"),
                                status_code=303)

    note = f"Из файла «{upload.filename}» прочитано строк: {result['rows']}."
    if result["hidden"]:
        note += " Внутренние столбцы агенту не показаны: " + ", ".join(result["hidden"]) + "."
    return RedirectResponse("/knowledge?ok=" + quote(note), status_code=303)


@app.post("/knowledge/file/{page_id}/delete")
async def kb_file_delete(page_id: int):
    pricefile.remove(page_id)
    return RedirectResponse("/knowledge?ok=" + quote("Файл убран из базы знаний"),
                            status_code=303)


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
    for row in db.q(f"SELECT id FROM kb_pages WHERE {knowledge.WEB_PAGES}"):
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
    """Проверить, что агент найдёт по вопросу.

    Раньше здесь вызывался поиск по словам, а агент собирает контекст иначе —
    и панель показывала «ничего не нашлось» там, где агент видел весь каталог.
    Теперь и проверка, и агент спрашивают одну и ту же функцию.
    """
    return JSONResponse(retrieval.hits_for(q))


# ── рассылки ───────────────────────────────────────────────────────────

# Языки рассылки. Русский первый и обязательный: он же запасной вариант для
# всех, чей язык не заполнен или не переведён.
BROADCAST_LANGUAGES = [
    ("ru", "Русский"), ("en", "English"), ("es", "Español"),
    ("de", "Deutsch"), ("zh", "中文"), ("ar", "العربية"),
]


@app.get("/broadcast", response_class=HTMLResponse)
async def broadcast_page(request: Request, preview: int | None = None):
    draft = db.q1("SELECT * FROM broadcasts WHERE id = ?", (preview,)) if preview else None
    history = db.q("SELECT * FROM broadcasts ORDER BY id DESC LIMIT 30")
    stages = [row for row in db.pipeline_stages() if not row["is_won"]]
    return page(request, "broadcast.html", draft=draft, history=history,
                audience=broadcast.audience_size(),
                languages=BROADCAST_LANGUAGES, stage_options=stages,
                by_stage={row["id"]: broadcast.audience_size(row["id"]) for row in stages},
                draft_texts=json.loads(draft["texts"] or "{}") if draft else {},
                draft_buttons=broadcast.buttons_of(draft) if draft else [])


@app.get("/broadcast/audience")
async def broadcast_audience(stage: str = ""):
    """Сколько получателей под выбранным этапом — показываем до отправки."""
    return JSONResponse({"count": broadcast.audience_size(stage or None)})


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

    texts = {code: str(form.get(f"text.{code}") or "").strip()
             for code, _ in BROADCAST_LANGUAGES}
    texts = {code: value for code, value in texts.items() if value}
    buttons = []
    for index in range(3):
        title = str(form.get(f"button_text.{index}") or "").strip()
        url = str(form.get(f"button_url.{index}") or "").strip()
        if title and url:
            buttons.append({"text": title, "url": url})
        elif title or url:
            return RedirectResponse(
                "/broadcast?error=" + quote("У кнопки нужны и надпись, и ссылка"),
                status_code=303)

    stage = str(form.get("stage_filter") or "").strip()
    if stage and stage not in db.stage_titles():
        stage = ""
    if not texts and not image:
        return RedirectResponse(
            "/broadcast?error=" + quote("Напишите текст хотя бы на русском"),
            status_code=303)

    broadcast_id = broadcast.create(
        texts.get("ru", ""), image, "", "", send_at,
        texts=texts, buttons=buttons, stage_filter=stage or None,
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


# ── автоцепочки ────────────────────────────────────────────────────────

@app.get("/autochains", response_class=HTMLResponse)
async def autochains_page(request: Request, edit: int | None = None):
    rows = autochain.chains()
    chain_steps = {row["id"]: autochain.steps(row["id"]) for row in rows}
    editing = next((row for row in rows if row["id"] == edit), None)
    return page(request, "autochains.html", rows=rows, chain_steps=chain_steps,
                editing=editing,
                editing_steps=[dict(step) for step in chain_steps.get(edit, [])],
                languages=BROADCAST_LANGUAGES, stats=autochain.stats())


@app.post("/autochains")
async def autochains_save(request: Request):
    """Сохранить цепочку целиком: шаги приходят из формы плоским списком."""
    form = await request.form()
    chain_id = int(form.get("chain_id") or 0) or None
    positions = sorted({key.split(".")[1] for key in form if key.startswith("delay.")},
                       key=lambda value: int(value))
    items = []
    for position in positions:
        items.append({
            "delay_min": int(str(form.get(f"delay.{position}") or 0) or 0),
            "enabled": form.get(f"enabled.{position}") is not None,
            "texts": {code: str(form.get(f"text.{position}.{code}") or "")
                      for code, _ in BROADCAST_LANGUAGES},
            "buttons": [{"text": str(form.get(f"btitle.{position}.{index}") or ""),
                         "url": str(form.get(f"burl.{position}.{index}") or "")}
                        for index in range(3)],
        })
    try:
        await asyncio.to_thread(autochain.save_chain,
                                str(form.get("name") or ""), items, chain_id)
    except ValueError as exc:
        return RedirectResponse("/autochains?error=" + quote(str(exc)), status_code=303)
    return RedirectResponse("/autochains?ok=" + quote("Цепочка сохранена"), status_code=303)


@app.post("/autochains/{chain_id}/toggle")
async def autochains_toggle(chain_id: int):
    row = db.q1("SELECT enabled FROM autochains WHERE id = ?", (chain_id,))
    if row:
        autochain.set_enabled(chain_id, not row["enabled"])
    return RedirectResponse("/autochains", status_code=303)


@app.post("/autochains/{chain_id}/delete")
async def autochains_delete(chain_id: int):
    autochain.delete_chain(chain_id)
    return RedirectResponse("/autochains?ok=" + quote("Цепочка удалена"), status_code=303)


# ── настройки ──────────────────────────────────────────────────────────

# Ключ OpenRouter выглядит как sk-or-v1-…. Проверка нужна не для красоты:
# менеджер паролей охотно подставляет в поле типа password пароль от панели,
# и тогда рабочий ключ молча заменяется на мусор, а панель по-прежнему
# показывает «ключ есть» — искать такую поломку потом очень долго.
KEY_PREFIX = "sk-or"


def _clean_key(raw: str) -> str:
    """Убрать пробелы и переносы строк, которые прилипают при копировании."""
    return "".join(str(raw or "").split())


def _key_looks_real(key: str) -> bool:
    return key.startswith(KEY_PREFIX) and len(key) >= 20


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    keys = ["business_name", "business_site", "greeting", "tone", "model",
            "operator_chat_id", "managers", "handoff_note", "ai_enabled_global",
            "sheets_crm_id", "sheets_crm_tab"]
    values = {key: db.setting(key, "") for key in keys}
    return page(request, "settings.html", values=values,
                models=await llm.available_models(),
                telegram_on=channels.telegram_enabled(),
                whatsapp_on=config.whatsapp_enabled(),
                ai_ready=llm.ai_ready(),
                key_check=await llm.check_key(),
                key_source=_key_source(),
                sa_file=bool(config.GOOGLE_SA_FILE),
                mode=config.MODE)


@app.get("/settings/ai/check")
async def settings_ai_check():
    """Живая проверка: принят ли ключ и отвечает ли выбранная модель.

    Раньше единственным признаком «всё хорошо» был загрузившийся список
    моделей, а он отдаётся без ключа — поэтому отказ в генерации выглядел
    загадочно. Здесь видно и ответ OpenRouter на ключ, и настоящий ответ модели.
    """
    key = await llm.check_key(force=True)
    model = await llm.check_model() if key["ok"] else {
        "ok": False, "model": llm.current_model(), "detail": "сначала нужен рабочий ключ",
    }
    return JSONResponse({"key": key, "model": model})


def _key_source() -> str:
    """Откуда взялся ключ — чтобы владелец видел, что он вообще есть."""
    if db.setting("openrouter_key", "").strip():
        return "вставлен в панели"
    if config.OPENROUTER_API_KEY:
        return "взят из .env"
    return ""


async def read_site(site: str) -> dict:
    """Обойти сайт и загрузить тексты страниц.

    Обход занимает от десятков секунд до пары минут, поэтому зовётся фоном:
    держать на нём сохранение настроек нельзя. Владелец видит результат в
    разделе «База знаний».
    """
    try:
        found = await asyncio.to_thread(knowledge.discover, site)
        loaded = await asyncio.to_thread(knowledge.fetch_pending)
        retrieval.invalidate()
        log.info("сайт %s: найдено страниц %s, загружено %s",
                 site, found.get("found", 0), loaded.get("loaded", 0))
        return {"found": found.get("found", 0), "loaded": loaded.get("loaded", 0)}
    except Exception as exc:  # noqa: BLE001 — фоновая задача не должна ронять службу
        log.warning("сайт %s не прочитался: %s", site, exc)
        return {"found": 0, "loaded": 0, "error": str(exc)}


@app.post("/settings")
async def settings_save(request: Request):
    form = await request.form()
    # Адрес сайта в этом поле раньше только запоминался: владелец сохранял его
    # и ждал, что агент прочитает сайт, а обход надо было запускать руками в
    # другом разделе. Теперь сохранение нового адреса и запускает чтение.
    previous_site = db.setting("business_site", "").strip()
    for key in ("business_name", "business_site", "greeting", "tone", "model",
                "operator_chat_id", "managers", "handoff_note",
                "sheets_crm_id", "sheets_crm_tab"):
        if key in form:
            db.set_setting(key, str(form.get(key) or ""))

    # Ключ пишем только если поле заполнили: пустое поле означает
    # «оставить как было», иначе ключ стирался бы при каждом сохранении.
    raw_key = str(form.get("openrouter_key") or "")
    new_key = _clean_key(raw_key)
    if raw_key.strip() and not _key_looks_real(new_key):
        return RedirectResponse(
            "/settings?error=" + quote(
                "Это не похоже на ключ OpenRouter — он начинается на sk-or-. "
                "Сохранённый ключ оставлен без изменений."),
            status_code=303)
    if new_key:
        db.set_setting("openrouter_key", new_key)
    if form.get("drop_key"):
        db.set_setting("openrouter_key", "")

    db.set_setting("ai_enabled_global", "1" if form.get("ai_enabled_global") else "0")

    site = str(form.get("business_site") or "").strip()
    site_note = ""
    if site and site != previous_site:
        asyncio.create_task(read_site(site))
        site_note = (f" Читаю сайт {site} — страницы появятся в разделе «База знаний»"
                     " через минуту-другую.")

    # Новый ключ проверяем сразу: узнать об отказе через неделю по молчащему
    # агенту — худший из возможных вариантов.
    if new_key:
        check = await llm.check_key(force=True)
        return RedirectResponse(
            "/settings?" + ("ok=" if check["ok"] else "error=") + quote(check["detail"] + site_note),
            status_code=303)
    if site_note:
        return RedirectResponse("/settings?ok=" + quote("Настройки сохранены." + site_note),
                                status_code=303)
    return RedirectResponse("/settings", status_code=303)


@app.post("/settings/sheets/sync")
async def sheets_sync():
    """Выгрузить лидов в таблицу прямо сейчас, не дожидаясь планировщика."""
    result = await sheets.sync_leads()
    return RedirectResponse(
        "/settings?ok=" + quote(f"Выгружено лидов: {result.get('synced', 0)}"),
        status_code=303)


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


@app.post("/hook/max/{bot_id}")
async def hook_max(bot_id: int, request: Request):
    """Апдейты MAX. У каждого бота свой адрес."""
    row = db.bot(bot_id)
    if row is None or row["platform"] != "max":
        return JSONResponse({"ok": False}, status_code=404)
    payload = await request.json()
    from ..channels import maxru
    asyncio.create_task(maxru.feed(row, payload))
    return JSONResponse({"ok": True})


@app.post("/hook/vk/{bot_id}")
async def hook_vk(bot_id: int, request: Request):
    """Callback API ВК. На запрос confirmation отвечаем строкой из настроек."""
    row = db.bot(bot_id)
    if row is None or row["platform"] != "vk":
        return PlainTextResponse("not found", status_code=404)

    from ..channels import vk
    event = await request.json()

    if event.get("type") == "confirmation":
        return PlainTextResponse(vk.settings(row).get("confirm", ""))

    asyncio.create_task(vk.feed(row, event))
    # ВК ждёт ровно "ok", иначе будет слать событие снова
    return PlainTextResponse("ok")


@app.post("/hook/avito/{bot_id}")
async def hook_avito(bot_id: int, request: Request):
    """Уведомления Авито о новых сообщениях в чатах объявлений."""
    row = db.bot(bot_id)
    if row is None or row["platform"] != "avito":
        return JSONResponse({"ok": False}, status_code=404)

    from ..channels import avito
    # адрес вебхука угадывается легко, поэтому сверяем секрет из подписки
    expected = avito.webhook_secret(row)
    if expected:
        got = (request.headers.get("x-avito-messenger-signature")
               or request.headers.get("x-webhook-secret") or "")
        if got != expected:
            log.warning("вебхук Авито с чужим секретом отброшен")
            return JSONResponse({"ok": False}, status_code=403)

    payload = await request.json()
    asyncio.create_task(avito.feed(row, payload))
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

# Подключение канала — по одной карточке на мессенджер, как в рассылке.
# Раньше форма показывала сразу все поля всех платформ: владелец видел IMAP,
# client_secret и строку подтверждения ВКонтакте, даже когда подключал Telegram.
BOT_PLATFORMS = [
    {"code": "tg", "title": "Telegram", "token_label": "Токен бота",
     "token_hint": "123456789:AA…", "roles": True,
     "steps": ["Откройте <a href=\"https://t.me/BotFather\" target=\"_blank\" rel=\"noreferrer\">@BotFather</a>,"
               " создайте бота командой <b>/newbot</b> и скопируйте токен."],
     "fields": []},
    {"code": "max", "title": "MAX", "token_label": "Токен бота",
     "token_hint": "токен из @MasterBot", "roles": False,
     "steps": ["Внутри MAX напишите <b>@MasterBot</b>, создайте бота и скопируйте токен."],
     "fields": []},
    {"code": "vk", "title": "ВКонтакте", "token_label": "Ключ доступа сообщества",
     "token_hint": "vk1.a.…", "roles": False,
     "steps": ["Сообщество → Настройки → Работа с API → создайте ключ с правом «Сообщения».",
               "Там же, Callback API: строку подтверждения впишите ниже."],
     "fields": [{"name": "confirm", "label": "Строка подтверждения",
                 "hint": "из раздела Callback API"}]},
    {"code": "avito", "title": "Авито", "token_label": "client_id",
     "token_hint": "из кабинета продавца", "roles": False,
     "steps": ["Кабинет продавца → Настройки → Avito API: создайте приложение и возьмите пару client_id / client_secret."],
     "fields": [{"name": "secret", "label": "client_secret", "hint": "вторая половина пары"}]},
    {"code": "mail", "title": "Почта", "token_label": "Пароль приложения",
     "token_hint": "не обычный пароль от почты", "roles": False,
     "steps": ["В почте включите IMAP и создайте пароль приложения — обычный пароль не подойдёт."],
     "fields": [{"name": "login", "label": "Адрес ящика", "hint": "sales@example.com"},
                {"name": "imap_host", "label": "IMAP-сервер", "hint": "определим по домену"},
                {"name": "smtp_host", "label": "SMTP-сервер", "hint": "определим по домену"}]},
]


@app.get("/bots", response_class=HTMLResponse)
async def bots_page(request: Request):
    rows = db.bots(only_enabled=False)
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["platform"]] = counts.get(row["platform"], 0) + 1
    return page(request, "bots.html", rows=rows, live=channels.live_ids(), mode=config.MODE,
                public_url=config.PUBLIC_URL, platforms=BOT_PLATFORMS, counts=counts)


@app.post("/bots/add")
async def bots_add(title: str = Form(""), token: str = Form(...),
                   role: str = Form("sales"), platform: str = Form("tg"),
                   confirm: str = Form(""), login: str = Form(""),
                   imap_host: str = Form(""), smtp_host: str = Form(""),
                   secret: str = Form("")):
    """Добавить бота по токену. Токен проверяем до сохранения."""
    token = token.strip()
    platform = platform if platform in ("tg", "max", "vk", "mail", "avito") else "tg"

    conf = {}
    if platform == "mail":
        conf = {"login": login.strip(),
                "imap_host": imap_host.strip() or f"imap.{login.split('@')[-1]}",
                "imap_port": 993,
                "smtp_host": smtp_host.strip() or f"smtp.{login.split('@')[-1]}",
                "smtp_port": 465}
    elif platform == "avito":
        conf = {"client_secret": secret.strip()}
    probe = await channels.check_token(platform, token, conf)
    if not probe["ok"]:
        return RedirectResponse(f"/bots?error={probe['error'][:120]}", status_code=303)

    if db.q1("SELECT 1 FROM bots WHERE token = ?", (token,)):
        return RedirectResponse("/bots?error=такой+бот+уже+добавлен", status_code=303)

    # у ВК свои настройки: id сообщества и строка подтверждения для Callback
    extra = None
    if platform == "vk":
        extra = json.dumps({"group_id": probe.get("group_id"),
                            "confirm": confirm.strip()}, ensure_ascii=False)
    elif platform in ("mail", "avito"):
        if platform == "avito":
            conf["user_id"] = probe.get("user_id")
        extra = json.dumps(conf, ensure_ascii=False)

    db.add_bot(title.strip() or f"@{probe['username']}", token,
               role if role in ("sales", "manager") else "sales", platform, extra)
    await channels.reload_all()
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
    await channels.reload_all()
    return RedirectResponse("/bots", status_code=303)


@app.post("/bots/{bot_id}/delete")
async def bots_delete(bot_id: int):
    db.run("DELETE FROM bots WHERE id = ?", (bot_id,))
    await channels.reload_all()
    return RedirectResponse("/bots", status_code=303)


@app.post("/bots/reload")
async def bots_reload():
    """Перечитать реестр и переставить вебхуки — без перезапуска службы."""
    await channels.reload_all()
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
        files=pricefile.files(),
        extra=db.setting("kb_extra", ""),
        script=db.script(),
        templates=db.SCRIPT_TEMPLATES,
        ai_ready=llm.ai_ready(),
        key_source=_key_source(),
        model=llm.current_model(),
    )


@app.post("/setup/bot")
async def setup_bot(title: str = Form(""), token: str = Form(...),
                    role: str = Form("sales"), platform: str = Form("tg")):
    platform = platform if platform in ("tg", "max") else "tg"
    probe = await channels.check_token(platform, token.strip())
    if not probe["ok"]:
        return RedirectResponse(f"/setup?step=bot&error={probe['error'][:100]}", status_code=303)
    if not db.q1("SELECT 1 FROM bots WHERE token = ?", (token.strip(),)):
        db.add_bot(title.strip() or f"@{probe['username']}", token.strip(),
                   role if role in ("sales", "manager") else "sales", platform)
        await channels.reload_all()
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

    upload = form.get("file")
    file_error = ""
    if upload is not None and getattr(upload, "filename", ""):
        try:
            await asyncio.to_thread(pricefile.save, upload.filename, await upload.read())
        except pricefile.PriceFileError as exc:
            file_error = f"Файл не прочитан: {exc}"

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
    if file_error:
        return RedirectResponse("/setup?step=knowledge&error=" + quote(file_error),
                                status_code=303)
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
    raw_key = str(form.get("openrouter_key") or "")
    key = _clean_key(raw_key)
    if raw_key.strip() and not _key_looks_real(key):
        return RedirectResponse(
            "/setup?step=launch&error=" + quote("Ключ OpenRouter начинается на sk-or-. Проверьте, что скопировали именно его."),
            status_code=303)
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


# ── онлайн-запись ──────────────────────────────────────────────────────

@app.get("/booking", response_class=HTMLResponse)
async def booking_page(request: Request):
    return page(request, "booking.html",
                services=booking.services(only_enabled=False),
                staff=booking.staff(only_enabled=False),
                hours=booking.hours(),
                weekdays=booking.WEEKDAYS,
                rows=booking.upcoming(),
                slots=booking.free_slots(limit=8) if booking.enabled() else [],
                on=db.setting("booking_enabled", "0") == "1",
                remind=db.setting("booking_remind_hours", "3"))


@app.post("/booking/settings")
async def booking_settings(request: Request):
    form = await request.form()
    db.set_setting("booking_enabled", "1" if form.get("on") else "0")
    db.set_setting("booking_remind_hours", str(form.get("remind") or "3"))
    for weekday in range(7):
        open_at = str(form.get(f"open_{weekday}") or "").strip()
        close_at = str(form.get(f"close_{weekday}") or "").strip()
        db.run(
            "INSERT INTO work_hours (weekday, open_at, close_at) VALUES (?, ?, ?)"
            " ON CONFLICT(weekday) DO UPDATE SET open_at = excluded.open_at,"
            " close_at = excluded.close_at",
            (weekday, open_at or None, close_at or None),
        )
    return RedirectResponse("/booking", status_code=303)


@app.post("/booking/service")
async def booking_service(title: str = Form(...), duration: int = Form(60),
                          price: str = Form("")):
    db.run("INSERT INTO services (title, duration_min, price) VALUES (?, ?, ?)",
           (title.strip(), max(duration, 5), price.strip() or None))
    return RedirectResponse("/booking", status_code=303)


@app.post("/booking/service/{service_id}/delete")
async def booking_service_delete(service_id: int):
    db.run("DELETE FROM services WHERE id = ?", (service_id,))
    return RedirectResponse("/booking", status_code=303)


@app.post("/booking/staff")
async def booking_staff(name: str = Form(...)):
    db.run("INSERT INTO staff (name) VALUES (?)", (name.strip(),))
    return RedirectResponse("/booking", status_code=303)


@app.post("/booking/staff/{staff_id}/delete")
async def booking_staff_delete(staff_id: int):
    db.run("DELETE FROM staff WHERE id = ?", (staff_id,))
    return RedirectResponse("/booking", status_code=303)


@app.post("/booking/{booking_id}/cancel")
async def booking_cancel(booking_id: int):
    db.run("UPDATE bookings SET status = 'cancelled' WHERE id = ?", (booking_id,))
    return RedirectResponse("/booking", status_code=303)


# ── чат для сайта ──────────────────────────────────────────────────────
# Виджет живёт на чужом домене, поэтому этим адресам нужен CORS и открытый
# доступ без сессии. Данные защищены подписанным токеном посетителя.

def _cors(payload: dict) -> JSONResponse:
    return JSONResponse(payload, headers={
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Headers": "Content-Type",
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    })


@app.get("/widget.js")
async def widget_js():
    """Скрипт виджета. Отдаём с открытым CORS — он грузится с чужих сайтов."""
    return FileResponse(
        BASE_DIR / "static" / "widget.js",
        media_type="application/javascript",
        headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "public, max-age=300"},
    )


@app.options("/api/widget/{rest:path}")
async def widget_preflight(rest: str):
    return _cors({"ok": True})


@app.post("/api/widget/start")
async def widget_start():
    """Новый посетитель: выдаём подписанный токен и оформление окна."""
    if not webchat.enabled():
        return _cors({"ok": False})
    return _cors({
        "ok": True,
        "token": webchat.new_visitor(),
        "title": db.setting("widget_title", "Чат"),
        "color": db.setting("widget_color", "#0a7c47"),
        "greeting": db.setting("widget_greeting", ""),
    })


@app.post("/api/widget/send")
async def widget_send(request: Request):
    if not webchat.enabled():
        return _cors({"ok": False})
    data = await request.json()
    contact = webchat.contact_for(str(data.get("token") or ""),
                                  language=str(data.get("language") or "") or None)
    if contact is None:
        return _cors({"ok": False, "error": "bad token"})

    text = str(data.get("text") or "").strip()[:4000]
    if text:
        await sales.handle_incoming(contact["id"], text)
    return _cors({"ok": True})


@app.get("/api/widget/poll")
async def widget_poll(token: str = "", after: int = 0):
    if not webchat.enabled():
        return _cors({"ok": False})
    contact = webchat.contact_for(token)
    if contact is None:
        return _cors({"ok": False, "error": "bad token"})
    return _cors({"ok": True, "messages": webchat.history_after(contact["id"], after)})


@app.get("/widget", response_class=HTMLResponse)
async def widget_settings(request: Request):
    return page(request, "widget.html",
                snippet=webchat.snippet(),
                on=webchat.enabled(),
                values={k: db.setting(k, "") for k in
                        ("widget_title", "widget_color", "widget_greeting")})


@app.post("/widget/save")
async def widget_save(request: Request):
    form = await request.form()
    db.set_setting("widget_enabled", "1" if form.get("on") else "0")
    for key in ("widget_title", "widget_color", "widget_greeting"):
        if key in form:
            db.set_setting(key, str(form.get(key) or ""))
    return RedirectResponse("/widget", status_code=303)


# ── каналы: витрина подключений ────────────────────────────────────────

CHANNEL_CARDS = [
    {"code": "tg", "title": "Telegram", "link": "/bots",
     "about": "ИИ-бот отвечает клиентам, ведёт по сценарию и передаёт менеджеру. "
              "Ботов можно подключить сколько угодно.",
     "tags": ["ИИ-бот", "Рассылки", "Фото и файлы", "Голосовые", "Кнопки"]},
    {"code": "web", "title": "Чат на сайте", "link": "/widget",
     "about": "Виджет ставится на сайт одной строкой. Ни токенов, ни ключей — "
              "работает сразу.",
     "tags": ["ИИ-бот", "Своё оформление", "Без ключей"]},
    {"code": "wa", "title": "WhatsApp Business", "link": "/settings",
     "about": "Через официальный Cloud API. Нужен домен с HTTPS и режим вебхуков.",
     "tags": ["ИИ-бот", "Фото и файлы", "Нужен домен"]},
    {"code": "max", "title": "MAX", "link": "/bots",
     "about": "Мессенджер VK. Токен берётся у @MasterBot внутри MAX.",
     "tags": ["ИИ-бот", "Вложения", "Кнопки"]},
    {"code": "vk", "title": "ВКонтакте", "link": "/bots",
     "about": "Сообщения сообщества. Ключ доступа — в настройках сообщества, "
              "раздел «Работа с API».",
     "tags": ["ИИ-бот", "Картинки", "Long Poll и Callback"]},
    {"code": "avito", "title": "Авито", "link": "/bots",
     "about": "Переписка с покупателями в объявлениях. Доступ выдаёт продавец "
              "в личном кабинете.",
     "tags": ["ИИ-бот", "Чаты объявлений", "Нужен домен"]},
    {"code": "mail", "title": "Почта", "link": "/bots",
     "about": "Тот же агент отвечает на письма и держит переписку в одном треде.",
     "tags": ["ИИ-бот", "IMAP и SMTP", "Ответ в тред"]},
]


@app.get("/channels", response_class=HTMLResponse)
async def channels_page(request: Request):
    """Витрина каналов: что подключено, что можно подключить."""
    live = channels.active()
    bots = db.bots(only_enabled=False)

    cards = []
    for card in CHANNEL_CARDS:
        code = card["code"]
        connected = code in live
        # сколько ботов этой платформы заведено
        count = sum(1 for b in bots if b["platform"] == code)
        cards.append({**card, "connected": connected, "count": count,
                      "contacts": db.q1(
                          "SELECT COUNT(*) AS c FROM contacts WHERE channel = ?",
                          (code,))["c"]})

    return page(request, "channels.html", cards=cards, live=live)
