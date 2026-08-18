"""Поиск по базе знаний — BM25 на стандартной библиотеке.

Векторная база тут была бы лишней: у сайта клиента десятки страниц, а не
миллионы. BM25 на такой коллекции даёт тот же результат, работает мгновенно
и не тащит ни numpy, ни внешний сервис.

Индекс держим в памяти и пересобираем, когда изменилось число кусков.
"""
from __future__ import annotations

import math
import re
from collections import Counter

from . import db

_WORD = re.compile(r"[a-zA-Zа-яА-ЯёЁ0-9]+")

# Клиент спрашивает «шапку на зиму», а в прайсе стоит «Сезон: Зима». Без
# нормализации окончаний это разные слова, строка не находится, и агент зовёт
# менеджера на вопрос, ответ на который у него есть. Полноценный стеммер тут
# не нужен: достаточно отрезать частые окончания, оставив основу.
_ENDINGS = sorted(
    ("иями", "ями", "ами", "ого", "его", "ому", "ему", "ыми", "ими", "ой", "ей",
     "ий", "ый", "ая", "яя", "ое", "ее", "ие", "ые", "ах", "ях", "ам", "ям",
     "ом", "ем", "ов", "ев", "ую", "юю", "ии", "а", "е", "и", "о", "у", "ы",
     "ь", "я", "ю"),
    key=len,
    reverse=True,
)
_MIN_STEM = 3


def stem(word: str) -> str:
    """Основа слова: «зиму», «зима» и «зимы» должны совпасть, «мех» — остаться."""
    for ending in _ENDINGS:
        if word.endswith(ending) and len(word) - len(ending) >= _MIN_STEM:
            return word[:-len(ending)]
    if len(word) >= 4 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word

# Слова, которые есть почти в каждом тексте и только шумят.
STOP = {
    "и", "в", "во", "не", "что", "он", "на", "я", "с", "со", "как", "а", "то",
    "все", "она", "так", "его", "но", "да", "ты", "к", "у", "же", "вы", "за",
    "бы", "по", "только", "ее", "мне", "было", "вот", "от", "меня", "еще",
    "нет", "о", "из", "ему", "теперь", "когда", "даже", "ну", "вдруг", "ли",
    "если", "уже", "или", "быть", "был", "него", "до", "вас", "нибудь", "для",
    "the", "a", "an", "and", "or", "of", "to", "in", "is", "are", "for", "on",
    "with", "this", "that", "it", "be", "as", "at", "by", "from", "you", "we",
}

_index: list[dict] = []
_df: Counter = Counter()
_avg_len: float = 0.0
_size: int = -1


def tokenize(text: str) -> list[str]:
    return [stem(w) for w in (t.lower() for t in _WORD.findall(text or "")) if w not in STOP]


def _build() -> None:
    global _index, _df, _avg_len, _size

    rows = db.q(
        "SELECT c.id, c.text, p.url, p.title FROM kb_chunks c"
        " JOIN kb_pages p ON p.id = c.page_id WHERE p.included = 1"
    )
    _index = []
    _df = Counter()
    total_len = 0

    for row in rows:
        tokens = tokenize(row["text"])
        if not tokens:
            continue
        counts = Counter(tokens)
        _index.append({
            "id": row["id"],
            "text": row["text"],
            "url": row["url"],
            "title": row["title"],
            "counts": counts,
            "len": len(tokens),
        })
        total_len += len(tokens)
        for token in counts:
            _df[token] += 1

    _avg_len = (total_len / len(_index)) if _index else 0.0
    _size = len(rows)


def _ensure_fresh() -> None:
    """Пересобрать индекс, если куски в базе изменились."""
    current = db.q1("SELECT COUNT(*) AS c FROM kb_chunks")["c"]
    if current != _size:
        _build()


def invalidate() -> None:
    """Сказать индексу, что база знаний поменялась."""
    global _size
    _size = -1


def search(query: str, top_k: int = 4) -> list[dict]:
    """Куски базы знаний, наиболее близкие к вопросу."""
    _ensure_fresh()
    if not _index:
        return []

    tokens = tokenize(query)
    if not tokens:
        return []

    n = len(_index)
    k1, b = 1.5, 0.75
    scored = []

    for doc in _index:
        score = 0.0
        for token in tokens:
            freq = doc["counts"].get(token)
            if not freq:
                continue
            # +1 внутри логарифма не даёт весу уйти в минус на частых словах
            idf = math.log(1 + (n - _df[token] + 0.5) / (_df[token] + 0.5))
            norm = freq * (k1 + 1) / (
                freq + k1 * (1 - b + b * doc["len"] / (_avg_len or 1))
            )
            score += idf * norm
        if score > 0:
            scored.append((score, doc))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [
        {"text": doc["text"], "url": doc["url"], "title": doc["title"], "score": round(score, 3)}
        for score, doc in scored[:top_k]
    ]


def everything(budget_chars: int = 6000) -> list[dict]:
    """Вся база знаний, сколько влезет в бюджет.

    Нужна для общих вопросов вроде «что у вас есть» и «покажите ассортимент»:
    в них нет ни одного слова из прайса, поиск по словам возвращает пустоту, и
    агент звал менеджера на самый частый вопрос клиента. База знаний тут обычно
    маленькая — прайс на десять строк это несколько сотен символов.
    """
    _ensure_fresh()
    picked = []
    used = 0
    for doc in _index:
        if used + len(doc["text"]) > budget_chars:
            break
        picked.append({"text": doc["text"], "url": doc["url"], "title": doc["title"], "score": 0.0})
        used += len(doc["text"])
    return picked


def hits_for(query: str, budget_chars: int = 6000) -> list[dict]:
    """Фрагменты, которые получит модель. Одно решение для агента и для панели.

    Пока вся база знаний влезает в бюджет запроса, поиск по словам не нужен и
    вреден: каталог из десяти строк — это меньше двухсот токенов, а любой промах
    поиска превращался в «передаю менеджеру». На общий вопрос «что у вас есть»
    слов из прайса нет вообще, и агент отказывался отвечать при полной базе.

    Поиск включается только когда знаний больше, чем влезает в запрос.
    """
    _ensure_fresh()
    total = sum(len(doc["text"]) for doc in _index)
    if total and total <= budget_chars:
        return everything(budget_chars)
    return search(query, top_k=6) or everything(budget_chars)


def context_for(query: str, budget_chars: int = 6000) -> str:
    """Готовый кусок контекста для модели — с указанием источников."""
    blocks = []
    used = 0
    for hit in hits_for(query, budget_chars):
        block = f"[Источник: {hit['title'] or hit['url']}]\n{hit['text']}"
        if used + len(block) > budget_chars:
            break
        blocks.append(block)
        used += len(block)
    return "\n\n---\n\n".join(blocks)
