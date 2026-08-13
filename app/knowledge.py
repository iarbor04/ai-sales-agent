"""База знаний: обход сайта клиента и превращение страниц в текст.

Работает на стандартной библиотеке — ни requests, ни bs4. Это сознательно:
агенту, который деплоит проект, нечего чинить в зависимостях, а на слабой
машине нечему падать.

Сценарий из ТЗ: по одному домену находим внутренние публичные страницы,
показываем список, владелец исключает лишние, включённые грузим в базу.
Поэтому обход разделён на два шага: discover() только собирает адреса,
fetch_pending() выкачивает текст только у включённых.
"""
from __future__ import annotations

import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

from . import config, db

log = logging.getLogger("kb")

UA = "Mozilla/5.0 (compatible; AiSalesBot/1.0)"

# Расширения, которые заведомо не текст.
SKIP_EXT = (
    ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".ico", ".pdf", ".zip",
    ".rar", ".mp4", ".mp3", ".avi", ".doc", ".docx", ".xls", ".xlsx", ".css", ".js",
)

# Разделы, которые в базе знаний только мешают.
SKIP_PARTS = ("/wp-admin", "/wp-json", "/cart", "/checkout", "/login", "/admin", "/feed")


class _LinkParser(HTMLParser):
    """Собирает href'ы и <title>."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag == "title":
            self._in_title = True
        if tag == "a":
            for key, value in attrs:
                if key == "href" and value:
                    self.links.append(value)

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title and not self.title:
            self.title = data.strip()[:200]


class _TextParser(HTMLParser):
    """Вытаскивает видимый текст, выбрасывая скрипты, стили, меню и подвал."""

    DROP = {"script", "style", "noscript", "nav", "footer", "header", "svg", "form",
            "title", "head"}
    BREAK = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "tr", "section"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._depth = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in self.DROP:
            self._depth += 1
        elif tag in self.BREAK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.DROP and self._depth:
            self._depth -= 1

    def handle_data(self, data: str) -> None:
        if self._depth:
            return
        text = data.strip()
        if text:
            self.parts.append(text + " ")

    def text(self) -> str:
        raw = "".join(self.parts)
        raw = re.sub(r"[ \t]+", " ", raw)
        raw = re.sub(r"\n\s*\n+", "\n\n", raw)
        return raw.strip()


def _get(url: str) -> str | None:
    """Скачать страницу. None — если не HTML или недоступна.

    По ТЗ: недоступная страница пропускается, обработка остальных продолжается.
    """
    try:
        request = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(request, timeout=config.CRAWL_TIMEOUT) as resp:
            ctype = resp.headers.get("Content-Type", "")
            if "html" not in ctype.lower():
                return None
            raw = resp.read(2_000_000)
        charset = "utf-8"
        match = re.search(r"charset=([\w-]+)", ctype)
        if match:
            charset = match.group(1)
        return raw.decode(charset, errors="replace")
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as exc:
        log.info("страница недоступна, пропускаем: %s (%s)", url, exc)
        return None


def _normalize(url: str) -> str:
    """Убрать якорь и utm-хвосты, чтобы одна страница не попала дважды."""
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parts.query)
    query = [(k, v) for k, v in query if not k.lower().startswith(("utm_", "fbclid", "gclid"))]
    path = parts.path.rstrip("/") or "/"
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, path, urllib.parse.urlencode(query), "")
    )


def _worth_crawling(url: str, host: str) -> bool:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return False
    if parts.netloc != host:
        return False
    low = parts.path.lower()
    if low.endswith(SKIP_EXT):
        return False
    return not any(part in low for part in SKIP_PARTS)


def discover(site: str, max_pages: int | None = None) -> dict:
    """Обойти домен и записать найденные адреса в kb_pages со статусом found.

    Текст на этом шаге не качаем — сначала владелец исключает лишнее.
    """
    max_pages = max_pages or config.CRAWL_MAX_PAGES
    if not site.startswith("http"):
        site = "https://" + site
    start = _normalize(site)
    host = urllib.parse.urlsplit(start).netloc

    seen: set[str] = {start}
    queue: list[str] = [start]
    found = 0

    while queue and found < max_pages:
        url = queue.pop(0)
        html = _get(url)
        if html is None:
            continue

        parser = _LinkParser()
        try:
            parser.feed(html)
        except Exception:  # noqa: BLE001 — кривая разметка не должна ронять обход
            pass

        db.run(
            "INSERT INTO kb_pages (url, title, included, status) VALUES (?, ?, 1, 'found')"
            " ON CONFLICT(url) DO UPDATE SET title = COALESCE(excluded.title, kb_pages.title)",
            (url, parser.title or url),
        )
        found += 1

        for href in parser.links:
            absolute = _normalize(urllib.parse.urljoin(url, href))
            if absolute in seen or not _worth_crawling(absolute, host):
                continue
            seen.add(absolute)
            queue.append(absolute)

        time.sleep(0.2)  # вежливость к чужому серверу

    return {"found": found, "host": host}


def fetch_pending() -> dict:
    """Выкачать текст для включённых страниц, у которых его ещё нет."""
    pages = db.q(
        "SELECT * FROM kb_pages WHERE included = 1 AND (text IS NULL OR text = '')"
    )
    loaded, skipped = 0, 0
    for page in pages:
        html = _get(page["url"])
        if html is None:
            db.run("UPDATE kb_pages SET status = 'error' WHERE id = ?", (page["id"],))
            skipped += 1
            continue

        parser = _TextParser()
        try:
            parser.feed(html)
        except Exception:  # noqa: BLE001
            pass
        text = parser.text()

        db.run(
            "UPDATE kb_pages SET text = ?, chars = ?, status = 'loaded', fetched_at = ?"
            " WHERE id = ?",
            (text, len(text), db.now(), page["id"]),
        )
        _rechunk(page["id"], text)
        loaded += 1
        time.sleep(0.2)

    return {"loaded": loaded, "skipped": skipped}


def _rechunk(page_id: int, text: str) -> None:
    """Порезать страницу на куски по абзацам — из них потом собирается ответ."""
    db.run("DELETE FROM kb_chunks WHERE page_id = ?", (page_id,))
    if not text:
        return

    chunk, size = [], 0
    for paragraph in text.split("\n\n"):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if size + len(paragraph) > config.CHUNK_CHARS and chunk:
            db.run("INSERT INTO kb_chunks (page_id, text) VALUES (?, ?)",
                   (page_id, "\n".join(chunk)))
            chunk, size = [], 0
        chunk.append(paragraph)
        size += len(paragraph)
    if chunk:
        db.run("INSERT INTO kb_chunks (page_id, text) VALUES (?, ?)",
               (page_id, "\n".join(chunk)))


def reindex_extra() -> None:
    """Пересобрать куски для текста, вписанного руками в Настройках."""
    row = db.q1("SELECT id FROM kb_pages WHERE url = 'manual://extra'")
    extra = db.setting("kb_extra", "").strip()

    if not extra:
        if row:
            db.run("DELETE FROM kb_chunks WHERE page_id = ?", (row["id"],))
            db.run("DELETE FROM kb_pages WHERE id = ?", (row["id"],))
        return

    if row:
        page_id = row["id"]
        db.run("UPDATE kb_pages SET text = ?, chars = ?, status = 'loaded', fetched_at = ?"
               " WHERE id = ?", (extra, len(extra), db.now(), page_id))
    else:
        page_id = db.run(
            "INSERT INTO kb_pages (url, title, text, chars, included, status, fetched_at)"
            " VALUES ('manual://extra', 'Вписано вручную', ?, ?, 1, 'loaded', ?)",
            (extra, len(extra), db.now()),
        )
    _rechunk(page_id, extra)


def stats() -> dict:
    total = db.q1("SELECT COUNT(*) AS c FROM kb_pages")["c"]
    included = db.q1("SELECT COUNT(*) AS c FROM kb_pages WHERE included = 1")["c"]
    loaded = db.q1("SELECT COUNT(*) AS c FROM kb_pages WHERE status = 'loaded'")["c"]
    chunks = db.q1("SELECT COUNT(*) AS c FROM kb_chunks")["c"]
    return {"total": total, "included": included, "loaded": loaded, "chunks": chunks}
