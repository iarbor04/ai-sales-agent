"""Прайс и любые таблицы файлом: xlsx и csv.

Файл кладётся в базу знаний так же, как страница сайта: каждая строка
становится фактом «Заголовок: значение». Разбирать структуру заранее не нужно —
одинаково работает и прайс на десять колонок, и таблица «вопрос / ответ».

xlsx читается стандартной библиотекой: это zip с XML внутри, и ради него не
стоит тащить в проект ещё одну зависимость. Старый бинарный .xls не
поддерживается — Excel и Google Таблицы сохраняют в xlsx, а для остального есть
csv.
"""
from __future__ import annotations

import csv
import io
import logging
import re
import zipfile
from xml.etree import ElementTree

from . import db, knowledge, retrieval

log = logging.getLogger("pricefile")

MAX_BYTES = 10 * 1024 * 1024
SUPPORTED = (".xlsx", ".csv", ".tsv")

# Столбцы, которые клиенту видеть нельзя. В прайсе рядом с розницей почти
# всегда лежит закупка и наценка, а агент отвечает клиенту по базе знаний —
# то есть без фильтра он может назвать себестоимость товара.
INTERNAL_COLUMNS = (
    "закуп", "себестоим", "cost", "purchase", "наценк", "маржа", "margin",
    "markup", "поставщик", "supplier", "прибыл", "profit", "оптов",
)

_NS = {
    "main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "rel": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "pkg": "http://schemas.openxmlformats.org/package/2006/relationships",
}


class PriceFileError(ValueError):
    """Понятная владельцу причина, почему файл не прочитался."""


def internal_column(header: str) -> bool:
    low = header.lower()
    return any(word in low for word in INTERNAL_COLUMNS)


def _decode(data: bytes) -> str:
    """Excel в России охотно сохраняет csv в cp1251 — учитываем оба варианта."""
    for encoding in ("utf-8-sig", "utf-8", "cp1251"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _read_csv(data: bytes) -> list[dict]:
    text = _decode(data)
    sample = text[:4000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        delimiter = dialect.delimiter
    except csv.Error:
        # одна колонка или необычный разделитель — берём самый частый из знакомых
        delimiter = max(",;\t", key=sample.count)
    return list(csv.DictReader(io.StringIO(text), delimiter=delimiter))


def _column_index(reference: str) -> int:
    """A1 → 0, B7 → 1, AA3 → 26. Нужно, чтобы пустые ячейки не сдвигали строку."""
    letters = re.match(r"[A-Z]+", reference or "")
    index = 0
    for char in letters.group(0) if letters else "A":
        index = index * 26 + (ord(char) - 64)
    return index - 1


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
    return ["".join(node.itertext()) for node in root.findall("main:si", _NS)]


def _sheet_paths(archive: zipfile.ZipFile) -> list[str]:
    """Листы в том порядке, в каком их видит человек в Excel."""
    try:
        workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
        rels = ElementTree.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    except KeyError:
        return sorted(n for n in archive.namelist() if n.startswith("xl/worksheets/sheet"))

    targets = {node.get("Id"): node.get("Target", "") for node in rels}
    paths = []
    for sheet in workbook.findall("main:sheets/main:sheet", _NS):
        target = targets.get(sheet.get(f"{{{_NS['rel']}}}id"), "")
        if not target:
            continue
        path = target[1:] if target.startswith("/") else f"xl/{target}"
        if path in archive.namelist():
            paths.append(path)
    return paths


def _sheet_rows(archive: zipfile.ZipFile, path: str, strings: list[str]) -> list[list[str]]:
    root = ElementTree.fromstring(archive.read(path))
    rows = []
    for row in root.findall("main:sheetData/main:row", _NS):
        values: list[str] = []
        for cell in row.findall("main:c", _NS):
            index = _column_index(cell.get("r", ""))
            while len(values) < index:
                values.append("")
            kind = cell.get("t")
            if kind == "s":
                node = cell.find("main:v", _NS)
                position = int(node.text) if node is not None and node.text else -1
                text = strings[position] if 0 <= position < len(strings) else ""
            elif kind == "inlineStr":
                node = cell.find("main:is", _NS)
                text = "".join(node.itertext()) if node is not None else ""
            else:
                node = cell.find("main:v", _NS)
                text = node.text or "" if node is not None else ""
                # 3960.0 в ячейке — это цена 3960, а не «3960.0»
                if text.endswith(".0"):
                    text = text[:-2]
            values.append(text.strip())
        rows.append(values)
    return rows


def _read_xlsx(data: bytes) -> list[dict]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise PriceFileError(
            "файл не похож на xlsx. Старый формат .xls не читается — "
            "откройте его в Excel и сохраните как xlsx или csv") from exc

    strings = _shared_strings(archive)
    records: list[dict] = []
    for path in _sheet_paths(archive):
        rows = [row for row in _sheet_rows(archive, path, strings) if any(row)]
        if len(rows) < 2:
            continue
        headers = rows[0]
        for row in rows[1:]:
            record = {}
            for position, header in enumerate(headers):
                if header:
                    record[header] = row[position] if position < len(row) else ""
            if record:
                records.append(record)
    return records


def read_rows(file_name: str, data: bytes) -> list[dict]:
    """Строки таблицы из файла. Формат определяем по расширению."""
    name = (file_name or "").strip().lower()
    if not name.endswith(SUPPORTED):
        raise PriceFileError("подойдёт xlsx или csv. Старый .xls нужно пересохранить")
    if not data:
        raise PriceFileError("файл пустой")
    if len(data) > MAX_BYTES:
        raise PriceFileError("файл больше 10 МБ — оставьте в нём только нужные листы")
    return _read_xlsx(data) if name.endswith(".xlsx") else _read_csv(data)


def rows_to_text(rows: list[dict]) -> tuple[str, list[str]]:
    """Строки → текст для базы знаний. Внутренние столбцы отбрасываем."""
    blocks = []
    hidden: list[str] = []
    for row in rows:
        parts = []
        for header, value in row.items():
            title = (header or "").strip()
            if not title or not str(value or "").strip():
                continue
            if internal_column(title):
                if title not in hidden:
                    hidden.append(title)
                continue
            parts.append(f"{title}: {str(value).strip()}")
        if parts:
            blocks.append("\n".join(parts))
    return "\n\n".join(blocks), hidden


def page_url(file_name: str) -> str:
    return "file://" + re.sub(r"[^\w.\-]+", "-", (file_name or "файл").strip(), flags=re.UNICODE)


def save(file_name: str, data: bytes) -> dict:
    """Прочитать файл и положить его в базу знаний.

    Неудача ничего не портит: старая версия файла остаётся на месте, пока
    новая не прочиталась целиком.
    """
    rows = read_rows(file_name, data)
    text, hidden = rows_to_text(rows)
    if not text:
        raise PriceFileError("в файле не нашлось строк с данными — "
                             "проверьте, что первая строка это заголовки столбцов")

    url = page_url(file_name)
    existing = db.q1("SELECT id FROM kb_pages WHERE url = ?", (url,))
    if existing:
        page_id = existing["id"]
        db.run(
            "UPDATE kb_pages SET title = ?, text = ?, chars = ?, included = 1,"
            " status = 'loaded', fetched_at = ? WHERE id = ?",
            (file_name, text, len(text), db.now(), page_id),
        )
    else:
        page_id = db.run(
            "INSERT INTO kb_pages (url, title, text, chars, included, status, fetched_at)"
            " VALUES (?, ?, ?, ?, 1, 'loaded', ?)",
            (url, file_name, text, len(text), db.now()),
        )

    knowledge._rechunk(page_id, text)
    retrieval.invalidate()
    log.info("прайс из файла «%s»: строк %s, символов %s", file_name, len(rows), len(text))
    if hidden:
        log.info("внутренние столбцы в базу знаний не попали: %s", ", ".join(hidden))
    return {"rows": len(rows), "chars": len(text), "hidden": hidden, "page_id": page_id}


def files() -> list:
    """Загруженные файлы — для списка в разделе «База знаний»."""
    return db.q("SELECT * FROM kb_pages WHERE url LIKE 'file://%' ORDER BY title")


def remove(page_id: int) -> None:
    row = db.q1("SELECT id FROM kb_pages WHERE id = ? AND url LIKE 'file://%'", (page_id,))
    if not row:
        return
    db.run("DELETE FROM kb_chunks WHERE page_id = ?", (page_id,))
    db.run("DELETE FROM kb_pages WHERE id = ?", (page_id,))
    retrieval.invalidate()
