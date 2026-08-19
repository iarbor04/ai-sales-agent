"""Документы в базу знаний: PDF, DOCX, TXT и Markdown.

Прайс — это таблица, а условия, гарантии и регламенты живут в документах, и
переносить их руками в поле «вписать руками» никто не будет.

DOCX читается стандартной библиотекой: это zip с XML внутри, как xlsx. Для PDF
взят pypdf — чистый Python без системных зависимостей. Самодельный разбор PDF
здесь был бы ошибкой: там сжатые потоки, шрифты и кодировки, и на половине
настоящих файлов он молча отдавал бы кашу вместо текста, а это хуже отсутствия
функции.

Скан без текстового слоя мы честно отказываемся принимать: распознавания
изображений в проекте нет, а пустой документ в базе знаний хуже, чем его
отсутствие — агент будет думать, что ответ есть.
"""
from __future__ import annotations

import io
import logging
import re
import zipfile
from xml.etree import ElementTree

from . import knowledge

log = logging.getLogger("docfile")

# pypdf охотно ругается на мелкие огрехи вёрстки («Ignoring wrong pointing
# object») в совершенно читаемых файлах. В журнале службы это шум, который
# мешает видеть настоящие ошибки.
logging.getLogger("pypdf").setLevel(logging.ERROR)

MAX_BYTES = 25 * 1024 * 1024
SUPPORTED = (".pdf", ".docx", ".txt", ".md")
# Ниже этого порога считаем, что текста в файле нет: у сканов pypdf возвращает
# пустоту или несколько случайных символов из подписей.
MIN_TEXT_CHARS = 40

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


class DocumentError(ValueError):
    """Понятная владельцу причина, почему документ не прочитался."""


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp1251"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _docx(data: bytes) -> str:
    """Абзацы и таблицы документа Word.

    Таблицы важны не меньше текста: в них обычно и лежат условия и тарифы.
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise DocumentError(
            "файл не похож на docx. Старый формат .doc не читается — "
            "откройте в Word и сохраните как docx") from exc

    if "word/document.xml" not in archive.namelist():
        raise DocumentError("внутри docx нет документа — файл повреждён")

    root = ElementTree.fromstring(archive.read("word/document.xml"))
    lines: list[str] = []

    def paragraph_text(node) -> str:
        return "".join(item.text or "" for item in node.iter(f"{_W}t")).strip()

    body = root.find(f"{_W}body")
    for node in list(body) if body is not None else []:
        if node.tag == f"{_W}p":
            text = paragraph_text(node)
            if text:
                lines.append(text)
        elif node.tag == f"{_W}tbl":
            for row in node.iter(f"{_W}tr"):
                cells = [paragraph_text(cell) for cell in row.iter(f"{_W}tc")]
                cells = [cell for cell in cells if cell]
                if cells:
                    lines.append(" | ".join(cells))
    return "\n".join(lines)


def _pdf(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # noqa: F841 — зависимость обязательна, но объясняем
        raise DocumentError(
            "на сервере не установлен pypdf — выполните "
            "«.venv/bin/pip install -r requirements.txt»") from exc

    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001 — библиотека бросает своё на битых файлах
        raise DocumentError(f"PDF не читается: {str(exc)[:120]}") from exc

    if getattr(reader, "is_encrypted", False):
        try:
            reader.decrypt("")
        except Exception as exc:  # noqa: BLE001
            raise DocumentError("PDF защищён паролем — снимите пароль и загрузите заново") from exc

    pages = []
    for number, page in enumerate(reader.pages, start=1):
        try:
            pages.append((page.extract_text() or "").strip())
        except Exception as exc:  # noqa: BLE001 — одна страница не должна ронять весь файл
            log.warning("страница %s PDF не прочиталась: %s", number, exc)
    return "\n\n".join(part for part in pages if part)


def read_text(file_name: str, data: bytes) -> str:
    """Текст документа. Формат определяем по расширению."""
    name = (file_name or "").strip().lower()
    # Про старый Word говорим прямо: общий список форматов тут не помогает,
    # человеку нужно одно действие — пересохранить.
    if name.endswith(".doc"):
        raise DocumentError("старый формат .doc не читается — откройте в Word "
                            "и сохраните как docx")
    if name.endswith(".rtf") or name.endswith(".odt"):
        raise DocumentError("этот формат не читается — сохраните документ как docx или PDF")
    if not name.endswith(SUPPORTED):
        raise DocumentError("подойдёт PDF, DOCX, TXT или Markdown")
    if not data:
        raise DocumentError("файл пустой")
    if len(data) > MAX_BYTES:
        raise DocumentError("файл больше 25 МБ — разбейте его на части")

    if name.endswith(".pdf"):
        text = _pdf(data)
        if len(text) < MIN_TEXT_CHARS:
            raise DocumentError(
                "в PDF нет текстового слоя — похоже, это сканы страниц. "
                "Распознавания у нас нет: сохраните документ с текстом или "
                "скопируйте его в поле «вписать руками»")
    elif name.endswith(".docx"):
        text = _docx(data)
    else:
        text = _decode(data)

    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) < MIN_TEXT_CHARS:
        raise DocumentError("в документе почти нет текста — проверьте, что загрузили нужный файл")
    return text


def save(file_name: str, data: bytes) -> dict:
    """Прочитать документ и положить его в базу знаний.

    Неудача ничего не портит: прежняя версия остаётся на месте, пока новая не
    прочиталась целиком.
    """
    text = read_text(file_name, data)
    page_id = knowledge.save_upload(file_name, text)
    log.info("документ «%s»: символов %s", file_name, len(text))
    return {"chars": len(text), "page_id": page_id}
