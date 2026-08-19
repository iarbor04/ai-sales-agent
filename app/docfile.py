"""Документы в базу знаний: DOCX, TXT и Markdown.

Прайс — это таблица, а условия, гарантии и регламенты живут в документах, и
переносить их руками в поле «вписать руками» никто не будет.

Всё читается стандартной библиотекой: docx — это zip с XML внутри, как xlsx.
Сторонних зависимостей здесь нет намеренно: коробку разворачивает агент запуска
одной командой, и каждый лишний пакет — это ещё одна причина, по которой
установка встанет на чужом сервере.

PDF по этой же причине не поддерживаем: без внешней библиотеки текст из него не
достать, а самодельный разбор на половине настоящих файлов молча отдаёт кашу.
Владельцу говорим прямо, что делать вместо этого.
"""
from __future__ import annotations

import io
import logging
import re
import zipfile
from xml.etree import ElementTree

from . import knowledge

log = logging.getLogger("docfile")

MAX_BYTES = 25 * 1024 * 1024
SUPPORTED = (".docx", ".txt", ".md")
# Ниже этого порога считаем, что текста в файле нет.
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


def read_text(file_name: str, data: bytes) -> str:
    """Текст документа. Формат определяем по расширению."""
    name = (file_name or "").strip().lower()
    # Про каждый непринятый формат говорим одно понятное действие, а не общий
    # список: человеку нужно знать, что сделать, а не что бывает.
    if name.endswith(".pdf"):
        raise DocumentError("PDF мы не читаем: сохраните документ как DOCX "
                            "(в Word «Сохранить как») или скопируйте текст "
                            "в поле «вписать руками»")
    if name.endswith(".doc"):
        raise DocumentError("старый формат .doc не читается — откройте в Word "
                            "и сохраните как docx")
    if name.endswith(".rtf") or name.endswith(".odt"):
        raise DocumentError("этот формат не читается — сохраните документ как docx")
    if not name.endswith(SUPPORTED):
        raise DocumentError("подойдёт DOCX, TXT или Markdown, а для прайса — xlsx или csv")
    if not data:
        raise DocumentError("файл пустой")
    if len(data) > MAX_BYTES:
        raise DocumentError("файл больше 25 МБ — разбейте его на части")

    if name.endswith(".docx"):
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
